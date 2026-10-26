# Architecture

[Project overview](../README.md)

## Data flow

- `server.py` serves the chart and `/api/historic`. Requests read in-process
  caches or bounded SQLite windows and never query the chain.
- The live price worker polls the archive RPC selected by `--hub-rpc` (default
  `https://polkadot-asset-hub-rpc.polkadot.io`). It verifies the Hub genesis,
  reads pool reserves at finalized block hashes, fetches at most eight recent
  blocks per pass, and waits six seconds between passes.
- `issuance.py` polls total issuance independently.
- With `--gap-repair-history PATH`, a repair worker backfills price gaps,
  issuance and Treasury holdings, and new samples persist to that SQLite file.
- `--archive PATH` reads the published, read-only price archive.

Prices follow dotUSD → DOT → USDT: a USDT pool reference without fees or
slippage. Pool timestamps are estimated; issuance and Treasury use each block's
`Timestamp.Now`. Missing blocks remain gaps; nothing is synthesized.

`hub_quotes.py` owns the RPC default and transport limits: 100 ms between
requests, 10-second timeout, 16 MiB response cap. Limits apply per client, not
globally.

## Price cache

- At most seven days / 350,000 samples, readable during RPC outages.
- Startup restores saved history in 2,000-row pages, independently of live
  polling. `historyReady` means local restoration finished, not complete chain
  coverage.
- The estimated clock allows up to two seconds per block, compresses spacing
  between close immutable boundaries, and never places the head in the future.
- Readers take an immutable snapshot under a brief lock and scan it unlocked;
  writers publish rows and freshness together.
- Each snapshot summarizes 256-sample groups (min/max price, highest block):
  at most 1,368 summaries, about 107 KiB. Window extrema scan only partial edge
  groups; live cursors skip groups without newer blocks.
- Small updates merge sorted rows and reuse immutable samples.

## Historic API

`GET /api/historic?series=price` currently serves prices only. Mixed requests
such as `series=price,ti,treasury` also return only prices, ignoring state windows
and cursors without reading state history. Requests for only TI/Treasury return
HTTP 400. State collection and persistence continue unchanged.
The state encoding and cursor machinery described below is retained internally
but is temporarily unavailable through HTTP.

### Parameters

| Parameter | Meaning |
| --- | --- |
| `start`, `end` | `(start, end]` window; default last hour. Omit `end` for the rolling live window. |
| `startpoint` | Only blocks greater than this number. Takes precedence over `start`. |
| `windowStart`, `windowEnd` | Full-window price extrema for a stable chart scale. |
| `tiStart`/`tiEnd`, `treasuryStart`/`treasuryEnd` | Per-series state bounds. |
| `tiCursor`, `treasuryCursor` | Opaque state cursors for incremental updates. |
| `format` | `stream` (NDJSON), `raw`, or default packed JSON. |

Each window spans one second to seven days with at most 350,000 samples per
series. Larger requests return HTTP 400, never a truncated result. Live
responses report the server-chosen boundary in `X-History-End`.

State cursors return only changed samples, including older backfill and
corrections, from a bounded 4,096-change journal without per-client server
state. Missing, expired or previous-server cursors return `reset: true` with a
fresh snapshot. State rows are ordered by block with nondecreasing timestamps;
consecutive blocks may share a timestamp because the runtime's `MinimumPeriod`
is zero.

### Formats

All formats are lossless.

- **Default:** price `{encoding: "ranges-v1", ranges, fetchedAt, historyReady,
  extremes}`; each state series `{encoding: "ranges-v1", ranges, cursor, reset,
  error}`.
- **`stream`** (used by the chart): line one is `ranges-stream-v2` metadata with
  price fields and each state's `{cursor, reset, error}`. Then come price
  `{"ranges":[…]}` batches of up to 8,192 samples, state
  `{"series":"ti"|"treasury","ranges":[…]}` batches of up to 1,024 samples, and
  `{"done":true}`. Each batch is independently seeded; empty selections emit
  metadata only.
- **`raw`:** price `samples` and state `rows` as `[timestamp, value, block]`,
  for diagnostics and benchmarks.

Range records start with `[timestamp, block, value]`, followed by
`[count, secondsPerStep, blocksPerStep, optionalNewValue]`; an omitted value
repeats the previous one. `[0, timestamp, block, value]` re-anchors at unknown
blocks or timestamps that cannot be reconstructed exactly. Fractional timestamps
stay individual records to avoid rounding drift.

### Status and errors

- Recent price requests return 503 until history is restored. Older saved
  windows can return 200 with `historyReady: false` and empty prices.
- State-only selections return 400 regardless of loading, saved data or RPC status;
  mixed selections follow the normal price response and omit state series.
- Disk read failures return 503. Responses carry `Cache-Control: no-store` and
  `X-App-Version`.

### Historical windows

