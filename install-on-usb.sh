#!/usr/bin/env bash
#
# install-on-usb.sh — installa KDE Media Remote su questo PC.
#
# Il progetto resta sulla USB: lo prepara e prepara il sistema attorno
# a lui (pacchetti, venv, servizi systemd utente, account, launcher).
# Lanciare dall'intercopia del progetto, MAI con sudo:
#
#   bash install-on-usb.sh
#
# Requisiti gia' presenti sul target: Arch/CachyOS, KDE Plasma,
# sessione grafica con systemd utente, Tailscale (verificato/consigliato
# dallo script).
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/.venv"
LINK="$HOME/kde-media-remote"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/kde-media-remote.service"
YDOTOOL_OVR="$UNIT_DIR/ydotool.service.d/override.conf"

C_CYAN=$'\033[1;36m'
C_GREEN=$'\033[32m'
C_YELLOW=$'\033[33m'
C_RED=$'\033[1;31m'
C_DIM=$'\033[2m'
C_OFF=$'\033[0m'

step() { printf '\n%s==> %s%s\n' "$C_CYAN" "$*" "$C_OFF"; }
ok()   { printf '  %sok%s   %s\n' "$C_GREEN" "$C_OFF" "$*"; }
info() { printf '  %s-%s   %s\n' "$C_DIM" "$C_OFF" "$*"; }
warn() { printf '  %s!%s   %s\n' "$C_YELLOW" "$C_OFF" "$*"; }
die()  { printf '\n%sERRORE:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Uso: bash install-on-usb.sh [--help]

Prepara questo PC per far girare KDE Media Remote dalla USB:
  1. controlla il sistema (Arch/CachyOS, systemd utente, Python)
  2. installa i pacchetti mancanti (ydotool e compagni)
  3. verifica/consiglia Tailscale
  4. crea il link $LINK verso la USB e ricrea il venv
  5. configura i servizi systemd utente (ydotool + kde-media-remote)
  6. crea il primo account admin (PIN)
  7. controlla bindings e PWA, installa il launcher KDE

Lo script e' idempotente: rilanciarlo e' sicuro.
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    "") ;;
    *) usage; exit 1 ;;
esac

ask() {
    local prompt="$1" def="${2:-s}" reply
    if [ "$def" = s ]; then
        read -rp "$prompt [S/n] " reply || reply=""
    else
        read -rp "$prompt [s/N] " reply || reply=""
    fi
    reply="${reply:-$def}"
    case "$reply" in
        s|S|si|SI|y|Y|yes|Yes) return 0 ;;
        *) return 1 ;;
    esac
}

# -------------------------------------------------------------------
step "1/8  Ambiente"
# -------------------------------------------------------------------

[ "$(id -u)" -eq 0 ] && die "lanciare come utente normale, non con sudo: i servizi finirebbero nella home di root."
[ -f "$PROJECT_DIR/run.py" ] && [ -d "$PROJECT_DIR/app" ] \
    || die "questo script deve stare dentro l'intercopia di kde-media-remote (manca run.py o app/)."

command -v pacman >/dev/null 2>&1 \
    || die "serve un sistema Arch/CachyOS (pacman non trovato)."

systemctl --user show-environment >/dev/null 2>&1 \
    || die "sessione systemd utente non attiva: logga in KDE e rilancia."

command -v python3 >/dev/null 2>&1 || info "python3 assente: verra' installato."

if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
        || die "python3 troppo vecchio (serve >= 3.10)."
    ok "sistema: $(. /etc/os-release && echo "$PRETTY_NAME"), $(python3 --version 2>&1)"
else
    ok "sistema: $(. /etc/os-release && echo "$PRETTY_NAME")"
fi
info "progetto: $PROJECT_DIR"

# -------------------------------------------------------------------
step "2/8  Pacchetti di sistema"
# -------------------------------------------------------------------

