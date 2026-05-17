#!/usr/bin/env python3
"""Mouse Fine-Tuning — GUI principal.

Três abas:
  * Configurações — gsettings nativos do GNOME (speed, accel-profile, drag).
  * Dispositivos  — lista de mouses, switch on/off e dropdown de preset por mouse.
  * Presets      — editor da biblioteca de presets, preview gráfico, live
                    monitor (osciloscópio do mouse via IPC com o daemon)."""

from __future__ import annotations

import collections
import json
import math
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mft_common  # noqa: E402

APP_ID = "br.dev.lcf2212.MouseFineTuning"
APP_VERSION = "0.3.0"
SCHEMA = "org.gnome.desktop.peripherals.mouse"
DAEMON_UNIT = "mouse-curve-daemon.service"

PROFILES = ["default", "flat", "adaptive"]
PROFILE_LABELS = ["Padrão do sistema", "Desativada (flat)", "Adaptativa"]


# ====================== utilitários ======================


def schema_available() -> bool:
    source = Gio.SettingsSchemaSource.get_default()
    return source is not None and source.lookup(SCHEMA, True) is not None


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def daemon_active() -> bool:
    return systemctl("is-active", "--quiet", DAEMON_UNIT).returncode == 0


def daemon_enabled() -> bool:
    return systemctl("is-enabled", "--quiet", DAEMON_UNIT).returncode == 0


def daemon_unit_exists() -> bool:
    return (
        Path.home() / ".config" / "systemd" / "user" / DAEMON_UNIT
    ).exists() or (Path("/etc/systemd/user") / DAEMON_UNIT).exists()


def daemon_start() -> tuple[bool, str]:
    res = systemctl("enable", "--now", DAEMON_UNIT)
    return res.returncode == 0, (res.stderr or res.stdout).strip()


def daemon_stop() -> tuple[bool, str]:
    res = systemctl("disable", "--now", DAEMON_UNIT)
    return res.returncode == 0, (res.stderr or res.stdout).strip()


def daemon_restart() -> tuple[bool, str]:
    res = systemctl("restart", DAEMON_UNIT)
    return res.returncode == 0, (res.stderr or res.stdout).strip()


def daemon_reload() -> None:
    systemctl("kill", "--signal=HUP", DAEMON_UNIT)


# ====================== preview gráfico da curva ======================


class CurvePreview(Gtk.DrawingArea):
    __gtype_name__ = "CurvePreview"

    MAX_SPEED_PPS = 3000.0
    MIN_Y_SCALE = 1.5

    def __init__(self) -> None:
        super().__init__()
        self.set_size_request(380, 200)
        self.set_hexpand(True)
        self.set_draw_func(self._on_draw)
        self.curve = dict(mft_common.DEFAULT_CURVE)

    def set_curve(self, curve: dict) -> None:
        self.curve = dict(curve)
        self.queue_draw()

    def _on_draw(self, _area, cr, width, height) -> None:
        margin_l, margin_r, margin_t, margin_b = 46, 14, 16, 30
        pw = width - margin_l - margin_r
        ph = height - margin_t - margin_b
        if pw < 20 or ph < 20:
            return

        fg = self.get_style_context().get_color()

        # eixos
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
        cr.set_line_width(1)
        cr.move_to(margin_l, margin_t)
        cr.line_to(margin_l, margin_t + ph)
        cr.line_to(margin_l + pw, margin_t + ph)
        cr.stroke()

        # linha de referência ×1.0
        y_max = max(self.curve.get("max_multiplier", 3.0), self.MIN_Y_SCALE)
        ref_y = margin_t + ph - (1.0 / y_max) * ph
        cr.set_dash([4, 4])
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.25)
        cr.move_to(margin_l, ref_y)
        cr.line_to(margin_l + pw, ref_y)
        cr.stroke()
        cr.set_dash([])

        # dead-zone
        dz = self.curve.get("deadzone", 0)
        if dz > 0:
            dz_x = margin_l + (dz / self.MAX_SPEED_PPS) * pw
            if dz_x < margin_l + pw:
                cr.set_source_rgba(1.0, 0.5, 0.2, 0.45)
                cr.set_dash([3, 3])
                cr.move_to(dz_x, margin_t)
                cr.line_to(dz_x, margin_t + ph)
                cr.stroke()
                cr.set_dash([])

        # curva
        cr.set_source_rgba(0.27, 0.6, 0.95, 1.0)
        cr.set_line_width(2.4)
        for i in range(161):
            speed = self.MAX_SPEED_PPS * (i / 160)
            mult = mft_common.multiplier_for(speed, self.curve)
            x = margin_l + pw * (i / 160)
            y = margin_t + ph - (mult / y_max) * ph
            y = max(margin_t, min(margin_t + ph, y))
            if i == 0:
                cr.move_to(x, y)
            else:
                cr.line_to(x, y)
        cr.stroke()

        # rótulos
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.75)
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(10)
        cr.move_to(6, ref_y + 4)
        cr.show_text("×1.0")
        cr.move_to(6, margin_t + 10)
        cr.show_text(f"×{y_max:.1f}")
        cr.move_to(margin_l - 2, margin_t + ph + 18)
        cr.show_text("0 px/s")
        label_max = f"{int(self.MAX_SPEED_PPS)} px/s"
        ext = cr.text_extents(label_max)
        cr.move_to(margin_l + pw - ext.width, margin_t + ph + 18)
        cr.show_text(label_max)


