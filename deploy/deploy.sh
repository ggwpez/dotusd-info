#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
host=${1:-server}
python3 -m unittest discover -s tests -v
ssh "$host" 'sudo -n install -d -m 0755 /opt/dotusd-info/deploy'
rsync -rt --chmod=u=rwX,go=rX --rsync-path='sudo -n rsync' server.py archive_reader.py backfill.py backfill_quotes.py publish_archive.py index.html app.js ranges.js style.css favicon.svg social-preview.png "$host:/opt/dotusd-info/"
rsync -rt --chmod=u=rwX,go=rX --rsync-path='sudo -n rsync' deploy/dotusd-info.service deploy/dotusd-info.caddy "$host:/opt/dotusd-info/deploy/"
ssh "$host" 'sudo -n sh -s' <<'REMOTE'
set -eu
systemctl is-active --quiet arb-prometheus arb-peg
systemd-analyze verify /opt/dotusd-info/deploy/dotusd-info.service
install -m 0644 /opt/dotusd-info/deploy/dotusd-info.service /etc/systemd/system/dotusd-info.service
systemctl daemon-reload
systemctl enable dotusd-info
systemctl restart dotusd-info
attempt=0
until curl -fsS http://127.0.0.1:3001/api/prices -o /dev/null; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 60 ] || exit 1
    sleep 1
done
site=/etc/caddy/sites/dotusd-info.caddy
backup=$(mktemp /etc/caddy/dotusd-info-backup.XXXXXX)
existed=false
if [ -f "$site" ]; then
    cp -p "$site" "$backup"
    existed=true
fi
restore_site() {
    if [ "$existed" = true ]; then cp -p "$backup" "$site"; else rm -f "$site"; fi
}
install -m 0644 /opt/dotusd-info/deploy/dotusd-info.caddy "$site"
if ! caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; then
    restore_site
    exit 1
fi
if ! systemctl reload caddy; then
    restore_site
    systemctl reload caddy
    exit 1
fi
systemctl is-active dotusd-info caddy arb-peg arb-prometheus
systemctl is-enabled dotusd-info
REMOTE
