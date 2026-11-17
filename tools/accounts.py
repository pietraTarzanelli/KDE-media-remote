#!/usr/bin/env python3
"""Gestione degli account (PIN) di KDE Media Remote.

Gli account vivono in data/auth.db, non in config/: cosi' rimuovere un
account revoca anche tutte le sue sessioni in un colpo solo. I PIN non
sono mai salvati in chiaro, solo PBKDF2 + salt.

Comandi:

    python tools/accounts.py list
    python tools/accounts.py add <nome> [--admin]  chiede il PIN
    python tools/accounts.py admin <id> [--revoke] ruolo admin
    python tools/accounts.py remove <id>
    python tools/accounts.py rename <id> <nuovo nome>
    python tools/accounts.py pin <id>         cambia il PIN
    python tools/accounts.py disable <id>     disattiva, libera uno slot
    python tools/accounts.py enable <id>
    python tools/accounts.py sessions          device collegati adesso
    python tools/accounts.py revoke-all       scollega tutti i device

L'account admin e' quello che dal sito (/auth/accounts) puo' creare,
cancellare e cambiare il PIN di tutti gli altri. Un account normale
cambia solo il proprio PIN. Per il primo account:

    python tools/accounts.py add pietro --admin

Il PIN viene chiesto con getpass: non finisce nella cronologia della
shell ne' nella lista dei processi. Se ti serve da script, passa
--pin-on-stdin e leggilo da stdin.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

sys.path.insert(
    0,
    str(
        Path(__file__)
        .resolve()
        .parent.parent
    ),
)

from app import auth  # noqa: E402


def ask_pin(args, confirm: bool) -> str:
    if args.pin_on_stdin:
        pin = sys.stdin.readline().rstrip("\n")
    else:
        pin = getpass.getpass("PIN: ")

        if confirm:
            again = getpass.getpass(
                "Conferma PIN: "
            )
            if pin != again:
                sys.exit("I due PIN non coincidono.")

    if not pin.strip():
        sys.exit("PIN vuoto.")

    return pin


def cmd_list(args) -> None:
    accounts = auth.list_accounts()
    sessions = {
        s.account_id: s
        for s in auth.list_active_sessions()
    }

    if not accounts:
        print("Nessun account. Creane uno:")
        print("  python tools/accounts.py add <nome>")
        return

    print(
        f"{'ID':>3}  {'RUOLO':<7} {'ATTIVO':<7} "
        f"{'SESSIONE':<9} {'ACCESSO':<17} NOME"
    )
    print("-" * 66)

    for a in accounts:
        session = sessions.get(a.id)
        flag = "si" if a.active else "no"
        role = "admin" if a.is_admin else "utente"
        state = "collegato" if session else "-"
        when = (
            format_ts(session.created_at)
            if session
            else "-"
        )
        print(
            f"{a.id:>3}  {role:<7} {flag:<7} {state:<9} "
            f"{when:<17} {a.name}"
        )

    active = sum(1 for a in accounts if a.active)
    admins = sum(
        1 for a in accounts if a.active and a.is_admin
    )
    print()
    print(
        f"{active}/{auth.max_accounts()} account attivi, "
        f"{admins} admin, {len(sessions)} sessioni aperte."
    )

    if not admins:
        print()
        print(
            "Nessun admin: dal sito non si possono creare o\n"
            "cancellare account. Promuovine uno con:\n"
            "  python tools/accounts.py admin <id>"
        )


def cmd_add(args) -> None:
    pin = ask_pin(args, confirm=True)
    account = auth.add_account(
        args.name,
        pin,
        is_admin=args.admin,
    )
    print(
        f"Creato l'account #{account.id} '{account.name}'"
        + (" (admin)." if args.admin else ".")
    )


def cmd_remove(args) -> None:
    if not auth.get_account(args.id):
        sys.exit(f"Nessun account con id {args.id}.")

    if not args.yes:
        try:
            answer = input(
                f"Eliminare davvero l'account #{args.id} "
                "(e tutte le sue sessioni)? [s/N] "
            )
        except EOFError:
            answer = ""

        if answer.strip().lower() not in ("s", "si", "y", "yes"):
            print("Annullato.")
            return

    try:
        removed = auth.remove_account_guarded(args.id)
    except ValueError as e:
        sys.exit(str(e))

    if removed:
        print(f"Account #{args.id} rimosso.")
    else:
        sys.exit(f"Nessun account con id {args.id}.")


def cmd_admin(args) -> None:
    """Promuove o declassa un account a admin."""
    account = auth.get_account(args.id)

    if account is None:
        sys.exit(f"Nessun account con id {args.id}.")

    want_admin = not args.revoke

    if account.is_admin == want_admin:
        stato = "gia' admin" if want_admin else "gia' utente"
        print(f"L'account #{args.id} e' {stato}.")
        return

    try:
        auth.set_admin(args.id, not args.revoke)
    except ValueError as e:
        sys.exit(str(e))

    if args.revoke:
        print(f"L'account #{args.id} non e' piu' admin.")
    else:
        print(
            f"L'account #{args.id} '{account.name}' e' ora admin:\n"
            "  puo' gestire tutti gli account dal sito."
        )


def cmd_rename(args) -> None:
    if not auth.get_account(args.id):
        sys.exit(f"Nessun account con id {args.id}.")

    auth.rename_account(args.id, args.name)
    print(
        f"Account #{args.id} rinominato in "
        f"'{args.name.strip()}'."
    )


def cmd_pin(args) -> None:
    if not auth.get_account(args.id):
        sys.exit(f"Nessun account con id {args.id}.")

    pin = ask_pin(args, confirm=True)
    auth.revoke_sessions_for_account(
        args.id,
        auth.REASON_ACCOUNT_REMOVED,
    )

    auth.change_pin(args.id, pin)
    print(
        f"PIN dell'account #{args.id} cambiato. "
        "I device collegati sono stati disconnessi."
    )


def cmd_disable(args) -> None:
    if not auth.set_account_active(args.id, False):
        sys.exit(f"Nessun account con id {args.id}.")

    print(
        f"Account #{args.id} disattivato e disconnesso. "
        "Il suo PIN non e' piu' valido."
    )


def cmd_enable(args) -> None:
    with auth.connect() as conn:
        count = auth.count_active_accounts(conn)

        if count >= auth.max_accounts():
            sys.exit(
                f"Limite di {auth.max_accounts()} account attivi "
                "raggiunto: disattiva qualcuno prima."
            )

    if not auth.set_account_active(args.id, True):
        sys.exit(f"Nessun account con id {args.id}.")

    print(f"Account #{args.id} riattivato.")


def cmd_sessions(args) -> None:
    sessions = auth.list_active_sessions()

    if not sessions:
        print("Nessun device collegato.")
        return

    print(
        f"{'ID':>3}  {'ACCOUNT':<12} {'DOVE':<7} "
        f"{'DEVICE':<12} {'ULTIMO USO':<17} IP"
    )
    print("-" * 70)

    for s in sessions:
        dove = "locale" if s.local_login else "remoto"
        device = s.device_label or "-"
        print(
            f"{s.id:>3}  {s.account_name:<12} {dove:<7} "
            f"{device:<12} {format_ts(s.last_used_at):<17} "
            f"{s.ip_addr or '-'}"
        )


def cmd_revoke_all(args) -> None:
    count = auth.revoke_all_sessions(
        auth.REASON_LOCAL_REVOKE
    )
    print(f"Revocate {count} sessioni.")
    print(
        "Ogni device dovra' fare di nuovo il login."
    )


def format_ts(ts: int | None) -> str:
    if not ts:
        return "-"

    return time.strftime(
        "%d/%m/%Y %H:%M",
        time.localtime(ts),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Gestisci gli account di KDE Media Remote.",
    )
    sub = parser.add_subparsers(
        dest="command",
        required=True,
    )

    def with_pin(p):
        p.add_argument(
            "--pin-on-stdin",
            action="store_true",
            help="leggi il PIN da stdin invece di chiederlo",
        )
        return p

    p = sub.add_parser("list", help="elenca gli account")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("add", help="crea un account")
    p.add_argument("name", help="nome dell'account")
    p.add_argument(
        "--admin",
        action="store_true",
        help="l'account puo' gestire tutti gli account dal sito",
    )
    with_pin(p)
    p.set_defaults(func=cmd_add)

    p = sub.add_parser(
        "admin",
        help="promuove un account a admin (o il contrario)",
    )
    p.add_argument("id", type=int)
    p.add_argument(
        "--revoke",
        action="store_true",
        help="togli il ruolo admin invece di darlo",
    )
    p.set_defaults(func=cmd_admin)

    p = sub.add_parser("remove", help="elimina un account")
    p.add_argument("id", type=int)
    p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="non chiedere conferma",
    )
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("rename", help="cambia il nome")
    p.add_argument("id", type=int)
    p.add_argument("name")
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("pin", help="cambia il PIN")
    p.add_argument("id", type=int)
    with_pin(p)
    p.set_defaults(func=cmd_pin)

    p = sub.add_parser("disable", help="disattiva un account")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_disable)

    p = sub.add_parser("enable", help="riattiva un account")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_enable)

    p = sub.add_parser(
        "sessions",
        help="device collegati in questo momento",
    )
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser(
        "revoke-all",
        help="scollega tutti i device",
    )
    p.set_defaults(func=cmd_revoke_all)

    return parser


def main() -> None:
    args = build_parser().parse_args()

    try:
        args.func(args)
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
