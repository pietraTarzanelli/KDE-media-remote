"""Autenticazione a PIN e gestione delle sessioni persistenti.

Il server non ha un database: qui introduciamo SQLite (stdlib, nessuna
dipendenza nuova) perche' le sessioni hanno due scadenze indipendenti e
una relazione "un account = una sessione viva" che un file piatto
renderebbe fragile (concorrenza su /api/* e scritture non atomiche).

Le scelte che contano per la sicurezza:

- il PIN non e' mai salvato in chiaro: si verifica con
  PBKDF2-HMAC-SHA256 + salt casuale per account, con hmac.compare_digest
  per il confronto
- il token di sessione nasce da secrets.token_urlsafe(32) e nel DB finisce
  solo il suo SHA-256: un dump del database non permette di impersonare
  nessuno
- ogni sessione ha DUE timer indipendenti:
    * assoluto  = 4 mesi dalla creazione, non si ricarica
    * inattivita' = 20 giorni dall'ultimo uso, si ricarica a ogni richiesta
- un account puo' avere al massimo una sessione viva: il login successivo
  revoca la precedente
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import load_toml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "auth.db"

COOKIE_NAME = "kmr_sid"
MAX_ACCOUNTS = 5
DEFAULT_ABSOLUTE_DAYS = 120
DEFAULT_IDLE_DAYS = 20
PIN_ITERATIONS = 240_000
DAY = 86400

# riduco le scritture di "last_used_at": il timer di inattivita' ha una
# granularita' di ore, non serve un UPDATE per ogni singola richiesta
TOUCH_INTERVAL = 3600

# un token per riga e' gia' sopra il gradino, ma un tetto esplicito
# evita che un input buggato crei tabelle enormi
MAX_ATTEMPTS_PER_WINDOW = 5
ATTEMPT_WINDOW = 60
LOCKOUT_SECONDS = 300

REASON_LOGOUT = "logout"
REASON_REPLACED = "replaced_by_new_login"
REASON_IDLE = "idle_expired"
REASON_ABSOLUTE = "absolute_expired"
REASON_LOCAL_REVOKE = "local_revoke"
REASON_ACCOUNT_REMOVED = "account_removed"


def _now() -> int:
    return int(time.time())


def now() -> int:
    """Secondi epoch. Un unico punto per il tempo, usato anche fuori."""
    return _now()


# ── configurazione ──

_settings: dict | None = None


def settings() -> dict:
    """Legge config/auth.toml una volta sola, con default sensati."""
    global _settings

    if _settings is None:
        merged = {
            "enabled": True,
            "max_accounts": MAX_ACCOUNTS,
            "session_absolute_days": DEFAULT_ABSOLUTE_DAYS,
            "session_idle_days": DEFAULT_IDLE_DAYS,
            "login_max_attempts": MAX_ATTEMPTS_PER_WINDOW,
            "login_lockout_seconds": LOCKOUT_SECONDS,
            # "auto" = Secure solo se la richiesta arriva su HTTPS
            "cookie_secure": "auto",
            # Reverse proxy fidati per X-Forwarded-For. Vuoto = nessuno,
            # che e' il caso normale: il server ascolta direttamente
            # sul loopback e su tailscale0, senza intermediari.
            "trusted_proxy_ips": [],
        }

        path = CONFIG_DIR / "auth.toml"

        if path.exists():
            section = load_toml("auth.toml").get("auth", {})

            for key in merged:
                if key in section:
                    merged[key] = section[key]

        # override locale non versionato: indirizzi IP e altri valori
        # specifici di questa macchina non finiscono nel repository
        local_path = CONFIG_DIR / "auth.local.toml"

        if local_path.exists():
            local = load_toml("auth.local.toml").get("auth", {})

            for key in merged:
                if key in local:
                    merged[key] = local[key]

        _settings = merged

    return _settings


def is_enabled() -> bool:
    return bool(settings()["enabled"])


def max_accounts() -> int:
    return int(settings()["max_accounts"])


def absolute_seconds() -> int:
    return int(settings()["session_absolute_days"]) * DAY


def idle_seconds() -> int:
    return int(settings()["session_idle_days"]) * DAY


def reset_settings_cache() -> None:
    """Serve ai test e a un eventuale reload esplicito della config."""
    global _settings
    _settings = None


# ── database ──


@contextmanager
def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        DB_PATH,
        timeout=10.0,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row

    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()


_db_ready = False


def _migrate_is_admin() -> None:
    """Aggiunge accounts.is_admin ai database creati prima dei ruoli.

    CREATE TABLE IF NOT EXISTS non aggiunge colonne a una tabella gia'
    esistente, quindi su un auth.db creato dalla versione precedente
    is_admin non ci sarebbe e ogni SELECT su quella colonna fallirebbe.
    """
    with connect() as conn:
        cols = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(accounts)"
            ).fetchall()
        }

        if "is_admin" in cols:
            return

        # DEFAULT 0: gli account gia' presenti sono normali, nessuno
        # diventa admin per effetto di un aggiornamento
        conn.execute(
            "ALTER TABLE accounts "
            "ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
        )


def init_db() -> None:
    global _db_ready

    if _db_ready:
        return

    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                pin_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                iterations INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL
                    REFERENCES accounts(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                device_id TEXT NOT NULL,
                device_label TEXT,
                created_at INTEGER NOT NULL,
                last_used_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                revoked_at INTEGER,
                revoked_by TEXT,
                ip_addr TEXT,
                local_login INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_token
                ON sessions(token_hash);

            CREATE INDEX IF NOT EXISTS idx_sessions_account
                ON sessions(account_id, revoked_at);

            CREATE TABLE IF NOT EXISTS login_attempts (
                key TEXT PRIMARY KEY,
                first_at INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                locked_until INTEGER
            );

            -- il login cerca per nome: due account omonimi
            -- renderebbero il login ambiguo
            CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_name
                ON accounts(name);
            """
        )

    _migrate_is_admin()

    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass

    # SQLite crea anche i file WAL e SHM, che contengono gli stessi
    # hash: senza chmod esplicito resterebbero leggibili a tutti
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chmod(f"{DB_PATH}{suffix}", 0o600)
        except OSError:
            # su filesystem che non supportano i permessi non e' un
            # blocco: nel DB ci sono solo hash, non segreti in chiaro
            pass

    _db_ready = True


