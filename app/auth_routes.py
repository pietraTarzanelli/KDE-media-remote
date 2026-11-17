"""Pagine di autenticazione: login, logout e revoca dei device.

Nessuna di queste pagine tocca la UI del telecomando: sono endpoint
separati, con HTML minimale autosufficiente. L'app esistente in web/
continua a funzionare identica, ma dietro il middleware di sessione.

La pagina di revoca (/auth/revoke) e' l'unica che fa azioni distruttive
sulle sessioni, ed e' chiusa su due condizioni indipendenti:

1. sessione autenticata valida (come qualunque altra rotta protetta)
2. connessione TCP che arriva davvero dal loopback della macchina

Il punto 2 guarda il peer della connessione, non le intestazioni: il
server ascolta in chiaro su 127.0.0.1 e sull'interfaccia Tailscale, e
un device remoto che si connette a 100.x non puo' fingere di essere
locale semplicemente mandando X-Forwarded-For. Quell'intestazione viene
letta solo se la connessione arriva da un proxy elencato in
config/auth.toml (trusted_proxy_ips), che di default e' vuoto.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import sys
import time
from html import escape
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from . import auth

router = APIRouter()

LOGIN_PATH = "/auth/login"
REVOKE_PATH = "/auth/revoke"
LOGOUT_PATH = "/auth/logout"
ACCOUNTS_PATH = "/auth/accounts"

# rate limit delle azioni che scrivono sugli account (aggiungere,
# cambiare PIN, cancellare): non e' il login, ma è comunque una
# superficie dove un PIN indovinato può scrivere
ACTION_MAX = 20
ACTION_WINDOW = 60
ACTION_LOCKOUT = 300

EXPIRED_MESSAGES = {
    auth.REASON_IDLE: (
        "Sessione scaduta per inattivita': troppi giorni senza uso."
    ),
    auth.REASON_ABSOLUTE: (
        "Sessione scaduta: i 4 mesi di accesso ricordato sono finiti."
    ),
    auth.REASON_REPLACED: (
        "Sei stato disconnesso: lo stesso account e' stato ricollegato"
        " da un altro dispositivo."
    ),
    auth.REASON_LOCAL_REVOKE: (
        "Sessione revocata dal pannello locale."
    ),
    auth.REASON_ACCOUNT_REMOVED: (
        "Account non piu' attivo."
    ),
}

STYLE = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; padding: 24px 16px;
  display: flex; align-items: center; justify-content: center;
  background: #16181c; color: #e8eaed;
  font: 16px/1.5 "Inter", system-ui, sans-serif;
}
.card {
  width: 100%; max-width: 420px; padding: 28px 24px;
  background: #22252b; border: 1px solid #33373f;
  border-radius: 18px;
}
h1 { margin: 0 0 4px; font-size: 22px; }
.sub { margin: 0 0 22px; color: #9aa0a6; font-size: 14px; }
label { display: block; margin: 0 0 6px; font-size: 13px; color: #9aa0a6; }
select, input {
  width: 100%; padding: 11px 12px; margin-bottom: 16px;
  background: #16181c; color: #e8eaed;
  border: 1px solid #3a3f48; border-radius: 10px; font-size: 16px;
}
select:focus, input:focus { outline: 2px solid #6ea8fe; outline-offset: 1px; }
button {
  width: 100%; padding: 12px; font-size: 16px; font-weight: 600;
  color: #16181c; background: #6ea8fe; border: 0; border-radius: 10px;
  cursor: pointer;
}
button:hover { background: #86b6ff; }
button.danger { background: #e5484d; color: #fff; }
button.danger:hover { background: #f06a6e; }
button.ghost {
  background: transparent; color: #9aa0a6;
  border: 1px solid #3a3f48; font-weight: 500;
}
button.ghost:hover { color: #e8eaed; border-color: #4a505a; }
.err {
  margin: 0 0 16px; padding: 10px 12px; font-size: 14px;
  color: #ffb4ab; background: #3b1f22; border: 1px solid #6b2b30;
  border-radius: 10px;
}
.ok {
  margin: 0 0 16px; padding: 10px 12px; font-size: 14px;
  color: #9be8b0; background: #1c2f22; border: 1px solid #2f5c3c;
  border-radius: 10px;
}
table { width: 100%; border-collapse: collapse; margin-bottom: 20px; }
th, td {
  padding: 9px 4px; text-align: left; font-size: 13px;
  border-bottom: 1px solid #2c3038; vertical-align: top;
}
th { color: #9aa0a6; font-weight: 500; }
td.mono { font-family: ui-monospace, monospace; color: #9aa0a6; }
.badge {
  display: inline-block; padding: 1px 7px; font-size: 11px;
  border-radius: 999px; background: #1f3b2a; color: #9be8b0;
}
.badge.remote { background: #3a3320; color: #f0c98a; }
.badge.admin { background: #24384f; color: #9cc7ff; }
.badge.off { background: #3b1f22; color: #ffb4ab; }
.stack { display: flex; flex-direction: column; gap: 10px; }
.row-actions button { padding: 6px 10px; font-size: 12px; width: auto; }
.row-actions { display: flex; gap: 8px; margin-top: 4px; flex-wrap: wrap; }
.back { display: block; margin-top: 18px; color: #6ea8fe; font-size: 14px; }
.links { display: flex; gap: 16px; margin-top: 18px; font-size: 14px; }
.links a { color: #6ea8fe; }
details {
  border: 1px solid #33373f; border-radius: 12px;
  padding: 12px 14px; margin-bottom: 10px;
}
details summary { cursor: pointer; font-size: 13px; color: #9aa0a6; }
details[open] summary { margin-bottom: 12px; }
details form { margin: 0; }
.inline { display: flex; gap: 8px; align-items: flex-end; }
.inline > div { flex: 1; }
.inline button { width: auto; padding: 11px 16px; }
.hint { font-size: 12px; color: #9aa0a6; margin: -8px 0 16px; }
"""


