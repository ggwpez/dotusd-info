"""Bounded HTTP archive RPC and chain verification for all Hub history."""
import json
import time
import urllib.request

from archive_reader import Unavailable

RPC = 'https://polkadot-asset-hub-rpc.polkadot.io'
GENESIS = '0x68d56f15f85d3136970ec16946040bc1752654e906147f7e43e9d539d7c3de2f'
MAX_RPC_BYTES = 16 * 1024 * 1024


class HubRpc:
    """Sequential HTTP calls with a per-client pause, timeout and response cap."""
    def __init__(self, endpoint, delay=0.1):
        self.endpoint = endpoint
        self.delay = delay

    def call(self, method, params):
        time.sleep(self.delay)
        request = urllib.request.Request(self.endpoint, json.dumps(dict(
            jsonrpc='2.0', id=1, method=method, params=params)).encode(),
            {'Content-Type': 'application/json', 'User-Agent': 'dotusd-info/1.0'})
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read(MAX_RPC_BYTES + 1)
        if len(body) > MAX_RPC_BYTES:
            raise ValueError('Hub RPC response exceeds size limit')
        result = json.loads(body)
        if result.get('id') != 1 or 'error' in result:
            raise ValueError(f'Hub RPC failed: {result.get("error", "invalid response ID")}')
        return result['result']

    def block_hash(self, number):
        at = self.call('chain_getBlockHash', [number])
        if at is None:
            raise Unavailable(f'No retained hash for block {number}')
        return at

    def storage(self, at, keys):
        result = self.call('state_queryStorageAt', [keys, at])
        if len(result) != 1 or result[0]['block'] != at:
            raise ValueError('Storage response is not pinned to the requested block')
        return dict(result[0]['changes'])


class HubChain:
    def __init__(self, endpoint=RPC):
        self.rpc = HubRpc(endpoint)
        self.verified = False

    def finalized(self):
        if not self.verified:
            if self.rpc.call('chain_getBlockHash', [0]) != GENESIS:
                raise ValueError('Wrong chain: expected Polkadot Hub')
            self.verified = True
        at = self.rpc.call('chain_getFinalizedHead', [])
        number = int(self.rpc.call('chain_getHeader', [at])['number'], 16)
        return number
