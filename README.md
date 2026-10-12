# dotUSD.info

A dotUSD / USDT peg chart with a Python backend and dependency-free browser assets.
Prices follow the dotUSD → DOT → USDT pools on Polkadot Asset Hub. The chart
preserves individual samples and gaps, with up to seven days of history.

## Run locally

Requires Python 3.10+ and access to a Prometheus instance containing the collector's
`arb_peg_dotusd_usdt` and `arb_peg_block_number` metrics with the
`clock="estimated_2s"` label. The live collector and Prometheus are external services
and are not included in this repository. The chart server needs no third-party
Python packages.

```sh
python3 server.py --upstream http://127.0.0.1:9090/api/v1 --no-browser
```

Open <http://127.0.0.1:3001>. The API returns 503 while the initial history loads;
without a working data source, the chart cannot display prices.

To reach a remote Prometheus instance through SSH instead:

```sh
python3 server.py --ssh-host user@host
```

This opens an SSH tunnel to the remote host's `127.0.0.1:9090` and launches the
browser. Running without either option uses the SSH alias `server`.
Use `python3 server.py --help` for bind, port, archive, and browser options.
Restart the server after editing assets: it snapshots them at startup.

## Development

Run the test suite from the repository root:

```sh
python3 -m unittest discover -s tests -v
```

Tests use local fixtures and servers, without production credentials or a live
collector. Client range checks use Node.js when installed, or JavaScriptCore on
macOS; otherwise they are skipped.

## Repository guide

- `server.py`: bounded history cache, HTTP API, and static asset serving.
- `index.html`, `app.js`, `ranges.js`, `style.css`: chart UI and packed sample handling.
- `archive_reader.py`, `backfill.py`: optional archive import. Backfill additionally
  requires `websocket-client` and `xxhash` (Debian packages `python3-websocket` and
  `python3-xxhash`).
- `tests/`: backend, archive, and client range checks.
- `tools/`, `benchmarks/`: benchmark scripts and recorded measurements.
- `deploy/`: deployment script, systemd units, and Caddy configuration.

See [architecture and API details](docs/architecture.md),
[benchmark methodology and results](docs/benchmarks.md), and
[deployment and archive backfill](docs/deployment.md).

Prices are reserve-ratio spot prices referenced to USDT. They exclude swap fees
and slippage; estimated timestamps and missing blocks remain visible as gaps.
