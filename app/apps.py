from pathlib import Path
import configparser
import json
import mimetypes
import re
import subprocess
import shutil
import os
import tempfile
import time
import uuid
from .config import load_toml

APP_DIRS = [
    Path.home() / ".local/share/applications",
    Path("/usr/local/share/applications"),
    Path("/usr/share/applications"),
]

# KDE/Arch icon locations + user icons.
ICON_DIRS = [
    Path.home() / ".local/share/icons",
    Path.home() / ".icons",
    Path("/usr/share/icons"),
    Path("/usr/local/share/icons"),
    Path("/usr/share/pixmaps"),
]

ICON_EXTENSIONS = (".svg", ".png", ".xpm", ".webp")


def bindings():
    doc = load_toml("bindings.toml")
    return [dict(x) for x in doc.get("bindings", [])]


def find_desktop_file(name: str):
    for folder in APP_DIRS:
        candidate = folder / name
        if candidate.exists():
            return candidate
    return None


def read_desktop(name: str):
    path = find_desktop_file(name)
    if not path:
        return None

    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    sec = parser["Desktop Entry"]

    return {
        "name": sec.get("Name"),
        "icon": sec.get("Icon"),
        "exec": sec.get("Exec"),
        "path": str(path),
    }


def _spawn_desktop(path: Path, app_id: str) -> bool:
    """Avvia una .desktop entry nel session bus grafico dell'utente.

    kioclient/kde-open su Wayland di Plasma escono con core dump e
    gtk-launch da solo non vede DISPLAY: il metodo affidabile e'
    'gio launch'. Gli altri restano come fallback per ambienti diversi.
    """
    if shutil.which("gio"):
        try:
            proc = subprocess.Popen(
                ["gio", "launch", str(path)],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.4)
            if proc.poll() is None or proc.returncode == 0:
                return True
        except Exception:
            pass

    for cmd in (
        ["kioclient", "exec", str(path)],
        ["gtk-launch", app_id],
        ["systemd-run", "--user", "--collect", "gtk-launch", app_id],
    ):
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.Popen(
                cmd,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            continue

    return False


def _candidate_score(path: Path) -> int:
    """Prefer scalable / large app icons over tiny status icons."""
    s = str(path).lower()
    score = 0
    if "/scalable/" in s:
        score += 100
    if "/apps/" in s:
        score += 70
    if "/128x128/" in s:
        score += 50
    elif "/64x64/" in s:
        score += 45
    elif "/48x48/" in s:
        score += 40
    elif "/32x32/" in s:
        score += 20
    if path.suffix.lower() == ".svg":
        score += 30
    return score


def resolve_icon(icon_value: str | None):
    """
    Resolve the Icon= value from a .desktop file to a real local icon file.

    Supports:
    - absolute paths
    - themed icon names like "firefox"
    - names that already include .svg/.png
    """
    if not icon_value:
        return None

    raw = Path(icon_value).expanduser()

    if raw.is_absolute() and raw.exists() and raw.is_file():
        return raw

    names = [icon_value]
    if not raw.suffix:
        names += [icon_value + ext for ext in ICON_EXTENSIONS]

    candidates: list[Path] = []

    # Fast paths.
    for base in ICON_DIRS:
        for name in names:
            direct = base / name
            if direct.exists() and direct.is_file():
                candidates.append(direct)

    # Search recursively in icon themes.
    # This happens only while building the 4 app binding entries, so it is fine
    # for V0 and avoids making the frontend KDE-theme-specific.
    target_stems = {Path(n).stem for n in names}
    for base in ICON_DIRS:
        if not base.exists():
            continue
        try:
            for p in base.rglob("*"):
                if (
                    p.is_file()
                    and p.suffix.lower() in ICON_EXTENSIONS
                    and p.stem in target_stems
                ):
                    candidates.append(p)
        except PermissionError:
            pass

    if not candidates:
        return None

    candidates = list(dict.fromkeys(candidates))
    candidates.sort(key=_candidate_score, reverse=True)
    return candidates[0]


def icon_content_type(path: Path):
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def binding_with_metadata(item: dict):
    item = dict(item)
    item["has_icon"] = False

    info = None
    if item.get("type") == "app" and item.get("desktop"):
        info = read_desktop(item["desktop"])
        item["desktop_info"] = info

    # Priority:
    # 1) icon="" explicitly configured in bindings.toml
    # 2) Icon= from the .desktop file
    # 3) frontend fallback: name + color
    explicit_icon = str(item.get("icon", "")).strip() or None

    icon_path = None
    if explicit_icon:
        icon_path = resolve_icon(explicit_icon)

    if icon_path is None and info:
        icon_path = resolve_icon(info.get("icon"))

    if icon_path:
        item["has_icon"] = True
        item["icon_url"] = f"/api/binding/{int(item['slot'])}/icon"

    return item


def _extract_wmclass_from_desktop(desktop_name: str) -> str | None:
    path = find_desktop_file(desktop_name)
    if not path:
        return None

    try:
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        parser.read(path, encoding="utf-8")
        if "Desktop Entry" in parser:
            sec = parser["Desktop Entry"]
            wmclass = sec.get("StartupWMClass") or sec.get("WMClass")
            if wmclass and wmclass.strip():
                return wmclass.strip()
    except Exception:
        pass
    return None


def _runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base)


