# dotUSD.info

Standalone dotUSD / USDT peg chart, extracted from `arbitrage/ops/dotusd`.
Python 3 standard library backend and dependency-free browser assets.

## Data source

The server's existing `arb-peg.service` collects finalized pool prices from the
arbitrage Asset Hub RPC at `http://127.0.0.1:9944` and writes history into
`arb-prometheus.service` at `127.0.0.1:9090`. This app reads that history through
a bounded `/api/prices` endpoint backed entirely by an in-process cache.
One background worker fetches recent prices at startup, then refreshes every six
seconds with a five-minute overlap for delayed ingestion. Seven days of older
history are seeded in six-hour chunks without waiting between polling cycles.
The API flags incomplete startup history; browsers retry those chunks, including
fixed shared ranges, and show chunk progress until history is complete.
Visitor requests never query Prometheus. The cache retains at most seven days and
350,000 samples, preserves raw timestamps and gaps, and remains readable during
upstream outages. Restarts rebuild the cache; the API returns 503 until the first
successful fetch. Each upstream response is limited to 4 MiB. Four HTTP handlers
can run concurrently; excess requests receive 503. Responses stream in batches
and client sockets have a ten-second inactivity timeout. The node RPC and Prometheus remain private.
The collector and Prometheus are managed by the arbitrage repository.
Prices follow dotUSD → DOT → USDT; timestamps are estimated and missing blocks
remain gaps. This is a USDT pool reference, with no fees or slippage included.

## Browser loading

The browser renders cached data immediately, fetches recent history first, and
fills missing history newest-first in chunks of at most six hours, redrawing
after each response. Live polls request only a five-minute overlap. An in-memory
browser cache holds at most seven days / 350,000 samples and revalidates historical
coverage every minute to discover late archive imports. The refresh button forces
revalidation; switching views reuses already fetched data. No samples are downsampled.
Caddy compresses even streamed JSON responses with unknown lengths. Static files
use ETags for cache revalidation, and the page preloads the JavaScript module.

## Development

Run `python3 server.py` to open the chart locally using an SSH tunnel to `server`.
Alternatively pass `--upstream http://127.0.0.1:9090/api/v1 --no-browser`.
Run tests with `python3 -m unittest discover -s tests -v`.

## Deployment

Run `./deploy/deploy.sh` from this checkout (optionally pass an SSH host).
Requires Python 3, Caddy, the existing arbitrage data services, rsync, and
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
After `./deploy/deploy.sh`, install/start it with:

```
ssh server 'sudo install -d -o prometheus -g prometheus -m 0755 /var/lib/dotusd-info'
scp deploy/dotusd-backfill.service server:/tmp/dotusd-backfill.service
ssh server 'sudo install -m 0644 /tmp/dotusd-backfill.service /etc/systemd/system/dotusd-backfill.service && sudo systemctl daemon-reload && sudo systemctl enable dotusd-backfill && sudo systemctl start --no-block dotusd-backfill'
```

Inspect progress with `ssh server journalctl -u dotusd-backfill -n 20 --no-pager`.
