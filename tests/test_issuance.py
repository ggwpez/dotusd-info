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
                self.assertEqual(db.execute('SELECT count(*) FROM issuance WHERE number=1').fetchone()[0], 0)

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

    def test_restart_prunes_expired_disk_rows_without_price_history(self):
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
                self.assertEqual(db.execute('SELECT number FROM issuance').fetchall(), [(2,)])