# ── PIN ──


def new_salt() -> str:
    return secrets.token_hex(16)


def hash_pin(
    pin: str,
    salt: str,
    iterations: int,
) -> str:
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        pin.encode("utf-8"),
        bytes.fromhex(salt),
        iterations,
    )
    return derived.hex()


def verify_pin(
    pin: str,
    salt: str,
    pin_hash: str,
    iterations: int,
) -> bool:
    candidate = hash_pin(
        pin,
        salt,
        iterations,
    )
    return hmac.compare_digest(
        candidate,
        pin_hash,
    )


# ── indirizzi ──


def is_loopback(host: str | None) -> bool:
    """True solo per 127.0.0.0/8 e ::1 (loopback vero, non 'localhost')."""
    if not host:
        return False

    candidate = host.strip().strip("[]")

    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return False

    if addr.is_loopback:
        return True

    # su un socket dual-stack un client IPv4 arriva come ::ffff:127.0.0.1,
    # e IPv6Address.is_loopback lo considera False
    mapped = getattr(addr, "ipv4_mapped", None)
    return mapped is not None and mapped.is_loopback


# ── modelli ──


@dataclass(frozen=True)
class Account:
    id: int
    name: str
    active: bool
    created_at: int
    last_login_at: int | None
    is_admin: bool = False
    open_sessions: int = 0


@dataclass(frozen=True)
class Session:
    id: int
    account_id: int
    account_name: str
    device_id: str
    device_label: str | None
    created_at: int
    last_used_at: int
    expires_at: int
    ip_addr: str | None
    local_login: bool
    # serve alle pagine per decidere se listare tutti gli account o solo
    # quello corrente, senza una query in piu' per richiesta
    is_admin: bool = False


@dataclass(frozen=True)
class SessionCheck:
    ok: bool
    reason: str | None
    session: Session | None


# ── account ──


def count_active_accounts(
    conn: sqlite3.Connection,
) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM accounts WHERE active = 1"
    ).fetchone()
    return int(row["n"])


