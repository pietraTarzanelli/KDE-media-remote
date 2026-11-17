"""Dove mettere in ascolto il server.

L'obiettivo e' restare su HTTP e sulla stessa porta di prima, ma
raggiungibili solo da chi e' in casa o nella tailnet. Si risolve
ascoltando su piu' socket invece che su uno solo:

- loopback    -> il pannello di revoca e il browser sul portatile
- tailscale   -> il telefono, via IP o MagicDNS, in HTTP semplice
- 0.0.0.0     -> tutta la LAN, perche' sia cosi' e' pericoloso

Nessun socket sulla LAN vuol dire che la LAN non ha nulla su cui
colpirsi: niente firewall da configurare, niente regole che invecchiano.
L'unica cosa che cambia se Tailscale riassegna l'IP e' che va riavviato
il server, che comunque riparte col boot.

Gli indirizzi delle interfacce si leggono con ioctl e /proc: nessun
subprocess, quindi funziona anche dal launcher .desktop dove PATH e'
minimalista.
"""

from __future__ import annotations

import fcntl
import ipaddress
import socket
import struct
from collections.abc import Callable, Iterable

SIOCGIFADDR = 0x8915
SIOCGIFINDEX = 0x8933

# IFNAMSIZ - 1: il nome interfaccia nel pacchetto e' un array di 16 byte
IF_NAMES = 15

PROC_IF_INET6 = "/proc/net/if_inet6"

LOOPBACK_NAMES = frozenset({
    "loopback",
    "localhost",
    "local",
    "127.0.0.1",
    "::1",
})

WILDCARD_NAMES = frozenset({
    "all",
    "any",
    "0.0.0.0",
    "::",
})

TAILSCALE_NAMES = frozenset({
    "tailscale",
    "tailscale0",
    "ts",
})

# il nome dell'interfaccia cambia fra le versioni di Tailscale
TAILSCALE_INTERFACES = ("tailscale0", "tailscale", "ts0")

Address = tuple[int, str, int]


def _ifreq(name: str) -> bytes:
    encoded = name.encode("ascii", "replace")[:IF_NAMES]
    return struct.pack("256s", encoded.ljust(IF_NAMES + 1, b"\0"))


def interface_index(name: str) -> int | None:
    try:
        with socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
        ) as probe:
            packed = fcntl.ioctl(
                probe.fileno(),
                SIOCGIFINDEX,
                _ifreq(name),
            )
    except OSError:
        return None

    return struct.unpack("I", packed[16:20])[0]


def interface_ipv4(name: str) -> str | None:
    """IPv4 di un'interfaccia via ioctl SIOCGIFADDR."""
    try:
        with socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
        ) as probe:
            packed = fcntl.ioctl(
                probe.fileno(),
                SIOCGIFADDR,
                _ifreq(name),
            )
    except OSError:
        return None

    return socket.inet_ntoa(packed[20:24])


def interface_ipv6(name: str) -> str | None:
    """IPv6 globale (no link-local) di un'interfaccia.

    Il formato di /proc/net/if_inet6 e' variabile fra versioni di
    kernel, quindi non si contano le posizioni: il nome interfaccia e'
    l'ultimo campo (i nomi possono contenere spazi) e l'unico campo di
    cui ci fidiamo e' l'indirizzo iniziale. L'ifindex, che serve come
    scope_id, lo prendo dall'ioctl: e' il valore che `ip -j` riporta.

    L'indirizzo arriva come 8 parole da 32 bit in ordine di rete,
    senza due punti: l'intero che ne esce e' esattamente quello che
    IPv6Address si aspetta.
    """
    try:
        with open(
            PROC_IF_INET6,
            encoding="ascii",
        ) as handle:
            rows = handle.read().splitlines()
    except OSError:
        return None

    for row in rows:
        fields = row.split()

        if len(fields) < 5 or fields[-1] != name:
            continue

        raw = fields[0].lower()

        # link-local fe80::/10 e multicast ff00::/8 non servono:
        # il telefono li raggiunge comunque via IPv4
        if raw.startswith(("fe8", "fe9", "fea", "feb", "ff")):
            continue

        try:
            return str(ipaddress.IPv6Address(int(raw, 16)))
        except ValueError:
            continue

    return None


