#!/usr/bin/env bash
# Set up a fresh Ubuntu 24.04 LTS droplet for the GLEN private preview.
#
# Run as root on the droplet, from a copy of glen-explorer/deploy/ that has
# glen-explorer/requirements.lock.txt beside it (the runbook copies both):
#
#     bash /root/glen-setup/deploy/bootstrap.sh
#
# Safe to run again: every step checks what is already there. It never
# overwrites /etc/glen/glen-preview.env or the tunnel token, never starts the
# app or the tunnel before they are configured, and never opens a web port.
# Cloudflare Tunnel connects outbound, so the firewall allows inbound SSH only.
#
# It installs no keys and buys nothing. See docs/preview-runbook.md.

set -Eeuo pipefail
trap 'echo "bootstrap.sh: failed at line $LINENO: $BASH_COMMAND" >&2' ERR

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQUIREMENTS="${GLEN_REQUIREMENTS:-$DEPLOY_DIR/../requirements.lock.txt}"
PYTHON_VERSION="${GLEN_PYTHON_VERSION:-3.14}"   # what the lock file and CI were verified with
PYTHON="python$PYTHON_VERSION"

# Layout. glen-preview.service and fix-permissions.sh use the same paths.
SERVICE_USER=glen
VENV=/opt/glen/venv
RELEASES=/srv/glen/releases          # build_preview_release.py outputs, one folder each
RUNTIME=/srv/glen/runtime            # code checkout at the release commit; data paths are symlinks
PRIVATE=/srv/glen/private            # private data at repo-relative paths (push_preview_runtime.py)
INCOMING=/srv/glen/incoming          # rsync staging
STATE_ROOT=/var/lib/glen             # the only directory the app may write
STATE="$STATE_ROOT/state"            # GLEN_PREVIEW_STATE: usage.sqlite3, STOP_ANALYST
ETC=/etc/glen
ENV_FILE="$ETC/glen-preview.env"
TUNNEL_ENV=/etc/cloudflared/glen-tunnel.env

