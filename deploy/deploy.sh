#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
host=${1:-server}
version_url=${2:-https://dotusd.info/version}
python=${PYTHON:-.venv/bin/python}
if [ -n "$(git status --porcelain --untracked-files=all)" ]; then
    printf '%s\n' 'Refusing deployment: commit or remove all working-tree and index changes first.' >&2
    exit 1
fi
if [ "$(git symbolic-ref --quiet --short HEAD)" != master ]; then
    printf '%s\n' 'Refusing deployment: check out master before deploying.' >&2
    exit 1
fi
deploy_commit=$(git rev-parse --verify 'HEAD^{commit}')
case "$deploy_commit" in
    ''|*[!0-9a-f]*) printf '%s\n' 'Invalid deployment commit hash.' >&2; exit 1 ;;
esac
if [ "${#deploy_commit}" -ne 40 ]; then
    printf '%s\n' 'Deployment requires a full 40-character Git commit hash.' >&2
    exit 1
fi
snapshot=$(mktemp -d /tmp/dotusd-deploy.XXXXXX)
trap 'rm -rf "$snapshot"' 0
trap 'exit 1' HUP INT TERM
mkdir "$snapshot/source"
git archive --format=tar --output="$snapshot/source.tar" "$deploy_commit"
tar -xf "$snapshot/source.tar" -C "$snapshot/source"
release_version=$("$python" "$snapshot/source/release.py" --check)
expected_version="${release_version%%+*}+$(printf '%.7s' "$deploy_commit")"
printf '%s\n' "$deploy_commit" > "$snapshot/source/DEPLOY_COMMIT"
"$python" "$snapshot/source/deploy/check_version.py" --before-deploy "$expected_version" "$version_url"
printf 'Deploying %s\n' "$expected_version"
ssh "$host" 'sudo -n install -d -m 0755 /opt/dotusd-info/deploy'
(
    cd "$snapshot/source"
    rsync -rt --chmod=u=rwX,go=rX --rsync-path='sudo -n rsync' DEPLOY_COMMIT VERSION release.py server.py gap_repair.py archive_reader.py hub_quotes.py issuance.py treasury.py requirements.txt backfill.py publish_archive.py about-dotusd.html index.html app.js ranges.js style.css favicon.svg social-preview.png "$host:/opt/dotusd-info/"
    rsync -rt --chmod=u=rwX,go=rX --rsync-path='sudo -n rsync' deploy/dotusd-info.service deploy/dotusd-info.caddy "$host:/opt/dotusd-info/deploy/"
)
ssh "$host" 'sudo -n sh -s' <<'REMOTE'
set -eu
systemctl is-active --quiet arb-prometheus arb-peg
rm -f /opt/dotusd-info/coingecko-details.html /opt/dotusd-info/backfill_quotes.py /opt/dotusd-info/import_quote_batches.py /opt/dotusd-info/repair_quote_gaps.py
/usr/bin/python3 -m venv /opt/dotusd-info/.venv
/opt/dotusd-info/.venv/bin/pip install -r /opt/dotusd-info/requirements.txt
systemd-analyze verify /opt/dotusd-info/deploy/dotusd-info.service
install -m 0644 /opt/dotusd-info/deploy/dotusd-info.service /etc/systemd/system/dotusd-info.service
systemctl daemon-reload
systemctl enable dotusd-info
systemctl restart dotusd-info
attempt=0
until curl -fsS 'http://127.0.0.1:3001/api/historic?series=price' -o /dev/null; do
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

"$python" "$snapshot/source/deploy/check_version.py" "$expected_version" "$version_url"
