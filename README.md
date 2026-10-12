# dotUSD.info

See how closely dotUSD tracks USDT on Polkadot Asset Hub. The chart shows up to
seven days of pool prices, so you can explore recent moves and zoom in on a
particular period.

**[Open the live chart →](https://dotusd.info)**

![The dotUSD peg chart](social-preview.png)

Prices come from the dotUSD → DOT → USDT pools. They are quoted against USDT and
do not include trading fees or slippage. Timestamps are estimated, and missing
blocks appear as gaps in the chart.

## Compare with CoinGecko

Open `/coingecko` for dotUSD / **USDT** calculated entirely from Asset Hub DEX
last-trade tickers: `DOT_1984.last_price / DOT_7873.last_price`. Both legs use the
DEX's qualifying-trade rules and its reserve fallback when no trade qualifies.
**No CoinGecko requests or prices are used.** The route name refers to the DEX's
CoinGecko-compatible ticker format, not an external price source.

Both pages refresh every two seconds and share the renderer, subscription logic,
peg-rating thresholds and history stream. The normal page retains its synchronized
pool-reserve calculation. The comparison worker polls the DEX every two seconds;
actual price changes depend on the upstream snapshot. Failed requests preserve
last successful data and freshness. Persistence runs at most once per minute.

The comparison history uses `dex-last-trade-usdt-v2`. Old CoinGecko-derived history
and imports are rejected rather than mixed into this calculation. The old
`backfill_quotes.py` reconstruction tool is legacy and cannot produce v2 imports.

## Run it locally

The app uses Python 3.10+ and plain JavaScript. There is no frontend build step,
and the chart server needs no extra Python packages.

You will need a data source, though: a Prometheus instance with the pool-price
history. The live data collector and Prometheus are not included in this repo.
The server expects the `arb_peg_dotusd_usdt` and `arb_peg_block_number` metrics,
both with the `clock="estimated_2s"` label.

If Prometheus is running locally, start the chart with:

```sh
python3 server.py --upstream http://127.0.0.1:9090/api/v1 --no-browser
```

Then open <http://127.0.0.1:3001>. Give the server a moment to load the price
history. It needs a working data source before it can show the chart.

If Prometheus is on another machine, connect through SSH:

```sh
python3 server.py --ssh-host user@host
```

Replace `user@host` with your SSH destination. The app forwards the remote
machine's port 9090 and opens the chart in your browser. Running
`python3 server.py` without options uses the SSH alias `server`.

For more options, including the listening port and archive path, run
`python3 server.py --help`.

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