def page(
    title: str,
    body: str,
    status: int = 200,
) -> HTMLResponse:
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{escape(title)} - KDE Media Remote</title>
<style>{STYLE}</style>
</head>
<body>
<main class="card">
<h1>{escape(title)}</h1>
{body}
</main>
</body>
</html>""",
        status_code=status,
    )


def fmt(ts: int | None) -> str:
    if not ts:
        return "-"
    return time.strftime(
        "%d/%m/%Y %H:%M",
        time.localtime(ts),
    )


def audit(message: str) -> None:
    """Scrive sul log del server chi ha fatto cosa.

    Le pagine di gestione account scrivono sul DB: senza una traccia,
    un account che sparisce non lascia nulla dietro di se'.
    """
    print(
        f"[auth] {message}",
        file=sys.stderr,
        flush=True,
    )


def safe_next(raw: str | None) -> str:
    """Ripulisce la destinazione post-login.

    Accetta solo un path interno. Senza questo controllo il parametro
    ?next= diventa un open redirect: un link costruito apposta
    manderebbe chi ha appena fatto il login su un sito esterno, con la
    sessione appena concessa in mano al browser dell'attaccante.
    """
    if not raw:
        return "/"

    candidate = raw.strip()

    if not candidate.startswith("/"):
        return "/"

    # '//example.com' e '/\example.com' sono URL assoluti travestiti:
    # il browser li risolve come protocol-relative, non come path
    if candidate.startswith(("//", "/\\")):
        return "/"

    # nessuna ruta interna contiene ':' o '\': se compaiono, il valore
    # porta con se' uno schema ('/https:evil') o un host nascosto
    if ":" in candidate or "\\" in candidate:
        return "/"

    return candidate


def current_session(request: Request):
    return getattr(request.state, "session", None)


def csrf_for(session) -> str:
    """Token anti-CSRF legato a questa sessione.

    Il cookie di sessione ha gia' SameSite=Lax, che da solo impedisce
    a un form su un altro sito di inviare la richiesta con il cookie
    dentro. Questo token e' la seconda barriera: anche se un giorno
    SameSite non fosse supportato o venisse allentato, un POST senza
    il token non vale niente.

    Non serve memorizzarlo: si ricalcola a ogni richiesta da dati che
    il browser non conosce (id sessione, device id, id account).
    """
    material = (
        f"kmr-csrf-v1:{session.id}:{session.account_id}:"
        f"{session.device_id}"
    )
    return hmac.new(
        material.encode("utf-8"),
        b"csrf",
        hashlib.sha256,
    ).hexdigest()


def csrf_ok(
    form: dict[str, str],
    session,
) -> bool:
    """Confronto in tempo costante col token atteso."""
    sent = str(form.get("csrf") or "")

    if not sent:
        return False

    return hmac.compare_digest(
        sent,
        csrf_for(session),
    )


async def read_form(
    request: Request,
) -> dict[str, str]:
    """Legge un POST application/x-www-form-urlencoded.

    request.form() di Starlette pretende python-multipart anche quando
    il body non e' multipart, e quella dipendenza (con i suoi
    aggiornamenti CVE) non vale la pena per due campi. Qui il parsing
    e' una riga di stdlib.
    """
    body = await request.body()
    parsed = parse_qs(
        body.decode("utf-8", "replace"),
        keep_blank_values=True,
    )
    return {
        key: values[0]
        for key, values in parsed.items()
    }


# ── client helpers ──


def _trusted_proxies() -> set[str]:
    """Indirizzi dei reverse proxy di cui ci fidiamo.

    Vuoto di default: il server ascolta direttamente su 127.0.0.1 e
    sull'interfaccia Tailscale, quindi l'unica cosa che arriva e' la
    connessione vera.
    """
    raw = auth.settings()["trusted_proxy_ips"]

    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()

    return {str(p).strip() for p in raw if str(p).strip()}


def _valid_ip(value: str) -> str | None:
    """Normalizza un indirizzo, oppure None se non e' un IP."""
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def client_ip(request: Request) -> str:
    """IP reale del client.

    X-Real-IP / X-Forwarded-For si considerano solo se la connessione
    arriva da un proxy dichiarato in config/auth.toml. Senza reverse
    proxy, fidarsi dell'intestazione sarebbe un buco: un device Tailscale
    potrebbe mandare X-Forwarded-For: 127.0.0.1 e aprire /auth/revoke da
    remoto, che e' esattamente quello che la pagina deve impedire.

    Nginx/Nginx Proxy Manager impostano X-Real-IP con $remote_addr
    (sovrascrivendolo) e accodano il client a X-Forwarded-For con
    $proxy_add_x_forwarded_for. Per questo si usa X-Real-IP quando c'e',
    altrimenti l'ULTIMO valore della catena: il primo puo' essere stato
    mandato dal client e non e' attendibile.
    """
    peer = request.client.host if request.client else ""

    if peer not in _trusted_proxies():
        return peer

    real = _valid_ip(
        (request.headers.get("x-real-ip") or "").strip()
    )

    if real:
        return real

    forwarded = request.headers.get("x-forwarded-for")

    if forwarded:
        parts = [
            part.strip()
            for part in forwarded.split(",")
            if part.strip()
        ]

        if parts:
            last = _valid_ip(parts[-1])
            if last:
                return last

    return peer


