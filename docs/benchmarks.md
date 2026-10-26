# Benchmarks

[Project overview](../README.md)

Run the commands below from the repository root.

## Compact combined history responses

On October 26, 2026, a captured seven-day production stream contained 115,703
TI samples and 88,566 Treasury samples. Re-encoding the same snapshot with
lossless state ranges, with no HTTP compression, produced:

| Measurement | Raw-state stream | Range-state stream |
| --- | ---: | ---: |
| Entire response (bytes) | 8,125,009 | 1,612,393 |
| First metadata record (bytes) | 7,935,424 | 271 |

This is an 80.16% payload reduction. Every decoded state timestamp, value and
block matched the captured input exactly. The largest state record was 11,858
bytes. The actual local HTTP endpoint returned 1,612,424 bytes (different cursor
metadata), and the browser rendered all three series from that stream.
State compaction preserves gaps and repeated timestamps; its compression ratio
depends on balance changes and timestamp/block spacing, not downsampling.
This measures response size, not a production latency guarantee.


## Seven-day memory regression

On October 26, 2026, the deployed `4402d4e` image was compared with the
bounded-history implementation in isolated Python 3.12 containers on the same
host, each limited to 512 MiB with no swap, network or production data mounts.
A temporary SQLite fixture contained 302,400 prices and approximately 302,400
rows in each state series: a fully backfilled seven-day workload, denser than the
current production state history.

The scenario restored all three caches, applied small updates every second,
then requested seven-day combined NDJSON history once and in two groups of four
concurrent clients. Clients consumed 64 KiB chunks without retaining/decoding the
whole response inside the measured container. Peaks are cgroup `memory.peak`,
not sampled RSS. The fixture and container were removed after each run.

| Measurement | Original image | Optimized |
| --- | ---: | ---: |
| Restored container memory | 223.7 MiB | 207.4 MiB |
| Peak after one request | 433.6 MiB | 243.5 MiB |
| Four concurrent requests | OOM kill, exit 137 | All completed |
| Peak across all nine requests | 512 MiB limit reached | 298.4 MiB |

The optimized requests all returned HTTP 200; the single request took 6.11 s,
and the four-client groups completed within 25.47 s and 21.57 s respectively.
SQLite `quick_check` returned `ok`. These are bounded-load measurements, not a
guarantee of latency under arbitrary traffic. Historical scans serialize, while
live updates and streamed output remain concurrent. The test exercises memory
under full state coverage rather than accepting a healthy post-restart snapshot.

## Concurrent requests and bursts

Run `python3 tools/benchmark_requests.py --fixture /tmp/prices.json --output /tmp/results.json`
with a saved raw `/api/historic?series=price&format=raw` response. The tool starts an isolated
backend process, rebases the fixture timestamps to the present, and runs three
rounds each at 1, 4, 8, 16, 32, and 64 concurrent clients. Steady clients each
issue eight requests; burst clients each issue one. The workload mixes 70% live
polls, 20% initial HTML requests, and 10% full-history downloads. Use `--source`
for an older `server.py` and `--backlog` to compare identical TCP backlogs.

The October 10, 2026 local test used 87,195 actual samples (about two days), a
separate backend process, and no Caddy, TLS, or upstream worker. Results below
compare the old immediate-rejection server with TCP backlog 5 against four fixed
workers, 32 queued requests, and TCP backlog 64. Latency is p95 of successful
responses; failures are shown separately so fast rejections do not look faster.

| Workload | Before successes | Queued successes | Before p95 | Queued p95 |
| --- | ---: | ---: | ---: | ---: |
| Steady, 8 clients | 177 / 192 | 192 / 192 | 31.85 ms | 23.07 ms |
| Steady, 32 clients | 670 / 768 | 768 / 768 | 159.58 ms | 70.16 ms |
| Burst, 32 clients | 65 / 96 | 96 / 96 | 93.88 ms | 34.28 ms |
| Burst, 64 clients | 100 / 192 | 142 / 192 | 215.87 ms | 55.50 ms |

The 32-client bursts rejected four initial HTML loads before and none with the
queue. At the same TCP backlog of 64, the old server still rejected 43 of 96
requests in those bursts; the queued server rejected none. Queue limits still
reject overload, as the 64-client results show. These short local runs measure
burst handling, not production capacity or guarantees under sustained load.
All four combinations of old/new handling and backlog 5/64, with per-endpoint
latencies and status counts, are saved in
[`benchmarks/request-queue-2026-10-10.json`](../benchmarks/request-queue-2026-10-10.json).

## Encoded response cache benchmark

The same isolated request benchmark compared commit `965fecf` with response reuse
using 87,195 actual samples (about 2.2 days). The snapshot and request windows stay
fixed, so later cases measure warm-cache fan-out without upstream invalidation,
TLS or Caddy compression. They are not production capacity estimates.

| Workload | Before p95 | Cached p95 | Successful requests before / after |
| --- | ---: | ---: | ---: |
| 32 steady clients | 157.19 ms | 6.13 ms | 768 / 768 |
| 32 simultaneous requests, three bursts | 82.89 ms | 4.14 ms | 96 / 96 |

A separate in-process measurement reduced full response preparation from about
22.6 ms to 0.002 ms on a hit; a three-row poll went from 0.064 ms to 0.002 ms.
HTTP parsing, socket writes and downstream compression still cost work per client.
The bounded queue still rejects overload: the 64-request bursts rejected 19
requests before and 57 after. Faster responses do not guarantee fewer rejections
for a synchronized burst larger than the queue; scheduling affects admission.
Fresh generations and expired entries pay encoding cost once per matching key.
Tests cover concurrent miss sharing, failures, expiry, bounds, changed cursors,
equivalent windows, corrected rows and readiness changes.
Full results: [`benchmarks/response-cache-2026-10-10.json`](../benchmarks/response-cache-2026-10-10.json).

## Sample packing benchmark

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
