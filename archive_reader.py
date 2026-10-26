#!/usr/bin/env python3
"""Canonical Asset Hub pool decoding and estimated price timestamps.

Prices are reserve-ratio spot prices, excluding swap fees and slippage.
"""
import hashlib
import json
import math

TIMESTAMP = '0xf0c365c3cf59d671eb72da0e7a4113c49f1f0515f462cdcf84e0f1d6045dfcbb'


class Unavailable(Exception):
    """Explicitly missing/pruned block state; other failures must be retried."""


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


def price_time(number, timestamp, lower=None, upper=None, now=None):
    """Fit an estimated price clock between immutable saved block boundaries.

    Chain time can repeat. Prefer two seconds per block, compressing the spacing
    only when preserved boundaries are closer. Never move a live sample into the
    future, and keep the original chain timestamp separately as evidence.
    """
    stamp = timestamp / 1000
    if lower is not None and upper is not None:
        width = upper[0] - lower[0]
        step = min(2, (upper[1] - lower[1]) / width)
        if step <= 0:
            raise ValueError('Price boundaries moved backwards')
        stamp = min(max(stamp, lower[1] + step * (number - lower[0])),
                    upper[1] - step * (upper[0] - number))
    elif upper is not None:
        stamp = min(stamp, upper[1] - 2 * (upper[0] - number))
    elif lower is not None:
        stamp = max(stamp, lower[1] + 2 * (number - lower[0]))
    if now is not None:
        stamp = min(stamp, now)
    if ((lower is not None and stamp <= lower[1]) or
            (upper is not None and stamp >= upper[1])):
        raise ValueError('Price boundaries cannot fit distinct sample times')
    return stamp

