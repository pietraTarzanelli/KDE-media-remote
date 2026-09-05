# KDE Media Remote — V1

> **VERSION 1 — ALL CURRENTLY KNOWN BUGS AND MINOR ISSUES HAVE BEEN FIXED. KDE MEDIA REMOTE IS NOW PWA-COMPATIBLE AND CAN BE INSTALLED ON THE iOS/iPadOS HOME SCREEN.**

First stable release focused on CachyOS + KDE Plasma + Wayland.

## Architecture

Phone/tablet -> WebSocket -> FastAPI -> ydotool -> uinput -> KDE/Wayland

The UI is served by the same FastAPI process.

## 1. System dependencies

On CachyOS/Arch:

```bash
sudo pacman -S ydotool python python-pip
```

The Arch package includes `ydotool`, `ydotoold`, a user systemd unit, and a udev rule for uinput.

Start the daemon:

```bash
systemctl --user enable --now ydotool.service
```

Verify:

```bash
systemctl --user status ydotool.service
ydotool mousemove -x 20 -y 0
```

If the daemon cannot access `/dev/uinput`, check the udev rule and system permissions before changing the application code.

## 2. Python environment

```bash
cd kde-media-remote-v0
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Launch

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8765
```

Alternatively, use the host/port configured in `config/config.toml` through the launcher:

```bash
python run.py
```

Then open from your phone:

```text
http://PC-IP:8765
```

When using Tailscale, you can use either the laptop's Tailscale IP or its MagicDNS hostname.

## PWA / iOS

KDE Media Remote can now be installed as a **Progressive Web App** on iPhone and iPad.

On iOS/iPadOS:

1. open KDE Media Remote in Safari;
2. tap **Share**;
3. select **Add to Home Screen**;
4. launch KDE Media Remote from the new Home Screen icon.

Once installed, the application opens in standalone mode instead of behaving like a normal browser tab.

The project includes the PWA manifest and the metadata required for iOS/iPadOS Home Screen integration.

Android also supports Progressive Web Apps through compatible browsers such as Chrome. Installation behavior and exact requirements may vary depending on the browser and Android version.

## TV-style remote mouse

Parameters in `config/config.toml`:

```toml
mouse_step = 22
mouse_repeat_ms = 70
mouse_accel_after_ms = 500
mouse_accel_multiplier = 2.2
```

When you press an inner direction:

1. movement starts immediately;
2. while the button remains pressed, the server sends one step every `mouse_repeat_ms`;
3. after `mouse_accel_after_ms`, every step is multiplied by `mouse_accel_multiplier`;
4. on `pointerup`, movement is stopped immediately.

This avoids waiting 500 ms before determining whether the input was a tap: taps remain immediately responsive, while long presses progressively become faster.

## Config

* `config/config.toml`: port, host, mouse speed, and general options.
* `config/commands.toml`: programmable commands.
* `config/bindings.toml`: four app/command bindings.

## V1 Status

Implemented:

* external D-pad -> keyboard arrow keys
* internal D-pad -> continuous mouse movement
* long-press mouse acceleration
* left/right click
* media keys
* keyboard page
* Ctrl/Alt/Meta hold mode
* phone keyboard input
* TOML-based commands
* adding commands from the web UI
* 4 bindings
* basic `.desktop` file parsing
* real `.desktop` application icons
* manual icon override for App Bindings
* improved iPhone keyboard handling
* textarea synchronization through value-diff
* iOS autocapitalization/autocorrect fixes
* PWA support
* iOS/iPadOS Home Screen installation
* standalone mode on iOS/iPadOS
* all currently known bugs and minor issues fixed

Planned / possible future improvements:

* authentication/pairing
* complete bindings editor
* web settings
* systemd service for the remote
* improved Unicode/clipboard handling
* better WebSocket connection/reconnection feedback

## V0.1 UI / KDE launcher

### App Binding icons

For `type = "app"`, the server reads `Icon=` from the matching `.desktop` file and resolves the icon from common KDE/Linux icon theme directories.

Frontend behavior:

* icon found -> show the real application icon
* icon not found -> show `name` over the configured `color`

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

so it does not depend on Fish, Bash, or activating the virtual environment manually.

## V0.2 - Phone keyboard fix

The phone keyboard now keeps a real textarea state instead of clearing the field after each character.

It uses `beforeinput` to distinguish:

* inserted text
* Backspace
* Enter
* some iOS replacement/composition events

This fixes the iOS behavior where every letter was treated as the start of a new sentence and therefore auto-capitalized.

## V0.3 - Icon override per App Bindings

Each binding can optionally define:

```toml
icon = ""
```

Icon priority:

1. `icon="..."` in the binding
2. the `Icon=` field from the `.desktop` file
3. frontend fallback using `name + color`

`icon` can be:

* an absolute path, for example `/home/user/Pictures/zen.svg`
* an icon theme name, for example `zen-browser`
* a filename such as `zen-browser.svg`

Example:

```toml
[[bindings]]
slot = 1
type = "app"
name = "Zen Browser"
desktop = "zen.desktop"
icon = "/home/user/Pictures/zen.svg"
color = "#6b5cff"
```

If `icon=""`, the server automatically uses the icon declared in the `.desktop` file.

## V0.4 - iPhone keyboard value-diff sync

The phone textarea is now synchronized by comparing the complete previous and current textarea values after each input event, instead of relying on iOS `beforeinput` key semantics.

This supports normal continuous typing, Backspace, replacement/paste, and Enter more reliably on mobile Safari.

iOS autocapitalization, autocorrect, and spellcheck are disabled for the remote input field so the browser does not silently rewrite the remote stream.

## V1 - Stable release / PWA support

Version 1 marks the first stable release of KDE Media Remote.

All currently known bugs and minor issues have been fixed.

Main additions and changes:

* PWA support
* iOS/iPadOS Home Screen installation
* standalone application mode
* mobile layout improvements
* improved iPhone keyboard handling
* application icon support
* general bug fixes
* minor UI/UX fixes
