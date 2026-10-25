from contextlib import closing
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, call, patch

from archive_reader import TIMESTAMP, metadata_key
from issuance import ASSET, RETENTION, IssuanceCache, IssuanceWorker, asset_key, total_supply


def details(supply):
    # Distinct administrative accounts ensure supply cannot be read at offset zero.
    data = (bytes(range(128)) + supply.to_bytes(16, 'little') +
            (987).to_bytes(16, 'little') + (1).to_bytes(16, 'little') +
            b'\x01' + (7).to_bytes(4, 'little') * 3 + b'\x00')
    return '0x' + data.hex()


def metadata(precision):
    return '0x' + (bytes(16) + b'\x18dotUSD\x18dotUSD' + bytes([precision, 0])).hex()


class IssuanceTests(unittest.TestCase):
    def worker(self, head=100):
        chain = Mock()
        chain.finalized.return_value = head
        chain.rpc.block_hash.side_effect = lambda number: f'block-{number}'
        chain.rpc.storage.side_effect = self.storage
        return IssuanceWorker(IssuanceCache(), chain=chain)

    def storage(self, at, keys):
        number = int(at.split('-')[1])
        self.assertEqual(keys, [TIMESTAMP, asset_key(ASSET), metadata_key(ASSET)])
        return dict(zip(keys, [
            '0x' + (1_700_000_000_001 + number * 6000).to_bytes(8, 'little').hex(),
            details(number * 10 ** 6 + 123456), metadata(6)]))

    def test_supply_uses_four_accounts_then_full_little_endian_u128(self):
        for amount in (0, 123456789, 2 ** 127 + 123):
            self.assertEqual(total_supply(details(amount)), amount)
        valid = details(123)
        invalid_bool = bytearray.fromhex(valid[2:])
        invalid_bool[176] = 2
        invalid_status = bytearray.fromhex(valid[2:])
        invalid_status[189] = 3
        for encoded in (None, '0x', valid[:-2], valid + '00',
                        '0x' + invalid_bool.hex(), '0x' + invalid_status.hex()):
            with self.subTest(encoded=encoded), self.assertRaises(ValueError):
                total_supply(encoded)

    def test_asset_key_is_u32_blake_concat_not_compact_parameter(self):
        key = bytes.fromhex(asset_key(ASSET)[2:])
        meta = bytes.fromhex(metadata_key(ASSET)[2:])
        self.assertEqual(len(key), 52)
        self.assertEqual(key[:16], meta[:16])
        self.assertNotEqual(key[16:32], meta[16:32])
        self.assertEqual(key[32:], meta[32:])
        self.assertEqual(key[-4:], ASSET.to_bytes(4, 'little'))

    def test_latest_fifteen_finalized_blocks_use_metadata_and_chain_time(self):
        worker = self.worker()
        worker.poll()
        snapshot = worker.cache.snapshot()
        self.assertIsNone(snapshot['error'])
        self.assertEqual([row[2] for row in snapshot['rows']], list(range(86, 101)))
        self.assertEqual(snapshot['rows'][0], ((1_700_000_000_001 + 86 * 6000) / 1000,
                                             86.123456, 86))
        worker.chain.rpc.block_hash.assert_has_calls([call(n) for n in range(86, 101)])
        self.assertEqual(worker.chain.rpc.storage.call_count, 15)
        worker.chain.rpc.storage.reset_mock()
        worker.chain.finalized.return_value = 102
        worker.poll()
        self.assertEqual(worker.chain.rpc.storage.call_count, 2)
        self.assertEqual([row[2] for row in worker.cache.snapshot()['rows']], list(range(88, 103)))
        worker.chain.rpc.storage.reset_mock()
        worker.poll()
        worker.chain.rpc.storage.assert_not_called()

    def test_equal_chain_timestamps_preserve_every_finalized_block(self):
        worker = self.worker(head=22274427)
        def storage(at, keys):
            values = self.storage(at, keys)
            number = int(at.split('-')[1])
            # Observed pinned state: these consecutive finalized blocks share time.
            values[TIMESTAMP] = ('0x0029c172a1010000' if number <= 22274418
                                 else '0xc086c172a1010000')
            return values
        worker.chain.rpc.storage.side_effect = storage
        worker.poll()
        snapshot = worker.cache.snapshot()
        self.assertIsNone(snapshot['error'])
        self.assertEqual([row[2] for row in snapshot['rows']], list(range(22274413, 22274428)))
        self.assertEqual([row[0] for row in snapshot['rows']],
                         [1792926624.0] * 6 + [1792926648.0] * 9)
        previous = snapshot['rows']
        worker.chain.finalized.return_value = 22274428
        def backwards(at, keys):
            values = self.storage(at, keys)
            values[TIMESTAMP] = '0x0029c172a1010000'
            return values
        worker.chain.rpc.storage.side_effect = backwards
        with patch('issuance.logging.exception'):
            worker.poll()
        self.assertEqual(worker.cache.snapshot()['rows'], previous)
        self.assertIn('moved backwards', worker.cache.snapshot()['error'])

    def test_reconnect_skips_backfill_and_retains_good_window_on_failure(self):
        worker = self.worker()
        worker.poll()
        previous = worker.cache.snapshot()['rows']
        worker.chain.finalized.return_value = 200
        worker.chain.rpc.storage.reset_mock()
        worker.chain.rpc.storage.side_effect = OSError('RPC unavailable')
        with patch('issuance.logging.exception'):
            worker.poll()
        self.assertEqual(worker.cache.snapshot()['rows'], previous)
        self.assertIn('RPC unavailable', worker.cache.snapshot()['error'])
        worker.chain.rpc.storage.side_effect = self.storage
        worker.chain.rpc.block_hash.reset_mock()
        worker.poll()
        self.assertIsNone(worker.cache.snapshot()['error'])
        self.assertEqual([row[2] for row in worker.cache.snapshot()['rows']], list(range(186, 201)))
        self.assertEqual(worker.chain.rpc.block_hash.call_args_list, [call(n) for n in range(186, 201)])

    def test_missing_metadata_or_timestamp_never_fabricates_rows(self):
        for missing in (TIMESTAMP, metadata_key(ASSET), asset_key(ASSET)):
            worker = self.worker()
            def storage(at, keys):
                values = self.storage(at, keys)
                values[missing] = None
                return values
            worker.chain.rpc.storage.side_effect = storage
            with patch('issuance.logging.exception'):
                worker.poll()
            self.assertEqual(worker.cache.snapshot()['rows'], ())
            self.assertIsNotNone(worker.cache.snapshot()['error'])

    def test_stopping_worker_does_not_fetch_or_publish_incomplete_window(self):
        worker = self.worker()
        worker.stop.set()
        worker.poll()
        worker.chain.rpc.storage.assert_not_called()
        self.assertEqual(worker.cache.snapshot()['rows'], ())

    def persistent_cache(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return IssuanceCache(Path(directory.name) / 'repairs.db')

    def test_live_windows_accumulate_and_restore_with_persistence(self):
        now = 1_700_001_000
        worker = self.worker()
        worker.cache = self.persistent_cache()
        historical = (now - 1000, 80., 50)
        with patch('issuance.time.time', return_value=now):
            worker.cache.merge([historical])
            worker.poll()
            worker.chain.finalized.return_value = 102
            worker.poll()
            rows = worker.cache.snapshot()['rows']
            self.assertEqual([row[2] for row in rows], [50] + list(range(86, 103)))
            restarted = IssuanceCache(worker.cache.history_path)
            restarted.restore()
            self.assertEqual(restarted.snapshot(), {'rows': rows, 'error': None})
            self.assertEqual(rows[0], historical)

    def test_concurrent_backfill_survives_live_snapshot_and_retention(self):
        now = 1_700_001_000
        worker = self.worker()
        worker.cache = self.persistent_cache()
        expired = (now - RETENTION, 1., 1)
        historical = (now - 1000, 50., 50)
        arriving = (now - 900, 51., 51)
        started, release = threading.Event(), threading.Event()
        def storage(at, keys):
            if at == 'block-86':
                started.set()
                if not release.wait(5):
                    raise TimeoutError('Test did not release live poll')
            return self.storage(at, keys)
        worker.chain.rpc.storage.side_effect = storage
        with patch('issuance.time.time', return_value=now):
            worker.cache.merge([expired, historical], now=now - 1)
            thread = threading.Thread(target=worker.poll)
            thread.start()
            try:
                self.assertTrue(started.wait(5))
                worker.cache.merge([arriving])
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertIsNone(worker.cache.snapshot()['error'])
            self.assertEqual([row[2] for row in worker.cache.snapshot()['rows']],
                             [50, 51] + list(range(86, 101)))
            restarted = IssuanceCache(worker.cache.history_path)
            restarted.restore()
            self.assertEqual(restarted.snapshot()['rows'], worker.cache.snapshot()['rows'])
            with closing(restarted.database()) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM issuance WHERE number=1').fetchone()[0], 1)

    def test_persistence_failure_preserves_good_history_and_retries(self):
        now = 1_700_001_000
        worker = self.worker()
        worker.cache = self.persistent_cache()
        with patch('issuance.time.time', return_value=now):
            worker.poll()
            previous = worker.cache.snapshot()['rows']
            worker.chain.finalized.return_value = 101
            with patch.object(worker.cache, 'database', side_effect=OSError('Disk unavailable')), \
                    patch('issuance.logging.exception'):
                worker.poll()
            self.assertEqual(worker.cache.snapshot()['rows'], previous)
            self.assertIn('Disk unavailable', worker.cache.snapshot()['error'])
            worker.poll()
            self.assertIsNone(worker.cache.snapshot()['error'])
            self.assertEqual([row[2] for row in worker.cache.snapshot()['rows']], list(range(86, 102)))

    def test_restart_preserves_expired_disk_rows_without_price_history(self):
        now = 1_700_001_000
        cache = self.persistent_cache()
        old = (now - RETENTION, 1., 1)
        kept = (now - 10, 2., 2)
        cache.merge([old, kept], now=now - 1)
        with patch('issuance.time.time', return_value=now):
            restarted = IssuanceCache(cache.history_path)
            restarted.restore()
            self.assertEqual(restarted.snapshot()['rows'], (kept,))
            with closing(restarted.database()) as db:
                self.assertEqual(db.execute('SELECT number FROM issuance ORDER BY number').fetchall(), [(1,), (2,)])

    def test_old_incoming_issuance_is_saved_without_expanding_cache(self):
        now = 1_700_001_000
        cache = self.persistent_cache()
        old = (now - RETENTION - 1, 1., 1)
        cache.merge([old], now=now)
        self.assertEqual(cache.rows, ())
        with closing(cache.database()) as db:
            self.assertEqual(db.execute('SELECT timestamp,supply,number FROM issuance').fetchall(), [old])

    def test_historical_selection_reads_saved_rows_without_expanding_live_cache(self):
        now = 1_700_001_000
        cache = self.persistent_cache()
        old = (now - 20 * RETENTION, 1., 1)
        recent = (now - 10, 2., 2)
        with patch('issuance.time.time', return_value=now):
            cache.merge([old, recent], live=True)
            live_rows, revision, changes = cache.rows, cache.revision, tuple(cache.changes)
            selected = cache.selection(old[0] - 1, old[0])
            self.assertEqual(selected['rows'], (old,))
            self.assertTrue(selected['reset'])
            self.assertIsNone(selected['error'])
            self.assertEqual(cache.rows, live_rows)
            self.assertEqual(cache.revision, revision)
            self.assertEqual(tuple(cache.changes), changes)
            restarted = IssuanceCache(cache.history_path)
            self.assertEqual(restarted.selection(old[0] - 1, old[0])['rows'], (old,))
            self.assertEqual(restarted.rows, ())

    def test_historical_windows_exclude_start_include_end_and_cross_retention(self):
        now = 1_700_001_000
        cutoff = now - RETENTION
        cache = self.persistent_cache()
        rows = ((cutoff - 20, 1., 1), (cutoff - 10, 2., 2),
                (cutoff - 10, 3., 3), (cutoff, 4., 4), (cutoff + 10, 5., 5))
        with patch('issuance.time.time', return_value=now):
            cache.merge(rows)
            self.assertEqual(cache.selection(cutoff - 20, cutoff - 10)['rows'], rows[1:3])
            self.assertEqual(cache.selection(cutoff - 20, cutoff + 10)['rows'], rows[1:])
            self.assertEqual(cache.selection(cutoff - 10, cutoff - 10)['rows'], ())
            self.assertEqual(cache.selection(cutoff - 9, cutoff - 1)['rows'], ())
            self.assertEqual(cache.selection(cutoff, cutoff + 10)['rows'], rows[4:])
            self.assertEqual(cache.rows, rows[4:])

    def test_historical_cursors_reset_to_saved_corrections_even_without_new_revision(self):
        now = 1_700_001_000
        start = now - 10 * RETENTION
        cache = self.persistent_cache()
        with patch('issuance.time.time', return_value=now):
            cache.merge([(start + 10, 1., 1), (start + 20, 2., 2)], live=True)
            first = cache.selection(start, start + 20)
            unchanged = cache.selection(start, start + 20, first['cursor'])
            self.assertEqual(unchanged['rows'], first['rows'])
            self.assertTrue(unchanged['reset'])
            cache.merge([(start + 10, 11., 1), (start + 30, 22., 2)])
            corrected = cache.selection(start, start + 20, first['cursor'])
            self.assertEqual(corrected['rows'], ((start + 10, 11., 1),))
            self.assertEqual(corrected['cursor'], first['cursor'])
            self.assertTrue(corrected['reset'])
            self.assertEqual(cache.rows, ())
            self.assertEqual(tuple(cache.changes), ())

    def test_historical_selection_rejects_overflow_with_bounded_query_without_live_lock(self):
        now = 1_700_001_000
        start = now - 10 * RETENTION
        cache = self.persistent_cache()
        rows = tuple((start + number, float(number), number) for number in range(1, 9))
        statements = []
        database = cache.database
        def traced_database():
            self.assertFalse(cache.lock.locked())
            db = database()
            db.set_trace_callback(statements.append)
            return db
        with patch('issuance.time.time', return_value=now), patch('issuance.MAX_SAMPLES', 2):
            cache.merge(rows)
            with patch.object(cache, 'database', side_effect=traced_database):
                self.assertEqual(cache.selection(start, start + 2)['rows'], rows[:2])
                with self.assertRaisesRegex(ValueError, 'exceeds 2 samples; request a smaller window'):
                    cache.selection(start, start + 8)
            queries = [statement for statement in statements if statement.startswith('SELECT')]
            self.assertEqual(len(queries), 2)
            self.assertTrue(all(query.endswith('LIMIT 3') for query in queries))
            self.assertEqual(cache.rows, ())

    def test_recent_selection_rejects_oversized_windows_even_with_current_cursor(self):
        now = 1_700_001_000
        cache = self.persistent_cache()
        rows = ((now - 30, 1., 1), (now - 20, 2., 2), (now - 10, 3., 3))
        with patch('issuance.time.time', return_value=now), patch('issuance.MAX_SAMPLES', 2):
            cache.merge(rows[:2], live=True)
            first = cache.selection(now - 40, now)
            self.assertEqual(first['rows'], rows[:2])
            self.assertEqual(cache.selection(now - 40, now, first['cursor'])['rows'], ())
            cache.merge(rows[2:])
            for cursor in (None, first['cursor'], f'{cache.instance}:{cache.revision}'):
                with self.subTest(cursor=cursor), self.assertRaisesRegex(
                        ValueError, 'exceeds 2 samples; request a smaller window'):
                    cache.selection(now - 40, now, cursor)
            self.assertEqual(cache.selection(now - 30, now)['rows'], rows[1:])

    def test_revision_deltas_include_repairs_and_coalesce_corrections(self):
        now = 1_700_001_000
        cache = self.persistent_cache()
        with patch('issuance.time.time', return_value=now):
            cache.merge([(now - 20, 100., 20), (now - 10, 110., 30)], live=True)
            initial = cache.selection(now - 100, now)
            self.assertTrue(initial['reset'])
            self.assertIsNone(initial['error'])
            cursor = initial['cursor']
            cache.merge(cache.rows, live=True)
            unchanged = cache.selection(now - 100, now, cursor)
            self.assertEqual(unchanged['rows'], ())
            self.assertEqual(unchanged['cursor'], cursor)
            self.assertFalse(unchanged['reset'])
            cache.merge([(now - 50, 80., 10), (now - 20, 101., 20)])
            cache.merge([(now - 20, 102., 20), (now - 5, 120., 40)], live=True)
            delta = cache.selection(now - 100, now, cursor)
            self.assertFalse(delta['reset'])
            self.assertEqual(delta['rows'], (
                (now - 50, 80., 10), (now - 20, 102., 20), (now - 5, 120., 40)))
            self.assertNotEqual(delta['cursor'], cursor)
            with cache.lock:
                cache.error = 'Issuance refresh failed: RPC unavailable'
            failed = cache.selection(now - 100, now, delta['cursor'])
            self.assertEqual(failed['rows'], ())
            self.assertEqual(failed['cursor'], delta['cursor'])
            self.assertIn('RPC unavailable', failed['error'])
            self.assertEqual(cache.selection(now - 100, now)['rows'], cache.rows)

    def test_revision_windows_exclude_lower_and_include_upper_bound(self):
        now = 1_700_001_000
        cache = self.persistent_cache()
        with patch('issuance.time.time', return_value=now):
            cache.merge([(now - 50, 1., 1), (now - 40, 2., 2),
                         (now - 30, 3., 3), (now - 20, 4., 4)], live=True)
            initial = cache.selection(now - 50, now - 30)
            self.assertEqual(initial['rows'], ((now - 40, 2., 2), (now - 30, 3., 3)))
            cache.merge([(now - 50, 11., 1), (now - 40, 12., 2),
                         (now - 30, 13., 3), (now - 20, 14., 4)])
            delta = cache.selection(now - 50, now - 30, initial['cursor'])
            self.assertEqual(delta['rows'], ((now - 40, 12., 2), (now - 30, 13., 3)))
            self.assertFalse(delta['reset'])
            # Aging out of the live cache must not clip persisted selections.
            with patch('issuance.time.time', return_value=now + RETENTION - 35):
                retained = cache.selection(now - 100, now, delta['cursor'])
            self.assertTrue(retained['reset'])
            self.assertEqual(retained['rows'], (
                (now - 50, 11., 1), (now - 40, 12., 2),
                (now - 30, 13., 3), (now - 20, 14., 4)))

    def test_bounded_revision_journal_resets_expired_and_restart_cursors(self):
        now = 1_700_001_000
        with patch('issuance.MAX_CHANGES', 2):
            cache = self.persistent_cache()
        with patch('issuance.time.time', return_value=now):
            empty = cache.selection(now - 100, now)
            cache.merge([(now - 30, 1., 1)], live=True)
            first = cache.selection(now - 100, now)
            cache.merge([(now - 20, 2., 2)])
            cache.merge([(now - 10, 3., 3)])
            self.assertEqual(len(cache.changes), 2)
            self.assertFalse(cache.selection(now - 100, now, first['cursor'])['reset'])
            expired = cache.selection(now - 100, now, empty['cursor'])
            self.assertTrue(expired['reset'])
            self.assertEqual(expired['rows'], cache.rows)
            restarted = IssuanceCache(cache.history_path)
            restarted.restore()
            for cursor in (None, '', 'malformed', first['cursor'],
                           f'{restarted.instance}:999999', f'{restarted.instance}:-1'):
                with self.subTest(cursor=cursor):
                    reset = restarted.selection(now - 25, now, cursor)
                    self.assertTrue(reset['reset'])
                    self.assertEqual(reset['rows'], ((now - 20, 2., 2), (now - 10, 3., 3)))
            self.assertNotEqual(restarted.selection(now - 100, now)['cursor'], expired['cursor'])
