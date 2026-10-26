"""Finalized notifications drive durable, gap-free price ingestion."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
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
        self.cache = server.HistoryCache(history_path=self.path)
        self.worker = server.HistoryWorker(self.cache, chain=self.chain)

    def sample(self, rpc, number):
        return number, f'h{number}', (self.now - 30 - (100 - number) * 2) * 1000, 1 + number / 10000, '[[1,2],[3,4]]'

    def saved(self):
        with closing(gap_repair.price_database(self.path)) as db:
            return db.execute('SELECT * FROM repairs ORDER BY number').fetchall()

    def cursor(self):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT number FROM price_progress WHERE id=1').fetchone()[0]

    def catch_up(self, head, worker=None):
        worker = worker or self.worker
        for _ in range(100):
            if worker.ingest(head):
                return
        self.fail('Ingestion failed to reach the notified target')

    def test_cold_seed_is_tip_first_bounded_and_persisted_before_publication(self):
        def publish(*args, **kwargs):
            self.assertEqual([row[0] for row in self.saved()], list(range(86, 101)))
            return merge_original(*args, **kwargs)
        merge_original = self.cache.merge
        with patch.object(server, 'read_sample', side_effect=self.sample) as read, \
                patch.object(self.cache, 'merge', side_effect=publish) as merge:
            self.assertTrue(self.worker.ingest(100))
        self.assertEqual([item.args[1] for item in read.call_args_list], list(range(100, 85, -1)))
        self.assertEqual(merge.call_count, 1)
        self.assertEqual([row[2] for row in self.cache.rows], list(range(86, 101)))
        self.assertEqual(self.cache.fetched_at, self.now)
        self.assertEqual(self.cache.finalized, (100, self.now - 30))
        self.assertEqual(self.cursor(), 100)
        self.chain.verify.assert_called_once_with()
        self.chain.finalized.assert_not_called()
        with patch.object(server, 'read_sample', side_effect=self.sample) as read:
            self.assertTrue(self.worker.ingest(102))
            self.assertTrue(self.worker.ingest(102))
        self.assertEqual([item.args[1] for item in read.call_args_list], [102, 101])
        self.assertEqual(self.cache.finalized, (102, self.now - 26))
        self.chain.verify.assert_called_once_with()

    def test_head_jump_larger_than_seed_keeps_every_block_in_bounded_batches(self):
        with patch.object(server, 'read_sample', side_effect=self.sample) as read:
            self.worker.ingest(100)
            batches = []
            with patch.object(self.cache, 'merge', wraps=self.cache.merge) as merge:
                while True:
                    read.reset_mock()
                    done = self.worker.ingest(140)
                    batches.append([item.args[1] for item in read.call_args_list])
                    if done:
                        break
                self.assertEqual(merge.call_count, len(batches))
        self.assertGreater(len(batches), 1)
        self.assertEqual(batches[0][0], 140)
        self.assertTrue(all(len(batch) <= server.LIVE_BLOCKS for batch in batches))
        self.assertEqual(sorted(number for batch in batches for number in batch), list(range(101, 141)))
        self.assertEqual([row[2] for row in self.cache.rows], list(range(86, 141)))
        self.assertEqual([row[0] for row in self.saved()], list(range(86, 141)))
        self.assertEqual(self.cursor(), 140)
        self.chain.finalized.assert_not_called()

    def test_restart_reuses_saved_tip_and_retains_expired_evidence(self):
        with patch.object(server, 'read_sample', side_effect=self.sample):
            self.worker.ingest(100)
        old = (1, 'old', 1000, 1., .99, '[]')
        with closing(gap_repair.price_database(self.path)) as db, db:
            db.execute('INSERT INTO repairs VALUES(?,?,?,?,?,?)', old)
        before = self.saved()
        restarted = server.HistoryWorker(server.HistoryCache(history_path=self.path), chain=self.chain)
        with patch.object(server, 'read_sample') as read:
            self.assertTrue(restarted.ingest(100))
            self.assertTrue(restarted.load_history())
        read.assert_not_called()
        self.assertEqual(restarted.cache.rows, self.cache.rows)
        self.assertEqual(restarted.cache.finalized, (100, self.now - 30))
        self.assertEqual(self.saved(), before)
        self.assertNotIn(1, [row[2] for row in restarted.cache.rows])

    def test_partial_seed_survives_restart_without_losing_missing_tail(self):
        with patch.object(server, 'read_sample', side_effect=[self.sample(None, 100), OSError('offline')]):
            with self.assertRaises(OSError):
                self.worker.ingest(100)
        self.assertEqual([row[2] for row in self.cache.rows], [100])
        self.assertEqual([row[0] for row in self.saved()], [100])
        self.assertEqual(self.cursor(), 85)
        restarted = server.HistoryWorker(server.HistoryCache(history_path=self.path), chain=self.chain)
        with patch.object(server, 'read_sample', side_effect=self.sample) as read:
            self.catch_up(120, restarted)
        self.assertEqual(read.call_args_list[0].args[1], 120)
        self.assertNotIn(100, [item.args[1] for item in read.call_args_list])
        self.assertEqual([row[0] for row in self.saved()], list(range(86, 121)))
        self.assertEqual(self.cursor(), 120)

    def test_interrupted_catch_up_persists_successes_without_advancing_past_failure(self):
        with patch.object(server, 'read_sample', side_effect=self.sample):
            self.worker.ingest(100)
        def interrupted(rpc, number):
            if number == 110:
                raise OSError('offline')
            return self.sample(rpc, number)
        with patch.object(server, 'read_sample', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.worker.ingest(140)
        expected = list(range(86, 101)) + list(range(111, 115)) + [140]
        self.assertEqual([row[0] for row in self.saved()], expected)
        self.assertEqual([row[2] for row in self.cache.rows], expected)
        self.assertEqual(self.cursor(), 100)
        restarted = server.HistoryWorker(server.HistoryCache(history_path=self.path), chain=self.chain)
        with patch.object(server, 'read_sample', side_effect=self.sample) as read:
            self.catch_up(140, restarted)
            restarted.load_history()
        self.assertEqual(read.call_args_list[0].args[1], 110)
        self.assertEqual([row[0] for row in self.saved()], list(range(86, 141)))
        self.assertEqual([row[2] for row in restarted.cache.rows], list(range(86, 141)))
        self.assertEqual(self.cursor(), 140)

    def test_legacy_saved_tip_repairs_missing_retained_tail_before_resuming(self):
        with closing(gap_repair.price_database(self.path)) as db, db:
            for number in range(86, 101):
                if number != 97:
                    n, at, stamp, price, reserves = self.sample(None, number)
                    db.execute('INSERT INTO repairs VALUES(?,?,?,?,?,?)',
                               (n, at, stamp, stamp / 1000, price, reserves))
        with patch.object(server, 'read_sample', side_effect=self.sample) as read:
            self.catch_up(120)
        numbers = [item.args[1] for item in read.call_args_list]
        self.assertEqual(numbers[0], 120)
        self.assertIn(97, numbers)
        self.assertEqual(sorted(numbers), [97] + list(range(101, 121)))
        self.assertEqual([row[0] for row in self.saved()], list(range(86, 121)))

    def test_repeated_chain_times_keep_every_block_and_never_reach_future(self):
        def same_time(rpc, number):
            sample = list(self.sample(rpc, number))
            sample[2] = (self.now - 1) * 1000
            return tuple(sample)
        with patch.object(server, 'read_sample', side_effect=same_time):
            self.worker.ingest(100)
            self.worker.ingest(102)
        self.assertEqual([row[2] for row in self.cache.rows], list(range(86, 103)))
        self.assertTrue(all(a[0] < b[0] for a, b in zip(self.cache.rows, self.cache.rows[1:])))
        self.assertLessEqual(self.cache.rows[-1][0], self.now)
        self.assertEqual({row[2] for row in self.saved()}, {(self.now - 1) * 1000})
        self.assertEqual(self.cache.rows[-2][0], self.now - .5)

    def test_repeated_chain_times_fit_large_catch_up_between_preserved_boundaries(self):
        def same_time(rpc, number):
            sample = list(self.sample(rpc, number))
            sample[2] = (self.now - 1) * 1000
            return tuple(sample)
        with patch.object(server, 'read_sample', side_effect=same_time):
            self.worker.ingest(100)
            original = self.cache.rows
            self.catch_up(140)
        self.assertEqual(self.cache.rows[:len(original)], original)
        self.assertEqual([row[2] for row in self.cache.rows], list(range(86, 141)))
        self.assertTrue(all(a[0] < b[0] for a, b in zip(self.cache.rows, self.cache.rows[1:])))
        self.assertLessEqual(self.cache.rows[-1][0], self.now)
        self.assertEqual({row[2] for row in self.saved()}, {(self.now - 1) * 1000})

    def test_no_price_is_not_fabricated_or_marked_fresh_and_is_not_retried(self):
        with patch.object(server, 'read_sample', side_effect=lambda rpc, number:
                          (number, 'h', self.now * 1000, None, '[]')):
            self.worker.ingest(100)
        self.assertEqual(self.cache.rows, ())
        self.assertIsNone(self.cache.fetched_at)
        self.assertEqual(self.saved(), [])
        self.assertEqual(self.cache.finalized, (100, self.now))
        self.assertEqual(self.cursor(), 100)
        with patch.object(server, 'read_sample') as read:
            self.assertTrue(self.worker.ingest(100))
        read.assert_not_called()

    def test_explicitly_unavailable_block_remains_a_gap_without_stalling_catch_up(self):
        def available(rpc, number):
            if number == 110:
                raise archive_reader.Unavailable('Pruned state')
            return self.sample(rpc, number)
        with patch.object(server, 'read_sample', side_effect=available):
            self.worker.ingest(100)
            self.catch_up(140)
        expected = [number for number in range(86, 141) if number != 110]
        self.assertEqual([row[2] for row in self.cache.rows], expected)
        self.assertEqual([row[0] for row in self.saved()], expected)
        self.assertEqual(self.cursor(), 140)

    def test_wrong_http_chain_or_backwards_finality_cannot_write_prices(self):
        self.chain.verify.side_effect = ValueError('Wrong chain')
        with patch.object(server, 'read_sample') as read, self.assertRaisesRegex(ValueError, 'Wrong chain'):
            self.worker.ingest(100)
        read.assert_not_called()
        self.assertFalse(self.path.exists())
        self.chain.verify.side_effect = None
        self.cache.merge([(self.now - 10, 1., 101)], self.now - 5)
        with patch.object(server, 'read_sample') as read, self.assertRaisesRegex(ValueError, 'backwards'):
            self.worker.ingest(100)
        read.assert_not_called()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.cache.fetched_at, self.now - 5)

    def test_backwards_notification_after_ingestion_does_not_write(self):
        with patch.object(server, 'read_sample', side_effect=self.sample):
            self.worker.ingest(100)
        rows, saved, cursor = self.cache.rows, self.saved(), self.cursor()
        with patch.object(server, 'read_sample') as read, self.assertRaisesRegex(ValueError, 'backwards'):
            self.worker.ingest(99)
        read.assert_not_called()
        self.assertIs(self.cache.rows, rows)
        self.assertEqual(self.saved(), saved)
        self.assertEqual(self.cursor(), cursor)

    def test_disk_failure_does_not_advance_cursor_or_publish_unpersisted_samples(self):
        with patch.object(server, 'read_sample', side_effect=self.sample):
            self.worker.ingest(100)
            with patch.object(server, 'price_database', side_effect=OSError('disk offline')):
                with self.assertRaises(OSError):
                    self.worker.ingest(102)
            self.assertEqual(self.worker.cursor, 100)
            self.assertEqual(self.cache.rows[-1][2], 100)
            self.assertTrue(self.worker.ingest(102))
        self.assertEqual(self.cursor(), 102)

    def test_ingestion_keeps_nearest_block_neighbors_not_latest_timestamp(self):
        self.cache.merge([(self.now - 200, 1., 85), (self.now - 180, 1., 84),
                          (self.now - 100, 1., 87)], self.now)
        self.worker.cursor = 85
        with patch.object(server, 'read_sample', side_effect=self.sample) as read, \
                patch.object(server, 'price_time', wraps=server.price_time) as estimate:
            self.worker.ingest(100)
        self.assertEqual([item.args[1] for item in read.call_args_list], [*range(100, 87, -1), 86])
        self.assertEqual(estimate.call_args_list[-1].args[2:],
                         ((85, self.now - 200), (87, self.now - 100)))

    def test_new_shared_database_needs_no_imported_archive(self):
        cache = server.HistoryCache(archive=self.path, history_path=self.path)
        worker = server.HistoryWorker(cache, chain=self.chain)
        with patch.object(server, 'read_sample', side_effect=self.sample):
            worker.ingest(100)
        self.assertTrue(worker.load_history())
        self.assertTrue(worker.load_archive())
        self.assertEqual([row[2] for row in cache.rows], list(range(86, 101)))
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
            worker.ingest(100)
            worker.load_history()
            self.assertTrue(worker.load_archive())
        read.assert_not_called()
        self.assertEqual([row[2] for row in cache.rows], list(range(1, 101)))
        self.assertEqual(cache.rows[-1], (self.now - 30, .99, 100))
        self.assertEqual(self.saved(), [])
        self.assertEqual(worker.archive_seq, 100)

    def test_blocked_history_loading_does_not_gate_subscription_or_ingestion(self):
        loading, release, notified, sampled = (threading.Event() for _ in range(4))
        def history():
            loading.set()
            release.wait(5)
            return False
        def heads(stop):
            notified.set()
            yield 100
            stop.wait()
        def sample(rpc, number):
            sampled.set()
            return self.sample(rpc, number)
        self.worker.load_history = history
        self.worker.load_archive = Mock(return_value=False)
        self.chain.finalized_heads.side_effect = heads
        with patch.object(server, 'read_sample', side_effect=sample):
            self.worker.thread.start()
            try:
                self.assertTrue(loading.wait(2))
                self.assertTrue(notified.wait(2))
                self.assertTrue(sampled.wait(2))
                self.assertFalse(self.cache.history_ready)
            finally:
                release.set()
                self.worker.shutdown()
        self.assertFalse(self.worker.thread.is_alive())
        self.assertFalse(self.worker.listener.is_alive())
        self.assertFalse(self.worker.loader.is_alive())
        self.chain.finalized.assert_not_called()

    def test_history_loads_even_while_live_rpc_is_blocked(self):
        reading, release, loaded = (threading.Event() for _ in range(3))
        def heads(stop):
            yield 100
            stop.wait()
        def sample(rpc, number):
            reading.set()
            release.wait(5)
            return self.sample(rpc, number)
        def history():
            if reading.wait(2):
                loaded.set()
            return False
        self.worker.load_history = history
        self.worker.load_archive = Mock(return_value=False)
        self.chain.finalized_heads.side_effect = heads
        with patch.object(server, 'read_sample', side_effect=sample):
            self.worker.thread.start()
            try:
                self.assertTrue(reading.wait(2))
                self.assertTrue(loaded.wait(2))
            finally:
                release.set()
                self.worker.shutdown()

    def test_subscription_reconnect_and_rpc_retry_finish_interrupted_jump(self):
        seeded, complete, closed, reconnected = (threading.Event() for _ in range(4))
        subscriptions = []
        failed = False
        def heads(stop):
            subscriptions.append(True)
            try:
                if len(subscriptions) == 1:
                    yield 100
                    if not seeded.wait(3):
                        raise RuntimeError('Seed ingestion did not finish')
                    yield 140
                    raise OSError('WebSocket disconnected')
                reconnected.set()
                yield 140
                stop.wait()
            finally:
                closed.set()
        def sample(rpc, number):
            nonlocal failed
            if number == 110 and not failed:
                failed = True
                raise OSError('Transient HTTP failure')
            return self.sample(rpc, number)
        original = self.cache.merge
        def merge(*args, **kwargs):
            original(*args, **kwargs)
            numbers = [row[2] for row in self.cache.rows]
            if numbers == list(range(86, 101)):
                seeded.set()
            if numbers == list(range(86, 141)):
                complete.set()
        self.chain.finalized_heads.side_effect = heads
        with patch.object(server, 'read_sample', side_effect=sample), \
                patch.object(self.cache, 'merge', side_effect=merge), \
                patch.object(server.logging, 'exception'):
            self.worker.thread.start()
            try:
                self.assertTrue(complete.wait(8))
                self.assertTrue(reconnected.wait(3))
            finally:
                self.worker.shutdown()
        self.assertTrue(failed)
        self.assertGreaterEqual(len(subscriptions), 2)
        self.assertTrue(closed.is_set())
        self.assertFalse(self.worker.listener.is_alive())
        self.assertEqual([row[0] for row in self.saved()], list(range(86, 141)))
        self.assertEqual(self.cursor(), 140)
        self.chain.finalized.assert_not_called()

    def test_listener_rejects_backwards_and_duplicate_targets_without_ingesting(self):
        def heads(stop):
            yield 100
            yield 100
            yield 99
        self.chain.finalized_heads.side_effect = heads
        with patch.object(self.worker.stop, 'wait', side_effect=lambda _: self.worker.stop.set()), \
                patch.object(server.logging, 'exception') as logged, \
                patch.object(server, 'read_sample') as read:
            self.worker.listen()
        self.assertEqual(self.worker.target, 100)
        self.assertEqual(logged.call_count, 1)
        read.assert_not_called()
        self.assertFalse(self.path.exists())

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
        self.assertIsNone(self.cache.fetched_at)

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
        self.worker.ingest(100)
        self.assertEqual(len(self.cache.rows), server.LIVE_BLOCKS)
        self.assertTrue(all(row[1] == 1. for row in self.cache.rows))
        self.assertEqual(rpc.call.call_count, server.LIVE_BLOCKS * 2)
