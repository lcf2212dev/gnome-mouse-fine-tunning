# Mouse Fine-Tuning

Aplicativo GTK4 + libadwaita para Manjaro/GNOME que combina:

1. **Configurações nativas do mouse** via `gsettings` (velocidade, perfil de
   aceleração, limiar de arrasto) — estilo `gnome-control-center > Mouse`.
2. **Curva de aceleração customizada velocidade-dependente** via daemon
   `uinput` próprio, com **biblioteca de presets**, **hot-plug**, **live
   monitor** e **detecção automática do mouse em uso** — você não precisa
   escolher qual mouse configurar, o app detecta sozinho mexendo o mouse.

```
┌──────────────────────────────────────────────────────────┐
│  Mouse Fine-Tuning   [Configurações|Dispositivos|Presets]│
└──────────────────────────────────────────────────────────┘

╭─ Aba Dispositivos ───────────────────────────────────────╮
│ Curva customizada                                        │
│ • Daemon ativo                          [ on ]           │
│ • Status: Ativo · 1 mouse(s) habilitado(s)        [↻]    │
│                                                          │
│ Logitech USB Receiver Mouse                              │
│ ID 046d:c548 · ativo (curva sendo aplicada)              │
│ • Aceleração customizada                [ on ]           │
│ • Preset                       [ FPS           v ]       │
│                                                          │
│ Logitech Wireless Receiver Mouse                         │
│ ID 046d:c542 · presente, parado                          │
│ • Aceleração customizada                [ off ]          │
│ • Preset                       [ Desenho       v ]       │
╰──────────────────────────────────────────────────────────╯

╭─ Aba Presets ────────────────────────────────────────────╮
│ Biblioteca de presets                                    │
│ • Preset                       [ FPS           v ]       │
│ • Descrição (Built-in): Para jogos de tiro: ...          │
│ • Ações       [Duplicar] [Renomear] [Deletar]            │
│                                                          │
│ Parâmetros (read-only se built-in)                       │
│ • Sensibilidade base                       [ 0.80 ⏶⏷]    │
│ • Ganho de aceleração                      [ 1.00 ⏶⏷]    │
│ • Expoente                                 [ 1.80 ⏶⏷]    │
│ • Dead-zone (px/s)                         [    50 ⏶⏷]   │
│ • Multiplicador máximo                     [ 4.0  ⏶⏷]    │
│                                                          │
│ Preview da curva                                         │
│ ┌──────────────────────────────────────────────────┐     │
│ │ ×4.0|                            _____           │     │
│ │     |                         __/                │     │
│ │     |                    ____/                   │     │
│ │ ×1.0|- - - - - -_______/                         │     │
│ │     |__________/                                 │     │
│ │     +─────────────────────────────────────────►  │     │
│ │     0 px/s                            3000 px/s  │     │
│ └──────────────────────────────────────────────────┘     │
│                                                          │
│ Live monitor (osciloscópio)                              │
│ • Mostrar movimento real             [ on ]              │
│ • Monitorar mouse              [ Logitech USB  v ]       │
│ ┌──────────────────────────────────────────────────┐     │
│ │ 2400 px/s|         _                             │     │
│ │          |      __/ \    ___    — Entrada        │     │
│ │          |    _/     \__/   \   — Saída          │     │
│ │          | __/                \_                 │     │
│ │          |/                                      │     │
│ │          +───────────────────────────────────►   │     │
│ │           -5s                              agora │     │
│ └──────────────────────────────────────────────────┘     │
╰──────────────────────────────────────────────────────────╯
```

## Como funciona

### A fórmula da curva

Para cada par `(dx, dy)` que sai do mouse, em cada `SYN_REPORT`:

```
speed_pps  = √(dx² + dy²) / dt
effective  = max(0, speed_pps − dead_zone)
accel      = gain × (effective / 1000)^power
multiplier = min(sensitivity × (1 + accel), max_multiplier)

out_dx = dx × multiplier
out_dy = dy × multiplier
```

| Parâmetro | O que faz |
|---|---|
| `sensitivity` | Multiplicador base — afeta tudo, mesmo em velocidade 0. |
| `gain` | Quão forte a aceleração cresce com a velocidade. |
| `power` | Curvatura. 1.0 = linear; >1 cresce mais rápido em alta speed. |
| `deadzone` | Movimentos abaixo desse limiar (px/s) não recebem aceleração. |
| `max_multiplier` | Teto absoluto do multiplicador. |

### Os 5 presets built-in

| Nome | Sensibilidade | Ganho | Expoente | Dead-zone | Max | Uso |
|---|---|---|---|---|---|---|
| **Linear** | 1.0 | 0.0 | 1.0 | 0 | 1.0 | Sem aceleração, movimento 1:1 |
| **Suave** | 0.9 | 0.3 | 1.3 | 20 | 2.0 | Produtividade — leve, intuitiva |
| **FPS** | 0.8 | 1.0 | 1.8 | 50 | 4.0 | Jogos de tiro |
| **Quake** | 1.0 | 0.5 | 2.0 | 0 | 6.0 | Quake/CS 1.6 clássica |
| **Desenho** | 0.7 | 0.5 | 2.5 | 100 | 3.0 | Edição/desenho — alta precisão |

