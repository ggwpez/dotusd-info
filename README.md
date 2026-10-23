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
just setup
```

These shortcuts require `just`. Without it, create the environment with
`python3 -m venv .venv` and install dependencies with
`.venv/bin/python -m pip install -r requirements.txt`.

You will need a data source, though: a Prometheus instance with the pool-price
history. The live data collector and Prometheus are not included in this repo.
The server expects the `arb_peg_dotusd_usdt` and `arb_peg_block_number` metrics,
both with the `clock="estimated_2s"` label.

If Prometheus is running locally, start the chart with:

```sh
just run --upstream http://127.0.0.1:9090/api/v1 --no-browser
```

Then open <http://127.0.0.1:3001>. Give the server a moment to load the price
history. It needs a working data source before it can show the chart.

If Prometheus is on another machine, connect through SSH:

```sh
just run --ssh-host user@host
```

Replace `user@host` with your SSH destination. The app forwards the remote
machine's port 9090 and opens the chart in your browser. Running
`just run` without options uses the SSH alias `server`.

For more options, including the listening port and archive path, run
`just run --help`. All arguments are forwarded to `server.py`.

## Working on the code

The backend lives in `server.py`. The chart is built from `index.html`, `app.js`,
`ranges.js`, and `style.css`. Restart the server after editing these files, since
it loads them once at startup.

The checked-in `VERSION` contains the release number and the first six bytes of
its SHA-256 code/asset hash, for example `1.0.0+abcdef123456`. After finishing
application changes, bump the version and refresh the hash together:

```sh
just release        # patch bump for fixes
just release minor  # minor bump for new features
just check-version
```

These commands require `just`; without it, use `python3 release.py --bump patch`
(or `--bump minor`) and `python3 release.py --check`.

Minor bumps reset the patch number to zero. Commit `VERSION` with the changes.
Tests and deployment reject stale release hashes. The hash covers the files in
`release.py`'s `RELEASE_FILES`, including runtime dependencies and the release
script itself; it excludes `VERSION` to avoid a circular hash, and excludes tests
and documentation. Add new runtime files to that list and the deployment copy list.

Run the tests from the repo root:

```sh
just test
```

`just test` uses the environment created by `just setup`; the direct equivalent
is `.venv/bin/python -m unittest discover -s tests -v`.
The tests run locally and do not need access to the live data collector. JavaScript
checks use Node.js, or JavaScriptCore on macOS. If neither is available, those
checks are skipped.

## Common commands

Run `just` to list the commands. Run `just setup` once before local development.

| Command | Purpose |
| --- | --- |
| `just setup` | Create `.venv` and install dependencies |
| `just run [options]` | Start the app locally with optional server arguments |
| `just test` | Run the test suite |
| `just release [patch\|minor]` | Bump the version and refresh its hash; defaults to patch |
| `just check-version` | Verify the recorded release hash |
| `just deploy [host]` | Check the release, run tests, and deploy this checkout |
| `just status [host]` | Inspect the deployed service |
| `just logs [host]` | Read its latest 50 log entries |

Deployment, status, and logs default to the SSH alias `server`. Deployment
restarts the remote service. See the [deployment guide](docs/deployment.md)
for prerequisites and specialized backfill and repair procedures.

## Agent instructions

`AGENTS.md` is a relative symlink to this README. Keep contributor and agent
instructions here so they stay in sync.

- After completing application changes, run `just release` for fixes or
  `just release minor` for features. Run the bump after the final code edit so
  the recorded hash matches. Include the updated `VERSION` in the same commit.
- Before committing application changes, run `just check-version` and
  `just test` (after `just setup`). Resolve failures before committing.
- Add new runtime files to `RELEASE_FILES` in `release.py` and the copy list in
  `deploy/deploy.sh` so hashing and deployment include them.
- Documentation-only changes do not need a version bump or the full test suite.
  Verify links and `just check-version` instead.
- Never use `/` in Git branch names; use hyphens, such as
  `fix-footer-version`.
- Create a commit after each completed change. Commit only task-related changes
  and preserve unrelated work. Disable signing with
  `git -c commit.gpgsign=false commit --no-gpg-sign`.
- Do not add `Signed-off-by` or `Co-authored-by` trailers. Verify the resulting
  commit message because local hooks may append them, and remove any such
  trailers from your new commit.
- For printing tasks, use 97% scaling unless explicitly requested otherwise,
  and ensure the print pipeline does not apply additional automatic fitting.

## More details

- [How it works](docs/architecture.md): data loading, caching, and the price API.
- [Deployment and archive backfill](docs/deployment.md): running the app with
  systemd and Caddy, and importing older prices. The supplied configuration is
  for dotusd.info; adapt it for your own host. The optional backfill tool also
  needs `websocket-client` and `xxhash`.
- [Benchmarks](docs/benchmarks.md): performance measurements and how to repeat them.
