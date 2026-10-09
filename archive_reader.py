#!/usr/bin/env python3
"""Read-only finalized Asset Hub peg index; isolated from trader RPC and execution.

Debian dependencies: python3-websocket, python3-xxhash.
Extracted from arbitrage/ops/peg-collector.py for identical pool-price decoding.
Prices are reserve-ratio spot prices, excluding swap fees and finite-size slippage.
"""
import argparse
import hashlib
import json
import logging
import math
from pathlib import Path
import struct
import time
import urllib.error
import urllib.request

LOG = logging.getLogger('peg-collector')
TIMESTAMP = '0xf0c365c3cf59d671eb72da0e7a4113c49f1f0515f462cdcf84e0f1d6045dfcbb'


class Unavailable(Exception):
    """Explicitly missing/pruned block state; other failures must be retried."""


class Rpc:
    def __init__(self, endpoint, delay=0.1):
        self.endpoint = endpoint
        self.delay = delay

    def call(self, method, params):
        time.sleep(self.delay)  # One request at a time, at most ten requests/sec.
        request = urllib.request.Request(self.endpoint, json.dumps(dict(
            jsonrpc='2.0', id=1, method=method, params=params)).encode(),
            {'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.load(response)
        if 'error' in result:
            message = str(result['error'])
            if any(s in message.lower() for s in ('state already discarded', 'state is not available', 'unknown block')):
                raise Unavailable(message)
            raise RuntimeError(message)
        return result['result']

    def finalized(self):
        import websocket
        # A separate short-lived follow connection establishes finality; no best-head guess.
        ws = websocket.create_connection(self.endpoint.replace('http', 'ws', 1), timeout=10)
        try:
            ws.send(json.dumps(dict(jsonrpc='2.0', id=1, method='chainHead_v1_follow', params=[False])))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                message = json.loads(ws.recv())
                if 'error' in message:
                    raise RuntimeError(str(message['error']))
                event = message.get('params', {}).get('result', {})
                if event.get('event') == 'initialized':
                    at = event['finalizedBlockHashes'][-1]
                    return int(self.call('chain_getHeader', [at])['number'], 16)
            raise TimeoutError('No finalized initialization')
        finally:
            ws.close()

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

    def available(self, number):
        try:
            at = self.block_hash(number)
            return self.storage(at, [TIMESTAMP])[TIMESTAMP] is not None
        except Unavailable:
            return False


def oldest_retained(rpc, head):
    """Exponential/binary search, then oldest-first reads outrun pruning."""
    if not rpc.available(head):
        raise Unavailable('Finalized state unavailable')
    high, distance = head, 1
    while True:
        low = max(0, head - distance)
        if not rpc.available(low):
            break
        high = low
        if low == 0:
            return 0
        distance *= 2
    while high - low > 1:
        middle = (high + low) // 2
        if rpc.available(middle):
            high = middle
        else:
            low = middle
    return high


def compact(value):
    if value < 64:
        return bytes([value << 2])
    if value < 16384:
        return ((value << 2) | 1).to_bytes(2, 'little')
    raise ValueError('Unsupported asset ID')


def metadata_key(asset):
    import xxhash
    def twox128(text):
        return b''.join(xxhash.xxh64(text.encode(), seed=i).intdigest().to_bytes(8, 'little') for i in (0, 1))
    encoded = asset.to_bytes(4, 'little')
    return '0x' + (twox128('Assets') + twox128('Metadata') +
                   hashlib.blake2b(encoded, digest_size=16).digest() + encoded).hex()


def decimals(encoded):
    if encoded is None:
        raise ValueError('Missing asset metadata')
    data = bytes.fromhex(encoded[2:])
    offset = 16  # AssetMetadata: u128 deposit, bounded name, bounded symbol, decimals, frozen.
    for _ in range(2):
        mode = data[offset] & 3
        width = (1, 2, 4)[mode] if mode < 3 else 0
        if not width:
            raise ValueError('Unsupported metadata string length')
        length = int.from_bytes(data[offset:offset + width], 'little') >> 2
        offset += width + length
    if len(data) != offset + 2:
        raise ValueError('Unsupported asset metadata layout')
    return data[offset]


def reserves(rpc, at, asset):
    # SCALE v5 Location: parent/Here DOT, local X2(PalletInstance(50), GeneralIndex).
    pair = b'\x01\x00\x00\x02\x04\x32\x05' + compact(asset)
    result = bytes.fromhex(rpc.call('state_call', ['AssetConversionApi_get_reserves', '0x' + pair.hex(), at])[2:])
    if result == b'\x00':
        return None
    if len(result) != 33 or result[0] != 1:
        raise ValueError('Unsupported reserves response')
    return int.from_bytes(result[1:17], 'little'), int.from_bytes(result[17:], 'little')


def spot(dot_internal, internal, dot_external, external, internal_decimals, external_decimals):
    if min(dot_internal, internal, dot_external, external) <= 0:
        raise ValueError('Empty pool')
    return (dot_internal / internal) * (external / dot_external) * 10 ** (internal_decimals - external_decimals)


def read_sample(rpc, number):
    at = rpc.block_hash(number)
    keys = [TIMESTAMP, metadata_key(7873), metadata_key(1984)]
    values = rpc.storage(at, keys)
    if values[TIMESTAMP] is None:
        raise Unavailable(f'Missing timestamp at {number}')
    stamp = bytes.fromhex(values[TIMESTAMP][2:])
    if len(stamp) != 8:
        raise ValueError('Invalid timestamp')
    timestamp = int.from_bytes(stamp, 'little')
    internal, external = reserves(rpc, at, 7873), reserves(rpc, at, 1984)
    # Do not fabricate a peg when either pool is absent.
    price = None
    if internal and external and min(*internal, *external) > 0:
        price = spot(*internal, *external, decimals(values[keys[1]]), decimals(values[keys[2]]))
        if not math.isfinite(price) or price <= 0:
            raise ValueError('Invalid spot price')
    return number, at, timestamp, price, json.dumps([internal, external])

