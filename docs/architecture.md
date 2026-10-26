# Architecture

[Project overview](../README.md)

## Data source

The server reads finalized dotUSD pool prices directly from a normal Polkadot
Asset Hub archive RPC, selected by `--hub-rpc` (default
`https://polkadot-asset-hub-rpc.polkadot.io`). No external collector, Prometheus,
private node or SSH tunnel is involved. A background worker verifies chain
identity and reads pool reserves at finalized block hashes. Each live poll reads
at most eight recent finalized blocks, then waits six seconds. Saved SQLite
history is restored in bounded 2,000-row pages independently of live polling,
so historical recovery does not hold up fresh prices. The estimated price clock
uses up to two seconds per block, compresses spacing between close immutable
boundaries, and never places the head in the future; evidence retains chain timestamps.
The bounded `/api/historic?series=price` endpoint reads the in-process cache;
visitor requests never query the chain. The cache retains at most seven days
and 350,000 samples, preserves gaps, and remains readable during RPC outages.
With `--gap-repair-history PATH`, new prices persist alongside existing repair
evidence without rewriting imported history. `--archive PATH` continues to
read the published SQLite archive. `historyReady` reports completion of local
history restoration, not complete chain coverage. Browsers retry incomplete
startup ranges, including fixed shared ranges.
`hub_quotes.py` owns the default endpoint and HTTP transport limits: each client
pauses 0.1 seconds before requests, uses a 10-second request timeout and rejects
responses larger than 16 MiB. Workers use those defaults rather than resetting
the transport configuration. These are per-client limits, not a global rate cap.

Readers capture an immutable snapshot under a brief lock, then scan it without
holding the lock. Writers publish rows and freshness together, so ongoing reads
retain a consistent view.
Each snapshot caches min/max prices and the highest block number for groups of
256 samples. Window extrema reuse fully covered groups and scan only partial
groups at the edges; live cursors skip groups containing no newer blocks.
The cache holds at most 1,368 summaries (about 107 KiB per snapshot on the server),
references existing samples, and never grows with the number of requested windows.
Summaries are rebuilt on merges, including corrections and eviction, and are
released with their snapshot when its readers finish.

Small price/state updates merge sorted rows and reuse immutable samples rather
than rebuilding full-history dictionaries. Live workers keep only their recent
block lookup; repair membership uses the ordered state snapshots.

Historic API requests may select any past `(start, end]` window, not just the
latest seven days. Each price/TI window must span one second to seven days,
with at most 350,000 samples per series; oversized windows return HTTP 400
rather than a truncated result. Explicit `end` (or `tiEnd`) anchors an old
window; omitted ends retain the rolling live-window behavior.
Windows crossing the live cache boundary also read persisted SQLite data.
Price repairs use an indexed time-range query; the read-only published archive
uses bounded seeks on its block-number index and validated monotonic clock.
Live cache rows override saved prices by block, and published archive
prices override repair evidence. Historical reads do not query
the chain, expand the live cache, or recover data that was never saved.
Absent samples remain gaps. Disk read failures return HTTP 503.
Historical price responses bypass the live response cache so saved corrections
are visible; historical TI returns full reset snapshots rather than live deltas.

Historical reads consume SQLite cursors and reuse cached row objects instead of
retaining a second decoded copy of seven days. They still inspect the complete
requested disk range so unpublished writes and corrections remain visible.
One shared admission lock serializes historical disk scans across the three
series to avoid concurrent SQLite row-decoding contention. It does not lock
live publication or network streaming; SQLite WAL permits writers during reads.
Cursors close before network output, so slow clients do not pin read transactions.

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
Four fixed HTTP workers
serve requests from a FIFO holding at most 32 waiting connections. A full queue
returns 503 with `Retry-After: 6`. Requests waiting two seconds expire with the
same response; idle-server expiry checks run every 0.5 seconds. The separate TCP
listen backlog is 64 connections. Responses stream in batches
and client sockets have a ten-second inactivity timeout.

All three series use lossless delta/range encoding by default. State stream
batches cover at most 1,024 input samples and are independently seeded; the
first NDJSON record contains only metadata, not full state-history arrays.
Nonstream JSON encodes at most 256 range records per write. `format=raw` remains
an explicit uncompressed diagnostic representation.

Prices follow dotUSD → DOT → USDT; timestamps are estimated and missing blocks
remain gaps. This is a USDT pool reference, with no fees or slippage included.

## Total issuance