declare -a REQ_PKGS=()
pacman -Q python >/dev/null 2>&1    || REQ_PKGS+=("python")
pacman -Q python-pip >/dev/null 2>&1 || REQ_PKGS+=("python-pip")
pacman -Q ydotool >/dev/null 2>&1   || REQ_PKGS+=("ydotool")

if [ "${#REQ_PKGS[@]}" -gt 0 ]; then
    info "mancano: ${REQ_PKGS[*]}"
    sudo pacman -S --needed "${REQ_PKGS[@]}" \
        || die "installazione pacchetti fallita."
else
    ok "pacchetti base gia' presenti (python, python-pip, ydotool)"
fi

declare -a OPT_PKGS=()
for p in brightnessctl playerctl cec-utils; do
    pacman -Q "$p" >/dev/null 2>&1 || OPT_PKGS+=("$p")
done

if [ "${#OPT_PKGS[@]}" -gt 0 ]; then
    info "comandi opzionali di config/commands.toml: ${OPT_PKGS[*]}"
    info "(luminosita', tasti media, switching HDMI CEC — senza restano non disponibili)"
    if ask "Installo anche questi?"; then
        sudo pacman -S --needed "${OPT_PKGS[@]}" || warn "installazione opzionali fallita, continuo."
    fi
fi

command -v ydotool >/dev/null 2>&1 \
    || die "ydotool non disponibile: il remote non potra' muovere mouse/tasti."

# -------------------------------------------------------------------
step "3/8  Tailscale"
# -------------------------------------------------------------------

if ! pacman -Q tailscale >/dev/null 2>&1; then
    warn "tailscale non installato: dal telefono non si arrivera' a questo PC."
    if ask "Installo e attivo tailscale?"; then
        sudo pacman -S --needed tailscale
        sudo systemctl enable --now tailscaled.service
        warn "poi esegui: sudo tailscale up   (e completa il login)"
    fi
else
    ok "tailscale installato"
    if command -v tailscale >/dev/null 2>&1 \
        && tailscale status --json 2>/dev/null | grep -q '"BackendState": "Running"'; then
        ok "tailscale connesso: $(tailscale ip -4 2>/dev/null | head -n1 || echo 'IP non disponibile')"
    else
        warn "tailscale non connesso: esegui 'tailscale up'."
        info "il server comunque parte (in attesa fino a 30s all'avvio), ma il telefono non vede la porta."
    fi
fi

# -------------------------------------------------------------------
step "4/8  Link stabile + ambiente Python"
# -------------------------------------------------------------------

# Il servizio punta a $LINK invece che al mount point della USB, che
# potrebbe cambiare tra un riavvio e l'altro.
if [ -L "$LINK" ]; then
    ln -sfn "$PROJECT_DIR" "$LINK"
    ok "link aggiornato: $LINK -> $PROJECT_DIR"
    UNIT_ROOT="$LINK"
elif [ -e "$LINK" ]; then
    warn "$LINK esiste ed e' una cartella reale: la lascio intatta."
    warn "il servizio usera' il path diretto."
    UNIT_ROOT="$PROJECT_DIR"
else
    ln -s "$PROJECT_DIR" "$LINK"
    ok "link creato: $LINK -> $PROJECT_DIR"
    UNIT_ROOT="$LINK"
fi

if [ -x "$VENV/bin/python" ] \
    && "$VENV/bin/python" -c "import fastapi, uvicorn, tomlkit" >/dev/null 2>&1; then
    ok "venv gia' pronto: $VENV"
else
    if ask "venv assente o incompleto. Lo creo (python -m venv + pip install)?"; then
        command -v python3 >/dev/null 2>&1 || die "python3 non disponibile."
        rm -rf "$VENV"
        python3 -m venv "$VENV" || die "creazione venv fallita."
        "$VENV/bin/pip" install --quiet --upgrade pip \
            || warn "aggiornamento pip fallito, continuo."
        "$VENV/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt" \
            || die "pip install -r requirements.txt fallito."
        ok "venv creato e dipendenze installate"
    else
        die "senza venv il server non parte."
    fi
