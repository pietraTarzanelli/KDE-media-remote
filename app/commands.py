import shlex
import subprocess
from .config import load_toml, save_toml

def list_commands():
    doc = load_toml("commands.toml")
    return [dict(x) for x in doc.get("commands", [])]

def run_command(index: int):
    commands = list_commands()
    if index < 0 or index >= len(commands):
        raise IndexError("Comando inesistente")

    # V0: il comando può contenere pipe/redirection solo se l'utente lo ha
    # inserito deliberatamente nel file/interfaccia.
    # Non accettiamo mai una stringa comando arbitraria dall'endpoint di esecuzione.
    return subprocess.Popen(
        commands[index]["command"],
        shell=True,
        start_new_session=True,
    )

def add_command(name: str, command: str):
    doc = load_toml("commands.toml")
    if "commands" not in doc:
        doc["commands"] = []
    doc["commands"].append({"name": name, "command": command})
    save_toml("commands.toml", doc)
