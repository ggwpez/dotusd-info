#!/usr/bin/env python3
"""Resumable Parity archive import from referendum 1944 into chart-only history."""
import argparse
import logging
from pathlib import Path
import sqlite3
import time
import archive_reader as reader
from hub_quotes import HubChain, RPC

START = 21572110
BATCH = 32
PUBLISHED = Path('/var/lib/dotusd-info/archive.sqlite3')
STAGING = Path('/var/lib/dotusd-backfill/archive.sqlite3')


class Archive(HubChain):
    def verify(self, anchor, anchor_hash):
        if self.finalized() < anchor or self.rpc.block_hash(anchor) != anchor_hash:
            raise ValueError('Archive chain or finalized anchor mismatch')

    def samples(self, numbers):
        return [reader.read_sample(self.rpc, number) for number in numbers]


def database(path):
    if path.resolve() == PUBLISHED.resolve() or (path.exists() and PUBLISHED.exists() and path.samefile(PUBLISHED)):
        raise ValueError('Backfill must not write to the published archive; use staging')
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    # Read-only consumers cannot recreate WAL sidecars after the writer exits.
    # Rollback journaling leaves a self-contained archive on completion.
    db.execute('PRAGMA journal_mode=DELETE')
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
    parser.add_argument('--database', type=Path, default=STAGING)
    parser.add_argument('--hub-rpc', default=RPC, help='Polkadot Asset Hub HTTP archive RPC')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    db = database(args.database)
    archive = Archive(args.hub_rpc)
    saved = dict(db.execute('SELECT key,value FROM metadata'))
    if saved:
        if set(saved) != {'start', 'anchor', 'anchor_hash', 'anchor_time'} or saved['start'] != str(START):
            raise ValueError('Import metadata is incompatible')
        anchor, anchor_hash, anchor_time = int(saved['anchor']), saved['anchor_hash'], int(saved['anchor_time'])
        expected = saved
    else:
        anchor = archive.finalized()
        _, anchor_hash, anchor_time, _, _ = reader.read_sample(archive.rpc, anchor)
        expected = {'start': str(START), 'anchor': str(anchor), 'anchor_hash': anchor_hash, 'anchor_time': str(anchor_time)}
    archive.verify(anchor, anchor_hash)
    with db:
        db.executemany('INSERT OR IGNORE INTO metadata VALUES(?,?)', expected.items())
    oldest = db.execute('SELECT number,sample_time FROM samples ORDER BY number LIMIT 1').fetchone()
    end, next_time = oldest if oldest else (anchor, anchor_time)
    try:
        logging.info('Backfill %s..%s from %s; resuming at %s', START, anchor - 1, args.hub_rpc, end - 1)
        while end > START:
            begin = max(START, end - BATCH)
            rows = archive.samples(range(end - 1, begin - 1, -1))
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
        db.close()


if __name__ == '__main__':
    main()
