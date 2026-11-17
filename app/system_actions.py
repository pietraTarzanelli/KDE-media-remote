"""Azioni di sistema per la modalità PC.

Sono cose che non passano da ydotool: cattura dello schermo e
luminosità. Vengono eseguite fuori dal loop asyncio (``to_thread``) e
con un ambiente grafico ricostruito, perché il server può essere
avviato come servizio utente prima che tutte le variabili Wayland
siano visibili nel processo.
"""

import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .config import main_config


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _desktop_config():
    return main_config().get("desktop", {})


def _graphical_env() -> dict[str, str]:
    """Copia l'ambiente aggiungendo ciò che serve a KDE/Wayland.

    In una sessione grafica normale le variabili ci sono già. Se il
    server parte da systemd prima che l'ambiente sia completo, almeno
    XDG_RUNTIME_DIR e WAYLAND_DISPLAY vengono ricostruiti.
    """
    env = os.environ.copy()

    runtime = env.get("XDG_RUNTIME_DIR")
    if not runtime:
        runtime = f"/run/user/{os.getuid()}"
        env["XDG_RUNTIME_DIR"] = runtime

    if not env.get("WAYLAND_DISPLAY"):
        try:
            for sock in Path(runtime).glob("wayland-*"):
                if sock.is_socket() and not sock.name.endswith(".lock"):
                    env["WAYLAND_DISPLAY"] = sock.name
                    break
        except OSError:
            pass

    if env.get("WAYLAND_DISPLAY"):
        env.setdefault("QT_QPA_PLATFORM", "wayland")

    return env


def screenshot_dir() -> Path:
    raw = str(
        _desktop_config().get("screenshot_dir", "") or ""
    ).strip()

    if raw:
        return Path(raw).expanduser()

    return Path.home() / "Pictures" / "Screenshots"


def take_screenshot():
    """Cattura lo schermo intero con Spectacle.

    Ritorna ``(ok, messaggio)``. Il messaggio contiene il percorso del
    file quando la cattura riesce, così la UI può mostrarlo.
    """
    spectacle = shutil.which("spectacle")
    if not spectacle:
        return False, "Spectacle non trovato"

    folder = screenshot_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"Cartella screenshot non creabile: {exc}"

    path = folder / (
        "Screenshot_"
        + datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")[:-3]
        + ".png"
    )

    try:
        proc = subprocess.run(
            [
                spectacle,
                "-b",   # in background, senza rubare il focus
                "-n",   # nessuna notifica
                "-f",   # schermo intero
                "-o",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=_graphical_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Screenshot fallito: {exc}"

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        message = f"Screenshot fallito ({proc.returncode})"
        if detail:
            message += f": {detail}"
        return False, message

    # Spectacle con -b puo' uscire prima che il PNG sia chiuso: aspetta
    # un attimo invece di dichiarare fallita una cattura riuscita.
    for _ in range(50):
        if path.exists():
            break
        time.sleep(0.1)

    if not path.exists():
        return False, "Spectacle non ha salvato il file"

    return True, f"Screenshot salvato in {path}"


def _current_brightness(text: str):
    """Estrae (output, percentuale) dal testo di ``kscreen-doctor -o``."""
    output = None

    for line in ANSI_RE.sub("", text).splitlines():
        match = re.match(
            r"\s*Output:\s+(?:\d+\s+)?(\S+)",
            line,
        )
        if match:
            output = match.group(1)

        match = re.search(
            r"Brightness control:\s+supported.*?"
            r"set to\s+(\d+)%",
            line,
        )
        if match:
            return output, int(match.group(1))

    return output, None


def brightness_delta(delta: int):
    """Aumenta o diminuisce la luminosità del primo output utile.

    Ritorna ``(ok, messaggio)``. La percentuale viene limitata a 0..100.
    """
    kscreen = shutil.which("kscreen-doctor")
    if not kscreen:
        return False, "kscreen-doctor non trovato"

    try:
        proc = subprocess.run(
            [kscreen, "-o"],
            capture_output=True,
            text=True,
            timeout=10,
            env=_graphical_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Luminosità non leggibile: {exc}"

    if proc.returncode != 0:
        return False, "kscreen-doctor non ha risposto"

    output, current = _current_brightness(proc.stdout)
    if not output or current is None:
        return False, "Luminosità non controllabile su questo schermo"

    value = max(0, min(100, current + int(delta)))

    try:
        proc = subprocess.run(
            [
                kscreen,
                f"output.{output}.brightness.{value}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=_graphical_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Luminosità non impostata: {exc}"

    if proc.returncode != 0:
        return False, "kscreen-doctor non ha applicato la luminosità"

    return True, f"Luminosità {value}%"
