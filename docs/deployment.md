# Deployment

[Project overview](../README.md)

Run these commands from the repository root. The supplied configuration targets
the existing dotusd.info installation; adapt the domain, service names, and data
paths for your own host. In the examples, `server` is an SSH configuration alias;
replace it with your own alias or `user@host`.

Run `./deploy/deploy.sh` from this checkout (optionally pass an SSH host).
Requires Python 3.10+, Caddy, the existing arbitrage data services, rsync, and
passwordless sudo and `python3-venv` on the target. Deployment installs
`requirements.txt` into `/opt/dotusd-info/.venv` for the Hub runtime decoder.
The comparison worker uses the public Hub archive RPC by default; set
`--hub-rpc URL` in the service command to use another Hub archive node.

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
/tmp/dotusd-quote-work/bin/pip install -r requirements.txt
curl -fsS 'https://dotusd.info/api/prices?format=raw' -o /tmp/pool-history.json
/tmp/dotusd-quote-work/bin/python backfill_quotes.py --targets /tmp/pool-history.json --output /tmp/quote-backfill --work /tmp/quote-blocks.jsonl
```

Keep the evidence artifact locally. Transfer it to a temporary server path, then
install it as `/var/lib/dotusd-quotes/backfill.json` by atomic rename, readable by
the web service. Never replace `history.json` while the quote worker is running.
The worker validates and merges it within a minute without restarting the website;
the temporary import is removed after its contents are saved to the final history.
Confirm the comparison API has the expected timestamps and the normal page remains
healthy. `substrate-interface` is required by both reconstruction and the live Hub RPC reader.


The Hub runtime decoder and price caches share a 512 MiB service limit. For a
bounded history import, use `import_quote_batches.py --max-memory-mib 384` to
leave at least 128 MiB below that cap; the default remains 180 MiB for smaller
installations. Reconstruction runs on the workstation. Existing import batches
can be replayed from a fresh directory after validation, without restarting the
web service or replacing its live history file.

### Repair comparison-history gaps

For gaps left by restarts or RPC outages, save a raw comparison API response with
explicit `start` and `end` Unix seconds covering the affected period (at most seven
days). Omitting those parameters returns only the last hour. Then run locally:

```sh
.venv/bin/python repair_quote_gaps.py --history /tmp/comparison-history.json --output /tmp/comparison-repair --work /tmp/comparison-repair-blocks.jsonl
.venv/bin/python import_quote_batches.py --directory /tmp/comparison-repair --expected-samples N --max-memory-mib 384
```

Replace `N` with the completed sample count reported by reconstruction. Use a fresh
output directory for each saved history snapshot. The repair finds intervals over
120 seconds by default and scans every finalized block strictly inside them,
using actual chain timestamps and independently reconstructed trade legs. Blocks
sharing a timestamp produce one observation using the final block's state.
It retains the existing boundary observations and produces the same evidence
batches as the full backfill. Reconstruction is resumable through its block cache;
imports use the existing health gates and require no site restart. After import,
fetch the same API window again and verify the samples, remaining gaps, and live
freshness. This repairs historical outages; it does not change live collection or
prevent gaps during future restarts.

## Automatic pool gap recovery

The supplied service enables `--gap-repair-history
/var/lib/dotusd-quotes/pool-repairs.sqlite3`. This uses the existing writable
state directory and needs no additional service or timer. It queries the Parity
archive independently of the live collector, with eight blocks per pass, at most
two sequential RPC requests per second and a one-minute pause after each pass.
Restarting the website resumes saved progress. RPC failures back off up to
fifteen minutes and do not interrupt live collection. Inspect `Archive gap repair`
messages in `journalctl -u dotusd-info`. Omit the option to disable automatic
repair. The seven-day retention bounds both repaired history and saved evidence.
