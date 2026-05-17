# Mouse Fine-Tuning

Aplicativo GTK4 + libadwaita para Manjaro/GNOME que combina:

1. **Configurações nativas do mouse** via `gsettings` (velocidade, perfil de
   aceleração, limiar de arrasto) — estilo `gnome-control-center > Mouse`.
2. **Curva de aceleração customizada velocidade-dependente** via daemon
   `uinput` próprio, controlado pela GUI e iniciado pelo `systemd --user`.

```
┌──────────────────────────────────────────────────────────┐
│  Mouse Fine-Tuning   [Configurações | Curva]      ☰     │
├──────────────────────────────────────────────────────────┤
│  Velocidade ─────────────────────────────────────────────│
│  • Velocidade do ponteiro     [────●────] -1.0 ... 1.0   │
│  • Ajuste fino                          [-0.05  ⏶⏷]      │
│                                                          │
│  Aceleração nativa ──────────────────────────────────────│
│  • Perfil de aceleração        [ Adaptativa     v ]      │
│                                                          │
│  Fine-tuning ────────────────────────────────────────────│
│  • Limiar de arrasto                          [ 8  ⏶⏷]   │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  Mouse Fine-Tuning   [Configurações | Curva]      ☰     │
├──────────────────────────────────────────────────────────┤
│  Curva customizada ──────────────────────────────────────│
│  • Habilitar curva customizada              [ on/off ]   │
│  • Mouse                       [ Logitech G502  v ]      │
│  • Status do daemon: Ativo                       [↻]     │
│                                                          │
│  Parâmetros da curva ────────────────────────────────────│
│  • Sensibilidade base                    [ 1.00  ⏶⏷]    │
│  • Ganho de aceleração                   [ 0.10  ⏶⏷]    │
│  • Expoente                              [ 1.50  ⏶⏷]    │
│  • Dead-zone (px/s)                      [    0  ⏶⏷]    │
│  • Multiplicador máximo                  [  3.0  ⏶⏷]    │
│                                                          │
│  Visualização da curva ──────────────────────────────────│
│  ┌─────────────────────────────────────────────┐         │
│  │ ×3.0│                              _______   │         │
│  │     │                         ____/          │         │
│  │     │                  ______/               │         │
│  │ ×1.0│- - - - - - _____/ - - - - - - - - - - │         │
│  │     │__________/                             │         │
│  │     └──────────────────────────────────────►│         │
│  │       0 px/s                       3000 px/s│         │
│  └─────────────────────────────────────────────┘         │
└──────────────────────────────────────────────────────────┘
```

## Como funciona a curva customizada

A fórmula aplicada por sample do mouse (a cada `SYN_REPORT`):

```
speed_pps   = √(dx² + dy²) / dt            # pixels por segundo
effective   = max(0, speed_pps − dead_zone)
accel       = gain × (effective / 1000)^power
multiplier  = min(sensitivity × (1 + accel), max_multiplier)

out_dx = dx × multiplier
out_dy = dy × multiplier
```

Pontos-chave:

- **sensibilidade**: multiplicador "base", aplicado mesmo em velocidade zero.
- **ganho**: define o quão forte é o efeito de "mais velocidade → mais aceleração".
- **expoente**: curvatura. `1.0` = linear; `2.0` = quadrática (cresce rápido); `0.5` = raiz (cresce devagar).
- **dead-zone**: movimentos lentos abaixo deste limiar não recebem aceleração extra (bom para mira precisa).
- **multiplicador máximo**: teto absoluto — impede "voo" descontrolado.

A curva é aplicada por um daemon Python (`mouse-curve-daemon.py`) que:

1. Captura eventos do mouse físico via `evdev` (`/dev/input/event*`).
2. Faz `grab` exclusivo (eventos do mouse físico não chegam mais no compositor).
3. Aplica a curva em cada par `(dx, dy)`.
4. Re-emite via `uinput` em um mouse virtual visível ao Wayland/Mutter.
5. Recarrega config em vivo ao receber `SIGHUP` (a GUI faz isso ao salvar).

Quando o daemon está ativo, o GNOME aplica `accel-profile = flat` em cima — a
única curva em ação é a sua. A GUI seta isso automaticamente ao habilitar.

## Requisitos

Pacman (Manjaro/Arch):

```bash
sudo pacman -S python python-gobject gtk4 libadwaita python-evdev
```

Versões testadas: Python 3.14, PyGObject 3.56, GTK 4.22, libadwaita 1.9,
python-evdev 1.9, GNOME 50.

## Instalação completa (recomendado)

Use o `setup.sh`:

