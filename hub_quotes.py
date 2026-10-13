"""Finalized Hub RPC last-trade prices. No exchange or price-provider API."""
from decimal import Decimal
import json
import time
import urllib.request

from archive_reader import Rpc, TIMESTAMP, reserves, metadata_key, decimals
from backfill_quotes import GENESIS, RPC, qualifying_trades


class HubRpc(Rpc):
    def call(self, method, params):
        time.sleep(self.delay)
        request = urllib.request.Request(self.endpoint, json.dumps(dict(
            jsonrpc='2.0', id=1, method=method, params=params)).encode(),
            {'Content-Type': 'application/json', 'User-Agent': 'dotusd-info/1.0'})
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read(16 * 1024 * 1024 + 1)
        if len(body) > 16 * 1024 * 1024:
            raise ValueError('Hub RPC response exceeds size limit')
        result = json.loads(body)
        if result.get('id') != 1 or 'error' in result:
            raise ValueError(f'Hub RPC failed: {result.get("error", "invalid response ID")}')
        return result['result']


class HubChain:
    def __init__(self, endpoint=RPC):
        self.rpc = HubRpc(endpoint)
        self.decoder = None
        self.verified = False

    def finalized(self):
        if not self.verified:
            if self.rpc.call('chain_getBlockHash', [0]) != GENESIS:
                raise ValueError('Wrong chain: expected Polkadot Hub')
            self.verified = True
        at = self.rpc.call('chain_getFinalizedHead', [])
        number = int(self.rpc.call('chain_getHeader', [at])['number'], 16)
        return number

    def block(self, number):
        at = self.rpc.block_hash(number)
        if self.decoder is None:
            from substrateinterface import SubstrateInterface
            rpc = self.rpc

            class Decoder(SubstrateInterface):
                def rpc_request(self, method, params, result_handler=None):
                    return {'result': rpc.call(method, params)}

            self.decoder = Decoder(url=self.rpc.endpoint, auto_discover=False)
        # Metadata is selected for this block, including across runtime upgrades.
        try:
            self.decoder.init_runtime(block_hash=at)
        except Exception:
            self.decoder = None
            raise
        from substrateinterface.storage import StorageKey
        storage = StorageKey.create_from_storage_function(
            'System', 'Events', [], runtime_config=self.decoder.runtime_config,
            metadata=self.decoder.metadata)
        key = storage.to_hex()
        values = self.rpc.storage(at, [TIMESTAMP, key])
        if values.get(TIMESTAMP) is None or values.get(key) is None:
            raise ValueError('Missing historical Hub state; use an archive RPC')
        stamp = bytes.fromhex(values[TIMESTAMP][2:])
        if len(stamp) != 8:
            raise ValueError('Invalid chain timestamp')
        from scalecodec.base import ScaleBytes
        events = self.decoder.runtime_config.create_scale_object(
            storage.value_scale_type,
            data=ScaleBytes(values[key]), metadata=self.decoder.metadata).decode()
        return dict(number=number, timestamp=int.from_bytes(stamp, 'little'),
                    trades={str(asset): qualifying_trades(events, asset) for asset in (7873, 1984)})

    def reserve(self, number, asset):
        at = self.rpc.block_hash(number)
        # The shared swap decoder assumes DOT=10 and both stablecoins=6 decimals.
        key = metadata_key(asset)
        if decimals(self.rpc.storage(at, [key])[key]) != 6:
            raise ValueError('Unsupported stablecoin decimals')
        pool = reserves(self.rpc, at, asset)
        if pool is None or min(pool) <= 0:
            raise ValueError('Missing or empty Hub pool')
        dot, quote = pool
        return str(Decimal(quote * 10**22 // dot) / 10**18)


class HubQuotes:
    """Resume bounded startup scans and catch-up without skipping finalized blocks."""
    WINDOW_MS = 86400000
    BLOCKS_PER_POLL = 32

    def __init__(self, chain=None):
        self.chain = chain or HubChain()
        self.anchor = None
        self.scan = None
        self.cursor = None
        self.timestamp = None
        self.last = {}
        self.published = None

    def read(self):
        head = self.chain.finalized()
        if self.anchor is None:
            anchor = self.chain.block(head)
            self.anchor = anchor
            self.scan = head
        if head < self.anchor['number'] or (self.cursor is not None and head < self.cursor):
            raise ValueError('Finalized Hub head moved backwards')
        budget = self.BLOCKS_PER_POLL
        # Search backward for each leg independently. No reserve fallback until
        # the complete 24-hour window has been checked (or genesis is reached).
        while self.cursor is None and budget:
            record = self.anchor if self.scan == self.anchor['number'] else self.chain.block(self.scan)
            budget -= 1
            if record['timestamp'] >= self.anchor['timestamp'] - self.WINDOW_MS:
                for asset, trades in record['trades'].items():
                    if trades and asset not in self.last:
                        self.last[asset] = (trades[-1]['quote'], record['timestamp'])
            if (len(self.last) == 2 or self.scan == 0 or
                    record['timestamp'] < self.anchor['timestamp'] - self.WINDOW_MS):
                self.cursor = self.anchor['number']
                self.timestamp = self.anchor['timestamp']
            else:
                self.scan -= 1
        # Process every intervening block, including gaps between polls. State
        # advances only after a complete read, so errors retry the failed block.
        while self.cursor is not None and self.cursor < head and budget:
            record = self.chain.block(self.cursor + 1)
            if record['timestamp'] < self.timestamp:
                raise ValueError('Hub timestamps moved backwards')
            for asset, trades in record['trades'].items():
                if trades:
                    self.last[asset] = (trades[-1]['quote'], record['timestamp'])
            self.cursor = record['number']
            self.timestamp = record['timestamp']
            budget -= 1
        if self.cursor != head or self.published == head:
            return None
        # Read both reserves at the same finalized block. This also validates
        # token decimals, even when both legs have qualifying trades.
        legs = {}
        for asset in ('1984', '7873'):
            fallback = self.chain.reserve(head, int(asset))
            trade = self.last.get(asset)
            legs[asset] = trade[0] if trade and trade[1] >= self.timestamp - self.WINDOW_MS else fallback
        self.published = head
        return self.timestamp / 1000, legs