def client_is_local(request: Request) -> bool:
    return auth.is_loopback(client_ip(request))


def is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True

    peer = request.client.host if request.client else ""

    if peer not in _trusted_proxies():
        return False

    return (
        request.headers.get("x-forwarded-proto", "")
        .split(",")[0]
        .strip()
        .lower()
        == "https"
    )


def cookie_secure(request: Request) -> bool:
    mode = str(auth.settings()["cookie_secure"])
    if mode == "auto":
        return is_https(request)
    return mode.lower() in ("1", "true", "yes")


def device_label(user_agent: str) -> str:
    """Etichetta leggibile del device, per riconoscerlo nella lista."""
    ua = user_agent or ""

    for token, name in (
        ("iPhone", "iPhone"),
        ("iPad", "iPad"),
        ("Android", "Android"),
        ("Windows NT", "Windows"),
        ("Macintosh", "Mac"),
        ("CrOS", "ChromeOS"),
        ("Linux", "Linux"),
    ):
        if token in ua:
            return name

    return "Sconosciuto"


# ── login ──


@router.get(LOGIN_PATH)
async def login_page(request: Request):
    accounts = [
        a for a in auth.list_accounts() if a.active
    ]

    if not accounts:
        return page(
            "Accesso",
            """<p class="sub">Nessun account attivo.</p>
<p class="sub">Crea il primo PIN dalla macchina locale con
<code>python tools/accounts.py add &lt;nome&gt;</code>.</p>""",
            status=503,
        )

    options = "".join(
        f'<option value="{a.id}">'
        f"{escape(a.name)}</option>"
        for a in accounts
    )

    message = ""
    reason = request.query_params.get("reason")

    if reason and reason in EXPIRED_MESSAGES:
        message = (
            f'<p class="err">{escape(EXPIRED_MESSAGES[reason])}</p>'
        )

    if request.query_params.get("locked"):
        message = (
            '<p class="err">Troppi tentativi sbagliati. '
            "Attendi qualche minuto e riprova.</p>"
        )
    elif request.query_params.get("error"):
        message = '<p class="err">PIN non valido.</p>'

    # la destinazione di prima del login: senza questo, aprire
    # /auth/revoke da una sessione scaduta riportava al telecomando
    # invece che alla pagina richiesta
    nxt = safe_next(request.query_params.get("next"))

    return page(
        "Accesso",
        f"""<p class="sub">KDE Media Remote - autenticazione</p>
{message}
<form method="post" action="{LOGIN_PATH}">
<input type="hidden" name="next" value="{escape(nxt)}">
<label for="account">Account</label>
<select id="account" name="account_id" autocomplete="off">{options}</select>
<label for="pin">PIN</label>
<input id="pin" name="pin" type="password" inputmode="numeric"
       autocomplete="current-password" autofocus required>
<button type="submit">Entra</button>
</form>""",
    )


