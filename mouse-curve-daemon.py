#!/usr/bin/env python3
"""mouse-curve-daemon — interceptor de eventos do mouse que aplica uma curva
de aceleração paramétrica (velocidade-dependente) e re-emite via /dev/uinput.

Curva aplicada por evento de sincronização (sample):
    speed_pps  = sqrt(dx² + dy²) / dt
    effective  = max(0, speed_pps - deadzone)
    accel      = gain * (effective / 1000) ** power
    multiplier = min(sensitivity * (1 + accel), max_multiplier)
    out_dx     = dx * multiplier
    out_dy     = dy * multiplier

Configuração lida de ~/.config/mouse-fine-tuning/curve.json. SIGHUP recarrega
a configuração em vivo (parâmetros tomam efeito sem reiniciar)."""

from __future__ import annotations

import json
import math
import os
import selectors
import signal
import sys
import time
from pathlib import Path

import evdev
from evdev import UInput
from evdev import ecodes as ec

CONFIG_PATH = Path.home() / ".config" / "mouse-fine-tuning" / "curve.json"

DEFAULT_CURVE = {
    "gain": 0.1,
    "power": 1.5,
    "deadzone": 0.0,
    "max_multiplier": 3.0,
    "sensitivity": 1.0,
}


def log(msg: str) -> None:
    print(f"[mouse-curve-daemon] {msg}", file=sys.stderr, flush=True)


def looks_like_mouse(dev: evdev.InputDevice) -> bool:
    caps = dev.capabilities()
    if ec.EV_REL not in caps:
        return False
    rels = caps[ec.EV_REL]
    if ec.REL_X not in rels or ec.REL_Y not in rels:
        return False
    if ec.EV_ABS in caps:  # touchpads e tablets
        return False
    keys = caps.get(ec.EV_KEY, [])
    return ec.BTN_LEFT in keys or ec.BTN_MOUSE in keys


def find_mouse(preferred_path: str = "") -> evdev.InputDevice | None:
    if preferred_path and Path(preferred_path).exists():
        try:
            dev = evdev.InputDevice(preferred_path)
            if looks_like_mouse(dev):
                return dev
            dev.close()
            log(f"{preferred_path} não parece mouse; buscando outro")
        except OSError as e:
            log(f"Erro abrindo {preferred_path}: {e}")

    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
            if looks_like_mouse(dev):
                return dev
            dev.close()
        except OSError:
            continue
    return None


class CurveDaemon:
    def __init__(self) -> None:
        self.config: dict = {"device_path": "", "curve": DEFAULT_CURVE.copy()}
        self.source: evdev.InputDevice | None = None
        self.virtual: UInput | None = None
        self.residue_x = 0.0
        self.residue_y = 0.0
        self.running = True

    # ----- config -----

    def load_config(self) -> bool:
        if not CONFIG_PATH.exists():
            log(f"Config não existe ({CONFIG_PATH}); usando defaults")
            return True
        try:
            with CONFIG_PATH.open() as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log(f"Erro lendo config: {e}; mantendo última válida")
            return False

        curve = {**DEFAULT_CURVE, **(data.get("curve") or {})}
        self.config = {
            "device_path": data.get("device_path", ""),
            "curve": curve,
        }
        return True

    # ----- curva -----

    def apply_curve(self, dx: float, dy: float, speed_pps: float) -> tuple[float, float]:
        c = self.config["curve"]
        effective = max(0.0, speed_pps - c["deadzone"])
        accel = c["gain"] * (effective / 1000.0) ** c["power"]
        mult = min(c["sensitivity"] * (1.0 + accel), c["max_multiplier"])
        return dx * mult, dy * mult

    # ----- ciclo de vida -----

    def open_devices(self) -> bool:
        self.source = find_mouse(self.config.get("device_path", ""))
        if not self.source:
            log("Nenhum mouse encontrado em /dev/input/")
            return False
        log(f"Mouse de origem: {self.source.name} ({self.source.path})")

        caps = self.source.capabilities(verbose=False)
        caps.pop(ec.EV_SYN, None)
        try:
            self.virtual = UInput(
                caps,
                name="Mouse Fine-Tuning Virtual",
                vendor=0x1D6B,
                product=0x0104,
                version=0x0001,
            )
        except Exception as e:
            log(f"Erro criando dispositivo virtual: {e}")
            log("Confirme que /dev/uinput é acessível ao seu usuário "
                "(udev rule + grupo input).")
            return False

        try:
            self.source.grab()
        except OSError as e:
            log(f"Erro fazendo grab exclusivo do mouse: {e}")
            return False

        return True

    def close_devices(self) -> None:
        if self.source is not None:
            try:
                self.source.ungrab()
            except OSError:
                pass
            self.source.close()
            self.source = None
        if self.virtual is not None:
            self.virtual.close()
            self.virtual = None

    # ----- loop de eventos -----

    def event_loop(self) -> None:
        assert self.source is not None
        assert self.virtual is not None

        sel = selectors.DefaultSelector()
        sel.register(self.source.fd, selectors.EVENT_READ)

        accum_dx = 0
        accum_dy = 0
        last_sync = time.monotonic()

        while self.running:
            events = sel.select(timeout=0.5)
            if not events:
                continue

            try:
                batch = list(self.source.read())
            except BlockingIOError:
                continue
            except OSError as e:
                log(f"Erro lendo eventos: {e}")
                break

            for event in batch:
                etype = event.type
                ecode = event.code

                if etype == ec.EV_REL:
                    if ecode == ec.REL_X:
                        accum_dx += event.value
                    elif ecode == ec.REL_Y:
                        accum_dy += event.value
                    else:
                        # wheel, hwheel, hires wheel — pass-through
                        self.virtual.write_event(event)

                elif etype == ec.EV_SYN and ecode == ec.SYN_REPORT:
                    now = time.monotonic()
                    dt = now - last_sync
                    last_sync = now

                    if dt > 0 and (accum_dx or accum_dy):
                        mag = math.hypot(accum_dx, accum_dy)
                        speed = mag / dt
                        out_dx, out_dy = self.apply_curve(accum_dx, accum_dy, speed)

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

                        accum_dx = 0
                        accum_dy = 0

                    self.virtual.syn()

                else:
                    # botões, MSC, etc. — pass-through direto
                    self.virtual.write_event(event)

    # ----- entry point -----

    def run(self) -> int:
        self.load_config()
        if not self.open_devices():
            self.close_devices()
            return 1
        try:
            self.event_loop()
        finally:
            self.close_devices()
        return 0


def install_signal_handlers(daemon: CurveDaemon) -> None:
    def reload_handler(signum, frame):
        log("SIGHUP — recarregando configuração")
        daemon.load_config()

    def stop_handler(signum, frame):
        log(f"Sinal {signum} — encerrando")
        daemon.running = False

    signal.signal(signal.SIGHUP, reload_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)


def main() -> int:
    if os.geteuid() == 0:
        log("Aviso: rodando como root. Recomenda-se rodar como usuário comum "
            "com /dev/uinput acessível via udev rule + grupo 'input'.")

    daemon = CurveDaemon()
    install_signal_handlers(daemon)
    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())
