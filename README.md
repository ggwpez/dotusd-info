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
history are seeded in six-hour chunks, one per refresh cycle (about three minutes).
Visitor requests never query Prometheus. The cache retains at most seven days and
350,000 samples, preserves raw timestamps and gaps, and remains readable during
upstream outages. Restarts rebuild the cache; the API returns 503 until the first
successful fetch. Each upstream response is limited to 4 MiB. Four HTTP handlers
can run concurrently; excess requests receive 503. Responses stream in batches
and client sockets have a ten-second inactivity timeout. The node RPC and Prometheus remain private.
The collector and Prometheus are managed by the arbitrage repository.
Prices follow dotUSD → DOT → USDT; timestamps are estimated and missing blocks
remain gaps. This is a USDT pool reference, with no fees or slippage included.

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
