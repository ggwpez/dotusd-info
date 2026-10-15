# dotUSD.info

See how closely dotUSD tracks USDT on Polkadot Asset Hub. The chart shows up to
seven days of pool prices, so you can explore recent moves and zoom in on a
particular period.

**[Open the live chart →](https://dotusd.info)**

![The dotUSD peg chart](social-preview.png)

Prices come from the dotUSD → DOT → USDT pools. They are quoted against USDT and
do not include trading fees or slippage. Timestamps are estimated, and missing
blocks appear as gaps in the chart. Production gradually repairs internal gaps
from the Parity archive: up to eight blocks per pass, with a one-minute pause
between passes. Open charts pick up repairs automatically.

## Compare with CoinGecko

Open `/coingecko` for dotUSD / **USDT** calculated directly from Polkadot Hub
RPC: `USDT per DOT / dotUSD per DOT`. Both legs use the latest qualifying
finalized swap within 24 hours, or pool reserves at the same finalized block
when no trade qualifies. Swaps must contain at least 10,000 raw units on each
side. **No exchange ticker API or CoinGecko API is called.** This is a Hub-only
cross-price, not a reproduction of CoinGecko's aggregated USD price.

The worker verifies the Hub genesis hash and decodes swap events using runtime
metadata. Startup searches backward for both trades, then follows every finalized
block, including blocks missed between polls. Reads are bounded to 32 blocks per
poll; incomplete scans and RPC failures publish no new observation. Both pages
share the renderer, peg-rating thresholds and history stream.

Configure an HTTP archive RPC with `--hub-rpc URL` (default:
`https://polkadot-asset-hub-rpc.polkadot.io`). The endpoint must retain at least
24 hours of state for startup. Samples use finalized block timestamps; repeated
heads do not refresh freshness. Heads older than 120 seconds are rejected.
Persistence runs at most once per minute.

The comparison history uses `hub-rpc-last-trade-usdt-v3`. Old observed ticker
history is rejected. Validated on-chain reconstruction imports remain supported.
`backfill_quotes.py` reconstructs both trade legs for every finalized block in a
saved normal-chart raw response. It works newest-first in 1,024-sample batches,
using at most 32 blocks per archive batch and two RPC HTTP requests per second.
`import_quote_batches.py` imports those batches sequentially, at least 15 seconds
apart, checking service memory and the normal API before and after each import.
Historical chart imports run separately from the live RPC worker’s startup scan.

## Run it locally

The app uses Python 3.10+ and plain JavaScript. There is no frontend build step,
and the Hub swap decoder uses `substrate-interface`. Install the runtime dependencies:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

You will need a data source, though: a Prometheus instance with the pool-price
history. The live data collector and Prometheus are not included in this repo.
The server expects the `arb_peg_dotusd_usdt` and `arb_peg_block_number` metrics,
both with the `clock="estimated_2s"` label.

If Prometheus is running locally, start the chart with:

```sh
.venv/bin/python server.py --upstream http://127.0.0.1:9090/api/v1 --no-browser
```

Then open <http://127.0.0.1:3001>. Give the server a moment to load the price
history. It needs a working data source before it can show the chart.

If Prometheus is on another machine, connect through SSH:

```sh
.venv/bin/python server.py --ssh-host user@host
```

Replace `user@host` with your SSH destination. The app forwards the remote
machine's port 9090 and opens the chart in your browser. Running
`.venv/bin/python server.py` without options uses the SSH alias `server`.

For more options, including the listening port and archive path, run
`.venv/bin/python server.py --help`.

## Working on the code

The backend lives in `server.py`. The chart is built from `index.html`, `app.js`,
`ranges.js`, and `style.css`. Restart the server after editing these files, since
it loads them once at startup.

Run the tests from the repo root:

```sh
python3 -m unittest discover -s tests -v
```

The tests run locally and do not need access to the live data collector. JavaScript
checks use Node.js, or JavaScriptCore on macOS. If neither is available, those
checks are skipped.

## More details

- [How it works](docs/architecture.md): data loading, caching, and the price API.
- [Deployment and archive backfill](docs/deployment.md): running the app with
  systemd and Caddy, and importing older prices. The supplied configuration is
  for dotusd.info; adapt it for your own host. The optional backfill tool also
  needs `websocket-client` and `xxhash`.
- [Benchmarks](docs/benchmarks.md): performance measurements and how to repeat them.