@router.post(LOGIN_PATH)
async def login_submit(request: Request):
    form = await read_form(request)
    ip = client_ip(request)
    nxt = safe_next(form.get("next"))

    if auth.login_locked_until(f"ip:{ip}") > int(time.time()):
        return RedirectResponse(
            f"{LOGIN_PATH}?locked=1",
            status_code=303,
        )

    account_id = form.get("account_id")
    pin = str(form.get("pin") or "").strip()

    account = next(
        (
            a
            for a in auth.list_accounts()
            if a.active and str(a.id) == str(account_id)
        ),
        None,
    )

    if account is None or not pin:
        auth.record_login_failure(f"ip:{ip}")
        return RedirectResponse(
            f"{LOGIN_PATH}?error=1&next={quote(nxt, safe='/')}",
            status_code=303,
        )

    result = auth.login(
        name=account.name,
        pin=pin,
        device_label=device_label(
            request.headers.get("user-agent", "")
        ),
        ip_addr=ip,
        local=auth.is_loopback(ip),
    )

    if result is None:
        locked = auth.record_login_failure(f"ip:{ip}")
        target = "locked" if locked else "error"
        return RedirectResponse(
            f"{LOGIN_PATH}?{target}=1&next={quote(nxt, safe='/')}",
            status_code=303,
        )

    _, token, session = result

    auth.clear_login_failures(f"ip:{ip}")

    response = RedirectResponse(nxt, status_code=303)
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        max_age=max(session.expires_at - int(time.time()), 60),
        httponly=True,
        samesite="lax",
        secure=cookie_secure(request),
        path="/",
    )
    return response


@router.get(LOGOUT_PATH)
@router.post(LOGOUT_PATH)
async def logout(request: Request):
    session = getattr(request.state, "session", None)

    if session is not None:
        auth.revoke_session(
            session.id,
            auth.REASON_LOGOUT,
        )

    response = RedirectResponse(
        LOGIN_PATH,
        status_code=303,
    )
    response.delete_cookie(
        auth.COOKIE_NAME,
        path="/",
    )
    return response


# ── gestione account ──

NOT_ADMIN = """<p class="err">Questa sezione e' riservata
all'account admin. Come utente puoi cambiare solo il tuo PIN.</p>"""


