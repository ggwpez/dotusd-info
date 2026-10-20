#!/usr/bin/env python3
"""Reconstruct DEX-only last-trade prices for the normal chart's exact sample range.

Requires substrate-interface and curl. Never opens or writes a production database.
"""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import time

import archive_reader

RPC = 'https://polkadot-asset-hub-rpc.polkadot.io'
GENESIS = '0x68d56f15f85d3136970ec16946040bc1752654e906147f7e43e9d539d7c3de2f'
START = 21572110
BATCH_BLOCKS = 32
RPC_INTERVAL = 0.5
_last_request = 0.0


def request(url, payload=None):
    global _last_request
    time.sleep(max(0, _last_request + RPC_INTERVAL - time.monotonic()))
    _last_request = time.monotonic()
    command = ['curl', '-fsS', '--max-time', '45']
    if payload is not None:
        command += ['-H', 'content-type: application/json', '--data-binary', '@-']
    for attempt in range(5):
        try:
            result = subprocess.run(command + [url], input=json.dumps(payload) if payload is not None else None,
                                    text=True, capture_output=True, check=True)
            return json.loads(result.stdout)
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)


def batch(calls):
    replies = request(RPC, [dict(jsonrpc='2.0', id=i, method=m, params=p) for i, (m, p) in enumerate(calls)])
    by_id = {row['id']: row for row in replies}
    if set(by_id) != set(range(len(calls))) or any('error' in row for row in replies):
        raise ValueError(f'Archive batch failed: {replies}')
    return [by_id[i]['result'] for i in range(len(calls))]


def make_decoder():
    from substrateinterface import SubstrateInterface

    class Decoder(SubstrateInterface):
        def rpc_request(self, method, params, result_handler=None):
            return request(RPC, dict(jsonrpc='2.0', id=1, method=method, params=params))

    return Decoder(url=RPC, auto_discover=False)


def is_dot(location):
    return location == {'parents': 1, 'interior': 'Here'}


def is_asset(location, asset_id):
    return location == {'parents': 0, 'interior': {'X2': [{'PalletInstance': 50}, {'GeneralIndex': asset_id}]}}


def qualifying_trades(events, asset_id=7873):
    result = []
    for index, record in enumerate(events):
        event = record.get('event', record)
        if event.get('module_id') != 'AssetConversion' or event.get('event_id') not in ('SwapExecuted', 'SwapCreditExecuted'):
            continue
        path = event['attributes']['path']
        for hop, ((a, amount_a), (b, amount_b)) in enumerate(zip(path, path[1:])):
            if is_dot(a) and is_asset(b, asset_id):
                dot, asset = amount_a, amount_b
            elif is_asset(a, asset_id) and is_dot(b):
                dot, asset = amount_b, amount_a
            else:
                continue
            if dot >= 10000 and asset >= 10000:
                # Same fixed-point precision and dust threshold as the DEX ticker.
                fixed = asset * 10 ** 22 // dot
                result.append(dict(quote=str(Decimal(fixed) / 10 ** 18), event=index, hop=hop))
    return result


