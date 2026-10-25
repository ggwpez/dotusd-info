# dotUSD.info

See how closely dotUSD tracks USDT on Polkadot Asset Hub. The chart shows up to
seven days of pool prices, so you can explore recent moves and zoom in on a
particular period.

**[Open the live chart →](https://dotusd.info)**

![The dotUSD peg chart](social-preview.png)

Prices come from the dotUSD → DOT → USDT pools. They are quoted against USDT and
do not include trading fees or slippage. Timestamps are estimated, and missing
blocks appear as gaps in the chart. Production gradually repairs internal price
gaps and backfills total issuance and Treasury dotUSD holdings from the Parity
archive: all share a limit of eight archive blocks per pass, with a one-second
pause between passes. Missing state first fills a fixed 1,800-block grid, newest-first, for a coarse graph
across the retained price history. It then fills midpoints at 900-block spacing,
then 450, 225, and progressively halves each interval down to individual blocks.
Each level is newest-first; odd intervals split into floor/ceiling halves.
This also fills state where price history is already complete. Saved samples are
reused on restart. TI and Treasury balances missing at the same block use one
storage query; already saved series are omitted. Open charts pick up all three
series' repairs automatically. Treasury holdings are saved in the `treasury`
table of the same repair database and exposed through the shared history API.
The Treasury dotUSD holding account is
[`166ASLGTqFioubHFAKzbSEyN9BgVQxpF2ruxr3uPNAB7RBQr`](https://assethub-polkadot.subscan.io/account/166ASLGTqFioubHFAKzbSEyN9BgVQxpF2ruxr3uPNAB7RBQr);
balances are in dotUSD units. This is not the generic Treasury pallet account.

The retired CoinGecko comparison implementation is preserved on the
`preserve-coingecko` branch. Production serves only the pool-price chart;
the comparison page, API, and background worker are removed.

## Run it locally

The app uses Python 3.10+ and plain JavaScript. There is no frontend build step,
and archive pool decoding uses `xxhash`. Install the runtime dependencies:

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
history. Price history requires a working Prometheus data source.

For a local UI preview on <http://127.0.0.1:5137>, run:

```sh
just preview
```

This uses local Prometheus without an SSH tunnel and does not open a browser.
Issuance loads from the public Asset Hub RPC even if Prometheus is unavailable.
Additional server flags are forwarded, for example `just preview --port 5138`.
The recipe defaults to `.venv/bin/python`; set `PYTHON` to use another environment.

In **Settings**, enable **Use browser fullscreen** to hide browser chrome when
pressing **Fullscreen** or double-clicking the chart. Use **Exit fullscreen**,
double-click again, or press Escape to restore the page. The preference is saved
locally and defaults to off, preserving the in-page expanded chart. Browsers
without Fullscreen API support disable this setting and retain in-page expansion.
A rejected fullscreen request displays an error without changing the chart mode.

In **Settings**, enable **Total dotUSD issuance** to show TI in blue and Treasury
holdings in purple on a shared right-hand, linear axis in dotUSD units. Values
use three significant digits with compact suffixes (for example, `1.31M` or `1.76B`). The setting is saved locally.
Chart links encode this setting as `ti=1` (shown) or `ti=0` (hidden), alongside
the range and scale. Explicit URL values override the local preference without
changing it; links without a valid `ti` value use the saved preference.
Browser Back/Forward restores the linked visibility.
The chart toolbar shows only the title and controls, without a duplicate legend.
An independent server-side live poll reads the latest 15 finalized blocks from the public
Asset Hub RPC and refreshes every six seconds after each fetch. With
`--gap-repair-history PATH` (enabled in production), successful live samples and
historical issuance and Treasury holdings share the price-repair SQLite database and are kept indefinitely.
Startup restores the latest seven days into memory. Historical filling follows the retained price block
range; it is gradual, not a guarantee of complete seven-day coverage.
Without that flag, issuance remains a recent-only, in-memory preview and Treasury
is unavailable. Treasury has no separate live poll.
The server never automatically prunes saved price repairs, evidence, issuance or Treasury.
The SQLite file can grow indefinitely; the chart still shows at most seven days.
Historical API requests can read saved data older than seven days. Supply explicit
`start` and `end` Unix timestamps to `/api/historic?series=price,ti,treasury`; each requested
window may span at most seven days and return at most 350,000 samples per series.
Larger requests return HTTP 400; split them into smaller windows. Older prices
come from the published archive and saved repairs, not an on-demand Prometheus
query. Missing history remains missing. This does not extend the chart's controls.

Issuance and Treasury use actual chain timestamps and render as thicker, solid
blue and purple lines without sample dots. Samples up to 1,800 blocks apart are connected to show the
coarse history; larger gaps stay disconnected. Lines between samples are visual
interpolation, not additional measured values.
Use the normal range buttons and drag-to-zoom controls for all three series.
With issuance enabled, hovering a price point shows TI and Treasury for the same
block (for example, `TI: 1.31M dotUSD` and `Treasury: 3.70M dotUSD`). Missing samples
show `unavailable` for that series, never a nearby block's value. There is no
separate value label outside the graph. Failed refreshes retain successful data.
The browser uses one history subscription:
`/api/historic?series=price` or `?series=price,ti,treasury`. It requests the selected
window, then merges incremental TI and Treasury updates, including historical
repairs, through the same six-second refresh as prices. Each state series has an
independent cursor; enabling the setting does not start another request loop.
The API also supports `?series=ti`, `?series=treasury`, or any unique subset.
`--hub-rpc URL` overrides the public RPC. Issuance can be previewed with the
command above even without local Prometheus; only price history remains unavailable.

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

Use this order after making application changes:

```sh
just test
git add <changed-files> VERSION
git -c commit.gpgsign=false commit --no-gpg-sign -m "Describe the change"
just deploy
```

`just test` automatically updates `VERSION` when the application hash changes,
then runs the test suite. It defaults to a patch bump; use `just test minor` for
a feature release. Minor bumps reset the patch number to zero. Running tests
again without application changes leaves the version unchanged. Fix any test
failures before committing, and include `VERSION` in the same commit.

Production exposes `MAJOR.MINOR.PATCH+<short Git commit SHA>`: the suffix is the
first seven hexadecimal characters of the deployed `master` commit. Use
`git show <short-SHA>` to inspect that release commit.
Deployment requires a clean `master` checkout: `git status --porcelain` must
be empty, including untracked files. Make the final commit before deploying,
and do not amend it afterward.

The checked-in `VERSION` remains internal integrity metadata: it contains the
release number and the first six bytes of its SHA-256 code/asset hash, for
example `1.0.0+abcdef123456`, not a Git SHA. Deployment validates this checksum
and refuses stale versions, then writes the full HEAD SHA and a newline to
`DEPLOY_COMMIT` beside `server.py` before restarting. The server verifies the
source checksum against `VERSION` before exposing the commit-based version.
Keeping the commit SHA out of `VERSION` avoids a self-referential commit hash.
Without `DEPLOY_COMMIT`, local development retains the content-hash version.
The checksum covers `release.py`'s `RELEASE_FILES`, including runtime dependencies
and the release script itself. It excludes `VERSION`, tests, and documentation.
Add new runtime files to that list and the deployment copy list; no separate
release or check-version command is needed.

`just test` uses the environment created by `just setup`. Without `just`, run
`.venv/bin/python release.py --prepare patch` followed by
`.venv/bin/python -m unittest discover -s tests -v`.
The tests run locally and do not need access to the live data collector.
JavaScript checks use Node.js, or JavaScriptCore on macOS. If neither is
available, those checks are skipped.

Gitea Actions runs `.gitea/workflows/lint.yaml` on pushes and pull requests.
This minimal CI checks Python syntax and critical errors with Ruff; any violation
fails the job. It does not run tests, enforce formatting, or deploy. Enable Actions
in Gitea and register a runner with the `ubuntu-latest` label and access to GitHub
and PyPI. To run the same check locally:

```sh
.venv/bin/python -m pip install ruff==0.14.2
.venv/bin/python -m ruff check --select E9,F63,F7,F82 .
```

## Common commands

Run `just` to list the commands. Run `just setup` once before local development.

| Command | Purpose |
| --- | --- |
| `just setup` | Create `.venv` and install dependencies |
| `just run [options]` | Start the app locally with optional server arguments |
| `just test [patch\|minor]` | Prepare the version if code changed, then run tests |
| `just preview [options]` | Preview locally on port 5137 without SSH or automatic browser launch |
| `just deploy [host] [version-url]` | Deploy and verify the public version; does not rerun tests |
| `just status [host]` | Inspect the deployed service |
| `just logs [host]` | Read its latest 50 log entries |

Deployment, status, and logs default to the SSH alias `server`. Deployment
requires a version number newer than the deployed one (a changed commit alone is
not enough), restarts the remote service, then checks `/version` every 5 seconds for up to
60 seconds, returning success when it matches the expected release or an error
on timeout. See the [deployment guide](docs/deployment.md)
for prerequisites and specialized backfill and repair procedures.

## Agent instructions

`AGENTS.md` is a relative symlink to this README. Keep contributor and agent
instructions here so they stay in sync.

- After application changes, use this order: `just test`, commit, `just deploy`.
  `just test` prepares the version automatically; use `just test minor` for
  features. Include the updated `VERSION` in the final commit on `master` and
  deploy from a clean checkout; never amend that commit after deployment.
  Resolve failures before committing. Do not run separate release/check commands
  or rerun tests in deploy.
- Deployment validates the release internally, refuses unchanged or older
  version numbers, and fails if the expected public version is not observed
  within 60 seconds.
- Add new runtime files to `RELEASE_FILES` in `release.py` and the copy list in
  `deploy/deploy.sh` so hashing and deployment include them.
- Documentation-only changes do not need a version bump or the full test suite.
  Verify links instead.
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