# ====================== live monitor ======================


class LiveMonitor(Gtk.DrawingArea):
    __gtype_name__ = "LiveMonitor"

    WINDOW_SECONDS = 5.0
    BUFFER_SIZE = 600  # ~30 Hz × 5s × margem
    TICK_INTERVAL_MS = 50  # 20 Hz heartbeat — timeline sempre rola

    def __init__(self) -> None:
        super().__init__()
        self.set_size_request(380, 160)
        self.set_hexpand(True)
        self.set_draw_func(self._on_draw)
        self.samples: collections.deque = collections.deque(maxlen=self.BUFFER_SIZE)
        self.active = False
        self.empty_message = "Mexa o mouse para ver o efeito da curva ao vivo."
        self._tick_id: int | None = None

    def add_sample(self, t: float, speed_in: float, speed_out: float) -> None:
        self.samples.append((t, speed_in, speed_out))
        self.queue_draw()

    def reset(self) -> None:
        self.samples.clear()
        self.queue_draw()

    def set_active(self, active: bool) -> None:
        if self.active == active:
            return
        self.active = active
        if active and self._tick_id is None:
            self._tick_id = GLib.timeout_add(self.TICK_INTERVAL_MS, self._tick)
        elif not active and self._tick_id is not None:
            GLib.source_remove(self._tick_id)
            self._tick_id = None
            self.reset()

    def _tick(self) -> bool:
        """Heartbeat: garante que a timeline rola mesmo sem movimento real."""
        if not self.active:
            return GLib.SOURCE_REMOVE
        now = time.monotonic()
        # se há pouco tempo desde a última sample (real ou sintética), pular
        if not self.samples or (now - self.samples[-1][0]) > self.TICK_INTERVAL_MS / 1000.0:
            self.add_sample(now, 0.0, 0.0)
        else:
            self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _on_draw(self, _area, cr, width, height) -> None:
        margin_l, margin_r, margin_t, margin_b = 46, 14, 16, 22
        pw = width - margin_l - margin_r
        ph = height - margin_t - margin_b
        if pw < 20 or ph < 20:
            return

        fg = self.get_style_context().get_color()

        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
        cr.set_line_width(1)
        cr.move_to(margin_l, margin_t)
        cr.line_to(margin_l, margin_t + ph)
        cr.line_to(margin_l + pw, margin_t + ph)
        cr.stroke()

        if not self.samples or not self.active:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
            cr.select_font_face("Sans", 0, 0)
            cr.set_font_size(11)
            msg = self.empty_message if self.active else "Live monitor desligado."
            ext = cr.text_extents(msg)
            cr.move_to(margin_l + (pw - ext.width) / 2, margin_t + ph / 2)
            cr.show_text(msg)
            return

        now = self.samples[-1][0]
        oldest_t = now - self.WINDOW_SECONDS

        max_speed = 100.0
        for t, sin, sout in self.samples:
            if t < oldest_t:
                continue
            max_speed = max(max_speed, sin, sout)
        max_speed *= 1.15

        # ticks Y
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(9)
        cr.move_to(6, margin_t + 10)
        cr.show_text(f"{int(max_speed)} px/s")

        def plot(idx, color):
            cr.set_source_rgba(*color)
            cr.set_line_width(1.6)
            first = True
            for t, sin, sout in self.samples:
                v = sin if idx == 0 else sout
                if t < oldest_t:
                    continue
                x_frac = (t - oldest_t) / self.WINDOW_SECONDS
                x = margin_l + pw * x_frac
                y = margin_t + ph - (v / max_speed) * ph
                y = max(margin_t, min(margin_t + ph, y))
                if first:
                    cr.move_to(x, y)
                    first = False
                else:
                    cr.line_to(x, y)
            cr.stroke()

        plot(0, (0.55, 0.55, 0.55, 0.85))    # cinza — entrada
        plot(1, (0.27, 0.60, 0.95, 1.0))     # azul — saída

        # legenda no canto superior direito
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.80)
        cr.set_font_size(10)
        lx = margin_l + pw - 130
        cr.set_source_rgba(0.55, 0.55, 0.55, 0.85)
        cr.move_to(lx, margin_t + 12)
        cr.show_text("— Entrada (mouse)")
        cr.set_source_rgba(0.27, 0.60, 0.95, 1.0)
        cr.move_to(lx, margin_t + 26)
        cr.show_text("— Saída (com curva)")

        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
        cr.set_font_size(9)
        cr.move_to(margin_l, margin_t + ph + 14)
        cr.show_text(f"-{self.WINDOW_SECONDS:.0f}s")
        ext = cr.text_extents("agora")
        cr.move_to(margin_l + pw - ext.width, margin_t + ph + 14)
        cr.show_text("agora")


# ====================== IPC client (thread) ======================


