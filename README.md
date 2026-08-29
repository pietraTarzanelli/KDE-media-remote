# KDE Media Remote — V0.5

Prima stesura focalizzata su CachyOS + KDE Plasma + Wayland.

## Architettura

Telefono/tablet -> WebSocket -> FastAPI -> ydotool -> uinput -> KDE/Wayland

La UI è servita dallo stesso processo FastAPI.

## 1. Dipendenze di sistema

Su CachyOS/Arch:

```bash
sudo pacman -S ydotool python python-pip
```

Il pacchetto Arch contiene `ydotool`, `ydotoold`, una unità systemd utente e una regola udev per uinput.

Avvia il daemon:

```bash
systemctl --user enable --now ydotool.service
```

Verifica:

```bash
systemctl --user status ydotool.service
ydotool mousemove -x 20 -y 0
```

Se il daemon non riesce ad accedere a `/dev/uinput`, controllare la regola udev e i permessi sul sistema prima di cambiare il codice.

## 2. Ambiente Python

```bash
cd kde-media-remote-v0
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Avvio

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8765
```

Oppure usa host/porta scelti in `config/config.toml` tramite il launcher:

```bash
python run.py
```

Poi dal telefono:

```text
http://IP-DEL-PC:8765
```

Su Tailscale puoi usare l'IP Tailscale o il nome MagicDNS del laptop.

## Mouse "telecomando TV"

Parametri in `config/config.toml`:

```toml
mouse_step = 22
mouse_repeat_ms = 70
mouse_accel_after_ms = 500
mouse_accel_multiplier = 2.2
```

Quando tocchi una direzione interna:

1. parte subito il movimento;
2. finché tieni premuto, il server invia uno step ogni `mouse_repeat_ms`;
3. dopo `mouse_accel_after_ms` ogni step viene moltiplicato per `mouse_accel_multiplier`;
4. al `pointerup` il movimento viene cancellato immediatamente.

Questo evita di aspettare 500 ms prima di capire se volevi un tap: il tap è reattivo subito, mentre la pressione lunga diventa progressivamente più veloce.

## Config

- `config/config.toml`: porta, host, velocità mouse, opzioni.
- `config/commands.toml`: comandi programmabili.
- `config/bindings.toml`: quattro app/command binding.

## Stato V0

Implementato:
- D-pad esterno -> frecce tastiera
- D-pad interno -> movimento mouse continuo
- accelerazione dopo pressione lunga
- click sinistro/destro
- media keys
- pagina keyboard
- Ctrl/Alt/Meta con modalità hold
- input tastiera telefono
- commands da TOML
- aggiunta commands dalla web UI
- 4 binding
- lettura base dei `.desktop`

Da fare:
- autenticazione/pairing
- icone reali dei `.desktop`
- editor completo bindings
- settings web
- service systemd del remote
- gestione migliore Unicode/clipboard
- feedback connessione/reconnect WebSocket
- PWA/installabile su home screen


## V0.1 UI / KDE launcher

### App Binding icons

For `type = "app"`, the server reads `Icon=` from the matching `.desktop` file and resolves the icon from common KDE/Linux icon theme directories.

Frontend behavior:
- icon found -> show the real application icon
- icon not found -> show `name` over the configured `color`

For `type = "command"`, there is no `.desktop` icon by definition, so the normal fallback is `name + color`.

### KDE launcher

After the virtual environment has been installed:

```bash
./install-desktop.sh
```

Then search for **KDE Media Remote** in Plasma's application menu and pin it to the taskbar.

The launcher runs:

```text
PROJECT/.venv/bin/python PROJECT/run.py
```

so it does not depend on Fish, Bash, or activating the venv.


## V0.2 - Phone keyboard fix

The phone keyboard now keeps a real textarea state instead of clearing the
field after each character.

It uses `beforeinput` to distinguish:
- inserted text
- Backspace
- Enter
- some iOS replacement/composition events

This fixes the iOS behavior where every letter was treated as the start of a
new sentence and therefore auto-capitalized.


## V0.3 - Icon override per App Bindings

Ogni binding può opzionalmente avere:

```toml
icon = ""
```

Priorità icona:
1. `icon="..."` nel binding
2. campo `Icon=` del file `.desktop`
3. fallback frontend `name + color`

`icon` può essere:
- un percorso assoluto, ad esempio `/home/user/Pictures/zen.svg`
- un nome icona del tema, ad esempio `zen-browser`
- un nome file come `zen-browser.svg`

Esempio:

```toml
[[bindings]]
slot = 1
type = "app"
name = "Zen Browser"
desktop = "zen.desktop"
icon = "/home/user/Pictures/zen.svg"
color = "#6b5cff"
```

Se `icon=""`, il server usa automaticamente l'icona dichiarata nel `.desktop`.


## V0.4 - iPhone keyboard value-diff sync

The phone textarea is now synchronized by comparing the complete previous and
current textarea values after each input event, instead of relying on iOS
`beforeinput` key semantics.

This supports normal continuous typing, Backspace, replacement/paste and Enter
more reliably on mobile Safari.

iOS autocapitalization, autocorrect and spellcheck are disabled for the remote
input field so the browser does not silently rewrite the remote stream.