- Windows crossing the live boundary also read SQLite: an indexed time range on
  repairs and bounded block-index seeks on the published archive.
- Per block, live cache rows override the published archive, which overrides
  repair evidence.
- Historical reads never query the chain, grow the live cache, or recover unsaved
  data. They bypass the response cache so saved corrections appear; state returns
  full reset snapshots.
- SQLite cursors reuse cached row objects rather than decoding a second copy,
  and close before network output. They scan the full requested range so
  unpublished writes are visible.
- One admission lock serializes historical disk scans across all three series.
  Live publication and streaming continue during scans (SQLite WAL).

## Request handling

- Four fixed HTTP workers serve a FIFO of at most 32 waiting connections; the TCP
  backlog is 64. A full queue or a two-second wait returns 503 with
  `Retry-After: 6`. Idle sockets time out after ten seconds.
- Encoded price responses share a six-second LRU: at most 32 entries and 8 MiB,
  4 MiB per response. Keys cover the history generation, selected rows and
  extrema boundaries, freshness, format and cursor, so equivalent windows share
  entries while corrections and cursor changes never serve stale output.
  Concurrent misses share one encoding job; raw and oversized responses stream
  without retention.
- State is encoded per request and never cached with prices. Output is written
  in bounded batches, so no response materializes a full-history string.

## Total issuance

TI is the total supply of asset 7873, not circulating supply or a pool reserve.
Values are whole dotUSD: base units divided by the decimals in that block's
`Assets.Metadata`.

`issuance.py` pins timestamp, asset details and metadata reads to each finalized
block hash. A refresh fetches at most 15 blocks, reuses cached ones, publishes
only on full success, and waits six seconds. After a reconnect it skips older
blocks; with repair enabled, the repair worker fills them. Without
`--gap-repair-history`, those 15 blocks are the entire history. With it, samples
persist indefinitely and
startup restores seven days into memory.

### Storage decoding evidence

