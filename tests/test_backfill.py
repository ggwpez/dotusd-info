import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
with patch.dict(sys.modules, {'websocket': Mock()}):
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
            worker = server.HistoryWorker(cache, 'http://unused', archive=path)
            worker.load_archive()
            worker.load_archive()
            rows, fetched, _, _ = cache.read(now - 200, now)
            self.assertEqual([r[2] for r in rows], [12, 13])
            self.assertEqual(fetched, now - 5)
            backfill.store_batch(db, [(11, 'h11', int((now-110)*1000), .99, '[]')], int((now-100)*1000))
            worker.load_archive()
            self.assertEqual([r[2] for r in cache.read(now - 200, now)[0]], [11, 12, 13])
            db.close()

    def test_chain_and_anchor_verified(self):
        archive = object.__new__(backfill.Archive)
        archive.batch = Mock(return_value=[backfill.GENESIS, 'anchor'])
        archive.verify(123, 'anchor')
        for response in [('wrong-chain', 'anchor'), (backfill.GENESIS, 'wrong-fork')]:
            archive.batch.return_value = response
            with self.assertRaises(ValueError):
                archive.verify(123, 'anchor')

    def test_batch_reorders_and_rejects_errors(self):
        archive = object.__new__(backfill.Archive)
        archive.ws = Mock()
        archive.ws.recv.return_value = '[{"id":1,"result":"b"},{"id":0,"result":"a"}]'
        self.assertEqual(archive.batch([('m', []), ('n', [])]), ['a', 'b'])
        archive.ws.recv.return_value = '[{"id":0,"error":{"code":-1}}]'
        with self.assertRaises(RuntimeError):
            archive.batch([('m', [])])