log() { printf '\n== %s\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "Run as root (sudo bash $0)." >&2; exit 1; }
# shellcheck source=/dev/null
. /etc/os-release
if [[ "${ID:-}" != ubuntu || "${VERSION_ID:-}" != 24.04 ]] && [[ "${GLEN_ALLOW_OTHER_OS:-}" != 1 ]]; then
    echo "This script targets Ubuntu 24.04 LTS; found ${PRETTY_NAME:-unknown}." >&2
    echo "Set GLEN_ALLOW_OTHER_OS=1 to try anyway." >&2
    exit 1
fi
[[ -f "$REQUIREMENTS" ]] || { echo "Missing $REQUIREMENTS (copy glen-explorer/requirements.lock.txt beside deploy/)." >&2; exit 1; }
for f in glen-preview.service glen-tunnel.service Caddyfile cloudflared-config.yml glen-preview.env.example fix-permissions.sh; do
    [[ -f "$DEPLOY_DIR/$f" ]] || { echo "Missing $DEPLOY_DIR/$f" >&2; exit 1; }
done

export DEBIAN_FRONTEND=noninteractive
APT=(apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

log "Time zone UTC (the analyst ledger counts UTC days and months)"
timedatectl set-timezone Etc/UTC

log "Base packages"
"${APT[@]}" update
"${APT[@]}" install ca-certificates curl gnupg software-properties-common \
    ufw unattended-upgrades rsync sqlite3 jq git

log "Package sources: Python $PYTHON_VERSION (deadsnakes), Caddy, cloudflared"
if ! apt-cache policy "$PYTHON" 2>/dev/null | grep -q 'Candidate: [0-9]'; then
    add-apt-repository -y ppa:deadsnakes/ppa
fi
install -d -m 0755 /usr/share/keyrings
if [[ ! -s /usr/share/keyrings/caddy-stable-archive-keyring.gpg ]]; then
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
fi
if [[ ! -s /etc/apt/sources.list.d/caddy-stable.list ]]; then
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
        -o /etc/apt/sources.list.d/caddy-stable.list
fi
if [[ ! -s /usr/share/keyrings/cloudflare-main.gpg ]]; then
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
fi
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
    > /etc/apt/sources.list.d/cloudflared.list
"${APT[@]}" update
"${APT[@]}" install "$PYTHON" "$PYTHON-venv" "$PYTHON-dev" caddy cloudflared

log "Unattended security updates"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
# Ubuntu's 50unattended-upgrades already limits upgrades to the -security
# pocket. No automatic reboot: the runbook says how to reboot by hand.
systemctl enable --now unattended-upgrades.service >/dev/null

log "SSH: keys only"
if [[ -s /root/.ssh/authorized_keys ]]; then
    # sshd keeps the FIRST value it reads for each keyword, and reads sshd_config.d/*.conf
    # in name order. DigitalOcean's cloud-init writes 50-cloud-init.conf, which may say
    # "PasswordAuthentication yes"; a file named after it would be ignored. 01- sorts first.
    rm -f /etc/ssh/sshd_config.d/60-glen.conf   # earlier name of this file
    cat > /etc/ssh/sshd_config.d/01-glen.conf <<'EOF'
# GLEN preview: key-based login only (written by deploy/bootstrap.sh).
# Named 01- so it is read before cloud-init's 50-cloud-init.conf: sshd keeps the first value.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
    sshd -t
    systemctl reload ssh.service 2>/dev/null || systemctl restart ssh.service
    # Check the settings sshd actually uses, not just the file.
    effective="$(sshd -T 2>/dev/null)"
    # sshd -T may print prohibit-password under its older name, without-password.
    for want in 'passwordauthentication no' 'kbdinteractiveauthentication no' \
                'permitrootlogin (prohibit-password|without-password)'; do
        grep -Eqx "$want" <<<"$effective" || echo "WARNING: sshd is not using '$want'; check /etc/ssh/sshd_config and sshd_config.d/."
    done
else
    echo "WARNING: /root/.ssh/authorized_keys is empty, so password login was left as it is."
    echo "         Add an SSH key to the droplet and run this script again."
fi

log "Firewall: inbound SSH only"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw limit OpenSSH >/dev/null
for rule in 80 443 80/tcp 443/tcp 'Nginx Full' 'Caddy'; do
    ufw --force delete allow "$rule" >/dev/null 2>&1 || true
done
ufw --force enable >/dev/null
ufw status verbose

log "Swap (2 GiB) and journal size"
if ! swapon --show=NAME --noheadings | grep -q .; then
    if [[ ! -f /swapfile ]]; then
        fallocate -l 2G /swapfile
        chmod 600 /swapfile
        mkswap /swapfile >/dev/null
    fi
    swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
echo 'vm.swappiness=10' > /etc/sysctl.d/60-glen.conf
sysctl -q --system
install -d -m 0755 /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=500M\n' > /etc/systemd/journald.conf.d/60-glen.conf
systemctl restart systemd-journald

log "Service user and directories"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$STATE_ROOT" --no-create-home \
        --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -o root -g root -m 0755 /srv/glen /opt/glen
install -d -o root -g "$SERVICE_USER" -m 2750 "$RELEASES" "$RUNTIME" "$PRIVATE"
install -d -o root -g root -m 0700 "$INCOMING"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$STATE_ROOT" "$STATE"
install -d -o root -g "$SERVICE_USER" -m 0750 "$ETC"
install -d -o root -g root -m 0755 /etc/cloudflared

log "Python environment $VENV"
if [[ -x "$VENV/bin/python" ]] && ! "$VENV/bin/python" -c "import sys; sys.exit(sys.version_info[:2] != tuple(map(int, '$PYTHON_VERSION'.split('.'))))"; then
    echo "Existing environment has another Python version; rebuilding it."
    rm -rf "$VENV"
fi
[[ -x "$VENV/bin/python" ]] || "$PYTHON" -m venv "$VENV"
lock_sha="$(sha256sum "$REQUIREMENTS" | cut -d' ' -f1)"
if [[ "$(cat "$VENV/.glen-lock-sha256" 2>/dev/null)" != "$lock_sha" ]]; then
    # Wheels only: a missing wheel should fail here, not try to compile GDAL.
    "$VENV/bin/python" -m pip install --disable-pip-version-check --require-virtualenv \
        --only-binary=:all: -r "$REQUIREMENTS"
    "$VENV/bin/python" -m pip check
    echo "$lock_sha" > "$VENV/.glen-lock-sha256"
else
    echo "Already matches requirements.lock.txt ($lock_sha)."
fi
chmod -R go-w "$VENV"

log "Configuration files"
if [[ ! -e "$ENV_FILE" ]]; then
    install -o root -g "$SERVICE_USER" -m 0640 "$DEPLOY_DIR/glen-preview.env.example" "$ENV_FILE"
    echo "Created $ENV_FILE from the example. Edit it before starting the app."
else
    chown root:"$SERVICE_USER" "$ENV_FILE"
    chmod 0640 "$ENV_FILE"
    echo "Kept existing $ENV_FILE."
fi
if [[ -e "$TUNNEL_ENV" ]]; then
    chown root:root "$TUNNEL_ENV"
    chmod 0600 "$TUNNEL_ENV"
fi
install -o root -g root -m 0644 "$DEPLOY_DIR/Caddyfile" /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
# Write the chosen hostname (from GLEN_PREVIEW_ORIGIN) into the reference ingress.
public_host="$(sed -n 's#^GLEN_PREVIEW_ORIGIN=https://\([a-z0-9.-]*\)$#\1#p' "$ENV_FILE" | tail -1)"
sed "s/glen\.example\.com/${public_host:-glen.example.com}/g" "$DEPLOY_DIR/cloudflared-config.yml" > /etc/cloudflared/config.yml
chown root:root /etc/cloudflared/config.yml
chmod 0644 /etc/cloudflared/config.yml
cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate
install -o root -g root -m 0644 "$DEPLOY_DIR/glen-preview.service" /etc/systemd/system/glen-preview.service
install -o root -g root -m 0644 "$DEPLOY_DIR/glen-tunnel.service" /etc/systemd/system/glen-tunnel.service
install -o root -g root -m 0755 "$DEPLOY_DIR/fix-permissions.sh" /usr/local/sbin/glen-fix-permissions
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/glen-preview.service /etc/systemd/system/glen-tunnel.service \
    || echo "WARNING: systemd-analyze reported the problems above; read them before starting the services."

log "Services"
systemctl enable caddy.service glen-preview.service glen-tunnel.service >/dev/null
systemctl restart caddy.service     # loopback only, nothing to serve until the app runs
/usr/local/sbin/glen-fix-permissions

ready=1
if grep -Eq '^(GLEN_PREVIEW_ORIGIN|GLEN_ACCESS_ISSUER|GLEN_ACCESS_AUD|GLEN_PREVIEW_EMAILS|GLEN_PREVIEW_RELEASE)=(|.*REPLACE.*)$' "$ENV_FILE"; then
    echo "The app is enabled but not started: fill in $ENV_FILE first."
    ready=0
fi
if ! grep -q '^TUNNEL_TOKEN=.\+' "$TUNNEL_ENV" 2>/dev/null; then
    echo "The tunnel is enabled but not started: put TUNNEL_TOKEN=... in $TUNNEL_ENV (mode 600)."
    ready=0
fi
if [[ ! -e "$RUNTIME/glen-explorer/backend/preview.py" ]]; then
    echo "No code under $RUNTIME yet: copy the runtime tree next."
    ready=0
fi

log "Done"
df -h / | tail -1
if [[ $ready == 1 ]]; then
    echo "Configured. Start with: systemctl restart glen-preview glen-tunnel"
else
    echo "Next steps are in docs/preview-runbook.md."
fi
