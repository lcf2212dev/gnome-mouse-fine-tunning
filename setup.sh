#!/usr/bin/env bash
# Mouse Fine-Tuning — installer / uninstaller
# Usage:
#   ./setup.sh install      # full install (udev rule, systemd unit, .desktop)
#   ./setup.sh uninstall    # remove everything installed
#   ./setup.sh status       # report current state

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_APP_DIR="${HOME}/.local/share/applications"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"
DESKTOP_FILE="br.dev.lcf2212.MouseFineTuning.desktop"
SERVICE_FILE="mouse-curve-daemon.service"
UDEV_RULE_SRC="${REPO_DIR}/data/99-uinput.rules"
UDEV_RULE_DST="/etc/udev/rules.d/99-uinput.rules"

step() { printf "\n\033[1;36m==> %s\033[0m\n" "$1"; }
ok()   { printf "    \033[1;32m✓\033[0m %s\n" "$1"; }
warn() { printf "    \033[1;33m!\033[0m %s\n" "$1"; }
fail() { printf "    \033[1;31m✗\033[0m %s\n" "$1"; }

need_pkg() {
    if ! pacman -Q "$1" >/dev/null 2>&1; then
        warn "Pacote '$1' não está instalado."
        return 1
    fi
    return 0
}

test_uinput_works() {
    # Retorna 0 se o usuário atual consegue abrir /dev/uinput, 1 caso contrário.
    python3 - <<'PYEOF' 2>/dev/null
import evdev
u = evdev.UInput()
u.close()
PYEOF
}

ensure_module_autoload() {
    local modules_conf="/etc/modules-load.d/uinput.conf"
    if [[ -f "${modules_conf}" ]] && grep -qx 'uinput' "${modules_conf}" 2>/dev/null; then
        ok "uinput já configurado para carregar no boot."
        return 0
    fi
    if echo "uinput" | sudo tee "${modules_conf}" >/dev/null 2>&1; then
        ok "Configurado para carregar uinput no boot (${modules_conf})."
    else
        warn "Não consegui escrever ${modules_conf} (sem sudo?). Rode manualmente:"
        printf "        echo 'uinput' | sudo tee %s\n" "${modules_conf}"
        warn "Sem esse arquivo, você precisará carregar uinput após cada reboot:"
        printf "        sudo modprobe uinput\n"
    fi
}

ensure_uinput_works() {
    step "Verificando acesso a /dev/uinput"

    # Caminho feliz: já funciona.
    if test_uinput_works; then
        ok "/dev/uinput acessível pelo seu usuário."
        ensure_module_autoload
        return 0
    fi

    warn "/dev/uinput não está utilizável. Configurando..."

    # 1) Garantir que o módulo está carregado
    sudo modprobe uinput 2>&1 || true
    ensure_module_autoload

    # 2) Garantir que o usuário está no grupo 'input'
    if ! id -nG "$USER" | tr ' ' '\n' | grep -qx input; then
        warn "Usuário '$USER' NÃO está no grupo 'input'. Adicionando..."
        sudo usermod -aG input "$USER"
        warn "Você PRECISA fazer logout/login para o grupo entrar em vigor."
    fi

    # 3) Instalar udev rule
    if [[ ! -r "${UDEV_RULE_DST}" ]] || ! cmp -s "${UDEV_RULE_SRC}" "${UDEV_RULE_DST}"; then
        sudo install -Dm644 "${UDEV_RULE_SRC}" "${UDEV_RULE_DST}"
        sudo udevadm control --reload-rules
        sudo udevadm trigger /dev/uinput 2>/dev/null || true
        ok "udev rule instalada."
    else
        ok "udev rule já presente."
    fi

    # 4) Se permissões ainda não bateram, reload do módulo força a rule a pegar
    sleep 0.3
    if ! test_uinput_works; then
        warn "Permissões não pegaram — recarregando módulo."
        sudo modprobe -r uinput 2>/dev/null || true
        sleep 0.2
        sudo modprobe uinput
        sudo udevadm trigger /dev/uinput 2>/dev/null || true
        sleep 0.3
    fi

    # 5) Validação final
    if test_uinput_works; then
        ok "/dev/uinput agora acessível."
    else
        local perms
        perms=$(stat -c '%a %G' /dev/uinput 2>/dev/null || echo "ausente")
        fail "/dev/uinput continua inacessível. Estado: ${perms}"
        fail "Você pode precisar fazer logout/login (se foi adicionado ao grupo input agora)"
        fail "ou um reboot."
        return 1
    fi
}

