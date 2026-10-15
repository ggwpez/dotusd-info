"""Slow, persistent recovery of bounded pool-history gaps from Parity archive."""
import logging
import sqlite3
import threading
import time

from archive_reader import read_sample
from hub_quotes import HubChain

RPC = 'https://polkadot-asset-hub-rpc.polkadot.io'
RETENTION = 7 * 86400
BATCH = 8
PAUSE = 60


class GapRepair:
    def __init__(self, cache, path, chain=None):
        self.cache = cache
        self.path = path
        self.chain = chain or HubChain(RPC)
        # Sequential calls, at most two RPC requests per second, including
        # genesis/finality checks. No JSON-RPC batches or concurrent requests.
        self.chain.rpc.delay = 0.5
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='pool-gap-repair', daemon=True)

    def database(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        db.execute('''CREATE TABLE IF NOT EXISTS repairs (
            number INTEGER PRIMARY KEY, hash TEXT NOT NULL,
            timestamp INTEGER NOT NULL, sample_time REAL NOT NULL,
            price REAL NOT NULL, reserves TEXT NOT NULL)''')
        return db

    def restore(self, db):
        with db:
            db.execute('DELETE FROM repairs WHERE sample_time<=?', (time.time() - RETENTION,))
        cursor = db.execute('SELECT sample_time,price,number FROM repairs ORDER BY number')
        while not self.stop.is_set():
            rows = cursor.fetchmany(2000)
            if not rows:
                break
            self.cache.merge(rows, time.time(), mark_fresh=False, missing_only=True)
            if self.stop.wait(1):
                break

    def poll(self, db):
        with db:
            db.execute('DELETE FROM repairs WHERE sample_time<=?', (time.time() - RETENTION,))
        with self.cache.lock:
            if not self.cache.history_ready:
                return 0
            rows = self.cache.rows
        # Repair recent gaps first, after giving normal ingestion two minutes.
        # Only internal block-number gaps are evidence of missing samples:
        # timestamp pauses alone do not mean that blocks exist to recover.
        cutoff = time.time() - 120
        gap = next(((a, b) for a, b in zip(reversed(rows[:-1]), reversed(rows[1:]))
                    if a[2] is not None and b[2] is not None and
                    b[2] > a[2] + 1 and b[0] < cutoff), None)
        if gap is None:
            return 0
        left, right = gap
        if self.chain.finalized() < right[2]:
            raise ValueError('Archive has not finalized the gap boundary')
        pending = []
        previous = left[0]
        for number in range(left[2] + 1, min(right[2], left[2] + BATCH + 1)):
            if self.stop.is_set():
                return 0
            sample = read_sample(self.chain.rpc, number)
            _, at, timestamp, price, reserves = sample
            if price is None:
                raise ValueError(f'No pool price at block {number}; preserving gap')
            # Keep both existing boundaries and the chart's estimated 2s clock.
            # Actual chain timestamps and reserves remain in the evidence DB.
            upper = right[0] - (right[2] - number) * 2
            if previous + 2 > upper:
                raise ValueError('Gap boundaries cannot fit the estimated block clock')
            stamp = min(max(timestamp / 1000, previous + 2), upper)
            pending.append((number, at, timestamp, stamp, price, reserves))
            previous = stamp
        with db:
            db.executemany('INSERT OR REPLACE INTO repairs VALUES(?,?,?,?,?,?)', pending)
        self.cache.merge([(r[3], r[4], r[0]) for r in pending], time.time(),
                         mark_fresh=False, missing_only=True)
        logging.info('Archive gap repair: %s blocks, %s..%s',
                     len(pending), pending[0][0], pending[-1][0])
        return len(pending)

    def run(self):
        failures = 0
        db = None
        try:
            while not self.stop.is_set():
                with self.cache.lock:
                    ready = self.cache.history_ready
                if not ready:
                    self.stop.wait(5)
                    continue
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
                self.stop.wait(min(900, PAUSE * 2 ** failures))
        finally:
            if db is not None:
                db.close()