fi

# -------------------------------------------------------------------
step "5/8  Servizi systemd utente"
# -------------------------------------------------------------------

# ydotool: drop-in con RestartSec (come sulla macchina principale)
if [ ! -f "$YDOTOOL_OVR" ]; then
    mkdir -p "$(dirname "$YDOTOOL_OVR")"
    printf '[Service]\nRestartSec=5\n' > "$YDOTOOL_OVR"
    ok "drop-in creato: $YDOTOOL_OVR"
fi

systemctl --user daemon-reload

if systemctl --user enable --now ydotool.service >/dev/null 2>&1; then
    ok "ydotool.service attivo"
else
    warn "ydotool.service non avviato: $(systemctl --user is-active ydotool.service 2>/dev/null || true)"
fi

if [ ! -e /dev/uinput ]; then
    warn "/dev/uinput assente: rigenera initramfs o controlla il modulo uinput."
elif [ ! -r /dev/uinput ] || [ ! -w /dev/uinput ]; then
    warn "/dev/uinput non accessibile dall'utente: serve la regola udev di ydotool (pacchetto Arch) e un re-login."
fi

# Servizio del remote: path via $LINK per restare valido se la USB
# viene rimontata in un punto diverso.
mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<EOF
[Unit]
Description=KDE Media Remote
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$UNIT_ROOT
ExecStart=$UNIT_ROOT/.venv/bin/python $UNIT_ROOT/run.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable kde-media-remote.service >/dev/null 2>&1
ok "servizio scritto: $UNIT (WorkingDirectory=$UNIT_ROOT)"

# -------------------------------------------------------------------
step "6/8  Account (PIN)"
# -------------------------------------------------------------------

PORT="$(sed -n 's/^[[:space:]]*port[[:space:]]*=[[:space:]]*\([0-9]\+\).*/\1/p' "$PROJECT_DIR/config/config.toml" | head -n1)"
PORT="${PORT:-8765}"

ACCT_OUT="$("$VENV/bin/python" "$PROJECT_DIR/tools/accounts.py" list 2>/dev/null || true)"

if grep -q "Nessun account" <<<"$ACCT_OUT"; then
    info "nessun account presente: creo il primo admin."
else
    info "account gia' presenti su questa copia (dati/auth.db):"
    sed -n '2,10p' <<<"$ACCT_OUT" | sed 's/^/      /'
    if ! ask "Mantenerli? (no = li sposto da parte e ne creo uno nuovo)"; then
        bak="$PROJECT_DIR/data/auth.db.bak-$(date +%Y%m%d-%H%M%S)"
        mv "$PROJECT_DIR/data/auth.db" "$bak"
        ok "vecchio database spostato in $bak"
        ACCT_OUT="Nessun account."
    fi
fi

if grep -q "Nessun account" <<<"$ACCT_OUT"; then
    read -rp "Nome del primo account admin: " ACCT_NAME || die "input interrotto."
    [ -n "$ACCT_NAME" ] || die "nome vuoto."
    read -rs -p "PIN: " PIN1; printf '\n'
    read -rs -p "Conferma PIN: " PIN2; printf '\n'
    [ -n "$PIN1" ] || die "PIN vuoto."
    [ "$PIN1" = "$PIN2" ] || die "i due PIN non coincidono."
    printf '%s\n' "$PIN1" \
        | "$VENV/bin/python" "$PROJECT_DIR/tools/accounts.py" add "$ACCT_NAME" --admin --pin-on-stdin
    unset PIN1 PIN2
    ok "account admin creato"
else
    ok "account esistenti conservati"
fi

# -------------------------------------------------------------------
step "7/8  Bindings, PWA, launcher"
# -------------------------------------------------------------------

# bindings.toml: crea dal template se mancano, avvisa su path morti
BINDER="$PROJECT_DIR/config/bindings.toml"
if [ ! -f "$BINDER" ]; then
    cp "$PROJECT_DIR/config/bindings-template.toml" "$BINDER"
    ok "config/bindings.toml creato dal template portabile"
