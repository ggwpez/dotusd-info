from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import backfill
import server


class BackfillTests(unittest.TestCase):
    def test_backward_clock_preserves_boundary_and_atomic_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            db = backfill.database(Path(directory) / 'archive.sqlite3')
            rows = [(12, 'h12', 10000, 1.1, '[]'), (11, 'h11', 10000, 1.0, '[]')]
            self.assertEqual(backfill.store_batch(db, rows, 12000), 8000)
            self.assertEqual(db.execute('SELECT number,sample_time FROM samples ORDER BY number').fetchall(),
                             [(11, 8000), (12, 10000)])
            with self.assertRaises(sqlite3.IntegrityError):
                backfill.store_batch(db, [(10, 'h10', 6000, 1, '[]'), rows[0]], 8000)
            self.assertEqual(db.execute('SELECT count(*) FROM samples').fetchone()[0], 2)
            db.close()

    def test_archive_import_does_not_mask_live_refresh_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'archive.sqlite3'
            db = backfill.database(path)
            now = time.time()
            backfill.store_batch(db, [(12, 'h12', int((now-100)*1000), 1.1, '[]')], int(now*1000))
            cache = server.HistoryCache()
            cache.merge([[now - 10, 1.0, 13]], now - 5)
            worker = server.HistoryWorker(cache, archive=path)
            worker.load_archive()
            worker.load_archive()
            rows, fetched, _, _ = cache.read(now - 200, now)
            self.assertEqual([r[2] for r in rows], [12, 13])
            self.assertEqual(fetched, now - 5)
            backfill.store_batch(db, [(11, 'h11', int((now-110)*1000), .99, '[]')], int((now-100)*1000))
            worker.load_archive()
            self.assertEqual([r[2] for r in cache.read(now - 200, now)[0]], [11, 12, 13])
            db.close()

    def test_unfinalized_or_wrong_anchor_rejected(self):
        archive = backfill.Archive()
        archive.finalized = Mock(return_value=123)
        archive.rpc.block_hash = Mock(return_value='anchor')
        archive.verify(123, 'anchor')
        archive.rpc.block_hash.return_value = 'wrong-fork'
        with self.assertRaises(ValueError):
            archive.verify(123, 'anchor')
        archive.rpc.block_hash.return_value = 'anchor'
        archive.finalized.return_value = 122
        with self.assertRaises(ValueError):
            archive.verify(123, 'anchor')

    def test_resume_keeps_original_finalized_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'archive.sqlite3'
            archive = Mock()
            archive.finalized.return_value = 13
            archive.samples.side_effect = lambda numbers: [
                (n, f'h{n}', n * 2000, 1.0, '[]') for n in numbers]
            with patch.object(backfill, 'START', 10), \
                    patch.object(backfill, 'Archive', return_value=archive), \
                    patch.object(backfill.reader, 'read_sample', return_value=(13, 'h13', 26000, 1.0, '[]')), \
                    patch.object(sys, 'argv', ['backfill.py', '--database', str(path)]), \
                    patch.object(backfill.time, 'sleep'):
                backfill.main()
                archive.finalized.return_value = 14
                backfill.main()
            with sqlite3.connect(path) as db:
                self.assertEqual(dict(db.execute('SELECT key,value FROM metadata'))['anchor'], '13')
                self.assertEqual(db.execute('SELECT number FROM samples ORDER BY number').fetchall(),
                                 [(10,), (11,), (12,)])

    def test_completed_archive_is_readable_without_wal_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'archive.sqlite3'
            db = backfill.database(path)
            now = time.time()
            backfill.store_batch(db, [(12, 'h12', int((now-100)*1000), 1.1, '[]')], int(now*1000))
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            db.close()
            self.assertFalse(Path(str(path) + '-wal').exists())
            self.assertFalse(Path(str(path) + '-shm').exists())
            worker = server.HistoryWorker(server.HistoryCache(), archive=path)
            self.assertTrue(worker.load_archive())
            self.assertEqual(worker.cache.rows[0][1:], (1.1, 12))

    def test_archive_failure_does_not_revoke_loaded_history(self):
        cache = server.HistoryCache()
        worker = server.HistoryWorker(cache)
        worker.load_archive = Mock(side_effect=[True, sqlite3.OperationalError('archive unavailable')])
        readiness = []
        def wait(_):
            readiness.append(cache.history_ready)
            if len(readiness) == 2:
                worker.stop.set()
        worker.stop.wait = wait
        with patch.object(server.logging, 'exception'):
            worker.restore()
        self.assertEqual(readiness, [True, True])
