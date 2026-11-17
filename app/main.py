import asyncio
import sys
from pathlib import Path
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from fastapi import (
    FastAPI,
    Request,
    WebSocket,
    WebSocketDisconnect,
    HTTPException,
)

from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import main_config

from . import auth
from .auth_routes import EXPIRED_MESSAGES
from .auth_routes import is_https
from .auth_routes import router as auth_router

from .input_backend import (
    KEYS,
    KeyRepeater,
    MouseRepeater,
    key_tap,
    key_down,
    key_up,
    type_text,
    left_click,
    right_click,
    scroll_up,
    scroll_down,
    task_switcher_open,
    task_switcher_close,
    queue_raw_key,
    queue_mouse_move,
    queue_mouse_button,
    queue_scroll,
)

from .system_actions import (
    brightness_delta,
    take_screenshot,
)

from .commands import (
    list_commands,
    run_command,
    add_command,
)

from .apps import (
    bindings,
    launch_binding,
    binding_with_metadata,
    read_desktop,
    resolve_icon,
    icon_content_type,
)


ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

cfg = main_config()
icfg = cfg["input"]
dcfg = cfg.get("desktop", {})

# Coppie raw (sinistra/destra) dei modificatori che possono essere
# tenuti premuti da modifier_toggle. Servono a non far spegnere il
# toggle dal keyup del tasto fisico corrispondente.
RAW_MODIFIER_CODES = {
    "ctrl": {KEYS["ctrl"], 97},
    "alt": {KEYS["alt"], 100},
    "meta": {KEYS["meta"], 126},
}

mouse = MouseRepeater(
    step=int(icfg["mouse_step"]),
    repeat_ms=int(icfg["mouse_repeat_ms"]),
    accel_after_ms=int(
        icfg["mouse_accel_after_ms"]
    ),
    accel_multiplier=float(
        icfg["mouse_accel_multiplier"]
    ),
)

key_repeater = KeyRepeater(
    hold_delay_ms=int(icfg["key_hold_delay_ms"]),
    repeat_ms=int(icfg["key_repeat_ms"]),
    min_repeat_ms=int(icfg["key_min_repeat_ms"]),
    accel_after_ms=int(
        icfg["key_accel_after_ms"]
    ),
    accel_factor=float(
        icfg["key_accel_factor"]
    ),
)

