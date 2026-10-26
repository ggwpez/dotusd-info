"""Finalized dotUSD issuance, with permanent storage and a seven-day cache."""
import hashlib
from collections import deque
from contextlib import closing
import logging
import secrets
import sqlite3
import threading
import time

from archive_reader import TIMESTAMP, decimals, metadata_key
from hub_quotes import HubChain

ASSET = 7873
BLOCKS = 15
RETENTION = 7 * 86400
MAX_CHANGES = 4096
MAX_SAMPLES = 350000


def asset_key(asset):
    import xxhash
    def twox128(text):
        return b''.join(xxhash.xxh64(text.encode(), seed=i).intdigest().to_bytes(8, 'little')
                        for i in (0, 1))
    encoded = asset.to_bytes(4, 'little')
    return '0x' + (twox128('Assets') + twox128('Asset') +
                   hashlib.blake2b(encoded, digest_size=16).digest() + encoded).hex()


def total_supply(encoded):
    """Decode pallet-assets AssetDetails<AccountId32, u128, u128> supply.

    Layout: four AccountId32, supply/deposit/min_balance u128, bool,
    three u32 counters, AssetStatus. See upstream sources in architecture.md.
    Reject changed/truncated layouts rather than displaying plausible balances.
    """
    if encoded is None:
        raise ValueError('Missing dotUSD asset details')
    data = bytes.fromhex(encoded[2:])
    if not encoded.startswith('0x') or len(data) != 190 or data[176] > 1 or data[189] > 2:
        raise ValueError('Unsupported dotUSD asset details layout')
    return int.from_bytes(data[128:144], 'little')


def read_issuance(rpc, number, at=None, timestamp=None):
    """Read supply at one finalized block; reuse a price repair's hash/time."""
    supply_key, precision_key = asset_key(ASSET), metadata_key(ASSET)
    keys = [supply_key, precision_key]
    if timestamp is None:
        keys.insert(0, TIMESTAMP)
    at = at if at is not None else rpc.block_hash(number)
    values = rpc.storage(at, keys)
    if timestamp is None:
        encoded = values[TIMESTAMP]
        if encoded is None:
            raise ValueError(f'Missing timestamp at block {number}')
        stamp = bytes.fromhex(encoded[2:])
        if not encoded.startswith('0x') or len(stamp) != 8:
            raise ValueError('Invalid issuance timestamp')
        timestamp = int.from_bytes(stamp, 'little')
    supply = total_supply(values[supply_key]) / 10 ** decimals(values[precision_key])
    return timestamp / 1000, supply, number


