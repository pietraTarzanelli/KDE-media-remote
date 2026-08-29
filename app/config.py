from pathlib import Path
from tomlkit import parse, dumps

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

def load_toml(name: str):
    path = CONFIG_DIR / name
    return parse(path.read_text(encoding="utf-8"))

def save_toml(name: str, data):
    path = CONFIG_DIR / name
    path.write_text(dumps(data), encoding="utf-8")

def main_config():
    return load_toml("config.toml")
