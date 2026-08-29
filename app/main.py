from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import main_config
from .input_backend import (
    MouseRepeater, key_tap, key_down, key_up, type_text,
    left_click, right_click
)
from .commands import list_commands, run_command, add_command
from .apps import bindings, launch_binding, binding_with_metadata, read_desktop, resolve_icon, icon_content_type

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

cfg = main_config()
icfg = cfg["input"]

mouse = MouseRepeater(
    step=int(icfg["mouse_step"]),
    repeat_ms=int(icfg["mouse_repeat_ms"]),
    accel_after_ms=int(icfg["mouse_accel_after_ms"]),
    accel_multiplier=float(icfg["mouse_accel_multiplier"]),
)

app = FastAPI(title="KDE Media Remote")
app.mount("/static", StaticFiles(directory=WEB), name="static")

@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")

@app.get("/keyboard")
async def keyboard_page():
    return FileResponse(WEB / "keyboard.html")

@app.get("/api/commands")
def api_commands():
    return list_commands()

@app.get("/api/bindings")
def api_bindings():
    return [binding_with_metadata(item) for item in bindings()]


@app.get("/api/binding/{slot}/icon")
def api_binding_icon(slot: int):
    item = next((b for b in bindings() if int(b["slot"]) == slot), None)
    if not item:
        raise HTTPException(404, "Binding non trovato")

    explicit_icon = str(item.get("icon", "")).strip() or None
    icon_path = resolve_icon(explicit_icon) if explicit_icon else None

    if icon_path is None and item.get("type") == "app":
        info = read_desktop(item.get("desktop", ""))
        if info:
            icon_path = resolve_icon(info.get("icon"))

    if not icon_path:
        raise HTTPException(404, "Icona non trovata")

    return FileResponse(
        icon_path,
        media_type=icon_content_type(icon_path),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )

@app.post("/api/command/{index}")
def api_run_command(index: int):
    try:
        run_command(index)
        return {"ok": True}
    except (IndexError, KeyError) as e:
        raise HTTPException(404, str(e))

@app.post("/api/binding/{slot}")
def api_launch_binding(slot: int):
    try:
        launch_binding(slot)
        return {"ok": True}
    except KeyError as e:
        raise HTTPException(404, str(e))

class NewCommand(BaseModel):
    name: str
    command: str

@app.post("/api/commands")
def api_add_command(body: NewCommand):
    if not cfg["security"].get("allow_web_config_edit", False):
        raise HTTPException(403, "Editing web disabilitato")
    add_command(body.name.strip(), body.command.strip())
    return {"ok": True}

@app.websocket("/ws")
async def ws_remote(ws: WebSocket):
    await ws.accept()
    held_modifiers: set[str] = set()

    try:
        while True:
            msg = await ws.receive_json()
            action = msg.get("action")

            if action == "key":
                key_tap(msg["key"])

            elif action == "modifier_toggle":
                key = msg["key"]
                if key in held_modifiers:
                    key_up(key)
                    held_modifiers.remove(key)
                else:
                    key_down(key)
                    held_modifiers.add(key)
                await ws.send_json({
                    "type": "held_modifiers",
                    "keys": sorted(held_modifiers)
                })

            elif action == "mouse_click":
                left_click()

            elif action == "right_click":
                right_click()

            elif action == "mouse_start":
                await mouse.start(msg["direction"])

            elif action == "mouse_stop":
                await mouse.stop(msg.get("direction"))

            elif action == "text":
                type_text(msg.get("text", ""))

    except WebSocketDisconnect:
        pass
    finally:
        await mouse.stop()
        for key in list(held_modifiers):
            key_up(key)