```bash
git clone git@github.com:lcf2212dev/gnome-mouse-fine-tunning.git
cd gnome-mouse-fine-tunning
./setup.sh install
```

O instalador:

1. Verifica/instala os pacotes pacman acima.
2. Cria `/etc/udev/rules.d/99-uinput.rules` (libera `/dev/uinput` ao grupo `input`)
   e recarrega o udev.
3. Verifica se você está no grupo `input` (avisa se faltar).
4. Copia `mouse-curve-daemon.service` para `~/.config/systemd/user/` e roda
   `daemon-reload`.
5. Copia `br.dev.lcf2212.MouseFineTuning.desktop` para `~/.local/share/applications/`.
6. Ajusta os caminhos absolutos para o diretório onde você clonou o repo.

Após isso, pressione `Super`, digite "Mouse" e o app aparece.

## Uso

### Aba "Configurações"

Mesma coisa que o `gnome-control-center` faz para mouse, em uma só tela:

| Controle | Chave de gsettings |
|---|---|
| Velocidade do ponteiro / Ajuste fino | `speed` (`double`, -1.0..1.0) |
| Perfil de aceleração | `accel-profile` (`default` / `flat` / `adaptive`) |
| Limiar de arrasto | `drag-threshold` (`int`, 1..30) |

Tudo persiste em `dconf` e tem efeito imediato.

### Aba "Curva"

1. **Selecione seu mouse** no combo "Mouse". Se houver mais de um, escolha o
   que você quer interceptar (o outro continua usando a aceleração do GNOME).
2. **Ajuste os 5 sliders** — o preview ao lado atualiza em tempo real.
3. **Habilite** com o switch "Habilitar curva customizada". O daemon é
   iniciado pelo systemd e o perfil de aceleração nativo é forçado para
   `flat` automaticamente.

Para reverter, basta desligar o switch (o daemon para, o perfil nativo
volta a ser o que você escolheu manualmente, e o mouse volta ao
comportamento normal do GNOME).

#### Sugestões de partida

| Perfil | sensibilidade | ganho | expoente | dead-zone | max |
|---|---|---|---|---|---|
| Linear (sem curva) | 1.0 | 0.0 | 1.0 | 0 | 1.0 |
| Suave (mira + produtividade) | 0.9 | 0.3 | 1.4 | 30 | 2.5 |
| Agressivo (FPS) | 0.8 | 1.0 | 1.8 | 50 | 4.0 |
| Quake-style | 1.0 | 0.5 | 2.0 | 0 | 6.0 |

## Verificar status / debug

```bash
# Status geral
./setup.sh status

# Logs do daemon
journalctl --user -u mouse-curve-daemon.service -f

# Reiniciar
systemctl --user restart mouse-curve-daemon.service

# Recarregar só config (sem reiniciar)
systemctl --user kill --signal=HUP mouse-curve-daemon.service
```

## Persistência

| Arquivo | O que guarda |
|---|---|
| `~/.config/dconf/user` (via gsettings) | velocidade, perfil de aceleração, limiar de arrasto |
| `~/.config/mouse-fine-tuning/curve.json` | parâmetros da curva e dispositivo selecionado |
| `~/.config/systemd/user/mouse-curve-daemon.service` | unit do daemon |
| `/etc/udev/rules.d/99-uinput.rules` | permissão de `/dev/uinput` |

## Limitações conhecidas

- **Só GNOME.** Schema usado é do GNOME. KDE/Cinnamon não vão reagir.
- **Curva é global por mouse selecionado.** Se você tem 2 mouses e quer curvas
  diferentes, hoje precisa alternar o "Mouse" selecionado e reiniciar o daemon.
- **Touchpad é fora do escopo.** O daemon ignora dispositivos com `EV_ABS`
  (touchpads, tablets) — só intercepta mouses com `EV_REL`.
- **Hot-plug.** Se você desconectar o mouse selecionado, o daemon termina
  com erro. Habilite-o de novo no app depois de reconectar.
- **Latência adicional.** O interceptor adiciona ~50–200 µs por sample. Imperceptível em uso normal; perceptível só em medições.

## Desinstalar

```bash
./setup.sh uninstall
```

Remove a unit do systemd, o `.desktop` e (opcionalmente) a udev rule. Os
arquivos de configuração em `~/.config/mouse-fine-tuning/` ficam, caso você
queira manter os parâmetros para uma reinstalação futura.

Para resetar também as configurações nativas do GNOME, use o menu
**"Restaurar padrões"** dentro do app antes de desinstalar.

## Licença

MIT — ver `LICENSE`.