def _badge_admin(account) -> str:
    if not account.active:
        return '<span class="badge off">disattivato</span>'

    if account.is_admin:
        return '<span class="badge admin">admin</span>'

    return ""


def _csrf_field(session) -> str:
    return (
        f'<input type="hidden" name="csrf" '
        f'value="{csrf_for(session)}">'
    )


def _accounts_body(
    session,
    message: str,
    error: str,
) -> str:
    """Corpo della pagina, diverso per admin e utente semplice."""
    if not session.is_admin:
        return _user_body(session, message, error)

    return _admin_body(session, message, error)


def _user_body(
    session,
    message: str,
    error: str,
) -> str:
    """Vista utente: solo il proprio PIN.

    Niente nome, niente cancellazione, niente altri account: ogni
    azione che potrebbe essere usata per prendere il controllo del
    sistema resta all'admin.
    """
    return f"""<p class="sub">Il tuo account: <strong>
{escape(session.account_name)}</strong>. Qui puoi cambiare il tuo PIN.
Gestire gli altri account e' compito dell'admin.</p>
{message}
{error}
<details open>
<summary>Cambia il tuo PIN</summary>
<form method="post" action="{ACCOUNTS_PATH}">
{_csrf_field(session)}
<input type="hidden" name="action" value="change_own_pin">
<label for="new_pin">Nuovo PIN</label>
<input id="new_pin" name="pin" type="password" inputmode="numeric"
       autocomplete="new-password" required>
<label for="confirm_pin">Conferma il nuovo PIN</label>
<input id="confirm_pin" name="confirm_pin" type="password"
       inputmode="numeric" autocomplete="new-password" required>
<button type="submit">Cambia PIN</button>
</form>
<p class="hint">Cambiandolo verrai disconnesso: dovrai fare di nuovo
il login con il PIN nuovo.</p>
</details>
<div class="links">
<a href="/">Torna al telecomando</a>
<a href="{LOGOUT_PATH}">Esci</a>
</div>"""


