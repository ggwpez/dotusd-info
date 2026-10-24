# Architecture

[Project overview](../README.md)

## Data source

The server's existing `arb-peg.service` collects finalized pool prices from the
arbitrage Asset Hub RPC at `http://127.0.0.1:9944` and writes history into
`arb-prometheus.service` at `127.0.0.1:9090`. This app reads that history through
a bounded `/api/prices` endpoint backed entirely by an in-process cache.
One background worker fetches recent prices at startup, then refreshes every six
seconds with a five-minute overlap before the last received sample (or the last
query boundary, if earlier), so collector outages do not skip delayed ingestion.
Seven days of older
history are seeded in six-hour chunks without waiting between polling cycles.
The API flags incomplete startup history; browsers retry incomplete ranges, including
fixed shared ranges, until history is complete.
Visitor requests never query Prometheus. The cache retains at most seven days and
350,000 samples, preserves raw timestamps and gaps, and remains readable during
upstream outages. Restarts rebuild the cache; the API returns 503 until the first
successful fetch. Readers capture an immutable snapshot under a brief lock, then
scan it without holding the lock. Writers build replacements separately and
publish rows and freshness together, so ongoing reads retain a consistent view.
Each snapshot caches min/max prices and the highest block number for groups of
256 samples. Window extrema reuse fully covered groups and scan only partial
groups at the edges; live cursors skip groups containing no newer blocks.
The cache holds at most 1,368 summaries (about 107 KiB per snapshot on the server),
references existing samples, and never grows with the number of requested windows.
Summaries are rebuilt on merges, including corrections and eviction, and are
released with their snapshot when its readers finish.
Packed JSON and NDJSON responses share a six-second LRU across the four workers.
It retains at most 32 entries and 8 MiB of encoded bytes, with a 4 MiB per-response
limit; allocation follows actual responses, not seven days of hypothetical data.
Keys use the immutable history generation, selected row and extrema boundaries,
freshness/readiness, format and block cursor. Different browser timestamps can
reuse identical selections, while publication, corrections, retention boundaries
and cursor changes cannot serve an incorrect cached response. Concurrent misses
for the same key share one encoding job. A short lock protects bookkeeping;
selection, encoding and socket writes happen outside it. Raw diagnostic responses
and oversized packed responses stream without retention. A cold cache buffers
an eligible response before sending its batches; oversized responses fall back
to streaming after the bounded cache attempt. The byte limit covers retained
payloads, with additional bounded metadata, in-flight builds and active readers.
Each upstream response is limited to 4 MiB. Four fixed HTTP workers
serve requests from a FIFO holding at most 32 waiting connections. A full queue
returns 503 with `Retry-After: 6`. Requests waiting two seconds expire with the
same response; idle-server expiry checks run every 0.5 seconds. The separate TCP
listen backlog is 64 connections. Responses stream in batches
and client sockets have a ten-second inactivity timeout. The node RPC and Prometheus remain private.
The collector and Prometheus are managed by the arbitrage repository.
Prices follow dotUSD → DOT → USDT; timestamps are estimated and missing blocks
remain gaps. This is a USDT pool reference, with no fees or slippage included.

## Browser loading

The white center (peg) reference is interrupted only where the price stroke
enters a two-CSS-pixel band around it, with two pixels of horizontal clearance.
The shaded fill never hides the reference. Intersection detection runs alongside
price-path rendering, respects data gaps, and follows the current zoom and
vertical scale. Clear portions, the center tick label, and the faint grid remain
visible; an empty chart retains the full reference.

Settings provides a **Log scale** checkbox, off by default for a linear Y-axis.
When checked, it uses `sign(bps) * log1p(abs(bps))` (a one-basis-point soft knee)
symmetrically around the peg, including zero and negative deviations. This is
log scaling of deviation, not log scaling of the absolute dollar price. Tick
labels remain in the selected units; prices, ratings, and tooltip values are
unchanged. Geometry and gradient stops share the transform. The axis preference
is saved in `dotusd-axis-scale` in browser local storage and switching it redraws
cached data without fetching history.
The settings menu closes on clicks outside it; clicks on its controls keep it open.
Changing units or the log checkbox writes `scale=bps|percent|dollars` and
`axis=linear|log` into the chart URL, preserving the selected range and zoom.
Valid URL values override browser preferences on load; missing or invalid
values fall back to preferences. Back/Forward restores both scale settings;
scale-only navigation redraws cached data without refetching history.
In dollar-price mode, the redundant spot-price summary is hidden with
`visibility: hidden`, preserving its space and the surrounding layout. It remains
visible in basis-point and percent modes.
Peg tightness, the current price/deviation, and spot price show `-` in historical
views, including zoomed and shared fixed ranges. Rating hover/focus previews are
also suppressed there. These metrics resume in live view; range low/high remains
available in either view.

