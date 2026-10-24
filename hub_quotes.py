"""Bounded Hub RPC access and chain verification for pool-history gap repair."""
import json
import time
import urllib.request

from archive_reader import Rpc

RPC = 'https://polkadot-asset-hub-rpc.polkadot.io'
GENESIS = '0x68d56f15f85d3136970ec16946040bc1752654e906147f7e43e9d539d7c3de2f'


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
        self.verified = False

    def finalized(self):
        if not self.verified:
            if self.rpc.call('chain_getBlockHash', [0]) != GENESIS:
                raise ValueError('Wrong chain: expected Polkadot Hub')
            self.verified = True
        at = self.rpc.call('chain_getFinalizedHead', [])
        number = int(self.rpc.call('chain_getHeader', [at])['number'], 16)
        return number
