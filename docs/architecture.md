# Architecture

[Project overview](../README.md)

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

The browser renders cached data immediately and loads the selected history window
in one request. `?format=stream` returns newline-delimited JSON: a
`ranges-stream-v1` metadata line (including full-window extrema), independently
packed `ranges` lines of up to 8,192 samples each, then a `{"done":true}` line.
The client decodes and draws complete lines as they arrive, without expanding
ranges or acknowledging batches. Network reads may split or combine lines.
Full-window extrema keep the vertical scale stable during loading. Only a
successfully finished stream enters the cache; interrupted streams retry.
Live polls send `afterBlock` with the last received finalized block number; the
server returns only newer blocks within the displayed window. Empty responses
retain the cursor, and reconnects catch up from it without a fixed overlap.
An in-memory browser cache holds at most seven days / 350,000 samples, retaining completed historical coverage
until those samples expire or are evicted. Incomplete history continues retrying;
reload the page to discover later archive imports before the block cursor.
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
