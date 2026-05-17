# Mouse Fine-Tuning

Aplicativo GTK4 + libadwaita para Manjaro/GNOME que controla, no estilo das
configurações do próprio GNOME:

- **Velocidade** do ponteiro do mouse (slider grosso + ajuste fino com precisão
  de 0.01).
- **Perfil de aceleração** (padrão do sistema, desativada, ou adaptativa).
- **Limiar de arrasto** em pixels — quanto o cursor precisa se mover antes do
  sistema iniciar um arrasto.

```
┌──────────────────────────────────────────────────────────┐
│  Mouse Fine-Tuning                                  ☰    │
├──────────────────────────────────────────────────────────┤
│  Velocidade                                              │
│  Ajuste a velocidade do ponteiro do mouse.               │
│  ┌────────────────────────────────────────────────────┐  │
│  │ Velocidade do ponteiro     ──────●──────           │  │
│  │ Ajuste fino                          [-0.05  ⏶⏷]  │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  Aceleração                                              │
│  ┌────────────────────────────────────────────────────┐  │
│  │ Perfil de aceleração        [ Adaptativa     v ]   │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  Fine-tuning                                             │
│  ┌────────────────────────────────────────────────────┐  │
│  │ Limiar de arrasto                       [ 8  ⏶⏷]  │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

## Requisitos

Em Manjaro/Arch (já instalados em sistemas com GNOME):

```bash
sudo pacman -S python python-gobject gtk4 libadwaita
```

Versões testadas: Python 3.14, PyGObject 3.56, GTK 4.22, libadwaita 1.9.

## Rodar direto (sem instalar)

```bash
cd /home/workstation/PersonalDevelopment/gnome-apps/mouse-fine-tinning
chmod +x mouse-fine-tuning.py
./mouse-fine-tuning.py
```

Os valores que você muda na janela ficam salvos imediatamente (via dconf) —
não é preciso fazer logout nem clicar em "Aplicar".

## Instalar no menu de aplicativos do GNOME

```bash
cp br.dev.lcf2212.MouseFineTuning.desktop ~/.local/share/applications/
update-desktop-database ~/.local/share/applications/ 2>/dev/null || true
```

Depois disso, pressione `Super`, digite "Mouse Fine-Tuning" (ou "Ajuste Fino")
e o app aparece. O GNOME varre essa pasta periodicamente — se não aparecer
imediatamente, faça logout/login.

## Como persistir e onde

Tudo é gravado via `Gio.Settings` no schema do GNOME
`org.gnome.desktop.peripherals.mouse`. As três chaves utilizadas são:

| Chave | Tipo | Range / valores |
|---|---|---|
| `speed` | double | -1.0 (mais lento) a 1.0 (mais rápido); 0 = padrão |
| `accel-profile` | enum | `default`, `flat`, `adaptive` |
| `drag-threshold` | int | pixels (default 8) |

Os valores ficam no banco do dconf (`~/.config/dconf/user`) e sobrevivem a
logout e reboot. Mexer no app é equivalente a editar o painel "Mouse e
Touchpad" do `gnome-control-center` — os dois mostram o mesmo estado.

## Limitações conhecidas

- **Só GNOME.** O schema usado é do GNOME. KDE/Cinnamon/XFCE têm schemas
  próprios e não vão reagir a esse app.
- **Todos os mouses ao mesmo tempo.** O GNOME não tem configuração
  por-dispositivo via gsettings — qualquer ajuste vale globalmente.
- **Touchpad é separado.** Não afeta `org.gnome.desktop.peripherals.touchpad`.
  Se você está testando num laptop, certifique-se de mover um mouse externo.
- **Sem curva customizada.** O GNOME/Wayland não expõe os parâmetros internos
  da curva de aceleração do libinput. Os três valores que este app oferece são
  o conjunto completo disponível pelo schema.

## Desinstalar

```bash
rm -f ~/.local/share/applications/br.dev.lcf2212.MouseFineTuning.desktop
rm -rf /home/workstation/PersonalDevelopment/gnome-apps/mouse-fine-tinning
```

Para também reverter as configurações de mouse ao padrão do sistema:

```bash
gsettings reset org.gnome.desktop.peripherals.mouse speed
gsettings reset org.gnome.desktop.peripherals.mouse accel-profile
gsettings reset org.gnome.desktop.peripherals.mouse drag-threshold
```

(Ou simplesmente use o menu **Restaurar padrões** dentro do próprio app antes
de remover.)