else
    BINDING_ISSUES=0
    while IFS= read -r icon; do
        case "$icon" in
            /*)
                if [ ! -e "$icon" ]; then
                    warn "icona inesistente su questo PC: $icon"
                    BINDING_ISSUES=1
                fi
                ;;
        esac
    done < <(sed -n 's/^icon[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$BINDER")

    while IFS= read -r desk; do
        if [ ! -e "$HOME/.local/share/applications/$desk" ] \
            && [ ! -e "/usr/share/applications/$desk" ] \
            && [ ! -e "/var/lib/flatpak/exports/share/applications/$desk" ] \
            && [ ! -e "$HOME/.local/share/flatpak/exports/share/applications/$desk" ]; then
            warn ".desktop non trovato su questo PC: $desk"
            BINDING_ISSUES=1
        fi
    done < <(sed -n 's/^desktop[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$BINDER")

    if [ "$BINDING_ISSUES" -eq 0 ]; then
        ok "config/bindings.toml: icone e .desktop tutti validi"
    else
        warn "bindings con riferimenti mancanti: il fallback nome+colore funziona comunque."
        info "modifica $BINDER a piacimento."
    fi
fi

# PWA doppia: servono manifest-keyboard + icone dedicate
if [ -f "$PROJECT_DIR/web/manifest-keyboard.webmanifest" ] \
    && [ -f "$PROJECT_DIR/web/icon-keyboard-192.png" ] \
    && [ -f "$PROJECT_DIR/web/icon-keyboard-512.png" ]; then
    ok "PWA doppia presente (remote + tastiera con manifest/icona propri)"
else
    warn "manca web/manifest-keyboard.webmanifest o le icone: copia non aggiornata?"
fi

if ask "Installo anche il launcher KDE (./install-desktop.sh)?"; then
    bash "$PROJECT_DIR/install-desktop.sh"
fi

# -------------------------------------------------------------------
step "8/8  Avvio e verifica"
# -------------------------------------------------------------------

systemctl --user restart kde-media-remote.service
ok "servizio riavviato (kde-media-remote.service)"

info "attendo l'ascolto sulla porta $PORT (fino a 40s: Tailscale puo' ritardare)..."
LISTENING=0
for _ in $(seq 1 40); do
    if ss -tln "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
        LISTENING=1
        break
    fi
    sleep 1
done

if [ "$LISTENING" -eq 1 ]; then
    SOCKETS="$(ss -tln "sport = :$PORT" 2>/dev/null | grep -c LISTEN || true)"
    ok "porta $PORT in ascolto ($SOCKETS socket)"
else
    warn "porta non ancora in ascolto. Diagnostica:"
    info "systemctl --user status kde-media-remote"
    info "journalctl --user -u kde-media-remote -f"
fi

TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"

# -------------------------------------------------------------------
printf '\n%s=== Installazione completata%s\n' "$C_CYAN" "$C_OFF"
cat <<EOF

  Telefono (fallback HTTP via Tailscale):
    http://${TS_IP:-<tailscale-ip>}:$PORT/
    http://${TS_IP:-<tailscale-ip>}:$PORT/keyboard

  PWA installabile (serve HTTPS, vedi README "Nginx Proxy Manager"):
    https://<tuo-dominio>/            -> "KDE Media Remote"
    https://<tuo-dominio>/keyboard    -> "KDE Keyboard Remote"

  Restart:    systemctl --user restart kde-media-remote
  Log:        journalctl --user -u kde-media-remote -f
  Account:    $VENV/bin/python $PROJECT_DIR/tools/accounts.py list

  Note:
   - il servizio parte dal link $LINK: se la USB
     viene montata in un altro punto, rilancia questo script per
     aggiornare il link (o aggiornalo a mano con ln -sfn).
   - per far partire il servizio al boot anche senza login grafico:
     sudo loginctl enable-linger $USER
EOF