class DaemonIPC:
    """Cliente do socket Unix do daemon. Thread leitora, callbacks no main loop."""

    def __init__(self) -> None:
        self.sock: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self._stop = threading.Event()
        # callbacks no main loop
        self.on_event = lambda msg: None
        self.on_device_list = lambda msg: None
        self.on_connect_changed = lambda connected: None

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def connect(self) -> bool:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(mft_common.SOCKET_PATH))
            s.setblocking(True)
        except (OSError, FileNotFoundError):
            return False
        self.sock = s
        self._stop.clear()
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()
        GLib.idle_add(self.on_connect_changed, True)
        return True

    def disconnect(self) -> None:
        self._stop.set()
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _send(self, msg: dict) -> bool:
        if not self.sock:
            return False
        try:
            self.sock.send((json.dumps(msg) + "\n").encode())
            return True
        except (OSError, BrokenPipeError):
            self.disconnect()
            GLib.idle_add(self.on_connect_changed, False)
            return False

    def subscribe(self, device_id: str) -> bool:
        return self._send({"cmd": "subscribe", "device_id": device_id or "*"})

    def unsubscribe(self) -> bool:
        return self._send({"cmd": "unsubscribe"})

    def request_device_list(self) -> bool:
        return self._send({"cmd": "list_devices"})

    def _read_loop(self) -> None:
        buf = b""
        sock = self.sock
        while not self._stop.is_set() and sock is not None:
            try:
                data = sock.recv(4096)
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("type")
                if mtype == "event":
                    GLib.idle_add(self.on_event, msg)
                elif mtype == "device_list":
                    GLib.idle_add(self.on_device_list, msg)
        self.sock = None
        GLib.idle_add(self.on_connect_changed, False)


# ====================== páginas ======================


class BasicPage:
    """Aba 'Configurações' — gsettings do GNOME."""

    def __init__(self, settings: Gio.Settings) -> None:
        self.settings = settings
        self.page = Adw.PreferencesPage()
        self.page.add(self._build_speed_group())
        self.page.add(self._build_accel_group())
        self.page.add(self._build_fine_group())

    def _build_speed_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Velocidade",
            description=(
                "Ajuste a velocidade do ponteiro. Aplica-se a todos os mouses conectados."
            ),
        )

        adj = Gtk.Adjustment(
            lower=-1.0, upper=1.0, step_increment=0.01, page_increment=0.1
        )
        self.settings.bind("speed", adj, "value", Gio.SettingsBindFlags.DEFAULT)

        slider_row = Adw.ActionRow(
            title="Velocidade do ponteiro",
            subtitle="Arraste o controle para escolher a velocidade",
        )
        scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=adj,
            hexpand=True,
            draw_value=False,
            valign=Gtk.Align.CENTER,
        )
        scale.set_size_request(260, -1)
        scale.add_mark(-1.0, Gtk.PositionType.BOTTOM, "Lento")
        scale.add_mark(0.0, Gtk.PositionType.BOTTOM, "Padrão")
        scale.add_mark(1.0, Gtk.PositionType.BOTTOM, "Rápido")
        slider_row.add_suffix(scale)
        group.add(slider_row)

        spin = Adw.SpinRow(
            title="Ajuste fino", subtitle="Mesma escala com precisão de 0.01", digits=2
        )
        spin.set_adjustment(adj)
        group.add(spin)

        return group

    def _build_accel_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Aceleração nativa",
            description=(
                "Perfil aplicado pelo libinput. Quando uma curva customizada "
                "(aba “Dispositivos”) estiver ativa, este perfil é forçado para "
                "“Desativada (flat)” automaticamente."
            ),
        )
        self.combo = Adw.ComboRow(title="Perfil de aceleração")
        self.combo.set_model(Gtk.StringList.new(PROFILE_LABELS))
        group.add(self.combo)

        self.settings.connect("changed::accel-profile", self._on_profile_setting_changed)
        self.combo.connect("notify::selected", self._on_profile_combo_changed)
        self._on_profile_setting_changed(self.settings, "accel-profile")
        return group

    def _build_fine_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Fine-tuning",
            description=(
                "Ajustes auxiliares que afetam a percepção de precisão em "
                "movimentos curtos."
            ),
        )
        adj = Gtk.Adjustment(
            lower=1, upper=30, step_increment=1, page_increment=5
        )
        self.settings.bind(
            "drag-threshold", adj, "value", Gio.SettingsBindFlags.DEFAULT
        )
        row = Adw.SpinRow(
            title="Limiar de arrasto",
            subtitle="Pixels antes do sistema reconhecer um arrasto",
            digits=0,
        )
        row.set_adjustment(adj)
        group.add(row)
        return group

    def _on_profile_setting_changed(self, settings, _key) -> None:
        current = settings.get_string("accel-profile")
        try:
            idx = PROFILES.index(current)
        except ValueError:
            idx = 0
        if self.combo.get_selected() != idx:
            self.combo.set_selected(idx)

    def _on_profile_combo_changed(self, combo, _pspec) -> None:
        idx = combo.get_selected()
        if idx >= len(PROFILES):
            return
        new = PROFILES[idx]
        if self.settings.get_string("accel-profile") != new:
            self.settings.set_string("accel-profile", new)