Built-ins são **read-only**. Pra ajustar, **Duplique** e edite a cópia.

### Multi-device + hot-plug

Cada mouse físico (identificado por `vendor:product`) tem o seu próprio preset
assinado em `~/.config/mouse-fine-tuning/devices.json`. O daemon roda **todos
os mouses habilitados simultaneamente** — você pode ter um Logitech G502 no
preset FPS e um trackball no preset Suave ao mesmo tempo.

Hot-plug funciona via `python-pyudev`: quando você pluga um mouse novo, o
daemon detecta em segundos e a GUI lista ele. Quando você despluga, o daemon
fecha o handle limpo.

### Live monitor

A aba "Presets" tem um **osciloscópio**: ele mostra em tempo real a velocidade
do seu mouse passando pela curva. Linha **cinza** = velocidade de entrada (o
que sai do hardware); linha **azul** = velocidade de saída (depois da curva).

Implementado via socket Unix em `$XDG_RUNTIME_DIR/mouse-curve-daemon.sock` —
o daemon faz broadcast throttled a 30 Hz, a GUI conecta on-demand quando o
switch "Mostrar movimento real" é ligado.

## Requisitos

Pacotes pacman (Manjaro/Arch):

```bash
sudo pacman -S python python-gobject gtk4 libadwaita python-evdev python-pyudev
```

`python-pyudev` é opcional — sem ele, daemon roda mas sem hot-plug.

Versões testadas: Python 3.14, PyGObject 3.56, GTK 4.22, libadwaita 1.9,
python-evdev 1.9, GNOME 50.

## Instalação completa

```bash
git clone git@github.com:lcf2212dev/gnome-mouse-fine-tunning.git
cd gnome-mouse-fine-tunning
./setup.sh install
```

O instalador:

1. Verifica/instala pacotes pacman (incluindo `python-pyudev`).
2. Carrega o módulo `uinput` no kernel e configura autoload via
   `/etc/modules-load.d/uinput.conf`.
3. Cria `/etc/udev/rules.d/99-uinput.rules` (libera `/dev/uinput` ao
   grupo `input`) e recarrega o udev.
4. Verifica que você está no grupo `input` (adiciona se faltar).
5. Copia `mouse-curve-daemon.service` para `~/.config/systemd/user/`.
6. Instala o `.desktop` em `~/.local/share/applications/`.

Depois disso, `Super → "Mouse"` abre o app.

## Persistência

| Arquivo | Conteúdo |
|---|---|
| `~/.config/dconf/user` (via gsettings) | velocidade, perfil de aceleração, limiar de arrasto |
| `~/.config/mouse-fine-tuning/devices.json` | per-device: id, preset escolhido, enabled |
| `~/.config/mouse-fine-tuning/presets/_builtin/*.json` | cópia em runtime dos built-ins (sincronizada com o repo) |
| `~/.config/mouse-fine-tuning/presets/custom/*.json` | presets criados/editados por você |
| `~/.config/systemd/user/mouse-curve-daemon.service` | unit do daemon |
| `/etc/udev/rules.d/99-uinput.rules` | permissão de `/dev/uinput` |
| `/etc/modules-load.d/uinput.conf` | autoload do módulo no boot |

## Debug

```bash
# Status geral
./setup.sh status

# Logs do daemon
journalctl --user -u mouse-curve-daemon.service -f

# Reiniciar
systemctl --user restart mouse-curve-daemon.service

# Recarregar só config (sem reiniciar)
systemctl --user kill --signal=HUP mouse-curve-daemon.service

# Verificar socket IPC do live monitor
ls -la "${XDG_RUNTIME_DIR}/mouse-curve-daemon.sock"
```

## Migração da v0.2

Se você usou a v0.2 (curva única, sem presets), na primeira execução da v0.3
o app migra automaticamente:

- `~/.config/mouse-fine-tuning/curve.json` → renomeado para `curve.json.v0.2.bak`
- Os parâmetros viram um preset custom chamado **"Migrado da v0.2"**
- `devices.json` novo é criado, e seus mouses aparecem na aba "Dispositivos"
  com o preset migrado disponível pra escolher

## Limitações conhecidas

- **Só GNOME.** Schema usado é do GNOME. KDE/Cinnamon/XFCE têm schemas próprios.
- **Latência adicional.** O interceptor adiciona ~50–200 µs por sample. Imperceptível em uso normal.
- **Per-mouse via vendor:product.** Se você tem 2 mouses idênticos, eles compartilham o mesmo perfil. (Próxima versão: incluir `phys` no ID.)
- **Touchpad fora do escopo.** O daemon ignora dispositivos com `EV_ABS`.
- **Sem suporte a wheel/scroll customizado.** Pass-through direto.

## Desinstalar

```bash
./setup.sh uninstall
```

Remove a unit do systemd, o `.desktop` e (opcionalmente) a udev rule. Os
arquivos de configuração em `~/.config/mouse-fine-tuning/` ficam.

## Licença

MIT — ver `LICENSE`.
