#!/usr/bin/env python3
"""mouse-curve-daemon — multi-device, hot-plug, per-device curve daemon.

Intercepta múltiplos mouses simultaneamente via evdev, aplica a cada um a
curva paramétrica do preset atribuído em devices.json e re-emite via
/dev/uinput. Suporta:

  * Hot-plug via pyudev (opcional — sem pyudev, ainda funciona pra mouses
    presentes ao boot, sem detectar plug/unplug em runtime)
  * Reload em vivo da configuração (devices.json + presets) via mtime
    polling ou SIGHUP
  * IPC via Unix socket (XDG_RUNTIME_DIR/mouse-curve-daemon.sock) com
    JSON Lines: clientes recebem eventos throttled a 30 Hz para o "live
    monitor" da GUI

Permissões: usuário precisa estar no grupo 'input' e ter acesso rw a
/dev/uinput (via udev rule 99-uinput.rules)."""

from __future__ import annotations

import json
import math
import os
import selectors
import signal
import socket
import sys
import time
from pathlib import Path

import evdev
from evdev import UInput
from evdev import ecodes as ec

try:
    import pyudev  # type: ignore

    HAVE_PYUDEV = True
except ImportError:
    HAVE_PYUDEV = False

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mft_common  # noqa: E402


# ---------- log ----------


def log(msg: str) -> None:
    print(f"[mouse-curve-daemon] {msg}", file=sys.stderr, flush=True)


# ---------- enumeração de mouses ----------
# (usar mft_common.enumerate_present_mice — ela já filtra virtuais nossos)


def looks_like_mouse(dev: evdev.InputDevice) -> bool:
    """Wrapper que reusa o filtro central."""
    return mft_common._is_mouselike(dev)


def enumerate_present_mice() -> list[tuple[str, str, str]]:
    """[(device_id, name, path), ...] dos mouses reais (não virtuais)."""
    return [(d["id"], d["name"], d["path"]) for d in mft_common.enumerate_present_mice()]


# ---------- handler de um device ----------


class DeviceHandler:
    """Mouse físico → mouse virtual com curva aplicada."""

    BROADCAST_INTERVAL = 1.0 / 30.0  # 30 Hz para o live monitor

    def __init__(self, fleet: MouseFleet, source: evdev.InputDevice, curve: dict) -> None:
        self.fleet = fleet
        self.source = source
        self.curve = curve
        self.device_id = mft_common.device_id_from_evdev(source)

        caps = source.capabilities(verbose=False)
        caps.pop(ec.EV_SYN, None)
        # Copia vendor/product/version do mouse físico — Mutter precisa ver
        # o virtual como um mouse "real" pra rotear cursor através dele.
        # Adiciona sufixo no nome pra distinguir do físico nos logs.
        self.virtual = UInput(
            caps,
            name=f"{source.name} (mft-virtual)",
            vendor=source.info.vendor,
            product=source.info.product,
            version=source.info.version,
        )

        # estado
        self.accum_dx = 0
        self.accum_dy = 0
        self.residue_x = 0.0
        self.residue_y = 0.0
        self.last_sync = time.monotonic()
        self.last_broadcast = 0.0

        source.grab()

    def fileno(self) -> int:
        return self.source.fd

    def handle_ready(self) -> None:
        try:
            batch = list(self.source.read())
        except BlockingIOError:
            return
        except OSError as e:
            log(f"Erro lendo {self.device_id}: {e}")
            self.fleet.remove_handler(self.device_id)
            return
        if os.environ.get("MFT_DEBUG"):
            log(f"[{self.device_id}] batch de {len(batch)} eventos")
        self._process_batch(batch)

    def _process_batch(self, events) -> None:
        for event in events:
            etype = event.type
            ecode = event.code

            if etype == ec.EV_REL:
                if ecode == ec.REL_X:
                    self.accum_dx += event.value
                elif ecode == ec.REL_Y:
                    self.accum_dy += event.value
                else:
                    self.virtual.write_event(event)

            elif etype == ec.EV_SYN and ecode == ec.SYN_REPORT:
                self._emit_sample()
                self.virtual.syn()

            else:
                self.virtual.write_event(event)

    def _emit_sample(self) -> None:
        now = time.monotonic()
        dt = now - self.last_sync
        self.last_sync = now

        if dt <= 0:
            return

        if self.accum_dx == 0 and self.accum_dy == 0:
            # Mesmo sem movimento, broadcastear "0" periodicamente ajuda o
            # live monitor a manter o eixo do tempo correndo.
            self._maybe_broadcast(now, 0.0, 0.0)
            return

        mag = math.hypot(self.accum_dx, self.accum_dy)
        speed_in = mag / dt
        in_dx, in_dy = self.accum_dx, self.accum_dy
        out_dx, out_dy = mft_common.apply_curve(
            self.accum_dx, self.accum_dy, speed_in, self.curve
        )
        speed_out = math.hypot(out_dx, out_dy) / dt

        self.residue_x += out_dx
        self.residue_y += out_dy
        int_dx = int(self.residue_x)
        int_dy = int(self.residue_y)
        self.residue_x -= int_dx
        self.residue_y -= int_dy

        if int_dx:
            self.virtual.write(ec.EV_REL, ec.REL_X, int_dx)
        if int_dy:
            self.virtual.write(ec.EV_REL, ec.REL_Y, int_dy)

        if os.environ.get("MFT_DEBUG"):
            log(f"[{self.device_id}] in=({in_dx},{in_dy}) speed={speed_in:.0f}px/s "
                f"-> out=({out_dx:.2f},{out_dy:.2f}) emit=({int_dx},{int_dy})")

        self.accum_dx = 0
        self.accum_dy = 0

        self._maybe_broadcast(now, speed_in, speed_out)

    def _maybe_broadcast(self, now: float, speed_in: float, speed_out: float) -> None:
        if now - self.last_broadcast >= self.BROADCAST_INTERVAL:
            self.fleet.broadcast_event(self.device_id, now, speed_in, speed_out)
            self.last_broadcast = now

    def update_curve(self, curve: dict) -> None:
        self.curve = curve

    def close(self) -> None:
        try:
            self.source.ungrab()
        except OSError:
            pass
        try:
            self.source.close()
        except OSError:
            pass
        try:
            self.virtual.close()
        except OSError:
            pass


