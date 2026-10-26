import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import gap_repair
from issuance import IssuanceCache
from server import HistoryCache, HistoryWorker
from treasury import TreasuryHistory


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

    def test_restart_restores_progress_and_preserves_live_rows(self):
        with patch.object(gap_repair, 'read_sample', side_effect=self.sample):
            self.worker.poll(self.db)
        fresh = HistoryCache(history_path=self.worker.path)
        fresh.merge([(self.start, 1., 100), (self.start + 2.5, .99, 101),
                     (self.start + 40, 1.02, 120)], self.now)
        fresh.history_ready = True
        restarted = gap_repair.GapRepair(fresh, self.worker.path, self.chain)
        HistoryWorker(fresh, chain=self.chain).load_history()
        self.assertEqual([r for r in fresh.rows if r[2] == 101], [(self.start + 2.5, .99, 101)])
        with patch.object(gap_repair, 'read_sample', side_effect=self.sample) as read:
            restarted.poll(self.db)
        self.assertEqual(read.call_args_list[0].args[1], 109)

    def test_restore_and_poll_preserve_expired_disk_repairs(self):
        old = self.now - gap_repair.RETENTION - 1
        evidence = (1, 'old-hash', int(old * 1000), old, 1.01, '[]')
        with self.db:
            self.db.execute('INSERT INTO repairs VALUES(?,?,?,?,?,?)', evidence)
        self.cache.history_path = self.worker.path
        HistoryWorker(self.cache, chain=self.chain).load_history()
        self.cache.history_ready = False
        self.worker.poll(self.db)
        self.assertEqual(self.db.execute('SELECT * FROM repairs').fetchall(), [evidence])
        self.assertNotIn(1, [row[2] for row in self.cache.rows])

    def test_live_price_replaces_restored_repair_by_block(self):
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
        self.assertEqual(waits, [gap_repair.PAUSE, 120, 240, gap_repair.PAUSE])

    def test_leading_backfill_uses_only_unused_slots_and_retains_live_freshness(self):
        self.complete_prices()
        before = self.cache.fetched_at
        with patch.object(gap_repair, 'read_sample', side_effect=self.sample) as read:
            self.worker.backfill(self.db, 3)
        self.assertEqual([call.args[1] for call in read.call_args_list], [99, 98, 97])
        self.assertEqual([row[2] for row in self.cache.rows], list(range(97, 121)))
        self.assertEqual(self.cache.fetched_at, before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM repairs').fetchone()[0], 3)

    def test_leading_backfill_waits_for_restore_and_respects_retention_and_budget(self):
        with patch.object(gap_repair, 'read_sample') as read:
            self.cache.history_ready = False
            self.worker.backfill(self.db, 8)
            self.cache.history_ready = True
            self.worker.backfill(self.db, 0)
            self.cache.merge([(self.now - gap_repair.RETENTION + 1, 1., 1)],
                             self.now, replace=True)
            self.worker.backfill(self.db, 8)
        read.assert_not_called()

    def enable_issuance(self):
        self.worker.issuance = IssuanceCache(self.worker.path)
        return self.worker.issuance

    def issuance_sample(self, rpc, number, at=None, timestamp=None):
        stamp = timestamp if timestamp is not None else int((self.start + (number - 100) * 2) * 1000)
        return stamp / 1000, 1_000_000 + number, number

    def complete_prices(self):
        self.cache.merge([(self.start + (number - 100) * 2, 1., number)
                          for number in range(100, 121)], self.now, replace=True)

    def test_issuance_backfills_complete_price_history_by_midpoints(self):
        issuance = self.enable_issuance()
        self.complete_prices()
        prices = self.cache.rows
        with patch.object(gap_repair, 'read_sample') as price_read, \
                patch.object(gap_repair, 'read_issuance', side_effect=self.issuance_sample) as ti_read:
            self.assertEqual(self.worker.poll(self.db), 8)
            self.assertEqual([call.args[1] for call in ti_read.call_args_list],
                             [112, 119, 105, 116, 108, 101, 117, 114])
            ti_read.reset_mock()
            self.assertEqual(self.worker.poll(self.db), 8)
            self.assertEqual([call.args[1] for call in ti_read.call_args_list],
                             [110, 107, 103, 100, 120, 118, 115, 113])
            self.assertEqual(self.worker.poll(self.db), 5)
            self.assertEqual(self.worker.poll(self.db), 0)
            price_read.assert_not_called()
        self.assertEqual(self.cache.rows, prices)
        self.assertEqual([row[2] for row in issuance.snapshot()['rows']], list(range(100, 121)))
        restarted = IssuanceCache(self.worker.path)
        restarted.restore()
        self.assertEqual(restarted.snapshot()['rows'], issuance.snapshot()['rows'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM issuance').fetchone()[0], 21)
        self.assertEqual(self.db.execute('SELECT count(*) FROM repairs').fetchone()[0], 0)

    def test_coarse_grid_precedes_midpoints_and_survives_restart_and_tip_growth(self):
        issuance = self.enable_issuance()
        upper = 18005
        self.chain.finalized.return_value = upper + 1
        def sample(rpc, number, **kwargs):
            return self.now - (upper - number) * 2 - 200, 1_000_000 + number, number
        self.cache.merge([(sample(None, n)[0], 1., n) for n in range(100, upper + 1)],
                         self.now, replace=True)
        issuance.merge([sample(None, 14400)])
        with patch.object(gap_repair, 'read_issuance', side_effect=sample) as read:
            self.assertEqual(self.worker.poll(self.db), 8)
            self.assertEqual([call.args[1] for call in read.call_args_list],
                             [18000, 16200, 12600, 10800, 9000, 7200, 5400, 3600])
        self.worker.issuance = IssuanceCache(self.worker.path)
        self.worker.issuance.restore()
        self.cache.merge([(sample(None, upper + 1)[0], 1., upper + 1)], self.now)
        with patch.object(gap_repair, 'read_issuance', side_effect=sample) as read:
            self.assertEqual(self.worker.poll(self.db), 8)
            self.assertEqual([call.args[1] for call in read.call_args_list],
                             [1800, 17100, 15300, 13500, 11700, 9900, 8100, 6300])

    def test_bisection_levels_cover_odd_intervals_and_partial_boundaries(self):
        numbers = list(dict.fromkeys(gap_repair._issuance_numbers(0, 1800)))
        self.assertEqual(numbers[:9], [1800, 0, 900, 1350, 450, 1575, 1125, 675, 225])
        self.assertEqual(numbers[9:17], [1687, 1462, 1237, 1012, 787, 562, 337, 112])
        self.assertEqual(sorted(numbers), list(range(1801)))
        partial = list(dict.fromkeys(gap_repair._issuance_numbers(101, 1799)))
        self.assertEqual(partial, [n for n in numbers if 101 <= n <= 1799])

    def test_shared_batch_reserves_price_work_and_reuses_pinned_blocks(self):
        issuance = self.enable_issuance()
        with patch.object(gap_repair, 'read_sample', side_effect=self.sample) as price_read, \
                patch.object(gap_repair, 'read_issuance', side_effect=self.issuance_sample) as ti_read:
            self.assertEqual(self.worker.poll(self.db), 8)
        self.assertEqual([call.args[1] for call in price_read.call_args_list], [101, 102, 103, 104])
        self.assertEqual([call.args[1] for call in ti_read.call_args_list],
                         [101, 102, 103, 104, 112, 119, 105, 116])
        for request in ti_read.call_args_list[:4]:
            number = request.args[1]
            self.assertEqual(request.kwargs, {'at': f'h{number}', 'timestamp': self.sample(None, number)[2]})
        self.assertEqual(len(issuance.snapshot()['rows']), 8)
        self.assertEqual([row[2] for row in self.cache.rows], [100, 101, 102, 103, 104, 120])
        self.assertEqual(self.cache.fetched_at, self.now - 10)

    def test_old_price_repairs_get_issuance_using_saved_hash_and_chain_time(self):
        issuance = self.enable_issuance()
        self.complete_prices()
        issuance.merge([self.issuance_sample(None, number) for number in range(100, 121) if number != 110])
        number, at, timestamp, price, reserves = self.sample(None, 110)
        with self.db:
            self.db.execute('INSERT INTO repairs VALUES(?,?,?,?,?,?)',
                            (number, at, timestamp, self.start + 20.5, price, reserves))
        with patch.object(gap_repair, 'read_sample') as price_read, \
                patch.object(gap_repair, 'read_issuance', side_effect=self.issuance_sample) as ti_read:
            self.assertEqual(self.worker.poll(self.db), 1)
            ti_read.assert_called_once_with(self.chain.rpc, 110, at=at, timestamp=timestamp)
            price_read.assert_not_called()
        self.assertEqual([row for row in issuance.snapshot()['rows'] if row[2] == 110],
                         [(timestamp / 1000, 1_000_110, 110)])

    def test_short_price_boundaries_preserve_all_blocks_and_issuance(self):
        issuance = self.enable_issuance()
        self.cache.merge([(self.start, 1., 100), (self.start + 10, 1., 120)], self.now, replace=True)
        before = self.cache.rows
        with patch.object(gap_repair, 'read_sample', side_effect=self.sample), \
                patch.object(gap_repair, 'read_issuance', side_effect=self.issuance_sample):
            self.assertEqual(self.worker.poll(self.db), 8)
        self.assertEqual((self.cache.rows[0], self.cache.rows[-1]), before)
        self.assertEqual([row[0] for row in self.cache.rows[1:-1]],
                         [self.start + n / 2 for n in range(1, 5)])
        self.assertEqual(self.db.execute('SELECT count(*) FROM repairs').fetchone()[0], 4)
        self.assertEqual(len(issuance.snapshot()['rows']), 8)

    def test_missing_pool_price_does_not_discard_issuance(self):
        issuance = self.enable_issuance()
        missing_price = list(self.sample(None, 101))
        missing_price[3] = None
        with patch.object(gap_repair, 'read_sample', return_value=tuple(missing_price)), \
                patch.object(gap_repair, 'read_issuance', side_effect=self.issuance_sample):
            with self.assertRaisesRegex(ValueError, 'No pool price'):
                self.worker.poll(self.db)
        self.assertEqual(len(self.cache.rows), 2)
        self.assertIn(101, [row[2] for row in issuance.snapshot()['rows']])

    def test_one_failed_issuance_read_preserves_other_points_and_prices(self):
        issuance = self.enable_issuance()
        def read(rpc, number, **kwargs):
            if number == 112:
                raise OSError('TI state unavailable')
            return self.issuance_sample(rpc, number, **kwargs)
        with patch.object(gap_repair, 'read_sample', side_effect=self.sample), \
                patch.object(gap_repair, 'read_issuance', side_effect=read):
            with self.assertRaisesRegex(OSError, 'TI state unavailable'):
                self.worker.poll(self.db)
        self.assertEqual([row[2] for row in issuance.snapshot()['rows']], [101, 102, 103, 104, 105, 116, 119])
        self.assertEqual([row[2] for row in self.cache.rows], [100, 101, 102, 103, 104, 120])
        self.assertEqual(self.db.execute('SELECT count(*) FROM repairs').fetchone()[0], 4)
        restarted = IssuanceCache(self.worker.path)
        restarted.restore()
        self.assertEqual(restarted.snapshot()['rows'], issuance.snapshot()['rows'])

    def test_unfinalized_issuance_boundary_never_fetches_or_inserts(self):
        issuance = self.enable_issuance()
        self.complete_prices()
        self.chain.finalized.return_value = 118
        with patch.object(gap_repair, 'read_issuance') as ti_read:
            with self.assertRaisesRegex(ValueError, 'finalized'):
                self.worker.poll(self.db)
            ti_read.assert_not_called()
        self.assertEqual(issuance.snapshot()['rows'], ())

    def test_restore_runs_before_price_history_readiness(self):
        issuance = self.enable_issuance()
        saved = self.issuance_sample(None, 120)
        issuance.merge([saved], live=True)
        self.worker.issuance = IssuanceCache(self.worker.path)
        self.cache.history_ready = False
        self.worker.database = Mock(return_value=self.db)
        def wait(seconds):
            self.assertEqual(seconds, 5)
            self.worker.stop.set()
        with patch.object(self.worker.stop, 'wait', side_effect=wait):
            self.worker.run()
        self.assertEqual(self.worker.issuance.snapshot()['rows'], (saved,))
        self.chain.finalized.assert_not_called()

    def test_price_decoder_failure_still_reads_issuance_independently(self):
        issuance = self.enable_issuance()
        with patch.object(gap_repair, 'read_sample', side_effect=ValueError('Unsupported pool layout')), \
                patch.object(gap_repair, 'read_issuance', side_effect=self.issuance_sample) as ti_read:
            with self.assertRaisesRegex(ValueError, 'Unsupported pool layout'):
                self.worker.poll(self.db)
        self.assertEqual(ti_read.call_args_list[0].args, (self.chain.rpc, 101))
        self.assertEqual(ti_read.call_args_list[0].kwargs, {'at': None, 'timestamp': None})
        self.assertIn(101, [row[2] for row in issuance.snapshot()['rows']])
        self.assertEqual(self.db.execute('SELECT count(*) FROM repairs').fetchone()[0], 0)

    def test_live_issuance_commits_while_price_restore_cursor_is_open(self):
        issuance = self.enable_issuance()
        issuance.restore()
        with self.db:
            for number in range(101, 120):
                _, at, timestamp, price, reserves = self.sample(None, number)
                self.db.execute('INSERT INTO repairs VALUES(?,?,?,?,?,?)',
                                (number, at, timestamp, timestamp / 1000, price, reserves))
        cursor = self.db.execute('SELECT * FROM repairs ORDER BY number')
        self.addCleanup(cursor.close)
        self.assertEqual(cursor.fetchone()[0], 101)
        saved = self.issuance_sample(None, 120)
        issuance.merge([saved], live=True)
        self.assertEqual(issuance.snapshot()['rows'], (saved,))
        self.assertEqual(cursor.fetchone()[0], 102)

    def history_sample(self, rpc, number, issuance=False, treasury=False, **kwargs):
        row = self.issuance_sample(rpc, number, **kwargs)
        return {name: row for name, requested in (('issuance', issuance), ('treasury', treasury))
                if requested}

    def test_treasury_fills_grid_even_when_issuance_is_complete(self):
        issuance = self.enable_issuance()
        self.complete_prices()
        issuance.merge([self.issuance_sample(None, n) for n in range(100, 121)])
        self.worker.treasury = TreasuryHistory(self.worker.path)
        with patch.object(gap_repair, 'read_history', side_effect=self.history_sample) as read, \
                patch.object(gap_repair, 'read_issuance') as ti_read:
            self.assertEqual(self.worker.poll(self.db), 8)
            self.assertEqual([call.args[1] for call in read.call_args_list],
                             [112, 119, 105, 116, 108, 101, 117, 114])
            self.worker.treasury = TreasuryHistory(self.worker.path)
            self.worker.treasury.restore()
            self.assertEqual(self.worker.poll(self.db), 8)
            self.assertEqual(self.worker.poll(self.db), 5)
            self.assertEqual(self.worker.poll(self.db), 0)
            ti_read.assert_not_called()
            self.assertEqual(sorted(call.args[1] for call in read.call_args_list),
                             list(range(100, 121)))
            self.assertTrue(all(not call.kwargs['issuance'] and call.kwargs['treasury']
                                for call in read.call_args_list))
        self.assertEqual(self.db.execute('SELECT count(*) FROM treasury').fetchone()[0], 21)

    def test_failed_treasury_decode_preserves_issuance_and_retries_only_missing(self):
        issuance = self.enable_issuance()
        self.complete_prices()
        self.worker.treasury = TreasuryHistory(self.worker.path)
        def treasury_read(rpc, number, **kwargs):
            result = self.history_sample(rpc, number, **kwargs)
            if number == 112:
                result['treasury'] = OSError('Treasury state unavailable')
            return result
        with patch.object(gap_repair, 'read_history', side_effect=treasury_read):
            with self.assertRaisesRegex(OSError, 'Treasury state unavailable'):
                self.worker.poll(self.db)
        self.assertEqual(len(issuance.snapshot()['rows']), 8)
        self.assertEqual([r[2] for r in self.worker.treasury.snapshot()['rows']],
                         [101, 105, 108, 114, 116, 117, 119])
        with patch.object(gap_repair, 'read_history', side_effect=self.history_sample) as read:
            self.worker.poll(self.db)
        self.assertEqual(read.call_args_list[0].args[1], 112)
        self.assertFalse(read.call_args_list[0].kwargs['issuance'])
        self.assertTrue(read.call_args_list[0].kwargs['treasury'])

