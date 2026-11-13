#!/usr/bin/env bash
# Reset ownership and modes after copying a release, the code checkout or private data.
# bootstrap.sh installs this as /usr/local/sbin/glen-fix-permissions; run it as
# root after every rsync. The app user can read releases and the runtime tree
# but not change them, and only it can open the state directory.
set -Eeuo pipefail

[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }

# chown/chmod -R and find do not follow the checkout's symlinks into /srv/glen/private;
# that directory is listed here in its own right.
for dir in /srv/glen/releases /srv/glen/runtime /srv/glen/private; do
    [[ -d "$dir" ]] || continue
    chown -R root:glen "$dir"
    chmod -R u=rwX,g=rX,o= "$dir"
    find "$dir" -type d -exec chmod g+s {} +
done
chown -R glen:glen /var/lib/glen
chmod 0700 /var/lib/glen /var/lib/glen/state
[[ -e /etc/glen/glen-preview.env ]] && { chown root:glen /etc/glen/glen-preview.env; chmod 0640 /etc/glen/glen-preview.env; }
[[ -e /etc/cloudflared/glen-tunnel.env ]] && { chown root:root /etc/cloudflared/glen-tunnel.env; chmod 0600 /etc/cloudflared/glen-tunnel.env; }

# backend/server.py loads glen-explorer/.env if it exists. Secrets belong only in
# /etc/glen/glen-preview.env, so refuse a copied .env.
if [[ -e /srv/glen/runtime/glen-explorer/.env ]]; then
    echo "ERROR: /srv/glen/runtime/glen-explorer/.env exists. Delete it; keys go in /etc/glen/glen-preview.env." >&2
    exit 1
fi
echo "Permissions reset."