`GET /api/historic` selects any nonempty unique subset of `price`, `ti`, and
`treasury`, in any order. Default JSON returns TI under `ti` as
`{encoding: "ranges-v1", ranges, cursor, reset, error}`; Treasury has the same
shape under `treasury`. The shared range encoding preserves every
`[timestamp_seconds, value_dotusd, block_number]` sample, including repeated
timestamps, gaps and value changes. `format=raw` returns `{rows, cursor, reset, error}`
for each state series instead.
Generic `start`/`end` select the window (default: last hour).
`tiStart`/`tiEnd` and `treasuryStart`/`treasuryEnd` independently override state
bounds when price polls use a block cursor. Omitting the respective end in live
chart requests allows the server to advance the window.

The opaque `tiCursor` and `treasuryCursor` independently return only changed
samples, including older backfill and corrections, rather than downloading the full history on every poll.
A bounded 4,096-change journal tracks revisions without per-client server state.
Missing, expired, or previous-server cursors return `reset: true` with a fresh
selected-window snapshot. Unchanged polls return no rows for that series. Rows
are ordered by finalized block number, with nondecreasing timestamps.
With `--gap-repair-history PATH`, the seven-day in-memory history includes
restored, live and backfilled samples, with older saved samples available through
explicit historical windows; missing blocks are omitted, so this is
not a promise of complete coverage. Without that flag, the server retains at
most the latest 15 finalized Polkadot Hub blocks. This is total issued dotUSD
(asset ID 7873), not circulating supply or a pool reserve.
Timestamps come from `Timestamp.Now` in each block, unlike the
estimated clock used by pool-price history. The Hub runtime's timestamp
`MinimumPeriod` is zero, so consecutive blocks can legitimately share a timestamp;
all such blocks remain in the response without synthetic time offsets. Values
are JSON numbers in whole dotUSD units; integer base-unit supply is divided by
the decimals decoded from that block's `Assets.Metadata`.

An independent `issuance.py` background worker starts with the server. It uses
`--hub-rpc`, whose default is the public Parity archive endpoint
`https://polkadot-asset-hub-rpc.polkadot.io`; the recent issuance preview needs
neither a private node nor a preloaded archive. A local run is:

```sh
.venv/bin/python server.py --hub-rpc https://polkadot-asset-hub-rpc.polkadot.io --no-browser
```

Open <http://127.0.0.1:3001> and enable issuance in Settings. The cached endpoint
is also readable directly at <http://127.0.0.1:3001/api/historic?series=ti>. Install
`requirements.txt` first; the storage-key hash uses the existing `xxhash`
dependency. `--hub-rpc URL` overrides the RPC for live prices, issuance and
price/issuance/Treasury history repair.

The worker reuses the bounded `HubRpc`/`archive_reader.Rpc` storage reader,
verifies the Hub genesis before accepting finality, and pins timestamp, asset
details and metadata reads to each finalized block hash. Calls are sequential
with a 100 ms pause, a ten-second request timeout and a 16 MiB response limit.
Each refresh fetches at most 15 blocks; already cached finalized blocks are
reused. It waits six seconds between passes and after failures. A reconnect
skips older blocks rather than backfilling. Its recent window is published only
when the entire refresh succeeds. Without gap repair, this is the whole in-memory
issuance history. With gap repair, successful live samples also merge into the
retained issuance cache and persist in the same SQLite database as archive
repairs. SQLite history is kept indefinitely; startup restores only the latest
seven days, and only the in-memory issuance cache expires old samples.
The separate repair worker fills missing historical issuance within the retained
price block bounds, even when no price gaps remain; it does not enlarge the live
worker's 15-block fetch window.

Requests read cached rows or bounded SQLite windows and never trigger RPC work.
Without saved history, TI returns no samples and a loading `ti.error`.
TI-only and combined requests return HTTP 200 even while price history warms;
combined responses then include empty prices with `historyReady: false`.
Recent price-only requests return HTTP 503 until prices are ready; older saved
windows remain readable during startup. On live RPC or
decoding failure, retained TI remains available with a non-null `ti.error`;
a successful live refresh clears it. No synthetic values, zero replacements
for missing state, or extrapolated timestamps are generated.
`Cache-Control: no-store` and
`X-App-Version` follow the existing API conventions. Shutdown signals and joins
the issuance worker alongside the price and repair workers.

The chart uses an independent linear right axis. It connects issuance samples
up to 1,800 blocks apart, showing the coarse backfill before dense filling finishes.
Larger gaps stay disconnected; interpolation does not create API or tooltip samples. Both series use
the normal range buttons and drag-to-zoom controls. With issuance enabled, the
price tooltip binary-searches issuance by block number, not estimated price
timestamp. It shows the matching supply in compact dotUSD units, or
`TI: unavailable` for missing blocks. Disabling issuance removes this tooltip
row. There is no separate issuance legend or status readout.

### Storage decoding evidence

