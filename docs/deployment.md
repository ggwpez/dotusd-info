# Deployment

[Project overview](../README.md)

Run commands from the repository root. `server` is an SSH alias; substitute
your own alias or `user@host`.

## Release

```sh
just test          # just test minor for features
git add <changed-files> VERSION
git -c commit.gpgsign=false commit --no-gpg-sign -m "Describe the change"
just deploy        # or: just deploy user@host [https://example.org/version]
```

`just deploy` refuses to run unless:

- the checkout is a clean `master` (`git status --porcelain` empty);
- `VERSION` matches the source checksum;
- the local release number is strictly newer than the deployed `/version`.

It copies a Git snapshot, writes the full HEAD SHA to `DEPLOY_COMMIT`, restarts
the service, then polls `/version` every 5 seconds for up to 60 seconds until
it reports `MAJOR.MINOR.PATCH+<7-hex HEAD>`. A timeout fails but does not roll
back; check `just status` and `just logs`. To find a release's commit, run
`git show <suffix>` with the suffix after `+`.

Without `just`, run `./deploy/deploy.sh [host] [version-url]`. Local Python
defaults to `.venv/bin/python` (override with `PYTHON`); verification needs
`curl`.

## systemd and Caddy host

Requirements: Python 3.10+, Caddy, rsync, passwordless sudo, `python3-venv`, and
outbound HTTPS to an Asset Hub archive RPC.

- The app lives in `/opt/dotusd-info` with its own `.venv`, runs as a dynamic
  user, and listens on `127.0.0.1:3001`. `dotusd-info.service` is enabled at
  boot and restarts automatically.
- Caddy imports `/etc/caddy/sites/dotusd-info.caddy`: HTTPS for `dotusd.info`,
  with `www.dotusd.info` redirecting. Deployment validates Caddy before reload,
  backs up the previous site file, and leaves other sites alone.
- `just status [host]` and `just logs [host]` (latest 50 entries) inspect the
  service.

## Container (Coolify)

- Private URL `https://dotusd.vpn.selenio-software.ltd`; Coolify
  **dotUSD / production / dotusd-info**, 512 MiB.
- Pushing to `master` runs checks, builds and pushes the image, triggers the
  Coolify deployment, then runs `deploy/check_version.py` against the private
  `/version` (same 60-second poll). Pull requests do not deploy. CI secrets:
  `REGISTRY_USERNAME`, `REGISTRY_TOKEN`, `COOLIFY_DEPLOY_WEBHOOK`,
  `COOLIFY_DEPLOY_TOKEN`; the Coolify API must stay enabled.
- `/opt/dotusd-info/data` is mounted at `/data` (UID/GID `10001:10001`). The
  command uses one database for everything:
  `--archive /data/dotusd.sqlite3 --gap-repair-history /data/dotusd.sqlite3 --hub-rpc https://polkadot-asset-hub-rpc.polkadot.io`.
  New deployments default to separate `/data/archive.sqlite3` and
  `/data/pool-repairs.sqlite3`; keep existing paths when upgrading.
- To replace the database: take an SQLite backup, stop the deployment, keep the
  old file and its sidecars, rename the new file into place, set ownership, and
  restart. Verify samples through `/api/historic` and the chart, not just
  `/version`.

## History repair

`--gap-repair-history PATH` (enabled by the supplied service, at
`/var/lib/dotusd-quotes/pool-repairs.sqlite3`) stores live prices, repair
evidence, issuance and Treasury in one SQLite file. Nothing is pruned; plan for
disk growth. See [History repair](architecture.md#history-repair) for behavior.
Log lines start with `Archive gap repair` (`journalctl -u dotusd-info`).

Without the flag, issuance is a 15-block in-memory preview and Treasury is not
collected.

## Archive backfill

`backfill.py` imports every finalized block from 21,572,110 (referendum 1944's
enactment) up to a finalized anchor fixed when staging starts; resumes reuse and
re-verify that anchor. It writes only to
`/var/lib/dotusd-backfill/archive.sqlite3` and refuses the published
`/var/lib/dotusd-info/archive.sqlite3`. The manual service mounts production data
read-only, never retries, and runs at 25% CPU with idle I/O priority.

```sh
scp deploy/dotusd-backfill.service server:/tmp/dotusd-backfill.service
ssh server 'sudo install -m 0644 /tmp/dotusd-backfill.service /etc/systemd/system/dotusd-backfill.service && sudo systemctl daemon-reload && sudo systemctl start --no-block dotusd-backfill'
ssh server journalctl -u dotusd-backfill -n 20 --no-pager
```

To continue from an existing archive, seed staging with
`publish_archive.py --seed-staging` (throttled SQLite backup that preserves
sequence IDs, which the server uses as its import cursor).

Publish when the service completes:

```sh
ssh server 'sudo systemd-run --unit=dotusd-archive-publish --wait --collect --pipe -p CPUQuota=10% -p MemoryMax=192M -p Nice=19 -p IOSchedulingClass=idle /opt/dotusd-info/.venv/bin/python /opt/dotusd-info/publish_archive.py --hub-rpc https://polkadot-asset-hub-rpc.polkadot.io --cleanup-staging'
```

Publication verifies integrity, contiguous blocks, clock order, the finalized
boundary, and that every published row and sequence ID is preserved. It then
renames atomically, keeping `archive.sqlite3.previous` for rollback (delete it
after verifying production). The running server imports new rows without a
restart.