def _admin_body(
    session,
    message: str,
    error: str,
) -> str:
    accounts = auth.list_accounts()
    active = sum(1 for a in accounts if a.active)
    admins = sum(1 for a in accounts if a.active and a.is_admin)

    rows = ""

    for a in accounts:
        where = "collegato" if a.open_sessions else "-"
        self_row = a.id == session.account_id

        change = f"""<details>
<summary>Cambia il PIN di {escape(a.name)}</summary>
<form method="post" action="{ACCOUNTS_PATH}">
{_csrf_field(session)}
<input type="hidden" name="action" value="change_pin">
<input type="hidden" name="account_id" value="{a.id}">
<label for="pin{a.id}">Nuovo PIN</label>
<input id="pin{a.id}" name="pin" type="password" inputmode="numeric"
       autocomplete="new-password" required>
<label for="cf{a.id}">Conferma</label>
<input id="cf{a.id}" name="confirm_pin" type="password"
       inputmode="numeric" autocomplete="new-password" required>
<button type="submit">Cambia PIN</button>
</form>
</details>"""

        if a.active:
            toggle = (
                f"""<form method="post" action="{ACCOUNTS_PATH}">
{_csrf_field(session)}
<input type="hidden" name="action" value="disable">
<input type="hidden" name="account_id" value="{a.id}">
<button class="ghost" type="submit">Disattiva</button>
</form>"""
            )
        else:
            toggle = (
                f"""<form method="post" action="{ACCOUNTS_PATH}">
{_csrf_field(session)}
<input type="hidden" name="action" value="enable">
<input type="hidden" name="account_id" value="{a.id}">
<button class="ghost" type="submit">Riattiva</button>
</form>"""
            )

        if a.is_admin:
            role = (
                f"""<form method="post" action="{ACCOUNTS_PATH}">
{_csrf_field(session)}
<input type="hidden" name="action" value="demote">
<input type="hidden" name="account_id" value="{a.id}">
<button class="ghost" type="submit">Togli admin</button>
</form>"""
            )
        else:
            role = (
                f"""<form method="post" action="{ACCOUNTS_PATH}">
{_csrf_field(session)}
<input type="hidden" name="action" value="promote">
<input type="hidden" name="account_id" value="{a.id}">
<button class="ghost" type="submit">Rendi admin</button>
</form>"""
            )

        drop = f"""<form method="post" action="{ACCOUNTS_PATH}"
      onsubmit="return confirm('Cancellare {escape(a.name)}?')">
{_csrf_field(session)}
<input type="hidden" name="action" value="delete">
<input type="hidden" name="account_id" value="{a.id}">
<button class="danger" type="submit">Cancella</button>
</form>"""

        note = (
            "<br><span class=\"hint\">questo device</span>"
            if self_row
            else ""
        )

        rows += f"""<tr>
<td><strong>{escape(a.name)}</strong>
{_badge_admin(a)}<br><span class="hint">#{a.id}</span></td>
<td class="mono">{where}{note}</td>
<td class="mono">{fmt(a.last_login_at)}</td>
<td>
<div class="stack">
{change}
<div class="row-actions">{toggle}{role}{drop}</div>
</div>
</td>
</tr>"""

    table = f"""<table>
<tr><th>Account</th><th>Stato</th><th>Ultimo accesso</th>
<th></th></tr>
{rows}
</table>"""

    return f"""<p class="sub">Gestione account. Amministratore:
<strong>{escape(session.account_name)}</strong>. {active} account attivi
su {auth.max_accounts()}, {admins} admin.</p>
{message}
{error}
{table}
<details>
<summary>Aggiungi un account</summary>
<form method="post" action="{ACCOUNTS_PATH}">
{_csrf_field(session)}
<input type="hidden" name="action" value="create">
<label for="new_name">Nome</label>
<input id="new_name" name="name" autocomplete="off" required>
<label for="new_pin">PIN (minimo 4 caratteri)</label>
<input id="new_pin" name="pin" type="password" inputmode="numeric"
       autocomplete="new-password" required>
<label for="new_admin">Ruolo</label>
<select id="new_admin" name="is_admin">
<option value="0">utente normale</option>
<option value="1">admin (gestisce tutti gli account)</option>
</select>
<button type="submit">Crea account</button>
</form>
</details>
<div class="links">
<a href="{REVOKE_PATH}">Revoca device</a>
<a href="/">Torna al telecomando</a>
<a href="{LOGOUT_PATH}">Esci</a>
</div>"""


@router.get(ACCOUNTS_PATH)
async def accounts_page(request: Request):
    session = current_session(request)

    if session is None:
        return RedirectResponse(
            f"{LOGIN_PATH}?next={ACCOUNTS_PATH}",
            status_code=303,
        )

    message = ""
    error = ""

    if request.query_params.get("created"):
        message = (
            '<p class="ok">Account creato. Comunica il PIN '
            "alla persona interessata.</p>"
        )
    elif request.query_params.get("pin"):
        message = (
            '<p class="ok">PIN cambiato. I device di quell\'account '
            "sono stati disconnessi.</p>"
        )
    elif request.query_params.get("done"):
        message = '<p class="ok">Account aggiornato.</p>'
    elif request.query_params.get("err"):
        error = f'<p class="err">{escape(str(request.query_params["err"])[:200])}</p>'

    return page(
        "Account",
        _accounts_body(session, message, error),
    )


@router.post(ACCOUNTS_PATH)
async def accounts_action(request: Request):
    session = current_session(request)

    if session is None:
        return JSONResponse(
            {"error": "non autorizzato"},
            status_code=401,
        )

    form = await read_form(request)

    # senza questo controllo un POST forzato da un'altra pagina
    # riuscirebbe a creare o cancellare account
    if not csrf_ok(form, session):
        return JSONResponse(
            {"error": "csrf non valido"},
            status_code=403,
        )

    action = str(form.get("action") or "")

    # cambio del proprio PIN: consentito anche a un utente normale,
    # ma solo sul proprio account e senza toccare il ruolo
    if action == "change_own_pin":
        return _change_pin(
            request,
            session,
            session.account_id,
            form,
        )

    if not session.is_admin:
        audit(
            f"account #{session.account_id} ha provato "
            f"'{action}' senza essere admin"
        )
        return page(
            "Account",
            _accounts_body(session, "", NOT_ADMIN),
            status=403,
        )

    target_id = form.get("account_id")

    if action == "create":
        return _create_account(request, session, form)

    if not target_id:
        return _redirect_err("account mancante")

    try:
        account_id = int(target_id)
    except (TypeError, ValueError):
        return _redirect_err("account non valido")

    if action == "change_pin":
        return _change_pin(
            request, session, account_id, form
        )

    try:
        return _admin_action(
            action, session, account_id
        )
    except ValueError as e:
        return _redirect_err(str(e))


