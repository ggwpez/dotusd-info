"""Finalized RPC ingestion stays fresh while durable history catches up."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import archive_reader
import gap_repair
import server


class HistoryWorkerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'history.sqlite3'
        self.now = 2_000_000_000
        clock = patch.object(server.time, 'time', return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.chain = Mock()
        self.chain.finalized.return_value = 100
        self.cache = server.HistoryCache(history_path=self.path)
        self.worker = server.HistoryWorker(self.cache, chain=self.chain)

    def sample(self, rpc, number):
        return number, f'h{number}', (self.now - 30 - (100 - number) * 2) * 1000, 1 + number / 10000, '[[1,2],[3,4]]'

    def saved(self):
        with closing(gap_repair.price_database(self.path)) as db:
            return db.execute('SELECT * FROM repairs ORDER BY number').fetchall()

    def test_finalized_tip_first_bounded_and_persisted_before_single_publication(self):
        with patch.object(server, 'read_sample', side_effect=self.sample) as read, \
                patch.object(self.cache, 'merge', wraps=self.cache.merge) as merge:
            self.worker.poll()
        self.assertEqual([call.args[1] for call in read.call_args_list], list(range(100, 92, -1)))
        self.assertEqual(merge.call_count, 1)
        self.assertEqual([row[2] for row in self.cache.rows], list(range(93, 101)))
        self.assertEqual([row[0] for row in self.saved()], list(range(93, 101)))
        self.assertEqual(self.cache.fetched_at, self.now)
        self.chain.finalized.assert_called_once_with()
        self.chain.finalized.return_value = 102
        with patch.object(server, 'read_sample', side_effect=self.sample) as read:
            self.worker.poll()
        self.assertEqual([call.args[1] for call in read.call_args_list], [102, 101])
        self.assertEqual(self.cache.rows[-1][2], 102)

    def test_restart_reuses_saved_tip_and_retains_expired_evidence(self):
        with patch.object(server, 'read_sample', side_effect=self.sample):
            self.worker.poll()
        old = (1, 'old', 1000, 1., .99, '[]')
        with closing(gap_repair.price_database(self.path)) as db, db:
            db.execute('INSERT INTO repairs VALUES(?,?,?,?,?,?)', old)
        before = self.saved()
        restarted = server.HistoryWorker(server.HistoryCache(history_path=self.path), chain=self.chain)
        with patch.object(server, 'read_sample') as read:
            restarted.poll()
            self.assertTrue(restarted.load_history())
        read.assert_not_called()
        self.assertEqual(restarted.cache.rows, self.cache.rows)
        self.assertEqual(self.saved(), before)
        self.assertNotIn(1, [row[2] for row in restarted.cache.rows])

    def test_recent_success_survives_older_failure_and_retries_missing_blocks(self):
        with patch.object(server, 'read_sample', side_effect=[self.sample(None, 100), OSError('offline')]):
            with self.assertRaises(OSError):
                self.worker.poll()
        self.assertEqual([row[2] for row in self.cache.rows], [100])
        self.assertEqual([row[0] for row in self.saved()], [100])
        with patch.object(server, 'read_sample', side_effect=self.sample) as read:
            self.worker.poll()
        self.assertEqual(read.call_args_list[0].args[1], 99)
        self.assertEqual(len(self.saved()), server.LIVE_BLOCKS)

    def test_repeated_chain_times_keep_every_block_and_never_reach_future(self):
        def same_time(rpc, number):
            sample = list(self.sample(rpc, number))
            sample[2] = (self.now - 1) * 1000
            return tuple(sample)
        with patch.object(server, 'read_sample', side_effect=same_time):
            self.worker.poll()
            self.chain.finalized.return_value = 102
            self.worker.poll()
        self.assertEqual([row[2] for row in self.cache.rows], list(range(93, 103)))
        self.assertTrue(all(a[0] < b[0] for a, b in zip(self.cache.rows, self.cache.rows[1:])))
        self.assertLessEqual(self.cache.rows[-1][0], self.now)
        self.assertEqual({row[2] for row in self.saved()}, {(self.now - 1) * 1000})
        # The 100..102 interval has only one second left before wall time.
        self.assertEqual(self.cache.rows[-2][0], self.now - .5)

    def test_no_price_is_not_fabricated_or_marked_fresh(self):
        with patch.object(server, 'read_sample', return_value=(100, 'h', self.now * 1000, None, '[]')):
            self.worker.poll()
        self.assertEqual(self.cache.rows, ())
        self.assertIsNone(self.cache.fetched_at)
        self.assertEqual(self.saved(), [])

    def test_wrong_chain_or_backwards_finality_cannot_write_prices(self):
        self.chain.finalized.side_effect = ValueError('Wrong chain')
        with patch.object(server, 'read_sample') as read, self.assertRaisesRegex(ValueError, 'Wrong chain'):
            self.worker.poll()
        read.assert_not_called()
        self.chain.finalized.side_effect = None
        self.cache.merge([(self.now - 10, 1., 101)], self.now - 5)
        with patch.object(server, 'read_sample') as read, self.assertRaisesRegex(ValueError, 'backwards'):
            self.worker.poll()
        read.assert_not_called()
        self.assertEqual(self.cache.fetched_at, self.now - 5)

    def test_live_poll_keeps_nearest_block_neighbors_not_latest_timestamp(self):
        self.cache.merge([(self.now - 200, 1., 92), (self.now - 180, 1., 91),
                          (self.now - 100, 1., 94)], self.now)
        with patch.object(server, 'read_sample', side_effect=self.sample) as read, \
                patch.object(server, 'price_time', wraps=server.price_time) as estimate:
            self.worker.poll()
        self.assertEqual([call.args[1] for call in read.call_args_list], [100, 99, 98, 97, 96, 95, 93])
        self.assertEqual(estimate.call_args_list[-1].args[2:],
                         ((92, self.now - 200), (94, self.now - 100)))

    def test_large_archive_outage_never_changes_tip_priority_and_gaps_recover(self):
        self.cache.merge([(self.now - 1000, 1., 1)], self.now - 900)
        with patch.object(server, 'read_sample', side_effect=self.sample) as read:
            self.worker.poll()
        self.assertEqual(read.call_args_list[0].args[1], 100)
        self.assertEqual(read.call_count, server.LIVE_BLOCKS)
        self.cache.history_ready = True
        repair = gap_repair.GapRepair(self.cache, self.path, chain=self.chain)
        with closing(repair.database()) as db, \
                patch.object(server.time, 'time', return_value=self.now + 180), \
                patch.object(gap_repair, 'read_sample', side_effect=self.sample) as read:
            self.assertEqual(repair.poll(db), gap_repair.BATCH)
        self.assertEqual([call.args[1] for call in read.call_args_list], list(range(2, 10)))
        self.assertEqual(self.cache.rows[-1][2], 100)
        self.assertEqual(self.cache.fetched_at, self.now)

    def test_new_shared_database_needs_no_imported_archive(self):
        cache = server.HistoryCache(archive=self.path, history_path=self.path)
        worker = server.HistoryWorker(cache, chain=self.chain)
        with patch.object(server, 'read_sample', side_effect=self.sample):
            worker.poll()
        self.assertTrue(worker.load_history())
        self.assertTrue(worker.load_archive())
        self.assertEqual([row[2] for row in cache.rows], list(range(93, 101)))
        old = (1, 'old', 1000, 1., .99, '[]')
        with closing(gap_repair.price_database(self.path)) as db, db:
            db.execute('INSERT INTO repairs VALUES(?,?,?,?,?,?)', old)
        self.assertEqual(cache.read(0, 2)[0], [(1., .99, 1)])

    def test_same_database_archive_and_repairs_preserve_saved_rows(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE samples (seq INTEGER PRIMARY KEY, number INTEGER UNIQUE, '
                       'sample_time INTEGER, price REAL)')
            db.executemany('INSERT INTO samples VALUES(?,?,?,?)',
                           [(i, i, (self.now - 230 + i * 2) * 1000, .99) for i in range(1, 101)])
        cache = server.HistoryCache(archive=self.path, history_path=self.path)
        worker = server.HistoryWorker(cache, chain=self.chain)
        with patch.object(server, 'read_sample') as read:
            worker.poll()
            worker.load_history()
            self.assertTrue(worker.load_archive())
        read.assert_not_called()
        self.assertEqual([row[2] for row in cache.rows], list(range(1, 101)))
        self.assertEqual(cache.rows[-1], (self.now - 30, .99, 100))
        self.assertEqual(self.saved(), [])
        self.assertEqual(worker.archive_seq, 100)

    def test_restore_pages_do_not_gate_live_polling_or_erase_stored_history(self):
        self.worker.load_history = Mock(return_value=False)
        self.worker.load_archive = Mock(return_value=False)
        self.worker.poll = Mock()
        waits = []
        def wait(delay):
            waits.append(delay)
            if len(waits) == 3:
                self.worker.stop.set()
        with patch.object(self.worker.stop, 'wait', side_effect=wait), \
                patch.object(server.time, 'monotonic', side_effect=[0, 0, 7, 7, 14, 14]):
            self.worker.run()
        self.assertEqual(self.worker.poll.call_count, 3)
        self.assertEqual(waits, [.05, .05, .05])
        self.assertFalse(self.cache.history_ready)

    def test_archive_skips_expired_pages_and_accepts_later_upload(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE samples (seq INTEGER PRIMARY KEY, number INTEGER, '
                       'sample_time INTEGER, price REAL)')
            db.executemany('INSERT INTO samples VALUES(?,?,?,?)',
                           [(i, i, 1000, 1.) for i in range(1, 5001)])
        worker = server.HistoryWorker(server.HistoryCache(), chain=self.chain, archive=self.path)
        self.assertTrue(worker.load_archive())
        self.assertEqual(worker.archive_seq, 5000)
        self.assertEqual(worker.cache.rows, ())
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO samples VALUES(?,?,?,?)', (5001, 5001, (self.now - 1) * 1000, 1.01))
        self.assertTrue(worker.load_archive())
        self.assertEqual(worker.cache.rows, ((self.now - 1, 1.01, 5001),))
        self.assertIsNone(worker.cache.fetched_at)

    def test_live_ingestion_uses_canonical_reserve_and_decimal_decoding(self):
        rpc = self.chain.rpc
        rpc.block_hash.side_effect = lambda number: f'h{number}'
        metadata = '0x' + (bytes(16) + b'\x00\x00\x06\x00').hex()
        keys = [archive_reader.TIMESTAMP, archive_reader.metadata_key(7873), archive_reader.metadata_key(1984)]
        rpc.storage.return_value = dict(zip(keys, [
            '0x' + int((self.now - 30) * 1000).to_bytes(8, 'little').hex(), metadata, metadata]))
        def reserves(method, params):
            self.assertEqual(method, 'state_call')
            self.assertEqual(params[0], 'AssetConversionApi_get_reserves')
            self.assertTrue(params[2].startswith('h'))
            return '0x' + (b'\x01' + (10).to_bytes(16, 'little') + (20).to_bytes(16, 'little')).hex()
        rpc.call.side_effect = reserves
        self.worker.poll()
        self.assertEqual(len(self.cache.rows), server.LIVE_BLOCKS)
        self.assertTrue(all(row[1] == 1. for row in self.cache.rows))
        self.assertEqual(rpc.call.call_count, server.LIVE_BLOCKS * 2)