class PresetsPage:
    """Aba 'Presets' — editor + preview + live monitor."""

    def __init__(self, window: MouseFineTuningWindow) -> None:
        self.window = window
        self.page = Adw.PreferencesPage()
        self._save_timer_id: int | None = None
        self._suppressing = False

        # ----- grupo: seleção -----
        sel_group = Adw.PreferencesGroup(
            title="Biblioteca de presets",
            description=(
                "Built-ins são imutáveis (Linear, Suave, FPS, Quake, Desenho). "
                "Duplique para editar com seu próprio nome."
            ),
        )
        self.preset_combo = Adw.ComboRow(title="Preset")
        sel_group.add(self.preset_combo)

        self.description_row = Adw.ActionRow(title="Descrição", subtitle="")
        sel_group.add(self.description_row)

        action_row = Adw.ActionRow(title="Ações")
        self.new_btn = Gtk.Button(label="Novo", valign=Gtk.Align.CENTER)
        self.new_btn.connect("clicked", self._on_new)
        self.duplicate_btn = Gtk.Button(label="Duplicar", valign=Gtk.Align.CENTER)
        self.duplicate_btn.connect("clicked", self._on_duplicate)
        self.rename_btn = Gtk.Button(label="Renomear", valign=Gtk.Align.CENTER)
        self.rename_btn.connect("clicked", self._on_rename)
        self.delete_btn = Gtk.Button(label="Deletar", valign=Gtk.Align.CENTER)
        self.delete_btn.add_css_class("destructive-action")
        self.delete_btn.connect("clicked", self._on_delete)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.append(self.new_btn)
        box.append(self.duplicate_btn)
        box.append(self.rename_btn)
        box.append(self.delete_btn)
        action_row.add_suffix(box)
        sel_group.add(action_row)
        self.page.add(sel_group)

        # ----- grupo: parâmetros -----
        params_group = Adw.PreferencesGroup(
            title="Parâmetros",
            description=(
                "f(v) = sensibilidade × (1 + ganho × ((v − dead-zone) / 1000) ^ "
                "expoente), limitado pelo multiplicador máximo."
            ),
        )

        self.sensitivity_adj = Gtk.Adjustment(
            lower=0.1, upper=3.0, step_increment=0.05, page_increment=0.5
        )
        self.gain_adj = Gtk.Adjustment(
            lower=0.0, upper=5.0, step_increment=0.05, page_increment=0.5
        )
        self.power_adj = Gtk.Adjustment(
            lower=0.2, upper=3.0, step_increment=0.05, page_increment=0.5
        )
        self.deadzone_adj = Gtk.Adjustment(
            lower=0.0, upper=1000.0, step_increment=10.0, page_increment=50.0
        )
        self.max_mult_adj = Gtk.Adjustment(
            lower=1.0, upper=10.0, step_increment=0.1, page_increment=1.0
        )

        rows_data = [
            ("Sensibilidade base", "Multiplicador quando velocidade ≈ 0", self.sensitivity_adj, 2),
            ("Ganho de aceleração", "Quanto a aceleração cresce com a velocidade", self.gain_adj, 2),
            ("Expoente", "1.0 = linear; >1 acelera mais em alta velocidade", self.power_adj, 2),
            ("Dead-zone (px/s)", "Velocidades abaixo desta não recebem aceleração extra", self.deadzone_adj, 0),
            ("Multiplicador máximo", "Teto absoluto", self.max_mult_adj, 1),
        ]
        self.param_rows: list[Adw.SpinRow] = []
        for title, subtitle, adj, digits in rows_data:
            r = Adw.SpinRow(title=title, subtitle=subtitle, digits=digits)
            r.set_adjustment(adj)
            adj.connect("value-changed", self._on_param_changed)
            params_group.add(r)
            self.param_rows.append(r)
        self.page.add(params_group)

        # ----- grupo: preview -----
        preview_group = Adw.PreferencesGroup(
            title="Preview da curva",
            description=(
                "Eixo X: velocidade do mouse (px/s). Eixo Y: multiplicador aplicado. "
                "Tracejado horizontal é ×1.0; vertical laranja é a dead-zone."
            ),
        )
        self.preview = CurvePreview()
        self.preview.set_margin_top(6)
        self.preview.set_margin_bottom(6)
        frame = Gtk.Frame(child=self.preview)
        host = Adw.PreferencesRow()
        host.set_activatable(False)
        host.set_selectable(False)
        host.set_child(frame)
        preview_group.add(host)
        self.page.add(preview_group)

        # ----- grupo: velocidade do mouse (independente do preset) -----
        speed_group = Adw.PreferencesGroup(
            title="Velocidade do mouse",
            description=(
                "Velocidade global do ponteiro. Funciona em qualquer preset — "
                "mesmo em “Adaptativa”. Equivalente ao slider do GNOME."
            ),
        )
        speed_adj = Gtk.Adjustment(
            lower=-1.0, upper=1.0, step_increment=0.05, page_increment=0.2
        )
        self.window.settings.bind(
            "speed", speed_adj, "value", Gio.SettingsBindFlags.DEFAULT
        )

        slider_row = Adw.ActionRow(
            title="Velocidade do ponteiro",
            subtitle="Arraste o controle ou use o spin abaixo para ajuste fino",
        )
        scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=speed_adj,
            hexpand=True,
            draw_value=False,
            valign=Gtk.Align.CENTER,
        )
        scale.set_size_request(260, -1)
        scale.add_mark(-1.0, Gtk.PositionType.BOTTOM, "Lento")
        scale.add_mark(0.0, Gtk.PositionType.BOTTOM, "Padrão")
        scale.add_mark(1.0, Gtk.PositionType.BOTTOM, "Rápido")
        slider_row.add_suffix(scale)
        speed_group.add(slider_row)

        spin_speed = Adw.SpinRow(
            title="Ajuste fino", subtitle="Precisão de 0.05", digits=2
        )
        spin_speed.set_adjustment(speed_adj)
        speed_group.add(spin_speed)
        self.page.add(speed_group)

        # ----- grupo: live monitor -----
        live_group = Adw.PreferencesGroup(
            title="Live monitor (osciloscópio)",
            description=(
                "Mostra em tempo real a velocidade do seu mouse passando pela curva. "
                "Cinza: entrada (movimento físico). Azul: saída (após curva)."
            ),
        )
        self.live_toggle = Adw.SwitchRow(
            title="Mostrar movimento real",
            subtitle="Conecta ao daemon e exibe os eventos em vivo",
        )
        self.live_toggle.connect("notify::active", self._on_live_toggle)
        live_group.add(self.live_toggle)

        self.live_monitor = LiveMonitor()
        live_host = Adw.PreferencesRow()
        live_host.set_activatable(False)
        live_host.set_selectable(False)
        lframe = Gtk.Frame(child=self.live_monitor)
        live_host.set_child(lframe)
        live_group.add(live_host)
        self.page.add(live_group)

        self._live_devices: list[dict] = []
        self._in_use_ids: set[str] = set()
        self.rebuild_preset_list()

    # ----- preset list -----

    def rebuild_preset_list(self, select_name: str | None = None) -> None:
        names = [p["name"] for p in self.window.presets]
        self._suppressing = True
        try:
            self.preset_combo.set_model(Gtk.StringList.new(names))
            target = select_name or (self.window.active_preset_name or (names[0] if names else None))
            if target and target in names:
                self.preset_combo.set_selected(names.index(target))
            elif names:
                self.preset_combo.set_selected(0)
        finally:
            self._suppressing = False
        self.preset_combo.connect("notify::selected", self._on_preset_selected)
        self._on_preset_selected(self.preset_combo, None)

    def _current_preset(self) -> dict | None:
        idx = self.preset_combo.get_selected()
        if 0 <= idx < len(self.window.presets):
            return self.window.presets[idx]
        return None

    def _on_preset_selected(self, _combo, _pspec) -> None:
        preset = self._current_preset()
        if not preset:
            return
        previous = self.window.active_preset_name
        self.window.active_preset_name = preset["name"]
        is_builtin = preset["builtin"]

        self._suppressing = True
        try:
            c = preset["curve"]
            self.sensitivity_adj.set_value(c["sensitivity"])
            self.gain_adj.set_value(c["gain"])
            self.power_adj.set_value(c["power"])
            self.deadzone_adj.set_value(c["deadzone"])
            self.max_mult_adj.set_value(c["max_multiplier"])
            self.description_row.set_subtitle(preset.get("description", ""))
            for r in self.param_rows:
                r.set_sensitive(not is_builtin)
            self.rename_btn.set_sensitive(not is_builtin)
            self.delete_btn.set_sensitive(not is_builtin)
            tag = " (Built-in)" if is_builtin else ""
            self.description_row.set_title(f"Descrição{tag}")
        finally:
            self._suppressing = False

        self.preview.set_curve(preset["curve"])

        # Auto-aplica em qualquer mudança de preset (curva ou Adaptativa).
        if previous != preset["name"]:
            self.schedule_auto_apply()

    # ----- editing -----

    def _current_curve_from_sliders(self) -> dict:
        return {
            "sensitivity": self.sensitivity_adj.get_value(),
            "gain": self.gain_adj.get_value(),
            "power": self.power_adj.get_value(),
            "deadzone": self.deadzone_adj.get_value(),
            "max_multiplier": self.max_mult_adj.get_value(),
        }

    def _on_param_changed(self, _adj) -> None:
        if self._suppressing:
            return
        preset = self._current_preset()
        if not preset or preset["builtin"]:
            return
        curve = self._current_curve_from_sliders()
        self.preview.set_curve(curve)
        # debounce 400ms
        if self._save_timer_id:
            GLib.source_remove(self._save_timer_id)
        self._save_timer_id = GLib.timeout_add(400, self._commit_curve_change)

    def _commit_curve_change(self) -> bool:
        self._save_timer_id = None
        preset = self._current_preset()
        if not preset or preset["builtin"]:
            return GLib.SOURCE_REMOVE
        curve = self._current_curve_from_sliders()
        mft_common.save_custom_preset(
            preset["name"], preset.get("description", ""), curve
        )
        self.window.reload_presets(keep_selection=preset["name"])
        daemon_reload()
        return GLib.SOURCE_REMOVE

    # ----- new / duplicate / rename / delete -----

    def _on_new(self, _btn) -> None:
        """Abre dialog pra nomear um novo preset custom com curva default."""
        dlg = Adw.AlertDialog(
            heading="Novo preset",
            body="Crie um preset customizado vazio. Os parâmetros começam com valores padrão e podem ser editados depois.",
        )
        entry = Gtk.Entry(placeholder_text="Nome do novo preset")
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancelar")
        dlg.add_response("create", "Criar")
        dlg.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("create")
        dlg.set_close_response("cancel")

        def on_response(_dlg, response):
            if response != "create":
                return
            name = entry.get_text().strip()
            if not name:
                self.window.toast("Nome não pode estar vazio")
                return
            existing = {p["name"] for p in self.window.presets}
            if name in existing:
                self.window.toast(f"Já existe um preset chamado “{name}”")
                return
            mft_common.save_custom_preset(name, "", dict(mft_common.DEFAULT_CURVE))
            self.window.reload_presets(keep_selection=name)
            daemon_reload()

        dlg.connect("response", on_response)
        dlg.present(self.window)

    def _on_duplicate(self, _btn) -> None:
        preset = self._current_preset()
        if not preset:
            return
        base = preset["name"]
        new_name = base + " cópia"
        n = 2
        existing = {p["name"] for p in self.window.presets}
        while new_name in existing:
            new_name = f"{base} cópia {n}"
            n += 1
        mft_common.save_custom_preset(new_name, preset.get("description", ""), preset["curve"])
        self.window.reload_presets(keep_selection=new_name)
        daemon_reload()

    def _on_rename(self, _btn) -> None:
        preset = self._current_preset()
        if not preset or preset["builtin"]:
            return
        dlg = Adw.AlertDialog(
            heading="Renomear preset",
            body=f"Novo nome para “{preset['name']}”:",
        )
        entry = Gtk.Entry(text=preset["name"])
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancelar")
        dlg.add_response("ok", "Renomear")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("ok")
        dlg.set_close_response("cancel")

        def on_response(_dlg, response):
            if response != "ok":
                return
            new_name = entry.get_text().strip()
            if not new_name or new_name == preset["name"]:
                return
            result = mft_common.rename_custom_preset(preset["name"], new_name)
            if result is None:
                self.window.toast(f"Não foi possível renomear (conflito de nome?)")
                return
            self.window.reload_presets(keep_selection=result["name"])
            daemon_reload()

        dlg.connect("response", on_response)
        dlg.present(self.window)

    def _on_delete(self, _btn) -> None:
        preset = self._current_preset()
        if not preset or preset["builtin"]:
            return
        dlg = Adw.AlertDialog(
            heading=f"Deletar preset “{preset['name']}”?",
            body="Esta ação não pode ser desfeita.",
        )
        dlg.add_response("cancel", "Cancelar")
        dlg.add_response("delete", "Deletar")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def on_response(_dlg, response):
            if response != "delete":
                return
            if mft_common.delete_custom_preset(preset["name"]):
                self.window.reload_presets()
                daemon_reload()

        dlg.connect("response", on_response)
        dlg.present(self.window)

    # ----- aplicação / detecção (sempre on, oculto ao usuário) -----

    def update_devices(self, devices: list[dict]) -> None:
        """Recebe lista do daemon via IPC."""
        self._live_devices = [d for d in devices if d.get("present")]
        self._in_use_ids = set(self.window.daemon_active_devices)

    def refresh_apply_status(self) -> None:
        """Compat: chamado de refresh_all e periodic_tick."""
        self._in_use_ids = set(self.window.daemon_active_devices)

    def schedule_auto_apply(self) -> None:
        """Agenda uma aplicação automática (debounce 300ms)."""
        if getattr(self, "_auto_apply_timer", None) is not None:
            GLib.source_remove(self._auto_apply_timer)
        self._auto_apply_timer = GLib.timeout_add(300, self._do_auto_apply)

    def _do_auto_apply(self) -> bool:
        """Auto-apply silencioso, dispara em mudança de preset:
          - Adaptativa: aplica imediatamente (libera grabs, sem probe).
          - Curva: se já houver mouse interceptado, troca a curva.
                   Senão, faz probe assíncrono (até 3s) pra detectar mouse
                   em uso e aplica nele. Fallback: aplica em todos os mouses
                   presentes."""
        self._auto_apply_timer = None
        preset = self._current_preset()
        if not preset:
            return GLib.SOURCE_REMOVE
        if not daemon_unit_exists():
            return GLib.SOURCE_REMOVE

        if mft_common.is_system_default(preset):
            self.window.apply_preset_to_in_use(preset["name"], set())
            if self.window.ipc.connected:
                self.window.ipc.request_device_list()
            return GLib.SOURCE_REMOVE

        # Curva customizada
        if not daemon_active():
            daemon_start()

        already_active = set(self.window.daemon_active_devices)
        if already_active:
            # Troca curva nos mouses já interceptados — instantâneo
            self.window.apply_preset_to_in_use(preset["name"], already_active)
            if self.window.ipc.connected:
                self.window.ipc.request_device_list()
            return GLib.SOURCE_REMOVE

        # Nenhum mouse interceptado ainda — detectar via probe
        def worker():
            in_use = self.window.detect_in_use_now(timeout=3.0)
            if not in_use:
                # Fallback: aplica em todos os mouses presentes
                in_use = {m["id"] for m in mft_common.enumerate_present_mice()}
            GLib.idle_add(self._finish_auto_apply, preset["name"], in_use)

        threading.Thread(target=worker, daemon=True).start()
        return GLib.SOURCE_REMOVE

    def _finish_auto_apply(self, preset_name: str, in_use: set[str]) -> bool:
        self.window.apply_preset_to_in_use(preset_name, in_use)
        if self.window.ipc.connected:
            self.window.ipc.request_device_list()
        return False

    # ----- live monitor -----

    def _on_live_toggle(self, switch, _pspec) -> None:
        if switch.get_active():
            if not self.window.ipc.connected:
                ok = self.window.ipc.connect()
                if not ok:
                    self.window.toast("Daemon não responde — está rodando?")
                    switch.set_active(False)
                    return
            # Subscribe a "todos" — só recebe eventos dos mouses em uso de qualquer jeito
            self.window.ipc.subscribe("*")
            self.live_monitor.set_active(True)
        else:
            if self.window.ipc.connected:
                self.window.ipc.unsubscribe()
            self.live_monitor.set_active(False)

    def receive_event(self, msg: dict) -> None:
        if not self.live_monitor.active:
            return
        # Aceita eventos de qualquer mouse em uso
        self.live_monitor.add_sample(
            msg.get("t", time.monotonic()),
            msg.get("speed_in", 0.0),
            msg.get("speed_out", 0.0),
        )