def _redirect_err(message: str):
    return RedirectResponse(
        f"{ACCOUNTS_PATH}?err={quote(message, safe='')}",
        status_code=303,
    )


def _change_pin(
    request: Request,
    session,
    account_id: int,
    form: dict[str, str],
):
    """Cambia un PIN. Un utente normale arriva qui solo sul suo account."""
    pin = str(form.get("pin") or "").strip()
    confirm = str(form.get("confirm_pin") or "").strip()

    if pin != confirm:
        return _redirect_err("I due PIN non coincidono")

    if not auth.get_account(account_id):
        return _redirect_err("Account non trovato")

    # il form dell'utente non ha un campo account_id, ma se arrivasse
    # comunque non deve poter mirare a qualcun altro
    if (
        not session.is_admin
        and account_id != session.account_id
    ):
        return page(
            "Account",
            _accounts_body(session, "", NOT_ADMIN),
            status=403,
        )

    try:
        auth.change_pin(account_id, pin)
    except ValueError as e:
        return _redirect_err(str(e))

    # le sessioni di quell'account muoiono: il PIN nuovo non serve a
    # nulla per chi avesse il vecchio
    auth.revoke_sessions_for_account(
        account_id,
        auth.REASON_ACCOUNT_REMOVED,
    )
    audit(
        f"account #{session.account_id} ha cambiato il PIN "
        f"di #{account_id}"
    )

    # se hai cambiato il tuo stesso PIN, la sessione corrente e' morta:
    # il cookie porta a una sessione revocata
    if account_id == session.account_id:
        response = RedirectResponse(
            LOGIN_PATH,
            status_code=303,
        )
        response.delete_cookie(
            auth.COOKIE_NAME,
            path="/",
        )
        return response

    return RedirectResponse(
        f"{ACCOUNTS_PATH}?pin=1",
        status_code=303,
    )


def _create_account(
    request: Request,
    session,
    form: dict[str, str],
):
    name = str(form.get("name") or "").strip()
    pin = str(form.get("pin") or "").strip()
    is_admin = str(form.get("is_admin") or "0") == "1"

    try:
        account = auth.add_account(
            name,
            pin,
            is_admin=is_admin,
        )
    except ValueError as e:
        return _redirect_err(str(e))

    audit(
        f"account #{session.account_id} ha creato "
        f"#{account.id} ({'admin' if is_admin else 'utente'})"
    )

    return RedirectResponse(
        f"{ACCOUNTS_PATH}?created=1",
        status_code=303,
    )


def _admin_action(
    action: str,
    session,
    account_id: int,
):
    if action == "delete":
        auth.remove_account_guarded(account_id)
        audit(
            f"account #{session.account_id} ha cancellato "
            f"#{account_id}"
        )
        return RedirectResponse(
            f"{ACCOUNTS_PATH}?done=1",
            status_code=303,
        )

    if action == "disable":
        # disattivare l'ultimo admin lascerebbe il sistema senza un
        # account in grado di gestire gli altri
        target = auth.get_account(account_id)

        if (
            target is not None
            and target.is_admin
            and target.active
            and sum(
                1
                for a in auth.list_accounts()
                if a.active and a.is_admin
            ) <= 1
        ):
            return _redirect_err(
                "Non puoi disattivare l'ultimo admin attivo"
            )

        auth.set_account_active(account_id, False)
        audit(
            f"account #{session.account_id} ha disattivato "
            f"#{account_id}"
        )
        return RedirectResponse(
            f"{ACCOUNTS_PATH}?done=1",
            status_code=303,
        )

    if action == "enable":
        auth.set_account_active(account_id, True)
        audit(
            f"account #{session.account_id} ha riattivato "
            f"#{account_id}"
        )
        return RedirectResponse(
            f"{ACCOUNTS_PATH}?done=1",
            status_code=303,
        )

    if action in ("promote", "demote"):
        auth.set_admin(
            account_id,
            action == "promote",
        )
        audit(
            f"account #{session.account_id} ha "
            f"{'promosso' if action == 'promote' else 'declassato'} "
            f"#{account_id}"
        )
        return RedirectResponse(
            f"{ACCOUNTS_PATH}?done=1",
            status_code=303,
        )

    return _redirect_err("azione sconosciuta")


