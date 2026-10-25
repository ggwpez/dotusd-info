"""Recent finalized dotUSD issuance, independent of the pool-price history."""
import hashlib
import logging
import threading

from archive_reader import TIMESTAMP, decimals, metadata_key
from hub_quotes import HubChain

ASSET = 7873
BLOCKS = 15


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


class IssuanceCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.rows = ()
        self.error = 'Loading recent finalized dotUSD issuance.'

    def snapshot(self):
        with self.lock:
            return {'rows': self.rows, 'error': self.error}


class IssuanceWorker:
    def __init__(self, cache, chain=None):
        self.cache = cache
        self.chain = chain or HubChain()
        # Reuse the bounded, genesis-verified Hub RPC; requests stay sequential.
        self.chain.rpc.delay = 0.1
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='dotusd-issuance', daemon=True)

    def poll(self):
        try:
            head = self.chain.finalized()
            existing = {row[2]: row for row in self.cache.snapshot()['rows']}
            if existing and head < max(existing):
                raise ValueError('Hub finalized head moved backwards')
            keys = [TIMESTAMP, asset_key(ASSET), metadata_key(ASSET)]
            rows = []
            for number in range(max(1, head - BLOCKS + 1), head + 1):
                if self.stop.is_set():
                    return
                if number in existing:
                    rows.append(existing[number])
                    continue
                at = self.chain.rpc.block_hash(number)
                if self.stop.is_set():
                    return
                values = self.chain.rpc.storage(at, keys)
                timestamp = values[TIMESTAMP]
                if timestamp is None:
                    raise ValueError(f'Missing timestamp at block {number}')
                stamp = bytes.fromhex(timestamp[2:])
                if not timestamp.startswith('0x') or len(stamp) != 8:
                    raise ValueError('Invalid issuance timestamp')
                supply = total_supply(values[keys[1]]) / 10 ** decimals(values[keys[2]])
                rows.append((int.from_bytes(stamp, 'little') / 1000, supply, number))
            # Hub MinimumPeriod is zero: consecutive blocks may share chain time.
            if any(a[0] > b[0] for a, b in zip(rows, rows[1:])):
                raise ValueError('Issuance timestamps moved backwards')
            with self.cache.lock:
                self.cache.rows = tuple(rows)
                self.cache.error = None
        except Exception as error:
            # Publish complete successful windows only; failures keep the last one.
            with self.cache.lock:
                self.cache.error = f'Issuance refresh failed: {error}'
            logging.exception('Recent dotUSD issuance refresh failed')

    def run(self):
        while not self.stop.is_set():
            self.poll()
            self.stop.wait(6)