# Script KWin caricato via D-Bus (qdbus6): su Wayland non esistono
# xdotool/wmctrl e kdotool non e' sempre installato, mentre l'API di
# scripting di KWin e' sempre disponibile su Plasma.
# writeFile() non esiste piu' in KWin 6, quindi l'esito torna indietro
# con print(): kwin_wayland lo manda al journal, da cui lo rileggiamo.
# __PATTERNS__ e __TAG__ vengono sostituiti con JSON valido.
_KWIN_SCRIPT_TEMPLATE = """\
var patterns = __PATTERNS__;
var tag = __TAG__;
var ws = workspace;
var windows = ws.windowList();
var active = ws.activeWindow;
var classMatch = null;
var classMatchActive = null;
var captionMatch = null;
for (var i = 0; i < windows.length; i++) {
    var w = windows[i];
    if (!w || w.normalWindow !== true) {
        continue;
    }
    var cls = [
        String(w.resourceClass || ""),
        String(w.windowClass || ""),
        String(w.windowClassInstance || "")
    ].join(" ").toLowerCase();
    var cap = String(w.caption || "").toLowerCase();
    for (var j = 0; j < patterns.length; j++) {
        var p = String(patterns[j] || "").toLowerCase();
        if (!p) {
            continue;
        }
        if (cls.indexOf(p) !== -1) {
            if (classMatch === null) {
                classMatch = w;
            }
            if (active && w === active) {
                classMatchActive = w;
            }
            break;
        }
        if (captionMatch === null && cap.indexOf(p) !== -1) {
            captionMatch = w;
        }
    }
}
var match = classMatchActive || classMatch || captionMatch;
if (match) {
    if (match.minimized) {
        match.minimized = false;
    }
    // KWin 6 non ha piu' Window.activate e Window.desktop: il setter
    // supportato e' workspace.activeWindow (desktop e' una lista in
    // Window.desktops). Va impostato dopo l'unminimize, altrimenti una
    // finestra minimizzata resta dietro e KWin ignora la richiesta.
    if (
        match.desktops
        && match.desktops.length
        && !match.onAllDesktops
    ) {
        var onCurrent = false;
        for (var d = 0; d < match.desktops.length; d++) {
            if (match.desktops[d] === ws.currentDesktop) {
                onCurrent = true;
                break;
            }
        }
        if (!onCurrent) {
            ws.currentDesktop = match.desktops[0];
        }
    }
    ws.activeWindow = match;
    // OK solo se l'attivazione e' davvero avvenuta: senza questa verifica
    // il chiamante crede di aver portato la finestra davanti e non
    // riprova nemmeno quando KWin ha ignorato la richiesta.
    if (ws.activeWindow === match) {
        print("KMR-ACTIVATE " + tag + " OK");
    } else {
        print("KMR-ACTIVATE " + tag + " NONE");
    }
} else {
    print("KMR-ACTIVATE " + tag + " NONE");
}
"""