def get_account(account_id: int) -> Account | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, name, active, is_admin, created_at "
            "FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()

    if row is None:
        return None

    return Account(
        id=int(row["id"]),
        name=str(row["name"]),
        active=bool(row["active"]),
        created_at=int(row["created_at"]),
        last_login_at=None,
        is_admin=bool(row["is_admin"]),
    )


def list_accounts() -> list[Account]:
    init_db()

    with connect() as conn:
        rows = conn.execute(
            """
SELECT a.id, a.name, a.active, a.is_admin, a.created_at,
                   (
                        SELECT COUNT(*) FROM sessions s
                        WHERE s.account_id = a.id
                          AND s.revoked_at IS NULL
                    ) AS open_sessions,
                   (
                        SELECT MAX(s.created_at) FROM sessions s
                        WHERE s.account_id = a.id
                    ) AS last_login_at
            FROM accounts a
            ORDER BY a.id
            """
        ).fetchall()

    return [
        Account(
            id=int(r["id"]),
            name=str(r["name"]),
            active=bool(r["active"]),
            created_at=int(r["created_at"]),
            last_login_at=(
                int(r["last_login_at"])
                if r["last_login_at"] is not None
                else None
            ),
            is_admin=bool(r["is_admin"]),
            open_sessions=int(r["open_sessions"] or 0),
        )
        for r in rows
    ]


def count_active_admins(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM accounts "
            "WHERE active = 1 AND is_admin = 1"
        ).fetchone()["n"]
    )


def add_account(
    name: str,
    pin: str,
    is_admin: bool = False,
) -> Account:
    """Crea un account. Solleva ValueError se il limite e' raggiunto."""
    clean_name = name.strip()
    clean_pin = pin.strip()

    if not clean_name:
        raise ValueError("Nome account vuoto")

    if not clean_pin:
        raise ValueError("PIN vuoto")

    if len(clean_pin) < 4:
        raise ValueError(
            "PIN troppo corto: usane almeno 4 caratteri"
        )

    init_db()

    salt = new_salt()
    digest = hash_pin(
        clean_pin,
        salt,
        PIN_ITERATIONS,
    )

    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")

        try:
            active = count_active_accounts(conn)
            limit = max_accounts()

            if active >= limit:
                raise ValueError(
                    f"Limite raggiunto: {active}/{limit} account attivi. "
                    "Disattiva o rimuovi un account prima di aggiungerne uno."
                )

            existing = conn.execute(
                "SELECT id FROM accounts WHERE name = ?",
                (clean_name,),
            ).fetchone()

            if existing:
                raise ValueError(
                    f"Account '{clean_name}' gia' esiste"
                )

            cur = conn.execute(
                """
                INSERT INTO accounts
                    (name, pin_hash, salt, iterations,
                     active, is_admin, created_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    clean_name,
                    digest,
                    salt,
                    PIN_ITERATIONS,
                    1 if is_admin else 0,
                    _now(),
                ),
            )

            new_id = int(cur.lastrowid)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    return Account(
        id=new_id,
        name=clean_name,
        active=True,
        created_at=_now(),
        last_login_at=None,
        is_admin=bool(is_admin),
    )


def rename_account(
    account_id: int,
    name: str,
) -> bool:
    """Rinomina un account. Solleva ValueError se il nome e' gia' preso."""
    clean = name.strip()

    if not clean:
        raise ValueError("Nome account vuoto")

    init_db()

    with connect() as conn:
        clash = conn.execute(
            "SELECT id FROM accounts WHERE name = ? AND id != ?",
            (clean, account_id),
        ).fetchone()

        if clash:
            raise ValueError(
                f"Nome '{clean}' gia' usato "
                f"dall'account #{clash['id']}"
            )

        cur = conn.execute(
            "UPDATE accounts SET name = ? WHERE id = ?",
            (clean, account_id),
        )
        return cur.rowcount > 0


def revoke_sessions_for_account(
    account_id: int,
    reason: str,
) -> int:
    """Usata sia da remove_account sia dalla pagina di revoca locale."""
    init_db()

    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE sessions
            SET revoked_at = ?, revoked_by = ?
            WHERE account_id = ? AND revoked_at IS NULL
            """,
            (_now(), reason, account_id),
        )
        return cur.rowcount


