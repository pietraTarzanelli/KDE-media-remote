from pathlib import Path
import configparser
import mimetypes
import subprocess
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


def launch_binding(slot: int):
    all_bindings = bindings()
    item = next((b for b in all_bindings if int(b["slot"]) == slot), None)
    if not item:
        raise KeyError("Binding non trovato")

    if item["type"] == "command":
        subprocess.Popen(item["command"], shell=True, start_new_session=True)
        return

    desktop = item.get("desktop")
    app_id = desktop[:-8] if desktop and desktop.endswith(".desktop") else desktop

    if not app_id:
        raise KeyError("Desktop entry non configurata")

    # KDE understands desktop files too; gtk-launch is just a lightweight way
    # to launch an application by desktop ID.
    subprocess.Popen(["gtk-launch", app_id], start_new_session=True)
