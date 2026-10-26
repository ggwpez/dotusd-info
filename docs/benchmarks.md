# Benchmarks

[Project overview](../README.md)

Run commands from the repository root. Results are from the stated runs, not
capacity guarantees.

## Combined history response size

Seven-day `series=price,ti,treasury&format=stream` snapshot: 115,703 TI and
88,566 Treasury samples, no HTTP compression.

| Measurement | Raw state rows | State ranges |
| --- | ---: | ---: |
| Response (bytes) | 8,125,009 | 1,612,393 |
| First record (bytes) | 7,935,424 | 271 |

Every decoded state sample matched the input exactly; the largest state record
was 11,858 bytes.

## Seven-day memory

Isolated Python 3.12 container, 512 MiB, no swap. Fixture: 302,400 prices and
about 302,400 rows per state series (fully backfilled seven days). The run
restored all caches, applied updates every second, then requested seven-day
combined NDJSON once and in two groups of four concurrent clients. Peaks are
cgroup `memory.peak`.

| Measurement | Result |
| --- | ---: |
| Restored memory | 207.4 MiB |
| Peak after one request | 243.5 MiB |
| Peak across all nine requests | 298.4 MiB |
| Single request | 6.11 s |
| Four-client groups | 25.47 s, 21.57 s |

All requests returned HTTP 200.

## Concurrent requests and bursts

```sh
python3 tools/benchmark_requests.py --fixture /tmp/prices.json --output /tmp/results.json
```

The fixture is a saved `/api/historic?series=price&format=raw` response. The
tool starts an isolated backend, rebases timestamps to now, and runs three rounds
at 1, 4, 8, 16, 32 and 64 clients. Steady clients issue eight requests, burst
clients one. The mix is 70% live polls, 20% HTML, 10% full history. `--source`
selects another `server.py`; `--backlog` sets the TCP backlog.

87,195 samples, no Caddy or TLS, p95 of successful responses:

| Workload | Successes | p95 |
| --- | ---: | ---: |
| Steady, 8 clients | 192 / 192 | 23.07 ms |
| Steady, 32 clients | 768 / 768 | 70.16 ms |
| Burst, 32 clients | 96 / 96 | 34.28 ms |
| Burst, 64 clients | 142 / 192 | 55.50 ms |

The 64-client bursts exceed the 32-connection queue; the excess gets 503.
Raw results: [`benchmarks/request-queue-2026-10-10.json`](../benchmarks/request-queue-2026-10-10.json).

With the encoded response cache warm (same workload):

| Workload | p95 | Successes |
| --- | ---: | ---: |
| 32 steady clients | 6.13 ms | 768 / 768 |
| 32 simultaneous requests, three bursts | 4.14 ms | 96 / 96 |

A cache hit prepares a full response in about 0.002 ms. Raw results:
[`benchmarks/response-cache-2026-10-10.json`](../benchmarks/response-cache-2026-10-10.json).

## Price encoding

```sh
python3 tools/benchmark_packing.py [--samples snapshot.json]
```

Compares lossless encodings of production history or a JSON array of
`[timestamp, price, block]` samples; Python 3.14 adds zstd. Every candidate is
decoded and checked against every original sample.

85,182 observed samples (51.92 hours, 96.73% unchanged adjacent prices), repeated
to 275,652 samples for seven days; compressed in six-hour chunks with gzip
level 6 and zstd level 3:

| Encoding | JSON | gzip | zstd |
| --- | ---: | ---: | ---: |
| `[timestamp, price, block]` rows | 12,403,623 B | 1,465,086 B | 1,098,650 B |
| Compact rows | 11,576,695 B | 1,435,523 B | 1,091,445 B |
| Omit unchanged prices | 6,580,383 B | 1,414,119 B | 1,175,585 B |
| Timestamp/block deltas | 1,831,703 B | 112,592 B | 111,558 B |
| Runs of identical deltas and prices | 312,227 B | 99,665 B | 100,107 B |

The API's range encoding uses runs. In headless Chrome, building the range cache
took a median 1.5 ms versus 12.2 ms for parsing raw JSON, and preparing drawing
points 0.8 ms versus 1.4 ms (20 runs after five warmups).
