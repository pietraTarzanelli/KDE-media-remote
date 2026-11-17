#!/usr/bin/env python3
"""Avvia il server del telecomando.

Ascolta su piu' indirizzi invece che su uno solo: loopback per il
pannello di revoca e il browser sul portatile, interfaccia Tailscale per
il telefono. Stessa porta di sempre, HTTP semplice, niente
tailscale serve: la LAN semplicemente non ha socket su cui collegarsi.

Configurazione in config/config.toml, sezione [server]:

    bind = ["loopback", "tailscale"]

Override d'ambiente (una lista separata da virgole o spazi):

    KMR_BIND="loopback,tailscale" KMR_PORT=8765 python run.py
"""

import os
import sys
import time

import uvicorn

from app.config import main_config
from app import netbind


def fail(message: str) -> None:
    print(
        f"[server] {message}",
        file=sys.stderr,
        flush=True,
    )
    sys.exit(1)


def parse_targets(raw) -> list[str]:
    """Accetta sia una lista TOML sia una stringa tipo KMR_BIND."""
    if raw is None:
        return []

    if isinstance(raw, str):
        return [t for t in raw.replace(",", " ").split() if t]

    return [str(t) for t in raw]


def wait_for_addresses(
    targets: list[str],
    timeout: float = 30.0,
    interval: float = 1.0,
):
    """Aspetta che tutti i target richiesti risolvano.

    'tailscale' e' l'unico che al boot puo' non essere ancora pronto:
    tailscaled annuncia l'interfaccia qualche secondo dopo il login.
    Senza questa attesa il server si aprirebbe sul solo loopback e il
    telefono resterebbe fuori, senza che nulla lo segnali.

    Ritorna (indirizzi, target_senza_indirizzo). Non e' un'errore: se
    'tailscale' manca del tutto si parte lo stesso, con un avviso, cosi'
    il portatile resta comunque raggiungibile.
    """
    deadline = time.monotonic() + timeout
    announced = False

    while True:
        # una sola passata: se 'addresses' e 'missing' venissero calcolati
        # con due chiamate separate a resolve(), Tailscale potrebbe
        # arrivare proprio in mezzo e il server partirebbe sul solo
        # loopback senza nemmeno accorgersene
        per_target = [(t, netbind.resolve(t)) for t in targets]
        addresses = netbind.flatten(per_target)
        missing = [t for t, found in per_target if not found]

        if not missing or time.monotonic() >= deadline:
            return addresses, missing

        if not announced:
            print(
                f"[server] aspetto Tailscale "
                f"({', '.join(missing)}) prima di aprire la porta...",
                flush=True,
            )
            announced = True

        time.sleep(interval)


cfg = main_config()["server"]
port = int(os.environ.get("KMR_PORT") or cfg["port"])
targets = parse_targets(
    os.environ.get("KMR_BIND") or cfg.get("bind")
)

if not targets:
    fail(
        "config/config.toml: [server] bind e' vuoto.\n"
        '        Deve essere una lista, es. bind = ["loopback", "tailscale"]'
    )

if any(
    t.strip().lower() in netbind.WILDCARD_NAMES
    for t in targets
):
    print(
        f"[server] ATTENZIONE: bind={targets} apre la porta su TUTTE le\n"
        "          interfacce, quindi anche alla LAN e al Wi-Fi pubblico.\n"
        "          Per il solo telefono usa [\"loopback\", \"tailscale\"].",
        flush=True,
    )

# Tailscale spesso non e' ancora in ascolto quando parte il servizio:
# al boot l'utente e' gia' collegato ma tailscale0 non ha ancora l'IP, e
# senza aspettare il server si aprirebbe sul solo loopback. In quel caso
# il telefono non raggiunge nulla e sembra che l'app sia rotta.
addresses, missing = wait_for_addresses(targets)

if missing:
    print(
        f"[server] ATTENZIONE: nessun indirizzo per {missing}.\n"
        "          Il telefono non potra' collegarsi finche' Tailscale\n"
        "          non e' collegato. Controlla con 'tailscale status'.",
        flush=True,
    )

sockets = netbind.create_sockets(addresses, port)

if not sockets:
    fail(
        f"nessun indirizzo utilizzabile per {targets} sulla porta {port}.\n"
        "        Tailscale e' collegato? Prova 'tailscale status'."
    )

print(
    f"[server] telecomando su porta {port}, "
    f"indirizzi: {', '.join(a for _, a, _ in addresses)}",
    flush=True,
)

config = uvicorn.Config(
    "app.main:app",
    port=port,
    reload=False,
    # Non ci sono proxy davanti al server: la lettura di X-Forwarded-For
    # viene gestita esplicitamente in app/auth_routes.py e accettata
    # solo se la connessione viene da proxy dichiarati in config.
    proxy_headers=False,
)

uvicorn.Server(config).run(sockets=sockets)