# ── revoca (solo macchina locale) ──

LOCAL_ONLY = """<p class="err">Questa pagina si apre solo
dalla macchina che ospita il server.</p>"""


@router.get(REVOKE_PATH)
async def revoke_page(request: Request):
    if not client_is_local(request):
        return page(
            "Revoca device",
            LOCAL_ONLY,
            status=403,
        )

    session = getattr(request.state, "session", None)
    sessions = auth.list_active_sessions()
    message = ""

    if request.query_params.get("done"):
        message = (
            '<p class="ok">Sessioni aggiornate.</p>'
        )
    elif request.query_params.get("logout"):
        message = (
            '<p class="ok">Sei stato disconnesso.</p>'
        )

    if not sessions:
        rows = (
            '<p class="sub">Nessun device collegato.</p>'
        )
    else:
        body = ""
        for s in sessions:
            where = "questo" if (
                session is not None and s.id == session.id
            ) else "remoto"
            badge_class = (
                "badge" if s.local_login else "badge remote"
            )
            body += f"""<tr>
<td><strong>{escape(s.account_name)}</strong>
<br><span class="{badge_class}">{where}</span></td>
<td class="mono">{escape(s.device_label or "-")}
<br>{escape(s.ip_addr or "-")}</td>
<td class="mono">{fmt(s.created_at)}</td>
<td class="mono">{fmt(s.last_used_at)}</td>
<td>
<form method="post" action="{REVOKE_PATH}">
<input type="hidden" name="session_id" value="{s.id}">
<button class="ghost" type="submit">Disconnetti</button>
</form>
</td>
</tr>"""

        rows = f"""<table>
<tr><th>Account</th><th>Device</th><th>Accesso</th>
<th>Ultimo uso</th><th></th></tr>
{body}
</table>"""

    return page(
        "Revoca device",
        f"""<p class="sub">Sessioni attive. Un solo device per
account: quando lo stesso account si ricollega, il device precedente
viene disconnesso automaticamente.</p>
{message}
{rows}
<div class="stack">
<form method="post" action="{REVOKE_PATH}">
<input type="hidden" name="action" value="revoke_all">
<button class="danger" type="submit">
Disconnetti tutti i device</button>
</form>
<form method="post" action="{LOGOUT_PATH}">
<button class="ghost" type="submit">Disconnetti questo device</button>
</form>
</div>
<a class="back" href="/">Torna al telecomando</a>
<a class="back" href="{ACCOUNTS_PATH}">Gestisci gli account</a>""",
    )


@router.post(REVOKE_PATH)
async def revoke_action(request: Request):
    if not client_is_local(request):
        return JSONResponse(
            {"error": "solo macchina locale"},
            status_code=403,
        )

    form = await read_form(request)
    action = str(form.get("action") or "revoke_one")

    if action == "revoke_all":
        auth.revoke_all_sessions(auth.REASON_LOCAL_REVOKE)
        return RedirectResponse(
            f"{REVOKE_PATH}?done=1",
            status_code=303,
        )

    session_id = form.get("session_id")

    if session_id:
        try:
            auth.revoke_session(
                int(session_id),
                auth.REASON_LOCAL_REVOKE,
            )
        except (TypeError, ValueError):
            pass

    return RedirectResponse(
        f"{REVOKE_PATH}?done=1",
        status_code=303,
    )