The SDK's [`Assets.Asset` storage declaration](https://github.com/paritytech/polkadot-sdk/blob/master/substrate/frame/assets/src/lib.rs)
uses `Blake2_128Concat` over the SCALE `u32` asset ID. Its
[`AssetDetails` definition](https://github.com/paritytech/polkadot-sdk/blob/master/substrate/frame/assets/src/types.rs)
places owner, issuer, admin and freezer before `supply`. The
[Polkadot Hub runtime configuration](https://github.com/polkadot-fellows/runtimes/blob/main/system-parachains/asset-hubs/asset-hub-polkadot/src/lib.rs)
uses `parachains-common`'s [primitive types](https://github.com/paritytech/polkadot-sdk/blob/master/cumulus/parachains/common/types/src/lib.rs):
`u128` balances, `u32` asset IDs and
[`AccountId32`](https://github.com/paritytech/polkadot-sdk/blob/master/substrate/primitives/runtime/src/lib.rs)
accounts. Supply is therefore the little-endian `u128` at bytes 128–143, after
four 32-byte accounts. The decoder requires the complete 190-byte layout and
valid boolean/status discriminants, failing closed otherwise.

## Treasury holdings

`treasury.py` tracks asset 7873 in the Treasury dotUSD holding account
[`166ASLGTqFioubHFAKzbSEyN9BgVQxpF2ruxr3uPNAB7RBQr`](https://assethub-polkadot.subscan.io/account/166ASLGTqFioubHFAKzbSEyN9BgVQxpF2ruxr3uPNAB7RBQr)
(AccountId32 `e11d29483dc5b8673cbac3ada8e76a8fb747cf4a78107845db621adfd32709fb`),
not the generic Treasury pallet account.
[`Assets.Account`](https://paritytech.github.io/polkadot-sdk/master/pallet_assets/pallet/type.Account.html)
hashes both the asset ID and account with `Blake2_128Concat`; the
[account layout](https://github.com/paritytech/polkadot-sdk/blob/master/substrate/frame/assets/src/types.rs)
starts with the `u128` balance. The decoder validates the full reason-dependent
layout. A missing account records zero; unsupported data or RPC failures leave a
gap for retry.

- There is no live Treasury poll; without archive repair the series is empty.
- `TreasuryHistory` stores `(number, timestamp, balance)` in the `treasury`
  table and shares TI's cache implementation, revision journal and historical
  reads. The repair worker continues updating the shared instance while public
  state-series access is disabled.
- `treasury_identity` records the asset and account behind saved rows. A missing
  or mismatched identity atomically invalidates Treasury rows only, before any
  read; the repair grid then refills them.

## History repair

`--gap-repair-history PATH` starts a worker that fills internal price gaps and
missing TI and Treasury samples across the retained price block range.

- It starts after initial history loading. Price gaps get a two-minute grace
  period for live ingestion; timestamp pauses without missing blocks are not gaps.
  It never extends the live tip.
- Each pass visits at most eight archive blocks shared by all series, then pauses
  one second. Failures back off from 30 seconds to two minutes and never change the
  live `error` field.
- State fills a fixed grid of blocks divisible by 1,800, newest first, then
  bisects: 900, 450, 225, and so on down to single blocks. Each level runs
  newest first; odd intervals split into floor/ceiling halves. The grid stays
  stable across restarts and tip growth.
- One `state_queryStorageAt` call per block requests only the missing TI and/or
  Treasury keys. One series' decoder failure keeps the other's sample.
- Price gaps keep a share of each pass. Unused slots extend the oldest saved
  price history back toward seven days.

Repaired prices keep existing boundary samples and fit the estimated
two-second clock between them. Original timestamps, hashes, prices and reserves
are committed atomically in small batches to SQLite and kept indefinitely.
Published archive rows are never modified. Repairs do not refresh live
freshness, and live samples supersede repairs for the same block.

## Browser

### Chart

- The white peg reference breaks only where the price stroke enters a
  two-pixel band around it, respecting gaps, zoom and scale.
- **Log scale** applies `sign(bps) * log1p(abs(bps))` around the peg; labels,
  ratings and tooltips keep their units and values.
- Units (`scale=bps|percent|dollars`) and axis (`axis=linear|log`) are written
  to the URL. Valid URL values override saved preferences without changing them;
  Back/Forward restores them without refetching. TI visibility is forced off;
  saved preferences and `ti=1` links cannot enable it.
- Historical and zoomed views show `-` for peg tightness, current price and spot
  price; range low/high remains. Dollar mode hides the redundant spot price with
  `visibility: hidden`.
- The issuance setting and its explanatory text are removed. The state-series
  rendering machinery is retained but inactive.
- Hover redraws are coalesced per animation frame.

### Loading

- One subscription: a six-second poll and one abort controller for
  `/api/historic?series=price`. Hidden pages do not poll; navigation cancels
  the previous request. No TI or Treasury windows or cursors are requested.
- Live polls send `startpoint` with the last finalized block and omit `end`.
  Initial loads use timestamps and `windowStart` for early extrema.
- Prices draw while streaming. State rows and cursors commit only after
  `{"done":true}`; interrupted or stale responses change nothing and retry.
- The browser cache keeps compact ranges for at most seven days / 350,000 samples.
  Overlaps replace the requested interval, adjacent ranges coalesce, and samples
  are reconstructed on demand for hover and keyboard navigation. Rendering keeps
  pixel endpoints and extrema; nothing is downsampled.
- Price history with known missing blocks retries the displayed window once per
  minute.
- Returning to live reuses cached data only within the freshness threshold;
  otherwise it shows “Loading live prices…” until the current window arrives.

### Delivery

The server embeds CSS and both JavaScript modules in the HTML at startup;
CSP hashes authorize exactly those blocks. Static files use ETags. Caddy
flushes and compresses streamed responses.

### What's new

`CHANGELOG.md` ships with the release; startup parses it strictly and renders
its latest 10 entries into the bell menu. A yellow dot shows when the newest
version is newer than the version stored in `dotusd-changelog-seen` (or none is
stored); opening the menu stores it. The automatic reload after a deployment
brings the new list and marker.

## Releases

`VERSION` records the release number and a 12-hex SHA-256 prefix of
`RELEASE_FILES`; tests and deployment reject a stale checksum. Production
reports `MAJOR.MINOR.PATCH+<7-hex commit>`: deployment writes the full HEAD SHA
to `DEPLOY_COMMIT`, and the server checks the source checksum before using it.
Locally, without `DEPLOY_COMMIT`, the suffix is the current source checksum.

The version appears in the footer, `X-App-Version`, and `/version`
(`/api/version`), including while history warms. The browser compares
`X-App-Version` on every history response and reloads the current URL on a
mismatch. Assets are frozen at startup; restart the server to pick up changes.
See the [README](../README.md#working-on-the-code) for the release workflow.

## Health

`/health` reports chain freshness for monitors and the container `HEALTHCHECK`:
`{"status", "finalizedBlock", "finalizedAgeSeconds", "staleAfterSeconds"}`.
The age is wall time minus the on-chain timestamp of the newest finalized head
the live poll has read, recorded even when the pools have no price. Finality
normally keeps it near a minute. It returns 200 `ok` up to 180 seconds (the
chart's "Data delayed" cutoff), 503 `stale` beyond that, and 503 `starting`
before the first head is read after startup. A failing RPC leaves the last head
in place, so the age keeps growing.

The image `HEALTHCHECK` requests `/health` every 30 seconds after a 60-second
start period; three consecutive non-200 responses or timeouts mark the container
unhealthy about 3–3.5 minutes after finalized data stops advancing. Docker
does not restart unhealthy containers; the status is for operators and the
deployment platform. `/version` remains the release check used by deployment.
