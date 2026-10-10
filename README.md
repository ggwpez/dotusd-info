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
The API flags incomplete startup history; browsers retry incomplete ranges, including
fixed shared ranges, until history is complete.
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

The browser renders cached data immediately and loads the selected history window
in one request. `?format=stream` returns newline-delimited JSON: a
`ranges-stream-v1` metadata line (including full-window extrema), independently
packed `ranges` lines of up to 8,192 samples each, then a `{"done":true}` line.
The client decodes and draws complete lines as they arrive, without expanding
ranges or acknowledging batches. Network reads may split or combine lines.
Full-window extrema keep the vertical scale stable during loading. Only a
successfully finished stream enters the cache; interrupted streams retry.
Live polls request only a five-minute overlap. An in-memory browser cache holds
at most seven days / 350,000 samples, retaining completed historical coverage
until those samples expire or are evicted. Incomplete history continues retrying;
reload the page to discover later archive imports outside the live overlap.
Switching views reuses already fetched data. No samples are downsampled.
Hover redraws are coalesced per animation frame and reuse canvas buffers.
Caddy flushes upstream responses immediately.
Caddy compresses even streamed JSON responses with unknown lengths. Static files
use ETags for cache revalidation. The server embeds CSS and both JavaScript modules
in the HTML at startup, so initial loading needs only the document and price stream.
CSP hashes authorize exactly those embedded scripts and styles. Source files remain
separate for development. The price request also checks the release version,
avoiding a separate startup version request.

At startup the server snapshots its assets and hashes the server, archive reader,
HTML, JavaScript (including the range module), and CSS into a release version. Responses carry `X-App-Version`,
and `/api/version` returns the version even while price history is warming up.
The client checks this header before decoding price responses and reloads the
current URL on a mismatch, preserving the linked range/zoom. Live polling checks
the version through its price responses; there are no separate version requests.
Completed fixed views stay idle until another data request is needed. Returning
to a live tab or reconnecting refreshes prices and checks the version again.
Network failures retry without forcing a reload.
Assets remain tied to their startup snapshot during deployment; restart the
server to activate changes, including when developing locally. Tabs opened before
this feature was deployed need one manual reload to enable automatic updates.

### Packed history and rendering

`/api/prices` returns `encoding: "ranges-v1"` and a `ranges` array. The first
record is `[timestamp, block, price]`. Subsequent records are
`[count, secondsPerStep, blocksPerStep, optionalNewPrice]`, with omitted prices
reused from the previous record. A `[0, timestamp, block, price]` record resets
the anchor for missing block numbers or timestamps that cannot be reconstructed
exactly. Fractional timestamps are kept as individual records to avoid rounding
drift. Empty history uses an empty array. `fetchedAt`, `historyReady`, and raw
full-window `extremes` retain their original meaning. `?format=raw` is available
for diagnostics and benchmarks; the chart requests the streaming variant with the same packed records.

The browser keeps absolute-start ranges rather than expanding raw rows. Request
overlaps replace the requested interval, adjacent compatible ranges coalesce,
and expiry/zoom clip ranges without iterating over all their samples. Sample
counts, nearest-point hover, and keyboard navigation reconstruct individual
samples on demand. Rendering visits constant-price range endpoints, then keeps
pixel endpoints and extrema. Missing block numbers and block-number gaps remain
disconnected. Server-side history is still stored as raw samples.

Validated with 275,652 projected samples: 16,052 wire records become 12,585 cached
ranges. The previous six-hour request scheme totaled about 312 KB of JSON and 100 KB compressed
(gzip level 6 or zstd level 3), compared with 11.79 MB of compact raw JSON and
1.08–1.43 MB compressed. A local headless Chrome run measured median packed
parse/cache construction at 1.5 ms versus raw JSON parsing at 12.2 ms; drawing
point preparation at 0.8 ms versus 1.4 ms. These are component timings over 20
measured runs after five warmups, not end-to-end browser performance guarantees.

## Development

Run `python3 server.py` to open the chart locally using an SSH tunnel to `server`.
Alternatively pass `--upstream http://127.0.0.1:9090/api/v1 --no-browser`.
Run tests with `python3 -m unittest discover -s tests -v`.
Client range tests use Node when installed, or macOS JavaScriptCore, and otherwise
report a skip. They compare exact samples, line geometry, gaps, pixel extrema,
hover selection, clipping, and cache overlap behavior against raw data.

### Sample packing benchmark

Run `python3 tools/benchmark_packing.py` to compare lossless wire formats against
the available production history, or pass `--samples snapshot.json` with a JSON
array of `[timestamp, price, block]` samples. Python 3.14 also measures zstd.
This standalone benchmark does not modify the production API.

Measured on October 10, 2026: 85,182 samples across 51.92 hours; 96.73% of adjacent
samples have identical prices. Repeating this observed pattern to fill seven days
produces 275,652 samples. This is a size estimate, not a forecast of trading activity.

| Encoding | Seven-day JSON | gzip | zstd |
| --- | ---: | ---: | ---: |
| Current rows | 12,403,623 B | 1,465,086 B | 1,098,650 B |
| Compact JSON | 11,576,695 B | 1,435,523 B | 1,091,445 B |
| Omit unchanged prices; absolute timestamps/blocks | 6,580,383 B | 1,414,119 B | 1,175,585 B |
| Timestamp/block deltas; omit unchanged prices | 1,831,703 B | 112,592 B | 111,558 B |
| Runs of identical deltas and prices | 312,227 B | 99,665 B | 100,107 B |

These historical benchmark results reset compression every six hours, matching
the former loading scheme; shared response metadata/HTTP headers are excluded.
Local gzip level 6 and zstd level 3 provide controlled comparisons, not exact Caddy
wire sizes. The observed history itself uses 451,473 / 339,578 bytes with gzip/zstd
in the current format, versus 29,992 / 30,036 bytes with runs. Every candidate is
decoded and compared to every original sample, including timestamps, full prices,
and block numbers, for both observed and projected datasets.

The delta and run formats begin with `[timestamp, block, price]`. Delta records
are `[secondsSincePrevious, blocksSincePrevious, optionalNewPrice]`. Run records
are `[count, secondsPerStep, blocksPerStep, optionalNewPrice]`: they generate
`count` samples after the previous sample, all at the given price and step sizes.
An omitted price means reuse the preceding price. Runs split whenever the price
or either step changes, preserving real gaps rather than assuming two-second
spacing everywhere. In this snapshot, 1,108 timestamp steps differ from two
seconds. The repeat-price-only format retains `[timestamp, price, block]` for a
price change and uses `[timestamp, block]` otherwise.

Runs reduce projected compressed traffic by about 93% (gzip) / 91% (zstd), and
uncompressed JSON by 97.5%. Simple deltas achieve most of the transfer savings;
runs additionally reduce the JSON that the browser must parse. Neither format
reduces expanded browser memory if decoded back into the current raw sample cache.

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