# ---------- IPC ----------


class IPCClient:
    def __init__(self, server: IPCServer, conn: socket.socket) -> None:
        self.server = server
        self.conn = conn
        self.buf = b""
        self.subscribed = None  # None | "*" | device_id

    def fileno(self) -> int:
        return self.conn.fileno()

    def subscribed_to(self, device_id: str) -> bool:
        return self.subscribed in ("*", device_id)

    def handle_ready(self) -> None:
        try:
            data = self.conn.recv(4096)
        except (BlockingIOError, ConnectionResetError, OSError):
            self.close()
            return
        if not data:
            self.close()
            return
        self.buf += data
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            self._handle_line(line)

    def _handle_line(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        cmd = msg.get("cmd")
        if cmd == "subscribe":
            self.subscribed = msg.get("device_id", "*")
        elif cmd == "unsubscribe":
            self.subscribed = None
        elif cmd == "list_devices":
            self.send(
                {"type": "device_list", "devices": self.server.fleet.list_devices_info()}
            )
        elif cmd == "ping":
            self.send({"type": "pong"})

    def send(self, msg: dict) -> None:
        try:
            self.conn.send((json.dumps(msg) + "\n").encode())
        except (BlockingIOError, BrokenPipeError, ConnectionResetError, OSError):
            self.close()

    def close(self) -> None:
        try:
            self.server.fleet.sel.unregister(self.conn.fileno())
        except (KeyError, ValueError):
            pass
        try:
            self.conn.close()
        except OSError:
            pass
        if self in self.server.clients:
            self.server.clients.remove(self)


class IPCServer:
    def __init__(self, fleet: MouseFleet) -> None:
        self.fleet = fleet
        self.sock_path = mft_common.SOCKET_PATH
        self.sock: socket.socket | None = None
        self.clients: list[IPCClient] = []

    def start(self) -> None:
        try:
            self.sock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.sock_path))
        try:
            os.chmod(str(self.sock_path), 0o600)
        except OSError:
            pass
        self.sock.listen(5)
        self.sock.setblocking(False)
        self.fleet.sel.register(self.sock.fileno(), selectors.EVENT_READ, "ipc_accept")
        log(f"IPC socket em {self.sock_path}")

    def stop(self) -> None:
        for c in list(self.clients):
            c.close()
        if self.sock:
            try:
                self.fleet.sel.unregister(self.sock.fileno())
            except (KeyError, ValueError):
                pass
            try:
                self.sock.close()
            except OSError:
                pass
        try:
            self.sock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def accept(self) -> None:
        if not self.sock:
            return
        try:
            conn, _ = self.sock.accept()
        except BlockingIOError:
            return
        conn.setblocking(False)
        client = IPCClient(self, conn)
        self.clients.append(client)
        self.fleet.sel.register(conn.fileno(), selectors.EVENT_READ, client)
        client.send({"type": "device_list", "devices": self.fleet.list_devices_info()})

    def broadcast_event(self, device_id: str, t: float, speed_in: float, speed_out: float) -> None:
        if not self.clients:
            return
        msg = {
            "type": "event",
            "device_id": device_id,
            "t": t,
            "speed_in": speed_in,
            "speed_out": speed_out,
        }
        for c in list(self.clients):
            if c.subscribed_to(device_id):
                c.send(msg)

    def broadcast_device_list(self) -> None:
        if not self.clients:
            return
        msg = {"type": "device_list", "devices": self.fleet.list_devices_info()}
        for c in list(self.clients):
            c.send(msg)