app = FastAPI(
    title="KDE Media Remote",
    # /docs, /redoc e /openapi.json pubblicavano lo schema completo
    # dell'API a chiunque raggiungesse la porta: con l'autenticazione in
    # mezzo sono solo rumore (e un INV di URL da sondare)
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.include_router(auth_router)

app.mount(
    "/static",
    StaticFiles(directory=WEB),
    name="static",
)


# La UI veniva servita con gli header di default di StaticFiles/FileResponse
# (solo etag + last-modified): il browser poteva quindi riusare un vecchio
# wheel.svg senza nemmeno chiedere conferma. "no-cache" NON disabilita la
# cache, obbliga solo a rivalidare: la risposta arriva come 304 veloce e il
# contenuto e' sempre quello su disco.
CACHE_REVALIDATE = "no-cache, must-revalidate"

# header da conservare in una risposta 304
NOT_MODIFIED_HEADERS = ("cache-control", "etag", "last-modified")

# Il client manda un ping ogni 10 s. Oltre due minuti di silenzio
# significano che la pagina e' stata sospesa o la rete e' caduta: il
# socket non e' piu' utilizzabile e va chiuso lato server.
WS_IDLE_TIMEOUT = 120.0

# Il menu Alt+Tab resta aperto finche' non arriva Invio/Esc. Se il
# telefono viene abbandonato col menu aperto (o si esce col mouse),
# Alt resterebbe premuto: dopo questo tempo di inattivita' lo si rilascia.
SWITCHER_IDLE_TIMEOUT = 30.0


def is_not_modified(request_headers, response_headers) -> bool:
    """True se il client ha gia' la versione che stiamo per inviare.

    StaticFiles lo fa da solo, ma le route che rispondono con FileResponse
    (le pagine HTML) in Starlette emettono etag/last-modified senza onorare
    If-None-Match / If-Modified-Since: senza questo controllo 'no-cache'
    prometterebbe una rivalidazione che in realta non avviene e la pagina
    verrebbe riscaricata per intero a ogni visita.
    """
    if_none_match = request_headers.get("if-none-match")
    if if_none_match:
        tags = {t.strip() for t in if_none_match.split(",")}
        etag = response_headers.get("etag")
        return bool(etag) and ("*" in tags or etag in tags)

    if_modified_since = request_headers.get("if-modified-since")
    if if_modified_since:
        last_modified = response_headers.get("last-modified")
        if not last_modified:
            return False
        try:
            since = parsedate_to_datetime(if_modified_since)
            modified = parsedate_to_datetime(last_modified)
        except (TypeError, ValueError):
            return False
        if since is None or modified is None:
            return False
        # le date HTTP hanno precisione al secondo
        return modified.replace(microsecond=0) <= since

    return False


@app.middleware("http")
async def cache_headers(request, call_next):
    response = await call_next(request)
    path = request.url.path

    if path == "/static" or path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", CACHE_REVALIDATE)
    elif path.startswith("/api/"):
        # binding e comandi possono cambiare in runtime
        response.headers.setdefault("Cache-Control", "no-store")
    elif path in ("/", "/pc", "/keyboard") or path.endswith(".html"):
        response.headers.setdefault("Cache-Control", CACHE_REVALIDATE)

    # le richieste condizionali sulle pagine HTML non le gestisce FileResponse
    if (
        request.method in ("GET", "HEAD")
        and response.status_code == 200
        and is_not_modified(request.headers, response.headers)
    ):
        keep = {
            k: v for k, v in response.headers.items()
            if k.lower() in NOT_MODIFIED_HEADERS
        }
        return Response(status_code=304, headers=keep)

    return response


# ── autenticazione ──

# unica parte raggiungibile senza sessione: il form di login, gli asset
# statici (servono a far funzionare il form stesso) e il logout.
# Il logout e' pubblico di proposito: se la sessione e' gia' scaduta il
# cookie stale va comunque potuto cancellare, altrimenti il pulsante
# "esci" sembra non fare niente. Revocare una sessione valida richiede
# pero' il token, quindi una richiesta esterna non puo' disconnettere
# nessuno.
PUBLIC_PATHS = frozenset({
    "/auth/login",
    "/auth/logout",
    "/favicon.ico",
    "/static",
    # Il browser scarica il service worker senza le credenziali della
    # pagina: se lo rimandassimo al login la PWA non si potrebbe
    # installare. E' un file statico, non contiene dati di sessione.
    "/sw.js",
})
PUBLIC_PREFIXES = ("/static/",)

# la UI esistente e' tutta self-contained: niente CDN, ma style e script
# inline dentro index.html. 'unsafe-inline' e' quindi obbligatorio qui,
# e resta comunque limitato a 'self' per tutto il resto.
CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self' data:",
    "connect-src 'self' ws: wss:",
    "manifest-src 'self'",
    "worker-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
))

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy":
        "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": CSP,
}


def is_public(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    return any(
        path.startswith(prefix)
        for prefix in PUBLIC_PREFIXES
    )


def harden(
    response,
    request: Request,
):
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(
            header,
            value,
        )

    # HSTS solo se il traffico e' davvero su HTTPS: inviarlo su http
    # bloccherebbe l'utente sul proprio dominio
    if is_https(request):
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000",
        )

    return response


