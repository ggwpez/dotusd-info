# Deployment

[Project overview](../README.md)

Run these commands from the repository root. The supplied configuration targets
the existing dotusd.info installation; adapt the domain, service names, and data
paths for your own host. In the examples, `server` is an SSH configuration alias;
replace it with your own alias or `user@host`.

Before releasing application changes, run `just release`
(or `just release minor` for features), run `just test`, and commit the updated `VERSION`
with the changes. The deployment script checks the recorded hash and runs the
suite before changing the server; a stale release stops deployment. Before
transferring files or restarting services, it also checks the public version and
requires the local MAJOR.MINOR.PATCH number to be strictly newer. The same number
with a different hash is refused, as are older versions. If the deployed version
cannot be read or parsed, deployment stops without changing the server.

The preflight uses the existing `/api/version` alias when the verification URL
is `/version`, so it also works before the new route has been deployed. Custom
endpoint paths are used as supplied. The post-deployment check uses `/version`.

Deploy the current checkout with:

```sh
just deploy             # use the SSH alias server
# Or choose another host:
just deploy user@host
```

Run only the command for your intended host. This copies the local files and
restarts the remote service. It does not commit changes or bump the version.
After the remote setup and health checks, deployment polls the public `/version`
endpoint immediately, then every 5 seconds for up to 25 seconds. It returns
success as soon as the JSON `version` matches the local `VERSION` captured before
deployment. If the deadline expires, it exits with an error showing the last
version mismatch or request failure. This 25-second limit applies to the final
verification, not file transfer, dependency installation, or service startup.

For another domain, pass its version URL explicitly:

```sh
just deploy user@host https://example.org/version
```

Without `just`, use `./deploy/deploy.sh [host] [version-url]`. Public verification
requires `curl` locally. A verification failure does not roll back the deployment;
inspect `just status` and `just logs` before retrying.
Requires Python 3.10+, Caddy, the existing arbitrage data services, rsync, and
passwordless sudo and `python3-venv` on the target. Deployment installs
`requirements.txt` into `/opt/dotusd-info/.venv` for archive pool decoding.

The app runs as a dedicated dynamic user at `/opt/dotusd-info`, listening only
on `127.0.0.1:3001`. `dotusd-info.service` restarts automatically and is enabled
at boot. Caddy imports `/etc/caddy/sites/dotusd-info.caddy` and provides HTTPS
for `dotusd.info`, with `www.dotusd.info` redirecting to the main domain. The deployment validates Caddy before reload, backs up the
previous site configuration, and does not replace other sites.

Inspect the deployment with `just status` and `just logs`; both accept an
optional host, for example `just logs user@host`. Logs show the latest 50 entries
without following the stream. Run `just` to list all common commands.

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

## Retired comparison

The comparison implementation is preserved on `preserve-coingecko`.
Production no longer serves `/coingecko` or `/api/coingecko/prices` and does
not start a comparison worker. Existing comparison history files are left
untouched; `/var/lib/dotusd-quotes` remains the state directory for price repairs
and total issuance history.

## Automatic price gap recovery and issuance history

The supplied service enables `--gap-repair-history
/var/lib/dotusd-quotes/pool-repairs.sqlite3`. This uses the existing writable
state directory and needs no additional service or timer. The same SQLite file
stores price-repair evidence and seven days of total issuance, including
successful samples from the independent recent 15-block live issuance poll.
Startup restores retained issuance before new RPC samples arrive.

The repair worker queries the Parity archive independently of the live collector.
Price repair and issuance backfill share a limit of eight archive blocks per pass,
at most two sequential RPC requests per second and a one-minute pause after each
pass. Missing issuance is filled recent-first throughout the retained price block
range, even where prices are already complete; seven-day coverage grows gradually
and may remain incomplete. Restarting the website resumes saved progress.
RPC failures back off up to fifteen minutes and do not interrupt live collection
or the independent issuance poll. Inspect `Archive gap repair` messages in
`journalctl -u dotusd-info`.

Omit the option to disable automatic repair and disk-backed issuance history;
issuance then remains a recent-only, in-memory preview. Seven-day retention bounds
repaired history, saved evidence and persisted issuance. The original chain
timestamps are used for issuance rather than the estimated pool-price clock.
