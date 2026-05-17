#!/usr/bin/env python3
"""Mouse Fine-Tuning — controle visual de velocidade, perfil de aceleração e
limiar de arrasto do mouse para GNOME (Wayland/X11), usando gsettings."""

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, Gtk  # noqa: E402

APP_ID = "br.dev.lcf2212.MouseFineTuning"
APP_VERSION = "0.1.0"
SCHEMA = "org.gnome.desktop.peripherals.mouse"

PROFILES = ["default", "flat", "adaptive"]
PROFILE_LABELS = [
    "Padrão do sistema",
    "Desativada (flat)",
    "Adaptativa",
]


def schema_available() -> bool:
    source = Gio.SettingsSchemaSource.get_default()
    return source is not None and source.lookup(SCHEMA, True) is not None


class MouseFineTuningWindow(Adw.ApplicationWindow):
    __gtype_name__ = "MouseFineTuningWindow"

    def __init__(self, app: Adw.Application):
        super().__init__(
            application=app,
            title="Mouse Fine-Tuning",
            default_width=540,
            default_height=620,
        )
        self.set_size_request(420, 540)
        self.settings = Gio.Settings.new(SCHEMA)

        self._build_ui()
        self._install_actions()

    # ----- construção da UI -----

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle.new("Mouse Fine-Tuning", ""))

        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu_button.set_tooltip_text("Menu principal")
        menu = Gio.Menu()
        menu.append("Restaurar padrões", "win.reset")
        menu.append("Sobre o Mouse Fine-Tuning", "win.about")
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)

        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        page.set_icon_name("input-mouse-symbolic")
        page.add(self._build_speed_group())
        page.add(self._build_accel_group())
        page.add(self._build_fine_group())

        toolbar.set_content(page)
        self.set_content(toolbar)

    def _build_speed_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Velocidade",
            description=(
                "Ajuste a velocidade do ponteiro do mouse. "
                "Aplica-se a todos os mouses conectados."
            ),
        )

        self.speed_adj = Gtk.Adjustment(
            lower=-1.0,
            upper=1.0,
            step_increment=0.01,
            page_increment=0.1,
        )
        self.settings.bind(
            "speed",
            self.speed_adj,
            "value",
            Gio.SettingsBindFlags.DEFAULT,
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
            title="Aceleração",
            description=(
                "O perfil controla a curva de aceleração aplicada pelo libinput. "
                "“Padrão do sistema” deixa o sistema escolher por dispositivo."
            ),
        )

        self.combo = Adw.ComboRow(title="Perfil de aceleração")
        self.combo.set_model(Gtk.StringList.new(PROFILE_LABELS))
        group.add(self.combo)

        # bind manual com guard contra loop
        self.settings.connect("changed::accel-profile", self._on_profile_setting_changed)
        self.combo.connect("notify::selected", self._on_profile_combo_changed)
        self._on_profile_setting_changed(self.settings, "accel-profile")

        return group

    def _build_fine_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Fine-tuning",
            description=(
                "Ajustes auxiliares que afetam a percepção de precisão "
                "em movimentos curtos."
            ),
        )

        self.drag_adj = Gtk.Adjustment(
            lower=1,
            upper=30,
            step_increment=1,
            page_increment=5,
        )
        self.settings.bind(
            "drag-threshold",
            self.drag_adj,
            "value",
            Gio.SettingsBindFlags.DEFAULT,
        )

        drag_row = Adw.SpinRow(
            title="Limiar de arrasto",
            subtitle="Pixels antes do sistema reconhecer um arrasto",
            digits=0,
        )
        drag_row.set_adjustment(self.drag_adj)
        group.add(drag_row)

        return group

    # ----- handlers do enum accel-profile -----

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

    # ----- actions -----

    def _install_actions(self) -> None:
        reset = Gio.SimpleAction.new("reset", None)
        reset.connect("activate", self._on_reset_activated)
        self.add_action(reset)

        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._on_about_activated)
        self.add_action(about)

    def _on_reset_activated(self, _action, _param) -> None:
        for key in ("speed", "accel-profile", "drag-threshold"):
            self.settings.reset(key)

    def _on_about_activated(self, _action, _param) -> None:
        about = Adw.AboutDialog(
            application_name="Mouse Fine-Tuning",
            application_icon="input-mouse-symbolic",
            developer_name="lcf2212",
            version=APP_VERSION,
            comments=(
                "Ajuste a velocidade, o perfil de aceleração e a sensibilidade "
                "do mouse no estilo das configurações do GNOME."
            ),
            copyright="© 2026 lcf2212",
            license_type=Gtk.License.MIT_X11,
        )
        about.present(self)


class MissingSchemaWindow(Adw.ApplicationWindow):
    """Mostrada quando o schema do GNOME não está instalado."""

    def __init__(self, app: Adw.Application):
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
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )

    def do_activate(self):
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
