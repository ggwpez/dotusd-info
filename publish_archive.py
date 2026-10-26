#!/usr/bin/env python3
"""Validate a staged archive and publish append-only history without touching live services."""
import argparse
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time

from hub_quotes import HubChain, RPC

START = 21572110


def readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)


def copy_snapshot(source, destination):
    source.backup(destination, pages=64, progress=lambda *_: time.sleep(0.02))


def seed(staging, published):
    # Exclusive creation preserves an existing resumable work database.
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    os.close(fd)
    try:
        with closing(readonly(published)) as source, closing(sqlite3.connect(staging)) as destination:
            copy_snapshot(source, destination)
            destination.execute('PRAGMA journal_mode=DELETE')
    except Exception:
        staging.unlink(missing_ok=True)
        raise
    return 'Temporary staging copy seeded in 64-page batches; production untouched'


def validate(db, chain):
    if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
        raise ValueError('Archive integrity check failed')
    metadata = dict(db.execute('SELECT key,value FROM metadata'))
    if set(metadata) != {'start', 'anchor', 'anchor_hash', 'anchor_time'} or metadata['start'] != str(START):
        raise ValueError('Archive metadata is incompatible')
    anchor, anchor_time = int(metadata['anchor']), int(metadata['anchor_time'])
    if chain.finalized() < anchor or chain.rpc.block_hash(anchor) != metadata['anchor_hash']:
        raise ValueError('Archive chain or finalized anchor mismatch')
    count, low, high, unique = db.execute(
        'SELECT count(*),min(number),max(number),count(DISTINCT number) FROM samples').fetchone()
    if (count, low, high, unique) != (anchor - START, START, anchor - 1, anchor - START):
        raise ValueError('Archive coverage is not complete and contiguous')
    previous = None
    for number, stamp, price in db.execute('SELECT number,sample_time,price FROM samples ORDER BY number'):
        if stamp >= anchor_time or (previous is not None and stamp <= previous):
            raise ValueError('Archive sample clock is not ordered before the finalized anchor')
        if price is not None and not (0 < price < float('inf')):
            raise ValueError('Invalid archive price')
        previous = stamp
    return count


def publish(staging, target, chain):
    if staging.resolve() == target.resolve() or (target.exists() and staging.samefile(target)):
        raise ValueError('Staging and published archive must be separate files')
    # No in-place writers are allowed during publication. A rollback backup also
    # remains usable by the read-only server without auxiliary SQLite files.
    for suffix in ('-wal', '-shm', '-journal'):
        if Path(str(target) + suffix).exists():
            raise ValueError('Published archive has SQLite sidecars; refusing replacement')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.archive-publish-', suffix='.sqlite3', dir=target.parent)
    os.close(fd)
    snapshot = Path(name)
    try:
        # SQLite backup captures a consistent snapshot even if staging is resumed.
        with closing(readonly(staging)) as source, closing(sqlite3.connect(snapshot)) as db:
            # Limit each copy step to 64 SQLite pages (usually 256 KiB),
            # yielding between steps instead of copying the archive in one burst.
            copy_snapshot(source, db)
            db.execute('PRAGMA journal_mode=DELETE')
            count = validate(db, chain)
            if target.exists():
                with closing(readonly(target)) as current:
                    if dict(current.execute('SELECT key,value FROM metadata')) != dict(db.execute('SELECT key,value FROM metadata')):
                        raise ValueError('Published archive metadata differs')
                    # The server resumes by seq. Preserve every existing row and
                    # sequence; only newly appended rows may be published.
                    old_count, old_max = current.execute('SELECT count(*),coalesce(max(seq),0) FROM samples').fetchone()
                    for row in current.execute('SELECT * FROM samples ORDER BY seq'):
                        if db.execute('SELECT * FROM samples WHERE seq=?', (row[0],)).fetchone() != row:
                            raise ValueError('Publication would change existing history or sequence IDs')
                    if db.execute('SELECT count(*) FROM samples WHERE seq<=?', (old_max,)).fetchone()[0] != old_count:
                        raise ValueError('New rows must follow the published sequence cursor')
                    if count == old_count:
                        return f'Already published: {count} verified blocks; production archive untouched'
                backup = Path(str(target) + '.previous')
                with target.open('rb') as original, backup.open('wb') as saved:
                    while chunk := original.read(256 * 1024):
                        saved.write(chunk)
                        time.sleep(0.02)
                shutil.copystat(target, backup)
        snapshot.chmod(0o644)
        with snapshot.open('rb') as file:
            os.fsync(file.fileno())
        os.replace(snapshot, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return f'Published {count} verified blocks atomically; no service restart needed'
    finally:
        snapshot.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staging', type=Path, default=Path('/var/lib/dotusd-backfill/archive.sqlite3'))
    parser.add_argument('--target', type=Path, default=Path('/var/lib/dotusd-info/archive.sqlite3'))
    parser.add_argument('--seed-staging', action='store_true',
                        help='Create a temporary work copy from the published archive in small batches')
    parser.add_argument('--cleanup-staging', action='store_true',
                        help='Remove the temporary work database only after successful publication')
    parser.add_argument('--hub-rpc', default=RPC, help='Polkadot Asset Hub HTTP archive RPC')
    args = parser.parse_args()
    if args.seed_staging:
        print(seed(args.staging, args.target))
    else:
        print(publish(args.staging, args.target, HubChain(args.hub_rpc)))
    if args.cleanup_staging and not args.seed_staging:
        args.staging.unlink()
