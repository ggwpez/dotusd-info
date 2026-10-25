"""Persistent pool-gap, total-issuance and Treasury recovery from Parity archive."""
import logging
import sqlite3
import threading
import time

from archive_reader import read_sample
from hub_quotes import HubChain
from issuance import RETENTION, read_issuance
from treasury import read_history

RPC = 'https://polkadot-asset-hub-rpc.polkadot.io'
BATCH = 8
PAUSE = 1
ISSUANCE_STRIDE = 1800


def _issuance_numbers(lower, upper):
    """Stable, newest-first levels of a recursively bisected block grid."""
    yield from range(upper - upper % ISSUANCE_STRIDE, lower - 1, -ISSUANCE_STRIDE)
    for level in range(1, (ISSUANCE_STRIDE - 1).bit_length() + 1):
        divisions = 1 << level
        first = (lower * divisions + ISSUANCE_STRIDE - 1) // ISSUANCE_STRIDE
        last = ((upper + 1) * divisions - 1) // ISSUANCE_STRIDE
        # Even indices belong to earlier levels. Integer rounding splits odd
        # intervals into floor/ceiling halves without shifting existing points.
        yield from (index * ISSUANCE_STRIDE // divisions
                    for index in range(last if last % 2 else last - 1, first - 1, -2))


