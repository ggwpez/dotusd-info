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

Open `/coingecko` for the same chart using CoinGecko's aggregated dotUSD **USD**
prices. The footer links switch between sources while preserving the range and
zoom. Both views use the same peg-rating thresholds, renderer and history stream.
CoinGecko prices include market/trade effects and are not directly equivalent to
the pool reserve price against USDT.

The server checks CoinGecko every five minutes, fetching five-minute samples for
the past day and hourly samples for older history (refreshed hourly). The browser
checks the server cache every minute. Missing CoinGecko data shows an empty chart;
it never falls back to pool prices. At implementation time, CoinGecko returned
an empty dotUSD price history.

Public API access is attempted by default. If your access requires a Demo API
key, set `COINGECKO_DEMO_API_KEY` in the server environment; it stays server-side.
Failed requests retain the last successful data, with delayed-data status.

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