class Reconstruction:
    def __init__(self, work):
        self.work = work
        self.hashes, self.stamps, self.blocks = {}, {}, {}
        self.index_path = work.with_suffix('.index.json')
        if self.index_path.exists():
            saved = json.loads(self.index_path.read_text())
            self.hashes = {int(k): v for k, v in saved['hashes'].items()}
            self.stamps = {int(k): v for k, v in saved['stamps'].items()}
        if work.exists():
            for line in work.read_text().splitlines():
                value = json.loads(line)
                if value.get('source') != 'dex-both-legs-v2':
                    raise ValueError('Use a new work cache: old caches contain only one ticker leg')
                self.blocks[value['number']] = value
                self.stamps[value['number']] = value['timestamp']
        genesis, head_hash = batch([('chain_getBlockHash', [0]), ('chain_getFinalizedHead', [])])
        if genesis != GENESIS:
            raise ValueError('Wrong chain')
        header = batch([('chain_getHeader', [head_hash])])[0]
        self.head = int(header['number'], 16)
        self.decoder = make_decoder()
        self.decoder.init_runtime(block_hash=head_hash)
        from substrateinterface.storage import StorageKey
        self.storage = StorageKey.create_from_storage_function(
            'System', 'Events', [], runtime_config=self.decoder.runtime_config,
            metadata=self.decoder.metadata)
        self.needles = [bytes([module.value['index'], event.value['index']])
                        for module in self.decoder.metadata.pallets if module.name == 'AssetConversion'
                        for event in module.events if event.name in ('SwapExecuted', 'SwapCreditExecuted')]
        if not self.needles:
            raise ValueError('Swap event descriptors missing')
        print(f'Decoder ready, finalized #{self.head}; {len(self.blocks)} cached blocks', flush=True)

    def load_hashes(self, numbers):
        missing = list(dict.fromkeys(n for n in numbers if n not in self.hashes))
        for offset in range(0, len(missing), 32):
            chunk = missing[offset:offset+32]
            values = batch([('chain_getBlockHash', [n]) for n in chunk])
            if any(not value for value in values):
                raise ValueError('Missing archive block')
            self.hashes.update(zip(chunk, values))

    def load_stamps(self, numbers):
        missing = list(dict.fromkeys(n for n in numbers if n not in self.stamps))
        self.load_hashes(missing)
        for offset in range(0, len(missing), 32):
            chunk = missing[offset:offset+32]
            values = batch([('state_getStorage', [archive_reader.TIMESTAMP, self.hashes[n]]) for n in chunk])
            self.stamps.update((n, int.from_bytes(bytes.fromhex(raw[2:]), 'little')) for n, raw in zip(chunk, values))
            temporary = self.index_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(dict(hashes=self.hashes, stamps=self.stamps)))
            temporary.replace(self.index_path)

    def locate(self, stamps):
        # Reuse completed searches when both sides of the timestamp boundary are cached.
        ordered = sorted(self.stamps)
        import bisect
        cached_times = [self.stamps[n] for n in ordered]
        bounds = []
        for stamp in stamps:
            i = bisect.bisect_right(cached_times, stamp) - 1
            n = ordered[i] if i >= 0 else None
            exact = n is not None and n + 1 in self.stamps and self.stamps[n + 1] > stamp
            bounds.append([n, n] if exact else [START, self.head])
        while any(lo < hi for lo, hi in bounds):
            mids = [(lo+hi+1)//2 for lo, hi in bounds]
            self.load_stamps(mids)
            print(f'Locating timestamps: {sum(lo < hi for lo, hi in bounds)} searches remaining; {len(self.stamps)} cached timestamps', flush=True)
            for i, (lo, hi) in enumerate(bounds):
                if lo == hi:
                    continue
                mid = mids[i]
                bounds[i] = [mid, hi] if self.stamps[mid] <= stamps[i] else [lo, mid-1]
        if hasattr(self, 'index_path'):
            self.index_path.write_text(json.dumps(dict(hashes=self.hashes, stamps=self.stamps)))
        return [lo for lo, _ in bounds]

    def read_blocks(self, numbers):
        missing = [n for n in numbers if n not in self.blocks]
        self.load_hashes(missing)
        if missing:
            calls = []
            for n in missing:
                at = self.hashes[n]
                calls += [('state_getStorage', [self.storage.to_hex(), at]),
                          ('state_getStorage', [archive_reader.TIMESTAMP, at]),
                          ('state_getRuntimeVersion', [at])]
            values = batch(calls)
            with self.work.open('a') as file:
                for i, n in enumerate(missing):
                    raw, stamp, runtime = values[i*3:i*3+3]
                    if runtime['specVersion'] != self.decoder.runtime_version:
                        raise ValueError('Runtime changed; reconstruct each runtime separately')
                    encoded = bytes.fromhex(raw[2:])
                    trades = {'7873': [], '1984': []}
                    if any(needle in encoded for needle in self.needles):
                        from scalecodec.base import ScaleBytes
                        events = self.decoder.runtime_config.create_scale_object(
                            self.storage.value_scale_type, data=ScaleBytes(raw), metadata=self.decoder.metadata).decode()
                        trades = {str(asset): qualifying_trades(events, asset) for asset in (7873, 1984)}
                    record = dict(source='dex-both-legs-v2', number=n, timestamp=int.from_bytes(bytes.fromhex(stamp[2:]), 'little'), trades=trades)
                    self.blocks[n] = record
                    self.stamps[n] = record['timestamp']
                    file.write(json.dumps(record) + '\n')
            time.sleep(.05)
        return [self.blocks[n] for n in numbers]

    def quote(self, number, timestamp, asset_id=7873):
        cursor = number
        while cursor >= max(1, number-50000):
            numbers = list(range(cursor, max(0, cursor-32), -1))
            for record in self.read_blocks(numbers):
                if record['timestamp'] < timestamp - 86400000:
                    return self.reserve_quote(number, asset_id)
                if record['timestamp'] <= timestamp and record['trades'][str(asset_id)]:
                    return record['trades'][str(asset_id)][-1]['quote'], record['number'], record['timestamp']
            cursor -= len(numbers)
        return self.reserve_quote(number, asset_id)

    def reserve_quote(self, number, asset_id=7873):
        self.load_hashes([number])
        pair = b'\x01\x00\x00\x02\x04\x32\x05' + archive_reader.compact(asset_id)
        raw = batch([('state_call', ['AssetConversionApi_get_reserves', '0x'+pair.hex(), self.hashes[number]])])[0]
        data = bytes.fromhex(raw[2:])
        if len(data) != 33 or data[0] != 1:
            raise ValueError('Historical pool missing')
        dot, asset = int.from_bytes(data[1:17], 'little'), int.from_bytes(data[17:], 'little')
        return str(Decimal(asset * 10**22 // dot) / 10**18), None, None


def reconstruct_targets(worker, targets, output, chunk_size=1024):
    """One output sample per normal-chart block, with independent last-trade state per leg."""
    output.mkdir(parents=True, exist_ok=True)
    last = {}
    rows, evidence = [], []
    first = targets[0][2]
    worker.read_blocks([first])
    stamp = worker.stamps[first]
    for asset in (7873, 1984):
        last[asset] = worker.quote(first, stamp, asset)
    for offset in range(0, len(targets), BATCH_BLOCKS):
        group = targets[offset:offset+BATCH_BLOCKS]
        # Include intervening blocks even if the original chart has a collection gap.
        left = first if offset == 0 else targets[offset-1][2] + 1
        right = group[-1][2]
        needed = {row[2]: row[0] for row in group}
        for begin in range(left, right+1, BATCH_BLOCKS):
            for record in worker.read_blocks(list(range(begin, min(right+1, begin+BATCH_BLOCKS)))):
                number, chain_stamp = record['number'], record['timestamp']
                for asset in (7873, 1984):
                    trades = record['trades'][str(asset)]
                    if trades:
                        last[asset] = trades[-1]['quote'], number, chain_stamp
                if number not in needed:
                    continue
                legs = {}
                for asset in (7873, 1984):
                    quote, block, when = last[asset]
                    if when is None or when < chain_stamp - 86400000:
                        quote, block, when = worker.reserve_quote(number, asset)
                    legs[asset] = quote, block, when
                stamp = needed[number]
                dotusd, dotusd_block, dotusd_time = legs[7873]
                usdt, usdt_block, usdt_time = legs[1984]
                rows.append([stamp, float(Decimal(usdt) / Decimal(dotusd)), number])
                evidence.append(dict(timestamp=stamp, block=number, chainTimestamp=chain_stamp,
                                     dotusdPerDot=dotusd, usdtPerDot=usdt,
                                     tradeBlock=dotusd_block, tradeTimestamp=dotusd_time,
                                     usdtTradeBlock=usdt_block, usdtTradeTimestamp=usdt_time))
        if len(rows) >= chunk_size or offset+BATCH_BLOCKS >= len(targets):
            path = output / f"{int(rows[0][0]*1000)}.json"
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(dict(source='dex-last-trade-usdt-backfill-v2',
                                                  samples=rows, evidence=evidence), allow_nan=False))
            temporary.replace(path)
            print(f'Completed {min(offset+BATCH_BLOCKS,len(targets))}/{len(targets)} samples: {path}', flush=True)
            rows, evidence = [], []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--targets', type=Path, required=True, help='Saved normal-chart raw API response')
    parser.add_argument('--output', type=Path, required=True, help='Local directory for bounded import batches')
    parser.add_argument('--work', type=Path, required=True, help='Local resumable two-leg block cache')
    args = parser.parse_args()
    targets = json.loads(args.targets.read_text())['samples']
    if not targets or any(not isinstance(row[2], int) for row in targets):
        raise ValueError('Targets must contain finalized block numbers')
    if any(a[0] >= b[0] or a[2] >= b[2] for a,b in zip(targets,targets[1:])):
        raise ValueError('Targets must be strictly ordered by time and block')
    worker = Reconstruction(args.work)
    if targets[-1][2] > worker.head:
        raise ValueError('Target exceeds finalized head')
    # Newest batches first restore shared links promptly, then extend to the same oldest block.
    for stop in range(len(targets), 0, -1024):
        group = targets[max(0, stop-1024):stop]
        path = args.output / f"{int(group[0][0]*1000)}.json"
        if path.exists():
            continue
        reconstruct_targets(worker, group, args.output)


if __name__ == '__main__':
    main()