class GapRepair:
    def __init__(self, cache, path, chain=None, issuance=None, treasury=None):
        self.cache = cache
        self.path = path
        self.issuance = issuance
        self.treasury = treasury
        self.chain = chain or HubChain(RPC)
        # Sequential calls, at most ten RPC requests per second, including
        # genesis/finality checks. No JSON-RPC batches or concurrent requests.
        self.chain.rpc.delay = 0.1
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='pool-gap-repair', daemon=True)

    def database(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        # Restoring a long price cursor must not block live issuance commits.
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('''CREATE TABLE IF NOT EXISTS repairs (
            number INTEGER PRIMARY KEY, hash TEXT NOT NULL,
            timestamp INTEGER NOT NULL, sample_time REAL NOT NULL,
            price REAL NOT NULL, reserves TEXT NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS repairs_time ON repairs(sample_time)')
        return db

    def restore(self, db):
        for history in (self.issuance, self.treasury):
            if history is not None:
                history.restore()
        cursor = db.execute(
            'SELECT sample_time,price,number FROM repairs WHERE sample_time>? ORDER BY number',
            (time.time() - RETENTION,))
        while not self.stop.is_set():
            rows = cursor.fetchmany(2000)
            if not rows:
                break
            self.cache.merge(rows, time.time(), mark_fresh=False, missing_only=True)
            if self.stop.wait(1):
                break

    def poll(self, db):
        now = time.time()
        histories = [(history, name, set(), [])
                     for history, name in ((self.issuance, 'issuance'),
                                           (self.treasury, 'treasury'))
                     if history is not None]
        for history, _, known, _ in histories:
            # Expire only memory, never persisted history.
            history.merge((), now=now)
            known.update(row[2] for row in history.snapshot()['rows'])
        with self.cache.lock:
            if not self.cache.history_ready:
                return 0
            rows = self.cache.rows
        # Only price gaps need the ingestion grace period and estimated clock.
        cutoff = now - 120
        gap = next(((a, b) for a, b in zip(reversed(rows[:-1]), reversed(rows[1:]))
                    if a[2] is not None and b[2] is not None and
                    b[2] > a[2] + 1 and b[0] < cutoff), None)
        price_numbers = list(range(gap[0][2] + 1, min(gap[1][2], gap[0][2] + BATCH + 1))) if gap else []
        lower, upper = None, None
        if histories:
            for stamp, _, number in rows:
                if number is not None and stamp > now - RETENTION:
                    lower = number if lower is None else min(lower, number)
                    upper = number if upper is None else max(upper, number)
        missing = []
        if lower is not None:
            # Saved and already-selected blocks also deduplicate rounded
            # midpoints at the final, single-block resolution.
            for number in _issuance_numbers(lower, upper):
                if any(number not in known for _, _, known, _ in histories) and number not in missing:
                    missing.append(number)
                    if len(missing) == BATCH + BATCH // 2:
                        break
        # Balance price repair against the shared state-history backlog.
        # A block occupies one slot even when multiple decoders read its state.
        if missing:
            price_numbers = price_numbers[:BATCH // 2]
            missing = [n for n in missing if n not in price_numbers][:BATCH - len(price_numbers)]
        if not price_numbers and not missing:
            return 0
        boundary = max(([gap[1][2]] if price_numbers else []) + missing)
        if self.chain.finalized() < boundary:
            raise ValueError('Archive has not finalized the repair boundary')

        prices = []
        price_error = history_error = None

        def read_histories(number, at=None, timestamp=None):
            nonlocal history_error
            needed = {name: samples for _, name, known, samples in histories
                      if number not in known}
            if not needed:
                return
            try:
                if self.treasury is None:
                    result = {'issuance': read_issuance(
                        self.chain.rpc, number, at=at, timestamp=timestamp)}
                else:
                    result = read_history(
                        self.chain.rpc, number, issuance='issuance' in needed,
                        treasury='treasury' in needed, at=at, timestamp=timestamp)
                for name, value in result.items():
                    if isinstance(value, Exception):
                        history_error = value
                    else:
                        needed[name].append(value)
            except Exception as error:
                history_error = error
        attempted = 0
        previous = gap[0][0] if price_numbers else None
        for number in price_numbers:
            if self.stop.is_set():
                return 0
            attempted += 1
            sample = None
            try:
                sample = read_sample(self.chain.rpc, number)
                _, at, timestamp, price, reserves = sample
                if price is None:
                    raise ValueError(f'No pool price at block {number}; preserving gap')
                # Keep both existing boundaries and the chart's estimated 2s clock.
                upper_time = gap[1][0] - (gap[1][2] - number) * 2
                if previous + 2 > upper_time:
                    raise ValueError('Gap boundaries cannot fit the estimated block clock')
                stamp = min(max(timestamp / 1000, previous + 2), upper_time)
                prices.append((number, at, timestamp, stamp, price, reserves))
                previous = stamp
            except Exception as error:
                price_error = error
            read_histories(number, at=sample[1] if sample else None,
                           timestamp=sample[2] if sample else None)
            if price_error is not None:
                break
        for number in missing:
            if self.stop.is_set():
                return 0
            attempted += 1
            # Old price repairs can supply the pinned hash and chain timestamp.
            saved = db.execute('SELECT hash,timestamp FROM repairs WHERE number=?', (number,)).fetchone()
            read_histories(number, at=saved[0] if saved else None,
                           timestamp=saved[1] if saved else None)
        # Preserve successful state samples independently of decoder failures.
        if prices and price_error is None:
            with db:
                db.executemany('INSERT OR REPLACE INTO repairs VALUES(?,?,?,?,?,?)', prices)
            self.cache.merge([(r[3], r[4], r[0]) for r in prices], time.time(),
                             mark_fresh=False, missing_only=True)
        for history, _, _, samples in histories:
            if samples:
                history.merge(samples)
        if price_error is not None:
            raise price_error
        if history_error is not None:
            raise history_error
        counts = {type(history).__name__: len(samples) for history, _, _, samples in histories}
        logging.info('Archive history repair: %s blocks (%s prices, %s)',
                     attempted, len(prices), counts)
        return attempted

    def run(self):
        failures = 0
        db = None
        try:
            while not self.stop.is_set():
                try:
                    if db is None:
                        db = self.database()
                        try:
                            self.restore(db)
                        except Exception:
                            db.close()
                            db = None
                            raise
                    if self.stop.is_set():
                        break
                    self.poll(db)
                    failures = 0
                except Exception:
                    failures = min(failures + 1, 4)
                    logging.exception('Archive gap repair paused; will retry')
                with self.cache.lock:
                    ready = self.cache.history_ready
                delay = min(900, 60 * 2 ** failures) if failures else PAUSE
                self.stop.wait(delay if ready or failures else 5)
        finally:
            if db is not None:
                db.close()