The browser renders cached data immediately and loads the selected history window
in one request. `?format=stream` returns newline-delimited JSON: a
`ranges-stream-v1` metadata line (including full-window extrema), independently
packed `ranges` lines of up to 8,192 samples each, then a `{"done":true}` line.
The client decodes and draws complete lines as they arrive, without expanding
ranges or acknowledging batches. Network reads may split or combine lines.
Full-window extrema keep the vertical scale stable during history loading.
Incremental polls omit the statistics window and compute extrema from the cached
visible samples, including new samples and excluding those that leave the window. Only a
successfully finished stream enters the cache; interrupted streams retry.
Live polls send `startpoint` with the last received finalized block number
(exclusive: only larger block numbers are returned). They omit `end` and
`windowEnd`; the server chooses the current snapshot boundary and reports it in
`X-History-End`. History loads use `windowStart` for early chart extrema;
incremental polls omit it. It does not constrain the block cursor.
Explicit `end` timestamps still bound fixed historical queries. Timestamp `start`
and the old `afterBlock` parameter remain supported for existing clients;
`startpoint` takes precedence over `start` and cannot be combined with `afterBlock`.
Initial history loads use timestamp ranges until a block cursor is available.
Empty responses retain the cursor, and reconnects catch up without a fixed overlap.
An in-memory browser cache holds at most seven days / 350,000 samples, retaining completed historical coverage
until those samples expire or are evicted. Incomplete history continues retrying;
both charts retry the displayed history once per minute only while it contains known gaps
(missing pool block numbers or quote timestamp gaps over 900 seconds). Complete history
does not periodically reload; live polls continue fetching new samples.
Switching views reuses already fetched data. No samples are downsampled.
Live navigation reuses a completed cache endpoint only when it is within the
source's fetch-freshness threshold. Older or absent coverage triggers an immediate
request against the current live window and displays “Loading live prices…”
until completion, rather than treating historical samples as delayed live data.
Completed responses still report genuine stale prices or connection failures.
Hover redraws are coalesced per animation frame and reuse canvas buffers.
Caddy flushes upstream responses immediately.
Caddy compresses even streamed JSON responses with unknown lengths. Static files
use ETags for cache revalidation. The server embeds CSS and both JavaScript modules
in the HTML at startup, so initial loading needs only the document and price stream.
CSP hashes authorize exactly those embedded scripts and styles. Source files remain
separate for development. The price request also checks the release version,
avoiding a separate startup version request.

`VERSION` records a release such as `1.0.0+abcdef123456`. The release script
bumps the minor or patch number and records a 12-character SHA-256 prefix of
`RELEASE_FILES`; tests require that prefix to match the current files.
At startup the server snapshots those files and combines the recorded release
number with their actual hash. This keeps browser reload detection working even
while local edits have made the recorded hash stale. The footer shows this same
version on both chart pages. Responses carry `X-App-Version`,
and `/version` (also available at `/api/version`) returns the version even while price history is warming up.
The client checks this header before decoding price responses and reloads the
current URL on a mismatch, preserving the linked range/zoom. Live polling checks
the version through its price responses; there are no separate version requests.
Completed fixed views check for history repairs once per minute while visible. Returning
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

## Automatic pool-history gap repair

With `--gap-repair-history PATH`, a separate background worker fills internal
block-number gaps in the retained seven-day pool history using only Parity's
`https://polkadot-asset-hub-rpc.polkadot.io`. It waits for initial history loading
and gives live ingestion two minutes before considering a gap. Timestamp pauses
without missing block numbers are left intact. It does not extend the live tip.

Each pass repairs at most eight blocks, newest gaps first. All RPC requests are
sequential and spaced at least 500 ms apart, followed by a 60-second pause between
passes. Failures back off from two minutes up to fifteen minutes. The worker
verifies the Hub genesis and requires the gap's right boundary to be finalized.
It uses the shared pool reserve decoder; unavailable pools remain gaps.

Repairs preserve existing boundary samples and fit the chart's estimated
two-second clock between them. Original chain timestamps, block hashes, prices
and reserves are saved in a separate SQLite evidence database. Each small batch
is committed atomically before publication, resumes after restart, and expires
after seven days. The published backfill archive and collector database are never
modified. Repaired samples do not refresh the live freshness timestamp. Delayed
collector samples supersede repairs by block number, even if their estimated
timestamps differ. Open charts retry their displayed history once per minute
only while known gaps remain, including in fixed historical views.