def remove_account(account_id: int) -> bool:
    init_db()

    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")

        try:
            row = conn.execute(
                "SELECT id FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()

            if row is None:
                conn.execute("ROLLBACK")
                return False

            conn.execute(
                "DELETE FROM sessions WHERE account_id = ?",
                (account_id,),
            )
            conn.execute(
                "DELETE FROM accounts WHERE id = ?",
                (account_id,),
            )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    return True


def set_account_active(
    account_id: int,
    active: bool,
) -> bool:
    init_db()

    with connect() as conn:
        if not active:
            conn.execute(
                """
                UPDATE sessions
                SET revoked_at = ?, revoked_by = ?
                WHERE account_id = ? AND revoked_at IS NULL
                """,
                (
                    _now(),
                    REASON_ACCOUNT_REMOVED,
                    account_id,
                ),
            )

        cur = conn.execute(
            "UPDATE accounts SET active = ? WHERE id = ?",
            (1 if active else 0, account_id),
        )
        return cur.rowcount > 0


def set_admin(
    account_id: int,
    is_admin: bool,
) -> bool:
    """Promuove o declassa un account.

    Non si può togliere il ruolo all'ultimo admin attivo: senza un
    admin nessuno potrebbe più creare o cancellare account, e l'unica
    via di recupero resterebbe la CLI.
    """
    init_db()

    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")

        try:
            row = conn.execute(
                "SELECT is_admin, active FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()

            if row is None:
                conn.execute("ROLLBACK")
                return False

            was_admin = bool(row["is_admin"])

            if was_admin and not is_admin:
                if count_active_admins(conn) <= 1:
                    raise ValueError(
                        "Non puoi togliere il ruolo admin all'ultimo "
                        "admin attivo: promotesne un altro prima."
                    )

            cur = conn.execute(
                "UPDATE accounts SET is_admin = ? WHERE id = ?",
                (1 if is_admin else 0, account_id),
            )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    return True


def remove_account_guarded(
    account_id: int,
) -> bool:
    """Come remove_account, ma rifiuta di cancellare l'ultimo admin."""
    init_db()

    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")

        try:
            row = conn.execute(
                "SELECT is_admin FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()

            if row is None:
                conn.execute("ROLLBACK")
                return False

            if (
                bool(row["is_admin"])
                and count_active_admins(conn) <= 1
            ):
                raise ValueError(
                    "Non puoi cancellare l'ultimo admin attivo: "
                    "creane un altro prima."
                )

            conn.execute(
                "DELETE FROM sessions WHERE account_id = ?",
                (account_id,),
            )
            conn.execute(
                "DELETE FROM accounts WHERE id = ?",
                (account_id,),
            )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    return True


def change_pin(
    account_id: int,
    pin: str,
) -> bool:
    """Imposta un nuovo PIN a un account esistente."""
    clean_pin = pin.strip()

    if len(clean_pin) < 4:
        raise ValueError(
            "PIN troppo corto: usane almeno 4 caratteri"
        )

    init_db()

    salt = new_salt()
    digest = hash_pin(
        clean_pin,
        salt,
        PIN_ITERATIONS,
    )

    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE accounts
            SET pin_hash = ?, salt = ?, iterations = ?
            WHERE id = ?
            """,
            (
                digest,
                salt,
                PIN_ITERATIONS,
                account_id,
            ),
        )
        return cur.rowcount > 0


# ── rate limiting login ──


def login_locked_until(key: str) -> int:
    """Epoch di fine blocco, o 0 se il client non e' bloccato."""
    with connect() as conn:
        row = conn.execute(
            "SELECT locked_until FROM login_attempts WHERE key = ?",
            (key,),
        ).fetchone()

    if row is None or row["locked_until"] is None:
        return 0

    return int(row["locked_until"])


def record_login_failure(key: str) -> int:
    """Registra un tentativo fallito. Ritorna epoch di fine blocco.

    Nessun return dentro il try: con BEGIN IMMEDIATE esplicitamente
    aperto, un return anticipato lascerebbe la transazione aperta e
    la connessione in chiusura farebbe rollback, perdendo il tentativo.
    """
    now = _now()
    limit = int(settings()["login_max_attempts"])
    lockout = int(settings()["login_lockout_seconds"])

    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")

        try:
            row = conn.execute(
                "SELECT * FROM login_attempts WHERE key = ?",
                (key,),
            ).fetchone()

            if (
                row is None
                or now - int(row["first_at"]) > ATTEMPT_WINDOW
            ):
                conn.execute(
                    """
                    INSERT INTO login_attempts
                        (key, first_at, count, locked_until)
                    VALUES (?, ?, 1, NULL)
                    """,
                    (key, now),
                )
                locked = 0
            else:
                count = int(row["count"]) + 1
                locked = (
                    int(row["locked_until"])
                    if row["locked_until"] is not None
                    else 0
                )

                if count >= limit and locked == 0:
                    locked = now + lockout

                conn.execute(
                    """
                    UPDATE login_attempts
                    SET count = ?, locked_until = ?
                    WHERE key = ?
                    """,
                    (count, locked, key),
                )
        except Exception:
            conn.execute("ROLLBACK")
            raise

        conn.execute("COMMIT")

    return locked


def clear_login_failures(key: str) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM login_attempts WHERE key = ?",
            (key,),
        )


# ── sessioni ──


def new_token() -> tuple[str, str]:
    """Ritorna (token in chiaro da mettere nel cookie, SHA256 del token)."""
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()
    return token, digest


def hash_token(token: str) -> str:
    return hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()


def login(
    name: str,
    pin: str,
    device_label: str,
    ip_addr: str,
    local: bool,
) -> tuple[Account, str, Session] | None:
    """Verifica le credenziali e apre una sessione.

    Ritorna None se le credenziali non sono valide. Se tornano, la
    sessione attiva precedente dello stesso account e' gia' stata revocata:
    un account resta collegato a un solo device alla volta.
    """
    init_db()

    now = _now()
    token, digest = new_token()
    device_id = uuid.uuid4().hex

    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")

        try:
            row = conn.execute(
                """
                SELECT * FROM accounts
                WHERE name = ? AND active = 1
                """,
                (name,),
            ).fetchone()

            if row is None:
                conn.execute("ROLLBACK")
                return None

            if not verify_pin(
                pin,
                str(row["salt"]),
                str(row["pin_hash"]),
                int(row["iterations"]),
            ):
                conn.execute("ROLLBACK")
                return None

            account_id = int(row["id"])

            # un account = una sessione: la precedente muore qui
            conn.execute(
                """
                UPDATE sessions
                SET revoked_at = ?, revoked_by = ?
                WHERE account_id = ? AND revoked_at IS NULL
                """,
                (now, REASON_REPLACED, account_id),
            )

            cur = conn.execute(
                """
                INSERT INTO sessions
                    (account_id, token_hash, device_id,
                     device_label, created_at, last_used_at,
                     expires_at, ip_addr, local_login)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    digest,
                    device_id,
                    device_label[:200],
                    now,
                    now,
                    now + absolute_seconds(),
                    ip_addr,
                    1 if local else 0,
                ),
            )

            session_id = int(cur.lastrowid)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    return (
        Account(
            id=account_id,
            name=str(row["name"]),
            active=True,
            created_at=int(row["created_at"]),
            last_login_at=now,
            is_admin=bool(row["is_admin"]),
        ),
        token,
        Session(
            id=session_id,
            account_id=account_id,
            account_name=str(row["name"]),
            device_id=device_id,
            device_label=device_label[:200],
            created_at=now,
            last_used_at=now,
            expires_at=now + absolute_seconds(),
            ip_addr=ip_addr,
            local_login=local,
            is_admin=bool(row["is_admin"]),
        ),
    )


def _row_to_session(
    row: sqlite3.Row,
) -> Session:
    return Session(
        id=int(row["id"]),
        account_id=int(row["account_id"]),
        account_name=str(row["account_name"]),
        device_id=str(row["device_id"]),
        device_label=(
            str(row["device_label"])
            if row["device_label"] is not None
            else None
        ),
        created_at=int(row["created_at"]),
        last_used_at=int(row["last_used_at"]),
        expires_at=int(row["expires_at"]),
        ip_addr=(
            str(row["ip_addr"])
            if row["ip_addr"] is not None
            else None
        ),
        local_login=bool(row["local_login"]),
        is_admin=bool(row["is_admin"]),
    )


_SESSION_SELECT = """
    SELECT s.id, s.account_id, a.name AS account_name,
           s.device_id, s.device_label, s.created_at,
           s.last_used_at, s.expires_at, s.ip_addr, s.local_login,
           a.is_admin
    FROM sessions s
    JOIN accounts a ON a.id = s.account_id
"""


def touch_session(session_id: int) -> None:
    """Aggiorna il timer di inattivita', non piu' di una volta l'ora."""
    now = _now()

    with connect() as conn:
        conn.execute(
            """
            UPDATE sessions
            SET last_used_at = ?
            WHERE id = ? AND last_used_at <= ?
            """,
            (
                now,
                session_id,
                now - TOUCH_INTERVAL,
            ),
        )


def check_session(
    token: str | None,
) -> SessionCheck:
    """Verifica una sessione per una richiesta HTTP.

    Controlla in ordine: esiste e non e' revocata, scadenza assoluta
    (4 mesi), inattivita' (20 giorni). Se una scadenza cade, la sessione
    viene revocata subito: il client deve rifare il login.
    """
    if not token:
        return SessionCheck(False, "no_token", None)

    init_db()
    now = _now()
    digest = hash_token(token)

    with connect() as conn:
        row = conn.execute(
            _SESSION_SELECT
            + """
            WHERE s.token_hash = ?
              AND s.revoked_at IS NULL
              AND a.active = 1
            """,
            (digest,),
        ).fetchone()

        if row is None:
            return SessionCheck(False, "unknown", None)

        if now >= int(row["expires_at"]):
            conn.execute(
                """
                UPDATE sessions
                SET revoked_at = ?, revoked_by = ?
                WHERE id = ? AND revoked_at IS NULL
                """,
                (now, REASON_ABSOLUTE, int(row["id"])),
            )
            return SessionCheck(
                False,
                REASON_ABSOLUTE,
                None,
            )

        idle = idle_seconds()

        if now - int(row["last_used_at"]) > idle:
            conn.execute(
                """
                UPDATE sessions
                SET revoked_at = ?, revoked_by = ?
                WHERE id = ? AND revoked_at IS NULL
                """,
                (now, REASON_IDLE, int(row["id"])),
            )
            return SessionCheck(
                False,
                REASON_IDLE,
                None,
            )

    return SessionCheck(
        True,
        None,
        _row_to_session(row),
    )


def list_active_sessions() -> list[Session]:
    """Sessioni non revocate e non scadute, per la pagina di revoca."""
    init_db()
    purge_expired()

    with connect() as conn:
        rows = conn.execute(
            _SESSION_SELECT
            + """
            WHERE s.revoked_at IS NULL
            ORDER BY s.last_used_at DESC
            """
        ).fetchall()

    return [_row_to_session(r) for r in rows]


def revoke_session(
    session_id: int,
    reason: str,
) -> bool:
    init_db()

    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE sessions
            SET revoked_at = ?, revoked_by = ?
            WHERE id = ? AND revoked_at IS NULL
            """,
            (_now(), reason, session_id),
        )
        return cur.rowcount > 0


def revoke_all_sessions(reason: str) -> int:
    init_db()

    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE sessions
            SET revoked_at = ?, revoked_by = ?
            WHERE revoked_at IS NULL
            """,
            (_now(), reason),
        )
        return cur.rowcount


def purge_expired() -> int:
    """Ripulisce le sessioni morte da un pezzo, per non far crescere il DB."""
    now = _now()

    with connect() as conn:
        cur = conn.execute(
            """
            DELETE FROM sessions
            WHERE revoked_at IS NOT NULL
              AND revoked_at < ?
            """,
            (now - 30 * DAY,),
        )
        return cur.rowcount


# ── bootstrap ──


def ensure_ready() -> None:
    """Chiamata all'avvio: crea il DB e segnala se mancano gli account."""
    init_db()

    if not list_accounts():
        raise RuntimeError(
            "Nessun account configurato. Crea il primo PIN con:\n"
            "  python tools/accounts.py add <nome>"
        )