# ====================== janela ======================


class MouseFineTuningWindow(Adw.ApplicationWindow):
    __gtype_name__ = "MouseFineTuningWindow"

    def __init__(self, app: Adw.Application) -> None:
        super().__init__(
            application=app,
            title="Mouse Fine-Tuning",
            default_width=720,
            default_height=820,
        )
        self.set_size_request(560, 660)

        self.settings = Gio.Settings.new(SCHEMA)
        self.presets: list[dict] = []
        self.active_preset_name: str | None = None
        # devices que o daemon está interceptando agora (atualizado via IPC)
        self.daemon_active_devices: set[str] = set()

        # IPC
        self.ipc = DaemonIPC()
        self.ipc.on_event = self._on_ipc_event
        self.ipc.on_device_list = self._on_ipc_device_list
        self.ipc.on_connect_changed = self._on_ipc_connect_changed

        # toast overlay
        self._toast_overlay = Adw.ToastOverlay()

        mft_common.sync_builtin_presets()
        self.reload_presets()

        # Limpa mouses ausentes/históricos do devices.json antes de tudo
        self.cleanup_devices_config()
        daemon_reload()

        # Fallback adaptive (só se accel-profile estiver em 'default')
        if self.settings.get_string("accel-profile") == "default":
            self.settings.set_string("accel-profile", "adaptive")

        self._build_ui()
        self._install_actions()
        self.refresh_all()
        GLib.timeout_add_seconds(3, self._periodic_tick)

        # Auto-apply silencioso no boot — só pra Adaptativa (sem grab).
        # Pra curvas customizadas, o usuário precisa clicar "Aplicar curva"
        # explicitamente (evita travamento acidental do mouse).
        self.presets_page.schedule_auto_apply()

    # ----- presets/devices state -----

    def reload_presets(self, keep_selection: str | None = None) -> None:
        self.presets = mft_common.list_all_presets()
        if not self.active_preset_name and self.presets:
            self.active_preset_name = self.presets[0]["name"]
        if keep_selection:
            self.active_preset_name = keep_selection
        if hasattr(self, "presets_page"):
            self.presets_page.rebuild_preset_list(select_name=self.active_preset_name)

    def list_devices_with_config(self) -> list[dict]:
        """Lista combinada: presentes (evdev) + config (devices.json). Exclui virtuais."""
        cfg = mft_common.load_devices_config()
        present_raw = mft_common.enumerate_present_mice()
        present = [
            {"id": d["id"], "name": d["name"], "path": d["path"], "present": True}
            for d in present_raw
        ]
        present_ids = {d["id"] for d in present}

        for entry in present:
            cfg_entry = mft_common.find_device(cfg, entry["id"])
            entry["config"] = cfg_entry or {}
            entry["active"] = bool(cfg_entry and cfg_entry.get("enabled"))

        result = list(present)
        for d in cfg.get("devices", []):
            if d["id"] not in present_ids:
                result.append({
                    "id": d["id"],
                    "name": d.get("name", "(desconectado)"),
                    "present": False,
                    "active": False,
                    "config": d,
                })
        return result

    # ----- UI -----

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle.new("Mouse Fine-Tuning", ""))
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu_button.set_tooltip_text("Menu principal")
        menu = Gio.Menu()
        menu.append("Restaurar gsettings padrão", "win.reset_gsettings")
        menu.append("Sobre o Mouse Fine-Tuning", "win.about")
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)

        self.presets_page = PresetsPage(self)

        toolbar.add_top_bar(header)
        self._toast_overlay.set_child(self.presets_page.page)
        toolbar.set_content(self._toast_overlay)
        self.set_content(toolbar)

        # primeira chamada pra popular o preset combo
        self.reload_presets(keep_selection=self.active_preset_name)

    # ----- IPC callbacks (no main loop) -----

    def _on_ipc_event(self, msg: dict) -> bool:
        self.presets_page.receive_event(msg)
        return False

    def _on_ipc_device_list(self, msg: dict) -> bool:
        devices = msg.get("devices", [])
        self.daemon_active_devices = {d["id"] for d in devices if d.get("active")}
        self.presets_page.update_devices(devices)
        return False

    def _on_ipc_connect_changed(self, connected: bool) -> bool:
        if connected:
            self.ipc.request_device_list()
        return False

    # ----- actions / refresh -----

    def _install_actions(self) -> None:
        a1 = Gio.SimpleAction.new("reset_gsettings", None)
        a1.connect("activate", self._on_reset_gsettings)
        self.add_action(a1)

        a2 = Gio.SimpleAction.new("about", None)
        a2.connect("activate", self._on_about)
        self.add_action(a2)

    def _on_reset_gsettings(self, _action, _param) -> None:
        for key in ("speed", "accel-profile", "drag-threshold"):
            self.settings.reset(key)

    def _on_about(self, _action, _param) -> None:
        about = Adw.AboutDialog(
            application_name="Mouse Fine-Tuning",
            application_icon="input-mouse-symbolic",
            developer_name="lcf2212",
            version=APP_VERSION,
            comments=(
                "Configurações nativas do mouse + curva de aceleração customizada "
                "velocidade-dependente, com presets, multi-device e live monitor."
            ),
            copyright="© 2026 lcf2212",
            license_type=Gtk.License.MIT_X11,
            website="https://github.com/lcf2212dev/gnome-mouse-fine-tunning",
        )
        about.present(self)

    def toast(self, text: str) -> None:
        self._toast_overlay.add_toast(Adw.Toast.new(text))

    def refresh_all(self) -> None:
        self.presets_page.refresh_apply_status()
        if self.ipc.connected:
            self.ipc.request_device_list()

    def _periodic_tick(self) -> bool:
        # reconectar IPC se daemon ficou ativo agora; refrescar status
        self.presets_page.refresh_apply_status()
        if not self.ipc.connected and daemon_active():
            self.ipc.connect()
        return GLib.SOURCE_CONTINUE

    # ----- aplicação automática ao mouse em uso -----

    def cleanup_devices_config(self) -> None:
        """Remove de devices.json qualquer mouse não presente no momento."""
        cfg = mft_common.load_devices_config()
        present_ids = {m["id"] for m in mft_common.enumerate_present_mice()}
        before = len(cfg.get("devices", []))
        cfg["devices"] = [d for d in cfg.get("devices", []) if d["id"] in present_ids]
        if len(cfg["devices"]) != before:
            mft_common.save_devices_config(cfg)

    def detect_in_use_now(self, timeout: float = 1.5) -> set[str]:
        """Combina probe direto + IDs já interceptados pelo daemon."""
        result = set(self.daemon_active_devices)
        probe = mft_common.probe_mouse_activity(timeout=timeout)
        for did, in_use in probe.items():
            if in_use:
                result.add(did)
        return result

    def apply_preset_to_in_use(self, preset_name: str, in_use_ids: set[str]) -> int:
        """Aplica preset aos mouses em uso.

        - Adaptativa: nenhum mouse interceptado, `accel-profile=adaptive`.
        - Custom: mouses em `in_use_ids` ficam enabled=true; `accel-profile=flat`.

        O `gsettings speed` NÃO é tocado por essa função — é controlado pelo
        slider de velocidade do app (ligado diretamente ao gsettings).
        """
        if mft_common.is_system_default(preset_name):
            self.disable_curve_everywhere()
            self.settings.set_string("accel-profile", "adaptive")
            return 0

        cfg = mft_common.load_devices_config()
        present = {m["id"]: m["name"] for m in mft_common.enumerate_present_mice()}
        for did in in_use_ids:
            if did in present:
                mft_common.upsert_device(cfg, did, present[did])
        n = 0
        for d in cfg.get("devices", []):
            if d["id"] in in_use_ids:
                d["enabled"] = True
                d["preset"] = preset_name
                n += 1
            else:
                d["enabled"] = False
        mft_common.save_devices_config(cfg)
        self.settings.set_string("accel-profile", "flat")
        daemon_reload()
        return n

    def disable_curve_everywhere(self) -> None:
        cfg = mft_common.load_devices_config()
        for d in cfg.get("devices", []):
            d["enabled"] = False
        mft_common.save_devices_config(cfg)
        daemon_reload()


class MissingSchemaWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(
            application=app,
            title="Mouse Fine-Tuning",
            default_width=520,
            default_height=400,
        )
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(
            Adw.StatusPage(
                icon_name="dialog-error-symbolic",
                title="Schema do GNOME não encontrado",
                description=(
                    f"O schema “{SCHEMA}” não está disponível neste sistema."
                ),
            )
        )
        self.set_content(toolbar)


class MouseFineTuningApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )

    def do_activate(self) -> None:
        win = self.get_active_window()
        if win is None:
            win = (
                MouseFineTuningWindow(self)
                if schema_available()
                else MissingSchemaWindow(self)
            )
        win.present()


def main() -> int:
    return MouseFineTuningApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