class IssuanceCache:
    """Bounded state history; subclasses supply their persisted series identity."""
    table = 'issuance'
    value_column = 'supply'
    label = 'Issuance'
    loading_error = 'Loading recent finalized dotUSD issuance.'

    def __init__(self, history_path=None):
        self.lock = threading.Lock()
        self.rows = ()
        self.error = self.loading_error
        self.history_path = history_path
        self.restored = history_path is None
        self.instance = secrets.token_hex(16)
        self.revision = 0
        self.changes = deque(maxlen=MAX_CHANGES)

    def database(self):
        # Never share a SQLite connection between the live and archive threads.
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.history_path)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute(f'''CREATE TABLE IF NOT EXISTS {self.table} (
            number INTEGER PRIMARY KEY, timestamp REAL NOT NULL, {self.value_column} REAL NOT NULL)''')
        db.execute(f'CREATE INDEX IF NOT EXISTS {self.table}_time ON {self.table}(timestamp)')
        return db

    def restore(self):
        with self.lock:
            if not self.restored:
                self._merge((), time.time())

    def merge(self, rows, live=False, now=None):
        with self.lock:
            self._merge(rows, time.time() if now is None else now)
            if live:
                self.error = None

    def _merge(self, rows, now):
        if self.history_path is None:
            self._publish(tuple(tuple(row) for row in rows))
            return
        cutoff = now - RETENTION
        with closing(self.database()) as db:
            with db:
                retained = {row[2]: row for row in self.rows if row[0] > cutoff}
                if not self.restored:
                    for row in db.execute(
                            f'SELECT timestamp,{self.value_column},number FROM {self.table} WHERE timestamp>? ORDER BY number',
                            (cutoff,)):
                        retained.setdefault(row[2], row)
                incoming = [tuple(row) for row in rows]
                for row in incoming:
                    retained.pop(row[2], None)
                    if row[0] > cutoff:
                        retained[row[2]] = row
                merged = tuple(retained[number] for number in sorted(retained))
                if any(a[0] > b[0] for a, b in zip(merged, merged[1:])):
                    raise ValueError(f'{self.label} timestamps moved backwards')
                db.executemany(f'INSERT OR REPLACE INTO {self.table} VALUES(?,?,?)',
                               [(row[2], row[0], row[1]) for row in incoming])
            self._publish(merged)
            self.restored = True
            if self.rows and self.error and self.error.startswith('Loading'):
                self.error = None

    def _publish(self, rows):
        previous = {row[2]: row for row in self.rows}
        for row in rows:
            if previous.get(row[2]) != row:
                self.revision += 1
                self.changes.append((self.revision, row))
        self.rows = rows

    def selection(self, start, end, cursor=None):
        """Return recent revision deltas or bounded persisted historical snapshots."""
        historical = self.history_path is not None and start < time.time() - RETENTION
        limit_error = f'{self.label} window exceeds {MAX_SAMPLES} samples; request a smaller window'
        with self.lock:
            next_cursor, error = f'{self.instance}:{self.revision}', self.error
            if not historical:
                count = 0
                for row in self.rows:
                    if start < row[0] <= end:
                        count += 1
                        if count > MAX_SAMPLES:
                            raise ValueError(limit_error)
                revision = None
                if isinstance(cursor, str):
                    instance, separator, value = cursor.partition(':')
                    if separator and instance == self.instance and value.isascii() and value.isdigit():
                        # Cursors are opaque to callers; reject oversized or future revisions.
                        if len(value) <= 20:
                            revision = int(value)
                oldest = self.changes[0][0] - 1 if self.changes else self.revision
                reset = revision is None or not oldest <= revision <= self.revision
                if reset:
                    rows = self.rows
                else:
                    changed = {row[2]: row for version, row in self.changes if version > revision}
                    rows = (changed[number] for number in sorted(changed))
                rows = tuple(row for row in rows if start < row[0] <= end)
        if historical:
            # Historical corrections do not enter the live revision journal.
            # Always reset so callers replace their entire selected window.
            # Disk reads must not hold up live publication.
            with closing(self.database()) as db:
                rows = db.execute(
                    f'SELECT timestamp,{self.value_column},number FROM {self.table} '
                    'WHERE timestamp>? AND timestamp<=? '
                    'ORDER BY timestamp,number LIMIT ?',
                    (start, end, MAX_SAMPLES + 1)).fetchall()
            if len(rows) > MAX_SAMPLES:
                raise ValueError(limit_error)
            rows.sort(key=lambda row: row[2])
            rows = tuple(rows)
            reset = True
        return {
            'rows': rows,
            'cursor': next_cursor,
            'reset': reset,
            'error': error,
        }

    def snapshot(self):
        with self.lock:
            if self.history_path is not None:
                cutoff = time.time() - RETENTION
                self.rows = tuple(row for row in self.rows if row[0] > cutoff)
            return {'rows': self.rows, 'error': self.error}


class IssuanceWorker:
    def __init__(self, cache, chain=None):
        self.cache = cache
        self.chain = chain or HubChain()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='dotusd-issuance', daemon=True)

    def poll(self):
        try:
            self.cache.restore()
            head = self.chain.finalized()
            existing = {row[2]: row for row in self.cache.snapshot()['rows']}
            if existing and head < max(existing):
                raise ValueError('Hub finalized head moved backwards')
            rows = []
            for number in range(max(1, head - BLOCKS + 1), head + 1):
                if self.stop.is_set():
                    return
                if number in existing:
                    rows.append(existing[number])
                    continue
                rows.append(read_issuance(self.chain.rpc, number))
                if self.stop.is_set():
                    return
            # Hub MinimumPeriod is zero: consecutive blocks may share chain time.
            if any(a[0] > b[0] for a, b in zip(rows, rows[1:])):
                raise ValueError('Issuance timestamps moved backwards')
            self.cache.merge(rows, live=True)
        except Exception as error:
            # Failures retain every previously published live or historical row.
            with self.cache.lock:
                self.cache.error = f'Issuance refresh failed: {error}'
            logging.exception('Recent dotUSD issuance refresh failed')

    def run(self):
        while not self.stop.is_set():
            self.poll()
            self.stop.wait(6)
