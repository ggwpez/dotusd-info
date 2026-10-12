# Deployment

[Project overview](../README.md)

Run these commands from the repository root. The supplied configuration targets
the existing dotusd.info installation; adapt the domain, service names, and data
paths for your own host. In the examples, `server` is an SSH configuration alias;
replace it with your own alias or `user@host`.

Run `./deploy/deploy.sh` from this checkout (optionally pass an SSH host).
Requires Python 3.10+, Caddy, the existing arbitrage data services, rsync, and
passwordless sudo on the target. No external Python packages are needed.

The app runs as a dedicated dynamic user at `/opt/dotusd-info`, listening only
on `127.0.0.1:3001`. `dotusd-info.service` restarts automatically and is enabled
at boot. Caddy imports `/etc/caddy/sites/dotusd-info.caddy` and provides HTTPS
for `dotusd.info`, with `www.dotusd.info` redirecting to the main domain. The deployment validates Caddy before reload, backs up the
previous site configuration, and does not replace other sites.

Inspect with `ssh server systemctl status dotusd-info` and
`ssh server journalctl -u dotusd-info -n 50 --no-pager`.

## Archive backfill

`backfill.py` imports every finalized block from referendum 1944's enactment at
21,572,110 (2026-10-08 07:31:12 UTC) up to the first block in the existing live
collector database. It uses only Parity's
`wss://polkadot-asset-hub-rpc.polkadot.io`, verifies the genesis and the live
collector's boundary hash, and preserves both the original block timestamp and
pool reserves. Pool decoding is shared with the extracted `archive_reader.py`.

Historical samples live in `/var/lib/dotusd-info/archive.sqlite3`; Prometheus and
the live collector database are unchanged. The chart worker imports new archive
rows every six seconds, independently of visitor requests. The estimated chart
clock is extended backwards from the existing boundary, spacing tied timestamps
by two seconds; original block timestamps remain in SQLite. Archive loads never
mark a failed live refresh as healthy.

`dotusd-backfill.service` runs with the existing prometheus user, restarts on
failure, resumes committed batches, and exits after verifying complete block
coverage. Dependencies on the server: `python3-websocket` and `python3-xxhash`.
After `./deploy/deploy.sh server`, install/start it with:

```sh
ssh server 'sudo install -d -o prometheus -g prometheus -m 0755 /var/lib/dotusd-info'
scp deploy/dotusd-backfill.service server:/tmp/dotusd-backfill.service
ssh server 'sudo install -m 0644 /tmp/dotusd-backfill.service /etc/systemd/system/dotusd-backfill.service && sudo systemctl daemon-reload && sudo systemctl enable dotusd-backfill && sudo systemctl start --no-block dotusd-backfill'
```

Inspect progress with `ssh server journalctl -u dotusd-backfill -n 20 --no-pager`.
