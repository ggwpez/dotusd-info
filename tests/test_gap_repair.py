import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import gap_repair
from server import HistoryCache


class GapRepairTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = time.time()
        self.start = self.now - 1000
        self.cache = HistoryCache()
        self.cache.merge([(self.start, 1.0, 100), (self.start + 40, 1.02, 120)], self.now - 10)
        self.cache.history_ready = True
        self.chain = Mock()
        self.chain.finalized.return_value = 150
        self.worker = gap_repair.GapRepair(self.cache, Path(self.directory.name) / 'repairs.db', self.chain)
        self.db = self.worker.database()
        self.addCleanup(self.db.close)

    def sample(self, rpc, number):
        return number, f'h{number}', int((self.start + (number - 100) * 2) * 1000), 1.01, '[]'

    def test_small_batches_complete_gap_without_refreshing_live_status(self):
        with patch.object(gap_repair, 'read_sample', side_effect=self.sample) as read:
            self.assertEqual(self.worker.poll(self.db), 8)
            self.assertEqual(read.call_count, 8)
            self.assertEqual(self.worker.poll(self.db), 8)
            self.assertEqual(self.worker.poll(self.db), 3)
            self.assertEqual(self.worker.poll(self.db), 0)
        self.assertEqual(self.chain.rpc.delay, .5)
        self.assertEqual([r[2] for r in self.cache.rows], list(range(100, 121)))
        self.assertEqual(self.cache.rows[0], (self.start, 1.0, 100))
        self.assertEqual(self.cache.rows[-1], (self.start + 40, 1.02, 120))
        self.assertEqual(self.cache.fetched_at, self.now - 10)
        self.assertEqual(self.db.execute('SELECT count(*) FROM repairs').fetchone()[0], 19)

    def test_failed_batch_is_atomic_and_retried(self):
        with patch.object(gap_repair, 'read_sample', side_effect=[self.sample(None, 101), OSError('RPC down')]):
            with self.assertRaises(OSError):
                self.worker.poll(self.db)
        self.assertEqual(len(self.cache.rows), 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM repairs').fetchone()[0], 0)
        with patch.object(gap_repair, 'read_sample', side_effect=self.sample) as read:
            self.worker.poll(self.db)
        self.assertEqual(read.call_args_list[0].args[1], 101)

    def test_restart_restores_progress_and_preserves_collector_rows(self):
        with patch.object(gap_repair, 'read_sample', side_effect=self.sample):
            self.worker.poll(self.db)
        fresh = HistoryCache()
        fresh.merge([(self.start, 1., 100), (self.start + 2.5, .99, 101),
                     (self.start + 40, 1.02, 120)], self.now)
        fresh.history_ready = True
        restarted = gap_repair.GapRepair(fresh, self.worker.path, self.chain)
        with patch.object(restarted.stop, 'wait', return_value=False):
            restarted.restore(self.db)
        self.assertEqual([r for r in fresh.rows if r[2] == 101], [(self.start + 2.5, .99, 101)])
        with patch.object(gap_repair, 'read_sample', side_effect=self.sample) as read:
            restarted.poll(self.db)
        self.assertEqual(read.call_args_list[0].args[1], 109)

    def test_late_collector_replaces_repair_by_block(self):
        with patch.object(gap_repair, 'read_sample', side_effect=self.sample):
            self.worker.poll(self.db)
        self.cache.merge([(self.start + 2.5, .99, 101)], self.now)
        self.assertEqual([r for r in self.cache.rows if r[2] == 101], [(self.start + 2.5, .99, 101)])

    def test_no_queries_for_warming_recent_or_timestamp_only_gaps(self):
        self.cache.history_ready = False
        self.assertEqual(self.worker.poll(self.db), 0)
        self.cache.history_ready = True
        self.cache.merge([(self.now - 30, 1., 1), (self.now - 20, 1., 3)], self.now, replace=True)
        self.assertEqual(self.worker.poll(self.db), 0)
        self.cache.merge([(self.start, 1., 1), (self.start + 100, 1., 2)], self.now, replace=True)
        self.assertEqual(self.worker.poll(self.db), 0)
        self.chain.finalized.assert_not_called()

    def test_requires_finalized_boundary_and_available_price(self):
        self.chain.finalized.return_value = 119
        with patch.object(gap_repair, 'read_sample') as read:
            with self.assertRaisesRegex(ValueError, 'finalized'):
                self.worker.poll(self.db)
            read.assert_not_called()
        self.chain.finalized.return_value = 150
        with patch.object(gap_repair, 'read_sample', return_value=(101, 'h', 1000, None, '[]')):
            with self.assertRaisesRegex(ValueError, 'No pool price'):
                self.worker.poll(self.db)
        self.assertEqual(len(self.cache.rows), 2)

    def test_worker_pauses_between_batches_and_backs_off(self):
        worker = self.worker
        worker.database = Mock(return_value=self.db)
        worker.restore = Mock()
        worker.poll = Mock(side_effect=[8, OSError('RPC down'), OSError('RPC down'), 8])
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 4:
                worker.stop.set()
        with patch.object(worker.stop, 'wait', side_effect=wait), patch.object(gap_repair.logging, 'exception'):
            worker.run()
        self.assertEqual(waits, [60, 120, 240, 60])

