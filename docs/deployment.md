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

Production reads one final archive, `/var/lib/dotusd-info/archive.sqlite3`.
Backfill writes only to the temporary `/var/lib/dotusd-backfill/archive.sqlite3`.
The importer refuses the published path (including aliases), and its service mounts
production data read-only. It is manually started, has no automatic retries, and
uses a 25% CPU cap, low process priority and idle disk priority.

Keep the service disabled between manual runs. Install the scripts and unit, then:

```sh
ssh server 'sudo install -d -o prometheus -g prometheus -m 0755 /var/lib/dotusd-backfill'
scp deploy/dotusd-backfill.service server:/tmp/dotusd-backfill.service
ssh server 'sudo install -m 0644 /tmp/dotusd-backfill.service /etc/systemd/system/dotusd-backfill.service && sudo systemctl daemon-reload && sudo systemctl start --no-block dotusd-backfill'
```

For an existing partial archive, seed the temporary database with SQLite's backup
API in 64-page batches, pausing 20 ms between batches. Use `publish_archive.py --seed-staging` for this throttled copy. Preserve sequence IDs: the
web server uses them as its import cursor. Do not modify the published database.
The importer verifies the chain and live boundary and resumes the staging copy.
Dependencies on the server: `python3-websocket` and `python3-xxhash`.

After the service completes, publish with the separately throttled command:

```sh
ssh server 'sudo systemd-run --unit=dotusd-archive-publish --wait --collect --pipe -p CPUQuota=10% -p MemoryMax=192M -p Nice=19 -p IOSchedulingClass=idle /usr/bin/python3 /opt/dotusd-info/publish_archive.py --cleanup-staging'
```

Publication copies 64 SQLite pages per step with a 20 ms pause, checks SQLite
integrity, contiguous block coverage, clock order, the live boundary, and exact
preservation of every published row and sequence ID. An identical archive is a
no-op. A changed archive is made available with an atomic rename, after preserving
the prior file as `archive.sqlite3.previous` for rollback. No web restart is needed;
the existing worker lazily imports new rows in its usual 2,000-row batches. Once
production verification succeeds, remove the rollback file. The temporary staging
database is removed only after successful publication with `--cleanup-staging`.
No background backfill or second permanent archive remains.

Inspect progress with `ssh server journalctl -u dotusd-backfill -n 20 --no-pager`.

## Comparison-page history (Python only)

This is separate from pool archive backfill. Run reconstruction on a workstation:

```sh
python3 -m venv /tmp/dotusd-quote-work
/tmp/dotusd-quote-work/bin/pip install substrate-interface
curl -fsS 'https://api.coingecko.com/api/v3/coins/polkadot/market_chart?vs_currency=usd&days=1&precision=full' -o /tmp/dot-history.json
/tmp/dotusd-quote-work/bin/python backfill_quotes.py --references /tmp/dot-history.json --output /tmp/quote-backfill.json --work /tmp/quote-blocks.jsonl
```

Keep the evidence artifact locally. Transfer it to a temporary server path, then
install it as `/var/lib/dotusd-quotes/backfill.json` by atomic rename, readable by
the web service. Never replace `history.json` while the quote worker is running.
The worker validates and merges it within a minute without restarting the website;
the temporary import is removed after its contents are saved to the final history.
Confirm the comparison API has the expected timestamps and the normal page remains
healthy. `substrate-interface` is needed only for reconstruction, not web serving.