install_systemd_unit() {
    step "Instalando systemd user unit"
    mkdir -p "${USER_SYSTEMD_DIR}"
    install -m644 "${REPO_DIR}/data/${SERVICE_FILE}" "${USER_SYSTEMD_DIR}/${SERVICE_FILE}"
    # ajustar ExecStart pra caminho absoluto real do repo
    sed -i "s|%h/PersonalDevelopment/gnome-apps/mouse-fine-tinning|${REPO_DIR}|" \
        "${USER_SYSTEMD_DIR}/${SERVICE_FILE}"
    systemctl --user daemon-reload
    ok "${USER_SYSTEMD_DIR}/${SERVICE_FILE} instalado."
}

install_desktop_entry() {
    step "Instalando entry de aplicativo no menu do GNOME"
    mkdir -p "${USER_APP_DIR}"
    install -m644 "${REPO_DIR}/${DESKTOP_FILE}" "${USER_APP_DIR}/${DESKTOP_FILE}"
    # ajustar Exec= pra caminho real
    sed -i "s|/home/workstation/PersonalDevelopment/gnome-apps/mouse-fine-tinning|${REPO_DIR}|g" \
        "${USER_APP_DIR}/${DESKTOP_FILE}"
    update-desktop-database "${USER_APP_DIR}" 2>/dev/null || true
    ok ".desktop instalado em ${USER_APP_DIR}/"
}

install_deps() {
    step "Verificando dependências"
    local missing=()
    for pkg in python python-gobject gtk4 libadwaita python-evdev python-pyudev; do
        if ! need_pkg "$pkg"; then
            missing+=("$pkg")
        else
            ok "$pkg presente."
        fi
    done
    if (( ${#missing[@]} > 0 )); then
        warn "Pacotes faltando: ${missing[*]}"
        echo "    Instalando agora com sudo..."
        sudo pacman -S --needed --noconfirm "${missing[@]}"
    fi
}

do_install() {
    install_deps
    ensure_uinput_works
    install_systemd_unit
    install_desktop_entry
    step "Tudo pronto"
    cat <<'EOF'

    Próximos passos:
      • Abra o app pelo menu (busque "Mouse Fine-Tuning") ou rode:
          python3 mouse-fine-tuning.py
      • Vá na aba "Curva", escolha seu mouse e habilite o switch.
      • Ajuste os sliders e veja a curva no preview.

    O daemon será iniciado automaticamente pelo systemd-user.
    Logs:   journalctl --user -u mouse-curve-daemon.service -f
EOF
}

do_uninstall() {
    step "Removendo systemd unit"
    systemctl --user disable --now mouse-curve-daemon.service 2>/dev/null || true
    rm -f "${USER_SYSTEMD_DIR}/${SERVICE_FILE}"
    systemctl --user daemon-reload 2>/dev/null || true
    ok "systemd unit removida."

    step "Removendo .desktop"
    rm -f "${USER_APP_DIR}/${DESKTOP_FILE}"
    update-desktop-database "${USER_APP_DIR}" 2>/dev/null || true
    ok ".desktop removido."

    step "udev rule"
    if [[ -e "${UDEV_RULE_DST}" ]]; then
        read -rp "    Remover ${UDEV_RULE_DST}? Outros apps podem usar. [y/N] " a
        if [[ "${a,,}" == "y" ]]; then
            sudo rm -f "${UDEV_RULE_DST}"
            sudo udevadm control --reload-rules
            ok "udev rule removida."
        else
            warn "udev rule mantida."
        fi
    else
        ok "udev rule não estava instalada."
    fi

    cat <<'EOF'

    Nota: configurações (~/.config/mouse-fine-tuning/) e gsettings não foram
    tocadas. Para resetar gsettings, use o menu "Restaurar padrões" no app
    antes de desinstalar, ou rode:
      gsettings reset org.gnome.desktop.peripherals.mouse speed
      gsettings reset org.gnome.desktop.peripherals.mouse accel-profile
      gsettings reset org.gnome.desktop.peripherals.mouse drag-threshold
EOF
}

do_status() {
    step "Status da instalação"
    [[ -e "${UDEV_RULE_DST}" ]] && ok "udev rule instalada" || warn "udev rule ausente"
    [[ -e "${USER_SYSTEMD_DIR}/${SERVICE_FILE}" ]] && \
        ok "systemd unit instalada" || warn "systemd unit ausente"
    [[ -e "${USER_APP_DIR}/${DESKTOP_FILE}" ]] && \
        ok ".desktop instalado" || warn ".desktop ausente"
    if systemctl --user is-active --quiet mouse-curve-daemon.service; then
        ok "Daemon: ATIVO"
    else
        warn "Daemon: parado"
    fi
    if systemctl --user is-enabled --quiet mouse-curve-daemon.service 2>/dev/null; then
        ok "Daemon: habilitado no login"
    else
        warn "Daemon: NÃO habilitado no login"
    fi
}

case "${1:-install}" in
    install)   do_install ;;
    uninstall) do_uninstall ;;
    status)    do_status ;;
    *)
        echo "Uso: $0 {install|uninstall|status}"
        exit 1
        ;;
esac
