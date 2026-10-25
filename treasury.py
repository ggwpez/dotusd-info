"""Historical Treasury dotUSD holdings, with permanent storage and a seven-day cache."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import threading
import time

from archive_reader import TIMESTAMP, decimals, metadata_key
from issuance import ASSET, RETENTION, asset_key, total_supply

# Polkadot Treasury PalletId(*b"py/trsry"), converted to AccountId32.
# SS58: 13UVJyLnbVp9RBZYFwFGyDvVd1y27Tt8tkntv6Q7JVPhFsTB
# https://github.com/polkadot-fellows/runtimes/blob/main/system-parachains/asset-hubs/asset-hub-polkadot/src/treasury.rs
TREASURY_ACCOUNT = b'modlpy/trsry'.ljust(32, b'\0')


def account_key(asset=ASSET):
    """Assets.Account: Blake2_128Concat(u32), Blake2_128Concat(AccountId32)."""
    import xxhash
    def twox128(text):
        return b''.join(xxhash.xxh64(text.encode(), seed=i).intdigest().to_bytes(8, 'little')
                        for i in (0, 1))
    encoded = asset.to_bytes(4, 'little')
    return '0x' + (twox128('Assets') + twox128('Account') +
                   hashlib.blake2b(encoded, digest_size=16).digest() + encoded +
                   hashlib.blake2b(TREASURY_ACCOUNT, digest_size=16).digest() +
                   TREASURY_ACCOUNT).hex()


def account_balance(encoded):
    """Decode AssetAccount<u128, u128, (), AccountId32>; absent account is zero.

    Balance is followed by AccountStatus and ExistenceReason. DepositHeld adds
    u128; DepositFrom adds AccountId32 then u128; the other reasons add no data.
    Require the complete layout, including enums, rather than trusting a prefix.
    https://github.com/paritytech/polkadot-sdk/blob/master/substrate/frame/assets/src/types.rs
    """
    if encoded is None:
        return 0
    if not isinstance(encoded, str) or not encoded.startswith('0x'):
        raise ValueError('Invalid Treasury asset account encoding')
    data = bytes.fromhex(encoded[2:])
    sizes = {0: 18, 1: 18, 2: 34, 3: 18, 4: 66}
    if (len(data) < 18 or data[16] > 2 or len(data) != sizes.get(data[17]) or
            len(encoded) != 2 + 2 * len(data)):
        raise ValueError('Unsupported Treasury asset account layout')
    return int.from_bytes(data[:16], 'little')


def read_history(rpc, number, *, issuance=False, treasury=False, at=None, timestamp=None):
    """Read requested finalized histories in one pinned storage query.

    The optional timestamp is milliseconds. Shared RPC, metadata and timestamp
    failures raise; individual decoders return an Exception for just that series,
    allowing the caller to persist the other successful row independently.
    """
    requested = {}
    if issuance:
        requested['issuance'] = (asset_key(ASSET), total_supply)
    if treasury:
        requested['treasury'] = (account_key(), account_balance)
    if not requested:
        return {}
    precision_key = metadata_key(ASSET)
    keys = [key for key, _ in requested.values()] + [precision_key]
    if timestamp is None:
        keys.insert(0, TIMESTAMP)
    at = at if at is not None else rpc.block_hash(number)
    values = rpc.storage(at, keys)
    if timestamp is None:
        encoded = values[TIMESTAMP]
        if encoded is None:
            raise ValueError(f'Missing timestamp at block {number}')
        if not isinstance(encoded, str) or not encoded.startswith('0x') or len(encoded) != 18:
            raise ValueError('Invalid history timestamp')
        stamp = bytes.fromhex(encoded[2:])
        if len(stamp) != 8:
            raise ValueError('Invalid history timestamp')
        timestamp = int.from_bytes(stamp, 'little')
    precision = values[precision_key]
    if not isinstance(precision, str) or not precision.startswith('0x'):
        raise ValueError('Missing or invalid dotUSD asset metadata')
    divisor = 10 ** decimals(precision)
    rows = {}
    for name, (key, decode) in requested.items():
        try:
            rows[name] = timestamp / 1000, decode(values[key]) / divisor, number
        except Exception as error:
            rows[name] = error
    return rows


class TreasuryHistory:
    def __init__(self, path):
        self.history_path = Path(path)
        self.lock = threading.Lock()
        self.rows = ()
        self.restored = False

    def database(self):
        # A fresh connection per operation keeps archive and startup threads separate.
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.history_path)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('''CREATE TABLE IF NOT EXISTS treasury (
            number INTEGER PRIMARY KEY, timestamp REAL NOT NULL, balance REAL NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS treasury_time ON treasury(timestamp)')
        return db

    def restore(self):
        with self.lock:
            if not self.restored:
                self._merge((), time.time())

    def merge(self, rows, now=None):
        with self.lock:
            self._merge(rows, time.time() if now is None else now)

    def _merge(self, rows, now):
        cutoff = now - RETENTION
        with closing(self.database()) as db:
            with db:
                retained = {row[2]: row for row in self.rows if row[0] > cutoff}
                if not self.restored:
                    for row in db.execute(
                            'SELECT timestamp,balance,number FROM treasury WHERE timestamp>? ORDER BY number',
                            (cutoff,)):
                        retained.setdefault(row[2], row)
                incoming = [tuple(row) for row in rows]
                for row in incoming:
                    retained.pop(row[2], None)
                    if row[0] > cutoff:
                        retained[row[2]] = row
                merged = tuple(retained[number] for number in sorted(retained))
                if any(a[0] > b[0] for a, b in zip(merged, merged[1:])):
                    raise ValueError('Treasury timestamps moved backwards')
                db.executemany('INSERT OR REPLACE INTO treasury VALUES(?,?,?)',
                               [(row[2], row[0], row[1]) for row in incoming])
            # Publish only after a successful commit; old rows remain on disk indefinitely.
            self.rows = merged
            self.restored = True

    def snapshot(self):
        with self.lock:
            cutoff = time.time() - RETENTION
            self.rows = tuple(row for row in self.rows if row[0] > cutoff)
            return {'rows': self.rows}