def family_of(address: str) -> int:
    """AF_INET, AF_INET6, oppure AF_UNSPEC se e' un hostname."""
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, address)
            return family
        except OSError:
            continue

    return socket.AF_UNSPEC


def resolve(target: str) -> list[Address]:
    """Traduce un token di bind in (famiglia, indirizzo, scope_id)."""
    token = target.strip().lower()

    if not token:
        return []

    if token in WILDCARD_NAMES:
        return [(socket.AF_INET, "0.0.0.0", 0)]

    if token in LOOPBACK_NAMES:
        # anche ::1: i browser su localhost provano IPv6 per primo
        return [
            (socket.AF_INET, "127.0.0.1", 0),
            (socket.AF_INET6, "::1", 0),
        ]

    if token in TAILSCALE_NAMES:
        found: list[Address] = []

        for name in TAILSCALE_INTERFACES:
            index = interface_index(name)

            if index is None:
                continue

            v4 = interface_ipv4(name)
            if v4:
                found.append((socket.AF_INET, v4, 0))

            v6 = interface_ipv6(name)
            if v6:
                found.append(
                    (socket.AF_INET6, v6, index)
                )

        return found

    return [(family_of(token), token, 0)]


def flatten(pairs: Iterable[tuple[str, list[Address]]]) -> list[Address]:
    """appiattisce [(token, indirizzi)], togliendo i duplicati e
    mantenendo l'ordine dei token."""
    seen: set[tuple[int, str, int]] = set()
    out: list[Address] = []

    for _, addresses in pairs:
        for address in addresses:
            if address in seen:
                continue
            seen.add(address)
            out.append(address)

    return out


def resolve_all(targets: Iterable[str]) -> list[Address]:
    """appiattisce i token, togliendo i duplicati e mantenendo l'ordine."""
    return flatten([(t, resolve(t)) for t in targets])


def _hostname_pairs(
    host: str,
    port: int,
) -> list[tuple[int, str]]:
    try:
        infos = socket.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        return []

    pairs: list[tuple[int, str]] = []

    for family, _, _, _, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        pairs.append((family, sockaddr[0]))

    return pairs


def _bind(
    family: int,
    address: str,
    port: int,
    scope_id: int,
    log: Callable[[str], None],
) -> socket.socket | None:
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError as exc:
        log(f"[bind] {address}: socket non creato ({exc})")
        return None

    try:
        sock.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_REUSEADDR,
            1,
        )

        if family == socket.AF_INET6:
            sock.setsockopt(
                socket.IPPROTO_IPV6,
                socket.IPV6_V6ONLY,
                1,
            )

        if family == socket.AF_INET6:
            sock.bind((address, port, scope_id))
        else:
            sock.bind((address, port))

        sock.listen(socket.SOMAXCONN)
        sock.set_inheritable(True)
    except OSError as exc:
        log(f"[bind] {address}:{port} non disponibile ({exc})")
        sock.close()
        return None

    log(f"[bind] in ascolto su {address}:{port}")
    return sock


def create_sockets(
    addresses: Iterable[Address],
    port: int,
    log: Callable[[str], None] = print,
) -> list[socket.socket]:
    """Apre un socket per ogni indirizzo.

    Un indirizzo che non si puo' usare (per esempio l'IP Tailscale
    quando il tunnel non e' ancora salito) viene saltato con un
    messaggio: e' normale al boot. Solo se non resta niente il server
    non ha dove stare e va segnalato.
    """
    sockets: list[socket.socket] = []

    for family, address, scope_id in addresses:
        pairs = (
            _hostname_pairs(address, port)
            if family == socket.AF_UNSPEC
            else [(family, address)]
        )

        if not pairs:
            log(f"[bind] {address}: nome non risolvibile")
            continue

        for pair_family, pair_address in pairs:
            sock = _bind(
                pair_family,
                pair_address,
                port,
                scope_id,
                log,
            )
            if sock is not None:
                sockets.append(sock)

    return sockets