def unauthorized(
    request: Request,
    reason: str | None,
):
    path = request.url.path
    upgrade = request.headers.get(
        "upgrade",
        "",
    ).lower()

    # su un upgrade non si puo' rispondere con un redirect HTML
    if "websocket" in upgrade:
        return JSONResponse(
            {"error": "non autorizzato"},
            status_code=401,
        )

    # le chiamate del telecomando sono tutte sotto /api/ e devono
    # ricevere un 401 pulito: un redirect su una fetch() restituisce
    # HTML dove il client si aspetta JSON. Il resto sono pagine da
    # aprire nel browser, quindi vanno mandate al form di login.
    if path.startswith("/api/"):
        return JSONResponse(
            {"error": "non autorizzato"},
            status_code=401,
        )

    target = "/auth/login"
    params = []

    if reason and reason in EXPIRED_MESSAGES:
        params.append(f"reason={reason}")

    # la pagina richiesta torna nel form di login come 'next', altrimenti
    # aprire /auth/revoke con la sessione scaduta finiva sul telecomando
    # invece che sulla pagina di revoca
    if path != "/auth/login":
        params.append(
            f"next={quote(path, safe='/')}"
        )

    if params:
        target += "?" + "&".join(params)

    return RedirectResponse(
        target,
        status_code=303,
    )


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """Ogni rotta tranne login e statici richiede una sessione valida.

    La sessione viene riscontrollata a ogni richiesta: una revoca fatta
    dalla pagina locale vale per la richiesta successiva, non per la
    successiva-orama.
    """
    guarded = auth.is_enabled() and not is_public(
        request.url.path
    )

    if guarded:
        check = auth.check_session(
            request.cookies.get(auth.COOKIE_NAME)
        )

        if not check.ok or check.session is None:
            return harden(
                unauthorized(request, check.reason),
                request,
            )

        request.state.session = check.session

        # il timer di inattivita' si ricarica al massimo una volta l'ora
        if (
            auth.now() - check.session.last_used_at
            > auth.TOUCH_INTERVAL
        ):
            auth.touch_session(check.session.id)

    return harden(
        await call_next(request),
        request,
    )


if auth.is_enabled() and not auth.list_accounts():
    print(
        "[auth] Nessun account configurato: il telecomando resta"
        " bloccato finche' non crei un PIN.\n"
        "[auth]   python tools/accounts.py add <nome>",
        file=sys.stderr,
    )


@app.get("/")
async def index():
    return FileResponse(
        WEB / "index.html"
    )


@app.get("/pc")
async def pc_page():
    return FileResponse(
        WEB / "pc.html"
    )


@app.get("/sw.js")
async def service_worker():
    # Deve stare alla radice del sito per controllare tutte le pagine.
    # no-cache: il browser deve poter vedere subito una nuova versione.
    return FileResponse(
        WEB / "sw.js",
        media_type="text/javascript",
        headers={
            "Cache-Control": "no-cache, must-revalidate",
            "Service-Worker-Allowed": "/",
        },
    )


@app.get("/keyboard")
async def keyboard_page():
    return FileResponse(
        WEB / "keyboard.html"
    )


@app.get("/api/pc/settings")
def api_pc_settings():
    return {
        "mouse_sensitivity": float(
            dcfg.get("mouse_sensitivity", 1.0)
        ),
        "scroll_px_per_notch": int(
            dcfg.get("scroll_px_per_notch", 100)
        ),
        "brightness_step": int(
            dcfg.get("brightness_step", 15)
        ),
        "screenshot_dir": str(
            dcfg.get("screenshot_dir", "") or ""
        ),
        "allow_web_config_edit": bool(
            cfg["security"].get(
                "allow_web_config_edit",
                False,
            )
        ),
    }


@app.get("/api/commands")
def api_commands():
    return list_commands()


@app.get("/api/bindings")
def api_bindings():
    return [
        binding_with_metadata(item)
        for item in bindings()
    ]


@app.get("/api/binding/{slot}/icon")
def api_binding_icon(slot: int):

    item = next(
        (
            b
            for b in bindings()
            if int(b["slot"]) == slot
        ),
        None,
    )

    if not item:
        raise HTTPException(
            404,
            "Binding non trovato",
        )

    explicit_icon = (
        str(item.get("icon", "")).strip()
        or None
    )

    icon_path = (
        resolve_icon(explicit_icon)
        if explicit_icon
        else None
    )

    if (
        icon_path is None
        and item.get("type") == "app"
    ):
        info = read_desktop(
            item.get("desktop", "")
        )

        if info:
            icon_path = resolve_icon(
                info.get("icon")
            )

    if not icon_path:
        raise HTTPException(
            404,
            "Icona non trovata",
        )

    return FileResponse(
        icon_path,
        media_type=icon_content_type(
            icon_path
        ),
        headers={
            "Cache-Control":
                "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma":
                "no-cache",
            "Expires":
                "0",
        },
    )