The SDK's [`Assets.Asset` storage declaration](https://github.com/paritytech/polkadot-sdk/blob/master/substrate/frame/assets/src/lib.rs)
uses `Blake2_128Concat` over the SCALE `u32` asset ID. Its
[`AssetDetails` definition](https://github.com/paritytech/polkadot-sdk/blob/master/substrate/frame/assets/src/types.rs)
places owner, issuer, admin and freezer before `supply`. The
[Polkadot Hub runtime configuration](https://github.com/polkadot-fellows/runtimes/blob/main/system-parachains/asset-hubs/asset-hub-polkadot/src/lib.rs)
uses the shared `AccountId`, `Balance` and trust-backed asset ID types from
[`parachains-common` primitives](https://github.com/paritytech/polkadot-sdk/blob/master/cumulus/parachains/common/types/src/lib.rs):
`u128` balances and `u32` asset IDs; `MultiSignature`'s signer maps to
[`AccountId32`](https://github.com/paritytech/polkadot-sdk/blob/master/substrate/primitives/runtime/src/lib.rs).
Thus supply is the little-endian `u128` at bytes 128–143 after four 32-byte
accounts, not the first 16 bytes. The decoder requires the complete 190-byte
layout (three balances, a boolean, three `u32` counters and status after those
accounts) and valid boolean/status discriminants, failing closed on unexpected
layouts. Asset metadata decimals use the existing shared decoder.

## Treasury holdings

`treasury.py` tracks asset 7873 in the Treasury dotUSD holding account
[`166ASLGTqFioubHFAKzbSEyN9BgVQxpF2ruxr3uPNAB7RBQr`](https://assethub-polkadot.subscan.io/account/166ASLGTqFioubHFAKzbSEyN9BgVQxpF2ruxr3uPNAB7RBQr).
Its checksum-verified SS58 address decodes to AccountId32
`e11d29483dc5b8673cbac3ada8e76a8fb747cf4a78107845db621adfd32709fb`,
not the generic Treasury pallet account.
[`Assets.Account`](https://paritytech.github.io/polkadot-sdk/master/pallet_assets/pallet/type.Account.html)
uses `Blake2_128Concat` for both the SCALE u32 asset ID and AccountId32.
The [account layout](https://github.com/paritytech/polkadot-sdk/blob/master/substrate/frame/assets/src/types.rs)
starts with the u128 balance, followed by account status and existence reason.
The decoder validates the complete reason-dependent layout, including deposits;
a missing account means zero, while unsupported data leaves a retryable gap.

The archive worker uses the same 1,800-block recursive grid for both series,
selecting blocks missing either series. `read_history` requests missing supply
and/or account keys in one pinned `state_queryStorageAt` call, sharing metadata
and timestamp decoding. It omits saved series and skips fully populated blocks.
Independent decoder failures preserve the other series' valid sample.
Price repair and both state series share the existing eight-block pass limit.
`TreasuryHistory` stores `(number, timestamp, balance)` in the repair database's
`treasury` table indefinitely and restores seven days into memory. It shares
the bounded history cache implementation with TI, including an independent
revision journal and historical SQLite selection. The HTTP handler and archive
worker use the same cache instance, so new backfill points reach subscriptions.
There is no Treasury live RPC worker; without archive repair its API is empty.
The existing issuance setting shows both series on a shared right linear axis:
TI blue, Treasury purple. Exact-block hover values distinguish measured data
from coarse-line interpolation.

`treasury_identity` records the asset ID and AccountId32 associated with saved
holdings. On first database access, missing or mismatched identity atomically
invalidates only Treasury rows before any restore or historical read. This
corrects the earlier generic-pallet-account samples; TI and price history are
untouched. Correct-account rows survive subsequent restarts, while the normal
progressive grid refills invalidated history.


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

The chart has one history subscription: one six-second polling loop and one
abort controller for `/api/historic`. The TI setting selects `series=price`
or `series=price,ti,treasury`; it does not start another fetch loop. Navigation
and setting changes cancel the old request before replacing it. Hidden pages
do not poll. With TI enabled, fixed historical views also poll for repair deltas.
TI and Treasury cursors advance only after a complete response; stale or
interrupted responses cannot overwrite the current selection. Both series merge
deltas by block and track independent coverage; expanding the selected window
requests fresh snapshots for any series without full coverage.

The browser renders cached data immediately and loads the selected history window
in one request. `?format=stream` returns newline-delimited JSON: a
`ranges-stream-v2` metadata line (including full-window extrema and requested
state `{cursor, reset, error}` fields), price `{"ranges":[...]}` batches of up to
8,192 samples, state `{"series":"ti","ranges":[...]}` or
`{"series":"treasury","ranges":[...]}` batches of up to 1,024 samples, then a
`{"done":true}` line. Each batch starts with its own absolute seed; it never
depends on the preceding batch. Empty state selections emit metadata but no batches.
Prices draw before state transfer completes. The client expands state batches
into pending rows and commits them and their cursors only after a complete stream;
price ranges remain compact in the browser. Network reads may split or combine lines.
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
price history retries the displayed window once per minute only while it contains
known missing pool block numbers. Complete price history does not periodically
reload; live polls continue fetching new samples. TI repair deltas share those
requests, without requiring a full-history reload.
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
in the HTML at startup, so initial loading needs only the document and history stream.
CSP hashes authorize exactly those embedded scripts and styles. Source files remain
separate for development. The history request also checks the release version,
avoiding a separate startup version request.

`VERSION` is internal integrity metadata, recording a release such as
`1.0.0+abcdef123456`. Before running the suite, `just test` prepares the release:
if `RELEASE_FILES` changed, it bumps the patch number (or minor with
`just test minor`) and records their 12-character SHA-256 prefix. Unchanged
sources keep the same version. Tests and deployment reject stale checksums.
The workflow is test, make the final commit including `VERSION` on `master`,
then deploy from a clean checkout. Do not amend the commit after deployment.
Deployment does not rerun tests and requires the expected public version
within 60 seconds.

Production identity is `MAJOR.MINOR.PATCH+<7-hex Git commit SHA>`, not the
content checksum. Deployment generates `DEPLOY_COMMIT` beside `server.py`,
containing the full HEAD SHA followed by a newline, before restarting the
service. At startup the server snapshots the source files and verifies their
checksum against `VERSION` before combining its release number with the
deployment commit's first seven characters. The suffix can be inspected with
`git show <short-SHA>`. Keeping `VERSION` checksum-based avoids
embedding a commit's own hash in that commit.

Without `DEPLOY_COMMIT`, local development combines the recorded release number
with the actual source checksum. This keeps browser reload detection working
even while local edits have made the recorded checksum stale. The footer shows
the resulting version on both chart pages. Responses carry `X-App-Version`,
and `/version` (also available at `/api/version`) returns the same version even
while price history is warming up.
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

`/api/historic?series=price` returns `encoding: "ranges-v1"` and a `ranges` array. The first
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

## Automatic price and issuance history repair

With `--gap-repair-history PATH`, a separate background worker fills internal
block-number gaps in the retained seven-day pool history and missing total
issuance throughout that history's block range. Issuance is eligible even where
all prices are present. It uses Parity's
`https://polkadot-asset-hub-rpc.polkadot.io` by default (overridable with
`--hub-rpc`). Archive backfill waits for initial price-history loading; price gaps
also get a two-minute live-ingestion grace period. Missing issuance can be fetched
through the newest finalized price block without that grace period. Timestamp pauses without missing
block numbers are not price gaps. It does not extend the live price tip.

Each pass visits at most eight archive blocks shared between price repair and
issuance backfill, rather than granting each series its own budget. Issuance first
fills missing block numbers divisible by 1,800 across the retained price range,
newest-first, then recursively bisects those fixed intervals: 900-block spacing,
then 450, 225, and onward to individual blocks. Each level precedes finer levels
and runs newest-first. Midpoints round down, splitting odd intervals into
floor/ceiling halves while preserving all earlier grid points. The fixed grid
remains stable as the live tip advances and after restart; saved samples and
duplicate rounded midpoints are skipped.
Price gaps retain a share of the batch when both need work. Unused batch slots
extend the oldest saved price history backward toward the seven-day window;
this gradual recovery does not delay the independent live price poll.
All RPC requests are sequential and spaced at least 100 ms apart, followed by a
one-second pause between successful passes. Failures back off from two minutes up to fifteen
minutes. The worker verifies the Hub genesis and finalized boundaries.
It uses the shared pool reserve decoder and issuance storage decoder; missing
on-chain state is not replaced with synthetic values. The independent live
issuance poll keeps its existing cadence and does not use this repair budget.
Archive failures are logged and backed off; they do not replace successful
issuance samples or change the endpoint's live-refresh `error`.

Price repairs preserve existing boundary samples and fit the chart's estimated
two-second clock between them. Original chain timestamps, block hashes, prices
and reserves are saved in a separate SQLite evidence database. Each small batch
is committed atomically before publication and kept indefinitely. Restart loads
only the latest seven days into the chart cache without deleting older evidence.
Published archive price rows are not modified. When both storage flags share a
file, new evidence is written only to its repair/state tables. Repaired samples
do not refresh the live freshness timestamp. Live RPC samples supersede repairs by block number, even if their estimated
timestamps differ. Open charts retry their displayed history once per minute
only while known gaps remain, including in fixed historical views.