def _kwin_activate(patterns: list[str], wait: float = 1.2) -> bool:
    """Attiva la prima finestra che matcha i pattern, via scripting KWin.

    Lo script viene caricato, avviato e scaricato a ogni chiamata: gira
    una volta, stampa l'esito (riletto dal journal) e si chiude.
    Restituisce True solo se l'attivazione e' confermata.
    """
    if not patterns or not shutil.which("qdbus6"):
        return False

    token = uuid.uuid4().hex[:12]
    script_path = _runtime_dir() / f"kmr-activate-{token}.js"
    name = f"kmr-activate-{token}"

    script = (
        _KWIN_SCRIPT_TEMPLATE
        .replace("__PATTERNS__", json.dumps([str(p) for p in patterns]))
        .replace("__TAG__", json.dumps(token))
    )

    try:
        script_path.write_text(script, encoding="utf-8")
    except OSError:
        return False

    marker = f"KMR-ACTIVATE {token} "

    try:
        load = subprocess.run(
            [
                "qdbus6",
                "org.kde.KWin",
                "/Scripting",
                "org.kde.kwin.Scripting.loadScript",
                str(script_path),
                name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
            text=True,
        )
        if load.returncode != 0 or load.stdout.strip().startswith("-1"):
            return False

        subprocess.run(
            [
                "qdbus6",
                "org.kde.KWin",
                "/Scripting",
                "org.kde.kwin.Scripting.start",
            ],
            timeout=2,
        )

        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                log = subprocess.run(
                    [
                        "journalctl",
                        "--user",
                        "--since",
                        "3 seconds ago",
                        "--no-pager",
                        "-o",
                        "cat",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=2,
                    text=True,
                ).stdout
            except Exception:
                log = ""

            if marker + "OK" in log:
                return True
            if marker + "NONE" in log:
                return False

            time.sleep(0.1)

        return False

    except Exception:
        return False

    finally:
        try:
            subprocess.run(
                [
                    "qdbus6",
                    "org.kde.KWin",
                    "/Scripting",
                    "org.kde.kwin.Scripting.unloadScript",
                    name,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=2,
            )
        except Exception:
            pass
        try:
            script_path.unlink(missing_ok=True)
        except OSError:
            pass


def _try_activate_window(patterns: list[str]) -> bool:
    # Prima via KWin (funziona sia su X11 che su Wayland, senza tool
    # esterni); i tool classici restano come fallback.
    if _kwin_activate(patterns):
        return True

    for tool in ("kdotool", "wmctrl", "xdotool"):
        if not shutil.which(tool):
            continue
        try:
            if tool == "kdotool":
                try:
                    result = subprocess.run(
                        ["kdotool", "search", "--class", patterns[0] if patterns else ""],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=1,
                        text=True,
                    )
                    wids = [w.strip() for w in result.stdout.splitlines() if w.strip()]
                except Exception:
                    wids = []

                if not wids and len(patterns) > 1:
                    for p in patterns[1:]:
                        try:
                            result = subprocess.run(
                                ["kdotool", "search", "--classname", p],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                timeout=1,
                                text=True,
                            )
                            wids = [w.strip() for w in result.stdout.splitlines() if w.strip()]
                            if wids:
                                break
                        except Exception:
                            continue

                if not wids:
                    for p in patterns:
                        try:
                            result = subprocess.run(
                                ["kdotool", "search", p],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                timeout=1,
                                text=True,
                            )
                            wids = [w.strip() for w in result.stdout.splitlines() if w.strip()]
                            if wids:
                                break
                        except Exception:
                            continue

                for wid in wids:
                    try:
                        subprocess.run(["kdotool", "windowactivate", wid], timeout=1)
                        return True
                    except Exception:
                        continue

            elif tool == "wmctrl":
                try:
                    result = subprocess.run(
                        ["wmctrl", "-l", "-x"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=1,
                        text=True,
                    )
                except Exception:
                    continue

                for line in result.stdout.splitlines():
                    lower = line.lower()
                    for p in patterns:
                        if p and p.lower() in lower:
                            parts = line.split(None, 3)
                            if parts:
                                wid = parts[0]
                                try:
                                    subprocess.run(["wmctrl", "-ia", wid], timeout=1)
                                    return True
                                except Exception:
                                    pass

            elif tool == "xdotool":
                for p in patterns:
                    try:
                        result = subprocess.run(
                            ["xdotool", "search", "--class", p],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            timeout=1,
                            text=True,
                        )
                        wids = [w.strip() for w in result.stdout.splitlines() if w.strip()]
                    except Exception:
                        wids = []
                    for wid in wids:
                        try:
                            subprocess.run(["xdotool", "windowactivate", "--sync", wid], timeout=1)
                            return True
                        except Exception:
                            continue
        except Exception:
            continue
    return False


# App id flatpak in reverse-DNS (es. org.jellyfin.JellyfinDesktop):
# e' la classe reale della finestra, mentre i singoli pezzi del comando
# ("flatpak", "run", "/usr/bin/...") non matchano nulla.
_FLATPAK_RUN_RE = re.compile(r"\bflatpak\s+run\s+(\S+)")
_REVERSE_DNS_RE = re.compile(
    r"(?<![\w.-])([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+){2,})(?![\w.-])"
)


def _extra_match_patterns(item: dict) -> list[str]:
    cmd = str(item.get("command", ""))
    extra: list[str] = []
    m = _FLATPAK_RUN_RE.search(cmd)
    if m:
        extra.append(m.group(1).strip("\"'"))
    for tok in _REVERSE_DNS_RE.findall(cmd):
        if tok not in extra:
            extra.append(tok)
    return extra


def launch_binding(slot: int, activate_retries_s: float = 6.0):
    all_bindings = bindings()
    item = next((b for b in all_bindings if int(b["slot"]) == slot), None)
    if not item:
        raise KeyError("Binding non trovato")

    patterns: list[str] = []

    if item.get("type") == "app" and item.get("desktop"):
        desktop_name = item["desktop"]
        wmclass = _extract_wmclass_from_desktop(desktop_name)
        if wmclass:
            patterns.append(wmclass)
        name = Path(desktop_name).stem
        if name and name not in patterns:
            patterns.append(name)
        if desktop_name not in patterns:
            patterns.append(desktop_name)

    name_binding = item.get("name", "")
    if name_binding and name_binding not in patterns:
        patterns.append(name_binding)

    cmd = item.get("command", "")
    if cmd:
        try:
            # Try to get base command name
            parts = cmd.split()
            for p in parts:
                base = Path(p).name
                if base and base not in patterns:
                    patterns.append(base)
        except Exception:
            pass

    for p in _extra_match_patterns(item):
        if p not in patterns:
            patterns.append(p)

    patterns = [p for p in patterns if p]

    if patterns and _try_activate_window(patterns):
        return

    # L'app non era attiva (o non c'era): lancia e poi cerca la nuova
    # finestra per qualche secondo, cosi' arriva in primo piano anche su
    # Wayland dove puo' partire dietro.
    if item["type"] == "command":
        subprocess.Popen(item["command"], shell=True, start_new_session=True)
    else:
        desktop = item.get("desktop")
        if not desktop:
            raise KeyError("Desktop entry non configurata")

        path = find_desktop_file(desktop)
        app_id = desktop[:-8] if desktop.endswith(".desktop") else desktop
        if path:
            _spawn_desktop(path, app_id)
        else:
            subprocess.Popen(
                ["systemd-run", "--user", "--collect", "gtk-launch", app_id],
                start_new_session=True,
            )

    if not patterns:
        return

    deadline = time.monotonic() + activate_retries_s
    while time.monotonic() < deadline:
        time.sleep(0.3)
        if _try_activate_window(patterns):
            return
