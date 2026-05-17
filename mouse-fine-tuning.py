#!/usr/bin/env python3
"""Mouse Fine-Tuning — configuração visual do mouse para GNOME.

Inclui:
  * Configurações nativas do GNOME via gsettings (velocidade, perfil de
    aceleração, limiar de arrasto).
  * Curva de aceleração customizada velocidade-dependente, aplicada por um
    daemon uinput separado (mouse-curve-daemon). Esta janela edita a curva,
    salva em ~/.config/mouse-fine-tuning/curve.json e controla o daemon
    via systemd --user."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

try:
    import evdev  # type: ignore

    HAVE_EVDEV = True
except ImportError:
    HAVE_EVDEV = False

APP_ID = "br.dev.lcf2212.MouseFineTuning"
APP_VERSION = "0.2.0"
SCHEMA = "org.gnome.desktop.peripherals.mouse"

CONFIG_DIR = Path.home() / ".config" / "mouse-fine-tuning"
CURVE_PATH = CONFIG_DIR / "curve.json"

DAEMON_UNIT = "mouse-curve-daemon.service"

PROFILES = ["default", "flat", "adaptive"]
PROFILE_LABELS = ["Padrão do sistema", "Desativada (flat)", "Adaptativa"]

DEFAULT_CURVE = {
    "sensitivity": 1.0,
    "gain": 0.1,
    "power": 1.5,
    "deadzone": 0.0,
    "max_multiplier": 3.0,
}


# ---------- utilitários de schema e config ----------


def schema_available() -> bool:
    source = Gio.SettingsSchemaSource.get_default()
    return source is not None and source.lookup(SCHEMA, True) is not None


def load_curve_config() -> dict:
    if not CURVE_PATH.exists():
        return {"device_path": "", "curve": DEFAULT_CURVE.copy()}
    try:
        with CURVE_PATH.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"device_path": "", "curve": DEFAULT_CURVE.copy()}
    curve = {**DEFAULT_CURVE, **(data.get("curve") or {})}
    return {"device_path": data.get("device_path", ""), "curve": curve}


def save_curve_config(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CURVE_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(CURVE_PATH)


# ---------- detecção de mouses (opcional, requer evdev) ----------


def list_mice() -> list[tuple[str, str]]:
    """Retorna [(path, friendly_name), ...]. Vazia se evdev não disponível."""
    if not HAVE_EVDEV:
        return []
    found: list[tuple[str, str]] = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        caps = dev.capabilities()
        rels = caps.get(evdev.ecodes.EV_REL, [])
        if (
            evdev.ecodes.REL_X in rels
            and evdev.ecodes.REL_Y in rels
            and evdev.ecodes.EV_ABS not in caps
        ):
            keys = caps.get(evdev.ecodes.EV_KEY, [])
            if evdev.ecodes.BTN_LEFT in keys or evdev.ecodes.BTN_MOUSE in keys:
                found.append((path, f"{dev.name} ({path})"))
        dev.close()
    return found


# ---------- helpers do systemd --user ----------


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
    ).exists() or (
        Path("/etc/systemd/user") / DAEMON_UNIT
    ).exists()


def daemon_start() -> tuple[bool, str]:
    res = systemctl("enable", "--now", DAEMON_UNIT)
    return res.returncode == 0, (res.stderr or res.stdout).strip()


def daemon_stop() -> tuple[bool, str]:
    res = systemctl("disable", "--now", DAEMON_UNIT)
    return res.returncode == 0, (res.stderr or res.stdout).strip()


def daemon_reload_config() -> None:
    systemctl("kill", "--signal=HUP", DAEMON_UNIT)


# ---------- preview gráfico da curva ----------


class CurvePreview(Gtk.DrawingArea):
    __gtype_name__ = "CurvePreview"

    MAX_SPEED_PPS = 3000.0  # eixo X do gráfico
    MAX_MULT_DISPLAY_FLOOR = 1.5  # mantém escala mínima do eixo Y

    def __init__(self) -> None:
        super().__init__()
        self.set_size_request(360, 200)
        self.set_hexpand(True)
        self.set_draw_func(self._on_draw)
        self.params = DEFAULT_CURVE.copy()

    def set_params(self, **kwargs) -> None:
        self.params.update(kwargs)
        self.queue_draw()

    def _multiplier_at(self, speed: float) -> float:
        effective = max(0.0, speed - self.params["deadzone"])
        accel = self.params["gain"] * (effective / 1000.0) ** self.params["power"]
        return min(
            self.params["sensitivity"] * (1.0 + accel),
            self.params["max_multiplier"],
        )

    def _on_draw(self, _area, cr, width, height) -> None:
        margin_left = 44
        margin_right = 12
        margin_top = 14
        margin_bottom = 28
        plot_w = width - margin_left - margin_right
        plot_h = height - margin_top - margin_bottom

        style = self.get_style_context()
        fg_color = style.get_color()

        # fundo: card transparente, deixa o tema cuidar
        # eixos
        cr.set_source_rgba(fg_color.red, fg_color.green, fg_color.blue, 0.5)
        cr.set_line_width(1)
        cr.move_to(margin_left, margin_top)
        cr.line_to(margin_left, margin_top + plot_h)
        cr.line_to(margin_left + plot_w, margin_top + plot_h)
        cr.stroke()

        # linha de referência y=1 (sem multiplicação)
        y_max = max(self.params["max_multiplier"], self.MAX_MULT_DISPLAY_FLOOR)
        ref_y = margin_top + plot_h - (1.0 / y_max) * plot_h
        cr.set_dash([4, 4])
        cr.set_source_rgba(fg_color.red, fg_color.green, fg_color.blue, 0.25)
        cr.move_to(margin_left, ref_y)
        cr.line_to(margin_left + plot_w, ref_y)
        cr.stroke()
        cr.set_dash([])

        # marca da deadzone (linha vertical)
        if self.params["deadzone"] > 0:
            dz_x = margin_left + (self.params["deadzone"] / self.MAX_SPEED_PPS) * plot_w
            if dz_x < margin_left + plot_w:
                cr.set_source_rgba(1.0, 0.5, 0.2, 0.45)
                cr.set_dash([3, 3])
                cr.move_to(dz_x, margin_top)
                cr.line_to(dz_x, margin_top + plot_h)
                cr.stroke()
                cr.set_dash([])

        # curva
        cr.set_source_rgba(0.27, 0.6, 0.95, 1.0)
        cr.set_line_width(2.4)
        samples = 160
        for i in range(samples + 1):
            speed = self.MAX_SPEED_PPS * (i / samples)
            mult = self._multiplier_at(speed)
            x = margin_left + plot_w * (i / samples)
            y = margin_top + plot_h - (mult / y_max) * plot_h
            y = max(margin_top, min(margin_top + plot_h, y))
            if i == 0:
                cr.move_to(x, y)
            else:
                cr.line_to(x, y)
        cr.stroke()

        # texto: eixos
        cr.set_source_rgba(fg_color.red, fg_color.green, fg_color.blue, 0.75)
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(10)

        # ticks Y (1.0 e y_max)
        cr.move_to(6, ref_y + 4)
        cr.show_text("×1.0")
        cr.move_to(6, margin_top + 10)
        cr.show_text(f"×{y_max:.1f}")

        # rótulos X
        cr.move_to(margin_left - 2, margin_top + plot_h + 16)
        cr.show_text("0 px/s")
        label_max = f"{int(self.MAX_SPEED_PPS)} px/s"
        text_w = cr.text_extents(label_max).width
        cr.move_to(margin_left + plot_w - text_w, margin_top + plot_h + 16)
        cr.show_text(label_max)


# ---------- janela principal ----------


class MouseFineTuningWindow(Adw.ApplicationWindow):
    __gtype_name__ = "MouseFineTuningWindow"

    def __init__(self, app: Adw.Application) -> None:
        super().__init__(
            application=app,
            title="Mouse Fine-Tuning",
            default_width=620,
            default_height=720,
        )
        self.set_size_request(480, 600)
        self.settings = Gio.Settings.new(SCHEMA)

        self.curve_config = load_curve_config()
        self._save_timer_id: int | None = None
        self._refreshing_devices = False

        self._build_ui()
        self._install_actions()
        self._refresh_daemon_status()
        self._refresh_devices_combo()

        # polling lento de status do daemon
        GLib.timeout_add_seconds(3, self._on_periodic_tick)

    # ----- construção da UI -----

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()

        header = Adw.HeaderBar()

        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu_button.set_tooltip_text("Menu principal")
        menu = Gio.Menu()
        menu.append("Restaurar padrões", "win.reset")
        menu.append("Resetar curva", "win.reset_curve")
        menu.append("Sobre o Mouse Fine-Tuning", "win.about")
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)

        self.view_stack = Adw.ViewStack()
        self.view_stack.add_titled_with_icon(
            self._build_basic_page(), "basic", "Configurações", "input-mouse-symbolic"
        )
        self.view_stack.add_titled_with_icon(
            self._build_curve_page(),
            "curve",
            "Curva",
            "preferences-other-symbolic",
        )

        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self.view_stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        toolbar.add_top_bar(header)
        toolbar.set_content(self.view_stack)
        self.set_content(toolbar)

    # ----- página "Configurações" (gsettings) -----

    def _build_basic_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        page.add(self._build_speed_group())
        page.add(self._build_accel_group())
        page.add(self._build_fine_group())
        return page

    def _build_speed_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Velocidade",
            description=(
                "Ajuste a velocidade do ponteiro. Aplica-se a todos os "
                "mouses conectados."
            ),
        )

        self.speed_adj = Gtk.Adjustment(
            lower=-1.0,
            upper=1.0,
            step_increment=0.01,
            page_increment=0.1,
        )
        self.settings.bind(
            "speed", self.speed_adj, "value", Gio.SettingsBindFlags.DEFAULT
        )

        slider_row = Adw.ActionRow(
            title="Velocidade do ponteiro",
            subtitle="Arraste o controle para escolher a velocidade",
        )
        scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=self.speed_adj,
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

        spin_row = Adw.SpinRow(
            title="Ajuste fino",
            subtitle="Mesma escala com precisão de 0.01",
            digits=2,
        )
        spin_row.set_adjustment(self.speed_adj)
        group.add(spin_row)

        return group

    def _build_accel_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Aceleração nativa",
            description=(
                "Perfil aplicado pelo libinput. Quando a curva customizada "
                "(aba “Curva”) estiver ativa, recomenda-se manter este perfil "
                "em “Desativada (flat)”."
            ),
        )

        self.combo = Adw.ComboRow(title="Perfil de aceleração")
        self.combo.set_model(Gtk.StringList.new(PROFILE_LABELS))
        group.add(self.combo)

        self.settings.connect(
            "changed::accel-profile", self._on_profile_setting_changed
        )
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

        self.drag_adj = Gtk.Adjustment(
            lower=1, upper=30, step_increment=1, page_increment=5
        )
        self.settings.bind(
            "drag-threshold", self.drag_adj, "value", Gio.SettingsBindFlags.DEFAULT
        )
        drag_row = Adw.SpinRow(
            title="Limiar de arrasto",
            subtitle="Pixels antes do sistema reconhecer um arrasto",
            digits=0,
        )
        drag_row.set_adjustment(self.drag_adj)
        group.add(drag_row)

        return group

    # ----- página "Curva" -----

    def _build_curve_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        page.add(self._build_curve_status_group())
        page.add(self._build_curve_params_group())
        page.add(self._build_curve_preview_group())
        return page

    def _build_curve_status_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Curva customizada",
            description=(
                "Quando habilitada, um daemon em background substitui a "
                "aceleração do sistema pela curva configurada abaixo."
            ),
        )

        self.enable_row = Adw.SwitchRow(
            title="Habilitar curva customizada",
            subtitle="Inicia/para o serviço mouse-curve-daemon",
        )
        self.enable_row.connect("notify::active", self._on_enable_toggled)
        group.add(self.enable_row)

        self.device_row = Adw.ComboRow(
            title="Mouse",
            subtitle="Dispositivo de entrada interceptado",
        )
        self.device_row.connect("notify::selected", self._on_device_selected)
        group.add(self.device_row)

        self.status_row = Adw.ActionRow(
            title="Status do daemon",
            subtitle="—",
        )
        refresh_btn = Gtk.Button(
            icon_name="view-refresh-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text="Atualizar status e lista de mouses",
        )
        refresh_btn.add_css_class("flat")
        refresh_btn.connect("clicked", lambda *_: self._refresh_all())
        self.status_row.add_suffix(refresh_btn)
        group.add(self.status_row)

        return group

    def _build_curve_params_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Parâmetros da curva",
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

        c = self.curve_config["curve"]
        self.sensitivity_adj.set_value(c["sensitivity"])
        self.gain_adj.set_value(c["gain"])
        self.power_adj.set_value(c["power"])
        self.deadzone_adj.set_value(c["deadzone"])
        self.max_mult_adj.set_value(c["max_multiplier"])

        rows_data = [
            (
                "Sensibilidade base",
                "Multiplicador quando velocidade ≈ 0",
                self.sensitivity_adj,
                2,
            ),
            (
                "Ganho de aceleração",
                "Quanto a aceleração cresce com a velocidade",
                self.gain_adj,
                2,
            ),
            (
                "Expoente",
                "1.0 = linear; >1 acelera mais em alta velocidade",
                self.power_adj,
                2,
            ),
            (
                "Dead-zone (px/s)",
                "Velocidade abaixo desta não recebe aceleração extra",
                self.deadzone_adj,
                0,
            ),
            (
                "Multiplicador máximo",
                "Teto absoluto do multiplicador resultante",
                self.max_mult_adj,
                1,
            ),
        ]
        for title, subtitle, adj, digits in rows_data:
            row = Adw.SpinRow(title=title, subtitle=subtitle, digits=digits)
            row.set_adjustment(adj)
            adj.connect("value-changed", self._on_curve_param_changed)
            group.add(row)

        return group

    def _build_curve_preview_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Visualização da curva",
            description=(
                "Eixo X: velocidade do mouse (pixels por segundo). "
                "Eixo Y: multiplicador aplicado. A linha tracejada é "
                "“sem aceleração” (×1.0)."
            ),
        )

        self.preview = CurvePreview()
        self.preview.set_margin_top(8)
        self.preview.set_margin_bottom(8)
        self.preview.set_margin_start(8)
        self.preview.set_margin_end(8)

        wrapper = Gtk.Frame()
        wrapper.set_child(self.preview)
        wrapper.set_margin_top(4)
        wrapper.set_margin_bottom(4)

        host_row = Adw.PreferencesRow()
        host_row.set_activatable(False)
        host_row.set_selectable(False)
        host_row.set_child(wrapper)
        group.add(host_row)

        self._sync_preview_from_adjustments()
        return group

    # ----- handlers da página "Curva" -----

    def _sync_preview_from_adjustments(self) -> None:
        self.preview.set_params(
            sensitivity=self.sensitivity_adj.get_value(),
            gain=self.gain_adj.get_value(),
            power=self.power_adj.get_value(),
            deadzone=self.deadzone_adj.get_value(),
            max_multiplier=self.max_mult_adj.get_value(),
        )

    def _on_curve_param_changed(self, _adj) -> None:
        self._sync_preview_from_adjustments()
        # debounce: salva 400ms após última mudança
        if self._save_timer_id is not None:
            GLib.source_remove(self._save_timer_id)
        self._save_timer_id = GLib.timeout_add(400, self._commit_curve)

    def _commit_curve(self) -> bool:
        self.curve_config["curve"] = {
            "sensitivity": self.sensitivity_adj.get_value(),
            "gain": self.gain_adj.get_value(),
            "power": self.power_adj.get_value(),
            "deadzone": self.deadzone_adj.get_value(),
            "max_multiplier": self.max_mult_adj.get_value(),
        }
        try:
            save_curve_config(self.curve_config)
            daemon_reload_config()
        except OSError as e:
            self._set_status(f"Erro salvando: {e}", warning=True)
        self._save_timer_id = None
        return GLib.SOURCE_REMOVE

    def _on_enable_toggled(self, switch: Adw.SwitchRow, _pspec) -> None:
        if self._refreshing_devices:
            return
        if switch.get_active():
            if not daemon_unit_exists():
                self._set_status(
                    "Unit systemd ausente. Rode setup.sh primeiro.", warning=True
                )
                switch.set_active(False)
                return
            self.settings.set_string("accel-profile", "flat")
            self._commit_curve_now()
            ok, msg = daemon_start()
            if not ok:
                self._set_status(f"Falha ao iniciar: {msg}", warning=True)
                switch.set_active(False)
        else:
            ok, msg = daemon_stop()
            if not ok and "not loaded" not in msg.lower():
                self._set_status(f"Falha ao parar: {msg}", warning=True)
        GLib.timeout_add(300, self._refresh_daemon_status_and_return_false)

    def _commit_curve_now(self) -> None:
        if self._save_timer_id is not None:
            GLib.source_remove(self._save_timer_id)
            self._save_timer_id = None
        self._commit_curve()

    def _on_device_selected(self, combo: Adw.ComboRow, _pspec) -> None:
        if self._refreshing_devices:
            return
        idx = combo.get_selected()
        if idx >= len(self._device_paths):
            return
        path = self._device_paths[idx]
        if self.curve_config.get("device_path") != path:
            self.curve_config["device_path"] = path
            self._commit_curve_now()
            # reiniciar daemon se estava rodando para pegar novo device
            if daemon_active():
                systemctl("restart", DAEMON_UNIT)

    def _refresh_devices_combo(self) -> None:
        self._refreshing_devices = True
        try:
            mice = list_mice()
            self._device_paths = [p for p, _ in mice]
            labels = [name for _, name in mice]
            if not labels:
                labels = (
                    ["(python-evdev não instalado)"]
                    if not HAVE_EVDEV
                    else ["(nenhum mouse detectado)"]
                )
                self._device_paths = [""]
            self.device_row.set_model(Gtk.StringList.new(labels))
            saved = self.curve_config.get("device_path", "")
            if saved in self._device_paths:
                self.device_row.set_selected(self._device_paths.index(saved))
            else:
                self.device_row.set_selected(0)
            self.device_row.set_sensitive(bool(self._device_paths and self._device_paths[0]))
        finally:
            self._refreshing_devices = False

    def _set_status(self, text: str, warning: bool = False) -> None:
        self.status_row.set_subtitle(text)
        if warning:
            self.status_row.add_css_class("warning")
        else:
            self.status_row.remove_css_class("warning")

    def _refresh_daemon_status(self) -> None:
        if not daemon_unit_exists():
            self._set_status(
                "Unit systemd não instalada (rode `./setup.sh`)", warning=True
            )
            self.enable_row.set_sensitive(False)
            return
        self.enable_row.set_sensitive(True)

        active = daemon_active()
        enabled = daemon_enabled()

        if active and enabled:
            self._set_status("Ativo e habilitado no login")
        elif active:
            self._set_status("Ativo (não inicia no login)")
        elif enabled:
            self._set_status("Habilitado no login mas inativo")
        else:
            self._set_status("Parado")

        # refletir no switch sem disparar handler
        self._refreshing_devices = True
        try:
            self.enable_row.set_active(active)
        finally:
            self._refreshing_devices = False

    def _refresh_daemon_status_and_return_false(self) -> bool:
        self._refresh_daemon_status()
        return GLib.SOURCE_REMOVE

    def _refresh_all(self) -> None:
        self._refresh_devices_combo()
        self._refresh_daemon_status()

    def _on_periodic_tick(self) -> bool:
        self._refresh_daemon_status()
        return GLib.SOURCE_CONTINUE

    # ----- handlers do enum accel-profile (página "Configurações") -----

    def _on_profile_setting_changed(self, settings: Gio.Settings, _key: str) -> None:
        current = settings.get_string("accel-profile")
        try:
            idx = PROFILES.index(current)
        except ValueError:
            idx = 0
        if self.combo.get_selected() != idx:
            self.combo.set_selected(idx)

    def _on_profile_combo_changed(self, combo: Adw.ComboRow, _pspec) -> None:
        idx = combo.get_selected()
        if idx >= len(PROFILES):
            return
        new = PROFILES[idx]
        if self.settings.get_string("accel-profile") != new:
            self.settings.set_string("accel-profile", new)

    # ----- actions do menu -----

    def _install_actions(self) -> None:
        reset = Gio.SimpleAction.new("reset", None)
        reset.connect("activate", self._on_reset_activated)
        self.add_action(reset)

        reset_curve = Gio.SimpleAction.new("reset_curve", None)
        reset_curve.connect("activate", self._on_reset_curve_activated)
        self.add_action(reset_curve)

        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._on_about_activated)
        self.add_action(about)

    def _on_reset_activated(self, _action, _param) -> None:
        for key in ("speed", "accel-profile", "drag-threshold"):
            self.settings.reset(key)

    def _on_reset_curve_activated(self, _action, _param) -> None:
        self.sensitivity_adj.set_value(DEFAULT_CURVE["sensitivity"])
        self.gain_adj.set_value(DEFAULT_CURVE["gain"])
        self.power_adj.set_value(DEFAULT_CURVE["power"])
        self.deadzone_adj.set_value(DEFAULT_CURVE["deadzone"])
        self.max_mult_adj.set_value(DEFAULT_CURVE["max_multiplier"])
        # value-changed do último set já dispara commit, mas garantimos:
        self._commit_curve_now()

    def _on_about_activated(self, _action, _param) -> None:
        about = Adw.AboutDialog(
            application_name="Mouse Fine-Tuning",
            application_icon="input-mouse-symbolic",
            developer_name="lcf2212",
            version=APP_VERSION,
            comments=(
                "Configurações nativas do mouse + curva de aceleração "
                "customizada velocidade-dependente."
            ),
            copyright="© 2026 lcf2212",
            license_type=Gtk.License.MIT_X11,
            website="https://github.com/lcf2212dev/gnome-mouse-fine-tunning",
        )
        about.present(self)


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
                    f"O schema “{SCHEMA}” não está disponível neste sistema. "
                    "Verifique se o GNOME está instalado e se você está em uma "
                    "sessão GNOME."
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