@app.post("/api/command/{index}")
def api_run_command(index: int):

    try:
        run_command(index)

        return {
            "ok": True
        }

    except (IndexError, KeyError) as e:
        raise HTTPException(
            404,
            str(e),
        )


@app.post("/api/binding/{slot}")
def api_launch_binding(slot: int):

    try:
        launch_binding(slot)

        return {
            "ok": True
        }

    except KeyError as e:
        raise HTTPException(
            404,
            str(e),
        )


class NewCommand(BaseModel):
    name: str
    command: str


@app.post("/api/commands")
def api_add_command(body: NewCommand):

    if not cfg["security"].get(
        "allow_web_config_edit",
        False,
    ):
        raise HTTPException(
            403,
            "Editing web disabilitato",
        )

    add_command(
        body.name.strip(),
        body.command.strip(),
    )

    return {
        "ok": True
    }


@app.websocket("/ws")
async def ws_remote(ws: WebSocket):

    # il middleware HTTP non gira sulle scope websocket, quindi la
    # sessione va verificata qui, prima di accettare la connessione
    token = ws.cookies.get(auth.COOKIE_NAME)

    if auth.is_enabled():
        check = auth.check_session(token)

        if not check.ok or check.session is None:
            # Accettare e poi chiudere con 1008 consegna al browser un
            # codice di chiusura vero: cosi' il client distingue una
            # sessione scaduta da un server irraggiungibile e porta al
            # login invece di riconnettere all'infinito.
            await ws.accept()
            await ws.close(
                code=1008,
                reason="sessione non valida",
            )
            return

        session = check.session
    else:
        session = None

    await ws.accept()

    held_modifiers: set[str] = set()

    # Tasti Linux raw e bottoni del mouse prestati alla modalita' PC.
    # Sono per-connessione: se il client sparisce, il finally li
    # rilascia tutti, altrimenti resterebbero premuti sul PC remoto.
    raw_keys: set[int] = set()
    raw_buttons: set[str] = set()

    # True mentre il selettore finestre (Alt+Tab) e' aperto con Alt
    # tenuto premuto dal server: va rilasciato su Invio/Esc, per
    # inattivita' o quando la connessione cade.
    switcher_open = False
    switcher_deadline = 0.0
    switcher_close_code = 0

    # una connessione vive piu' della richiesta che l'ha aperta: senza
    # riverificare, una sessione revocata dal pannello locale continuerebbe
    # a iniettare tasti su quel device
    last_check = auth.now()

    def release_raw_input():
        for code in list(raw_keys):
            queue_raw_key(code, False)

        raw_keys.clear()

        for button in list(raw_buttons):
            queue_mouse_button(button, False)

        raw_buttons.clear()

    def raw_code_is_toggle_held(code: int) -> bool:
        for key in held_modifiers:
            if code in RAW_MODIFIER_CODES.get(key, ()):
                return True
        return False

    async def run_system_action(name: str):
        ok = False
        message = ""

        if name == "screenshot":
            ok, message = await asyncio.to_thread(
                take_screenshot
            )

        elif name in (
            "brightness_up",
            "brightness_down",
        ):
            step = int(
                dcfg.get("brightness_step", 15)
            )

            if name == "brightness_down":
                step = -step

            ok, message = await asyncio.to_thread(
                brightness_delta,
                step,
            )

        elif name == "close_app":
            await asyncio.to_thread(
                key_tap,
                "close_window",
            )
            ok = True

        else:
            message = "Azione sconosciuta"

        try:
            await ws.send_json({
                "type": "system_result",
                "name": name,
                "ok": ok,
                "message": message,
            })
        except Exception:
            # il client puo' essersi scollegato mentre Spectacle
            # lavorava: non c'e' nessuno a cui rispondere
            pass

    try:

        while True:

            # menu Alt+Tab lasciato aperto senza conferma: rilascia Alt
            # per non lasciarlo premuto (il loop si risveglia almeno a
            # ogni ping del client, quindi entro pochi secondi)
            if switcher_open and auth.now() > switcher_deadline:
                task_switcher_close()
                switcher_open = False
                switcher_close_code = 0

            if auth.is_enabled() and auth.now() - last_check >= 1:
                live = auth.check_session(token)

                if not live.ok or live.session is None:
                    await ws.close(code=1008)
                    return

                if (
                    auth.now() - live.session.last_used_at
                    > auth.TOUCH_INTERVAL
                ):
                    auth.touch_session(live.session.id)

                last_check = auth.now()

            # Se il telefono va in standby o perde la rete, il socket
            # puo' restare aperto per sempre dal lato server. Il client
            # manda un ping ogni 10 s: se non arriva piu' nulla, la
            # connessione e' morta e va chiusa (e con lei repeater e
            # modificatori tenuti premuti).
            try:
                msg = await asyncio.wait_for(
                    ws.receive_json(),
                    timeout=WS_IDLE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                await ws.close(
                    code=1001,
                    reason="idle",
                )
                break

            action = msg.get("action")

            if action == "ping":
                await ws.send_json({"type": "pong"})
                continue

            elif action == "key":

                key = msg["key"]

                key_tap(key)

                if switcher_open:
                    if key in ("enter", "esc"):
                        # Invio conferma e Esc annulla: in entrambi i casi
                        # il menu si chiude, quindi Alt va rilasciato o
                        # resterebbe premuto per tutti i tasti successivi.
                        task_switcher_close()
                        switcher_open = False
                        switcher_close_code = 0
                    elif key in ("up", "down", "left", "right"):
                        switcher_deadline = (
                            auth.now()
                            + SWITCHER_IDLE_TIMEOUT
                        )

            elif action == "key_start":

                await key_repeater.start(
                    msg["key"]
                )

                if (
                    switcher_open
                    and msg["key"] in ("up", "down", "left", "right")
                ):
                    switcher_deadline = (
                        auth.now()
                        + SWITCHER_IDLE_TIMEOUT
                    )

            elif action == "switcher":

                task_switcher_open()

                switcher_open = True
                switcher_close_code = 0
                switcher_deadline = (
                    auth.now()
                    + SWITCHER_IDLE_TIMEOUT
                )

            elif action == "key_stop":

                await key_repeater.stop(
                    msg.get("key")
                )

            elif action == "modifier_toggle":

                # Lo switcher tiene Alt per conto suo: se l'utente
                # tocca un toggle mentre e' aperto, chiudiamolo prima
                # per non avere due proprietari dello stesso Alt.
                if switcher_open:
                    task_switcher_close()
                    switcher_open = False
                    switcher_close_code = 0

                key = msg["key"]

                if key in held_modifiers:

                    key_up(key)

                    held_modifiers.remove(
                        key
                    )

                else:

                    key_down(key)

                    held_modifiers.add(
                        key
                    )

                await ws.send_json({
                    "type":
                        "held_modifiers",

                    "keys":
                        sorted(
                            held_modifiers
                        ),
                })

            elif action == "key_raw":

                try:
                    code = int(msg.get("code"))
                except (TypeError, ValueError):
                    continue

                if not 1 <= code <= 255:
                    continue

                pressed = bool(msg.get("pressed"))

                # Il modificatore e' gia' tenuto dal toggle: il tasto
                # fisico non deve duplicarlo ne' spegnerlo al keyup.
                if raw_code_is_toggle_held(code):
                    if not pressed:
                        raw_keys.discard(code)
                    continue

                # Alt e' gia' tenuto dallo switcher: un Alt fisico
                # rilascerebbe il menu se inoltrato.
                if switcher_open and code in (KEYS["alt"], 100):
                    if not pressed:
                        raw_keys.discard(code)
                    continue

                if pressed:
                    if code not in raw_keys and queue_raw_key(
                        code,
                        True,
                    ):
                        raw_keys.add(code)
                elif code in raw_keys:
                    queue_raw_key(code, False)
                    raw_keys.discard(code)

                if switcher_open:
                    if (
                        pressed
                        and not switcher_close_code
                        and code in (
                            KEYS["enter"],
                            KEYS["esc"],
                        )
                    ):
                        # Invio/Esc chiudono il menu: teniamo il code
                        # per rilasciare Alt dopo il keyup, rispettando
                        # l'ordine nel pump.
                        switcher_close_code = code
                        switcher_deadline = (
                            auth.now()
                            + SWITCHER_IDLE_TIMEOUT
                        )
                    elif pressed and code in (
                        KEYS["tab"],
                        KEYS["up"],
                        KEYS["down"],
                        KEYS["left"],
                        KEYS["right"],
                    ):
                        switcher_deadline = (
                            auth.now()
                            + SWITCHER_IDLE_TIMEOUT
                        )
                    elif (
                        not pressed
                        and switcher_close_code
                        and code == switcher_close_code
                    ):
                        queue_raw_key(KEYS["alt"], False)
                        switcher_open = False
                        switcher_close_code = 0

            elif action == "mouse_move":

                queue_mouse_move(
                    msg.get("dx", 0),
                    msg.get("dy", 0),
                )

            elif action == "mouse_button":

                button = str(msg.get("button", ""))
                pressed = bool(msg.get("pressed"))

                if button not in ("left", "middle", "right"):
                    continue

                if pressed:
                    if (
                        button not in raw_buttons
                        and queue_mouse_button(button, True)
                    ):
                        raw_buttons.add(button)
                elif button in raw_buttons:
                    queue_mouse_button(button, False)
                    raw_buttons.discard(button)

            elif action == "scroll":

                queue_scroll(
                    msg.get("dx", 0),
                    msg.get("dy", 0),
                )

            elif action == "release_transient":

                # Uscita dall'area nella modalita' PC: rilascia mouse,
                # tasti fisici e switcher, ma lascia premuti i toggle
                # CTRL/ALT/META scelti nella sidebar.
                await mouse.stop()
                await key_repeater.stop()

                release_raw_input()

                if switcher_open:
                    task_switcher_close()
                    switcher_open = False
                    switcher_close_code = 0

            elif action == "release_all":

                await mouse.stop()
                await key_repeater.stop()

                release_raw_input()

                for key in list(held_modifiers):
                    key_up(key)

                held_modifiers.clear()

                if switcher_open:
                    task_switcher_close()
                    switcher_open = False
                    switcher_close_code = 0

            elif action == "system":

                name = str(msg.get("name", ""))

                asyncio.create_task(
                    run_system_action(name)
                )

            elif action == "mouse_click":

                left_click()

            elif action == "right_click":

                right_click()

            elif action == "scroll_up":

                scroll_up()

            elif action == "scroll_down":

                scroll_down()

            elif action == "mouse_start":

                await mouse.start(
                    msg["direction"]
                )

            elif action == "mouse_stop":

                await mouse.stop(
                    msg.get("direction")
                )

            elif action == "text":

                type_text(
                    msg.get(
                        "text",
                        "",
                    )
                )

    except WebSocketDisconnect:
        pass

    finally:

        await mouse.stop()

        await key_repeater.stop()

        # mouse e tasti prestati dalla modalita' PC vanno rilasciati
        # anche se il browser sparisce senza mandare release_all
        release_raw_input()

        for key in list(
            held_modifiers
        ):
            key_up(key)

        # il telefono puo' sparire (standby/rete) col menu Alt+Tab
        # aperto: senza questo Alt resterebbe premuto sul PC
        if switcher_open:
            task_switcher_close()
            switcher_open = False
            switcher_close_code = 0