# ---------- fleet ----------


class MouseFleet:
    CONFIG_POLL_INTERVAL = 1.5  # segundos

    def __init__(self) -> None:
        self.handlers: dict[str, DeviceHandler] = {}
        self.presets: dict[str, dict] = {}
        self.devices_config: dict = {"devices": []}
        self.config_mtime = 0.0
        self.presets_dir_mtime = 0.0
        self.last_config_check = 0.0
        self.sel = selectors.DefaultSelector()
        self.ipc = IPCServer(self)
        self.udev_monitor = None
        self.running = True

    def start(self) -> None:
        mft_common.sync_builtin_presets()
        self._reload_presets()
        self._reload_devices_config()
        self.ipc.start()
        self._start_udev_monitor()
        self._initial_scan()
        self._main_loop()

    def stop(self) -> None:
        self.running = False

    def close_all(self) -> None:
        for did in list(self.handlers.keys()):
            self._close_handler(did)
        self.ipc.stop()

    # ----- presets/config -----

    def _reload_presets(self) -> None:
        self.presets = {p["name"]: p for p in mft_common.list_all_presets()}
        log(f"Presets carregados: {list(self.presets.keys())}")

    def _reload_devices_config(self) -> None:
        self.devices_config = mft_common.load_devices_config()
        try:
            self.config_mtime = mft_common.DEVICES_CONFIG_PATH.stat().st_mtime
        except OSError:
            self.config_mtime = 0.0

    def _check_config_updates(self) -> None:
        now = time.monotonic()
        if now - self.last_config_check < self.CONFIG_POLL_INTERVAL:
            return
        self.last_config_check = now

        changed = False
        try:
            mtime = mft_common.DEVICES_CONFIG_PATH.stat().st_mtime
            if mtime != self.config_mtime:
                self.config_mtime = mtime
                self.devices_config = mft_common.load_devices_config()
                changed = True
        except OSError:
            pass

        # Detectar mudanças em presets (qualquer .json em PRESETS_DIR)
        try:
            mtime = max(
                (f.stat().st_mtime for f in mft_common.PRESETS_DIR.rglob("*.json")),
                default=0.0,
            )
        except OSError:
            mtime = 0.0
        if mtime != self.presets_dir_mtime:
            self.presets_dir_mtime = mtime
            self._reload_presets()
            changed = True

        if changed:
            self._sync_handlers_to_config()

    # ----- handlers -----

    def _curve_for(self, preset_name: str) -> dict:
        preset = self.presets.get(preset_name)
        if preset:
            return preset["curve"]
        log(f"Preset '{preset_name}' não existe; usando defaults")
        return dict(mft_common.DEFAULT_CURVE)

    def _sync_handlers_to_config(self) -> None:
        wanted: dict[str, dict] = {}
        for d in self.devices_config.get("devices", []):
            if d.get("enabled"):
                wanted[d["id"]] = d

        for did in list(self.handlers.keys()):
            if did not in wanted:
                self._close_handler(did)

        for did, dev_entry in wanted.items():
            curve = self._curve_for(dev_entry.get("preset", "Linear"))
            if did in self.handlers:
                self.handlers[did].update_curve(curve)
            else:
                self._open_handler(did, curve)

        self.ipc.broadcast_device_list()

    def _open_handler(self, device_id: str, curve: dict) -> None:
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue
            try:
                if mft_common.device_id_from_evdev(dev) != device_id:
                    dev.close()
                    continue
                if not looks_like_mouse(dev):
                    dev.close()
                    continue
                handler = DeviceHandler(self, dev, curve)
                self.handlers[device_id] = handler
                self.sel.register(dev.fd, selectors.EVENT_READ, handler)
                log(f"Mouse '{dev.name}' [{device_id}] ativado em {path}")
                return
            except Exception as e:
                log(f"Erro abrindo {device_id} ({path}): {e}")
                try:
                    dev.close()
                except Exception:
                    pass
                continue
        log(f"Mouse {device_id} não está presente agora")

    def _close_handler(self, device_id: str) -> None:
        handler = self.handlers.pop(device_id, None)
        if not handler:
            return
        try:
            self.sel.unregister(handler.fileno())
        except (KeyError, ValueError):
            pass
        handler.close()
        log(f"Mouse [{device_id}] desativado")

    def remove_handler(self, device_id: str) -> None:
        self._close_handler(device_id)
        self.ipc.broadcast_device_list()

    def broadcast_event(self, device_id, t, speed_in, speed_out) -> None:
        self.ipc.broadcast_event(device_id, t, speed_in, speed_out)

    def list_devices_info(self) -> list[dict]:
        result = []
        present_ids = set()
        for did, name, path in enumerate_present_mice():
            present_ids.add(did)
            result.append({
                "id": did,
                "name": name,
                "path": path,
                "present": True,
                "active": did in self.handlers,
            })
        for d in self.devices_config.get("devices", []):
            if d["id"] not in present_ids:
                result.append({
                    "id": d["id"],
                    "name": d.get("name", "(desconectado)"),
                    "path": "",
                    "present": False,
                    "active": False,
                })
        return result

    # ----- udev hot-plug -----

    def _start_udev_monitor(self) -> None:
        if not HAVE_PYUDEV:
            log("pyudev ausente; sem hot-plug. Instale python-pyudev pra detecção em runtime.")
            return
        context = pyudev.Context()
        monitor = pyudev.Monitor.from_netlink(context)
        monitor.filter_by(subsystem="input")
        monitor.start()
        self.udev_monitor = monitor
        self.sel.register(monitor.fileno(), selectors.EVENT_READ, "udev")
        log("udev monitor ativo")

    def _handle_udev(self) -> None:
        if not self.udev_monitor:
            return
        any_change = False
        while True:
            device = self.udev_monitor.poll(timeout=0)
            if device is None:
                break
            action = device.action
            if action in ("add", "change"):
                # pequeno settling — evdev pode demorar a popular caps
                time.sleep(0.05)
                any_change = True
            elif action == "remove":
                any_change = True
        if any_change:
            # Fechar handlers cujo source sumiu
            for did, handler in list(self.handlers.items()):
                if not Path(handler.source.path).exists():
                    log(f"Mouse {did} removido fisicamente")
                    self._close_handler(did)
            self._sync_handlers_to_config()

    # ----- initial scan -----

    def _initial_scan(self) -> None:
        # Adicionar à config qualquer device presente que ainda não esteja listado
        present = enumerate_present_mice()
        changed = False
        for did, name, _path in present:
            if not mft_common.find_device(self.devices_config, did):
                mft_common.upsert_device(
                    self.devices_config,
                    did,
                    name,
                    preset="Linear",
                    enabled=False,
                )
                changed = True
        if changed:
            mft_common.save_devices_config(self.devices_config)
            self._reload_devices_config()

        self._sync_handlers_to_config()

    # ----- main loop -----

    def _main_loop(self) -> None:
        while self.running:
            events = self.sel.select(timeout=0.5)
            for key, _mask in events:
                data = key.data
                if isinstance(data, DeviceHandler):
                    data.handle_ready()
                elif isinstance(data, IPCClient):
                    data.handle_ready()
                elif data == "ipc_accept":
                    self.ipc.accept()
                elif data == "udev":
                    self._handle_udev()
            self._check_config_updates()


# ---------- entrada ----------


def install_signal_handlers(fleet: MouseFleet) -> None:
    def reload_handler(signum, frame):
        log("SIGHUP — recarregando presets + devices.json")
        fleet._reload_presets()
        fleet._reload_devices_config()
        fleet._sync_handlers_to_config()

    def stop_handler(signum, frame):
        log(f"Sinal {signum} — encerrando")
        fleet.stop()

    signal.signal(signal.SIGHUP, reload_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)


def main() -> int:
    if os.geteuid() == 0:
        log("Aviso: rodando como root. Recomenda-se usuário comum + grupo 'input' + udev rule.")
    fleet = MouseFleet()
    install_signal_handlers(fleet)
    try:
        fleet.start()
    except Exception as e:
        log(f"Erro fatal: {e}")
        import traceback

        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        fleet.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
