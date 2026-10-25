"""Slow, persistent pool-gap and total-issuance recovery from Parity archive."""
from itertools import islice
import logging
import sqlite3
import threading
import time

from archive_reader import read_sample
from hub_quotes import HubChain
from issuance import RETENTION, read_issuance

RPC = 'https://polkadot-asset-hub-rpc.polkadot.io'
BATCH = 8
PAUSE = 6


class GapRepair:
    def __init__(self, cache, path, chain=None, issuance=None):
        self.cache = cache
        self.path = path
        self.issuance = issuance
        self.chain = chain or HubChain(RPC)
        # Sequential calls, at most two RPC requests per second, including
        # genesis/finality checks. No JSON-RPC batches or concurrent requests.
        self.chain.rpc.delay = 0.5
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
        if self.issuance is not None:
            self.issuance.restore()
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
        if self.issuance is not None:
            # Expire only in-memory rows, including during RPC outages.
            self.issuance.merge((), now=now)
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
        known = set()
        lower, upper = None, None
        if self.issuance is not None:
            known = {row[2] for row in self.issuance.snapshot()['rows']}
            for stamp, _, number in rows:
                if number is not None and stamp > now - RETENTION:
                    lower = number if lower is None else min(lower, number)
                    upper = number if upper is None else max(upper, number)
        missing = []
        if lower is not None:
            missing = list(islice((n for n in range(upper, lower - 1, -1) if n not in known), BATCH))
        # A TI backlog must not starve price repair, or vice versa. Shared blocks
        # occupy one slot even though both independent decoders read their state.
        if missing:
            price_numbers = price_numbers[:BATCH // 2]
            missing = list(islice((n for n in range(upper, lower - 1, -1)
                                  if n not in known and n not in price_numbers),
                                 BATCH - len(price_numbers)))
        if not price_numbers and not missing:
            return 0
        boundary = max(([gap[1][2]] if price_numbers else []) + missing)
        if self.chain.finalized() < boundary:
            raise ValueError('Archive has not finalized the repair boundary')

        prices, issuance = [], []
        price_error = issuance_error = None
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
            if self.issuance is not None and number not in known:
                try:
                    issuance.append(read_issuance(self.chain.rpc, number,
                                                 at=sample[1] if sample else None,
                                                 timestamp=sample[2] if sample else None))
                except Exception as error:
                    issuance_error = error
            if price_error is not None:
                break
        for number in missing:
            if self.stop.is_set():
                return 0
            attempted += 1
            try:
                # Old price-only repairs also need TI; reuse their pinned hash.
                saved = db.execute('SELECT hash,timestamp FROM repairs WHERE number=?', (number,)).fetchone()
                issuance.append(read_issuance(self.chain.rpc, number,
                                             at=saved[0] if saved else None,
                                             timestamp=saved[1] if saved else None))
            except Exception as error:
                issuance_error = error
        # Price batches keep their original atomicity. Valid TI remains useful
        # even if a pool is absent, a price boundary fails, or another TI read fails.
        if prices and price_error is None:
            with db:
                db.executemany('INSERT OR REPLACE INTO repairs VALUES(?,?,?,?,?,?)', prices)
            self.cache.merge([(r[3], r[4], r[0]) for r in prices], time.time(),
                             mark_fresh=False, missing_only=True)
        if issuance:
            self.issuance.merge(issuance)
        if price_error is not None:
            raise price_error
        if issuance_error is not None:
            raise issuance_error
        logging.info('Archive history repair: %s blocks (%s prices, %s issuance)',
                     attempted, len(prices), len(issuance))
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
