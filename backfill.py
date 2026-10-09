#!/usr/bin/env python3
"""Resumable Parity archive import from referendum 1944 into chart-only history."""
import argparse
from contextlib import closing
import json
import logging
from pathlib import Path
import sqlite3
import time
import websocket
import archive_reader as reader

GENESIS = '0x68d56f15f85d3136970ec16946040bc1752654e906147f7e43e9d539d7c3de2f'
START = 21572110
RPC = 'wss://polkadot-asset-hub-rpc.polkadot.io'
BATCH = 32


class Archive:
    def __init__(self):
        self.ws = websocket.create_connection(RPC, timeout=30)

    def batch(self, calls):
        self.ws.send(json.dumps([dict(jsonrpc='2.0', id=i, method=m, params=p)
                                 for i, (m, p) in enumerate(calls)]))
        result = json.loads(self.ws.recv())
        if not isinstance(result, list) or len(result) != len(calls):
            raise ValueError('Incomplete archive batch')
        by_id = {r['id']: r for r in result}
        if set(by_id) != set(range(len(calls))):
            raise ValueError('Archive response IDs mismatch')
        rows = [by_id[i] for i in range(len(calls))]
        for row in rows:
            if 'error' in row:
                raise RuntimeError(row['error'])
        return [r['result'] for r in rows]

    def verify(self, anchor, anchor_hash):
        genesis, at = self.batch([('chain_getBlockHash', [0]), ('chain_getBlockHash', [anchor])])
        if genesis != GENESIS or at != anchor_hash:
            raise ValueError('Archive chain or finalized anchor mismatch')

    def samples(self, numbers):
        hashes = self.batch([('chain_getBlockHash', [n]) for n in numbers])
        if any(h is None for h in hashes):
            raise ValueError('Archive block hash missing')
        keys = [reader.TIMESTAMP, reader.metadata_key(7873), reader.metadata_key(1984)]
        calls = []
        for at in hashes:
            calls.append(('state_queryStorageAt', [keys, at]))
            for asset in (7873, 1984):
                pair = b'\x01\x00\x00\x02\x04\x32\x05' + reader.compact(asset)
                calls.append(('state_call', ['AssetConversionApi_get_reserves', '0x' + pair.hex(), at]))
        values = self.batch(calls)
        responses = {json.dumps(call): value for call, value in zip(calls, values)}
        class Recorded(reader.Rpc):
            def block_hash(self, number):
                return hashes[numbers.index(number)]
            def call(self, method, params):
                return responses[json.dumps((method, params))]
        rpc = Recorded(RPC)
        return [reader.read_sample(rpc, n) for n in numbers]


def database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    db.execute('''CREATE TABLE IF NOT EXISTS samples (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, number INTEGER UNIQUE NOT NULL,
        hash TEXT UNIQUE NOT NULL, timestamp INTEGER NOT NULL,
        sample_time INTEGER NOT NULL, price REAL, reserves TEXT NOT NULL)''')
    return db


def store_batch(db, rows, next_time):
    with db:
        for number, at, timestamp, price, reserves in rows:
            next_time = min(timestamp, next_time - 2000)
            db.execute('INSERT INTO samples(number,hash,timestamp,sample_time,price,reserves) VALUES(?,?,?,?,?,?)',
                       (number, at, timestamp, next_time, price, reserves))
    return next_time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=Path('/var/lib/dotusd-info/archive.sqlite3'))
    parser.add_argument('--live-database', default='/var/lib/arb-peg/peg.sqlite3')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    db = database(args.database)
    with closing(sqlite3.connect(f'file:{args.live_database}?mode=ro', uri=True)) as live:
        anchor, anchor_hash, anchor_time = live.execute(
            'SELECT number,hash,sample_time FROM blocks ORDER BY number LIMIT 1').fetchone()
    expected = {'start': str(START), 'anchor': str(anchor), 'anchor_hash': anchor_hash, 'anchor_time': str(anchor_time)}
    saved = dict(db.execute('SELECT key,value FROM metadata'))
    if saved and saved != expected:
        raise ValueError('Import anchor changed; refusing to mix histories')
    with db:
        db.executemany('INSERT OR IGNORE INTO metadata VALUES(?,?)', expected.items())
    oldest = db.execute('SELECT number,sample_time FROM samples ORDER BY number LIMIT 1').fetchone()
    end, next_time = oldest if oldest else (anchor, anchor_time)
    archive = Archive()
    try:
        archive.verify(anchor, anchor_hash)
        logging.info('Backfill %s..%s from %s; resuming at %s', START, anchor - 1, RPC, end - 1)
        while end > START:
            begin = max(START, end - BATCH)
            rows = archive.samples(list(range(end - 1, begin - 1, -1)))
            next_time = store_batch(db, rows, next_time)
            end = begin
            if (anchor - end) % 1024 == 0 or end == START:
                logging.info('Imported %s/%s blocks; oldest=%s', anchor - end, anchor - START, end)
            time.sleep(0.2)
        count, low, high = db.execute('SELECT count(*),min(number),max(number) FROM samples').fetchone()
        if (count, low, high) != (anchor - START, START, anchor - 1):
            raise ValueError('Backfill coverage is not contiguous')
        logging.info('COMPLETE: %s consecutive finalized blocks, %s..%s', count, low, high)
    finally:
        archive.ws.close()
        db.close()


if __name__ == '__main__':
    main()
