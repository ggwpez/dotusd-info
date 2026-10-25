from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from archive_reader import TIMESTAMP, metadata_key
from issuance import ASSET, RETENTION, asset_key
from treasury import (TREASURY_ACCOUNT, TreasuryHistory,
                      account_balance, account_key, read_history)


def account(amount, status=0, reason=1):
    extra = {0: b'', 1: b'', 2: (123).to_bytes(16, 'little'), 3: b'',
             4: bytes(range(32)) + (456).to_bytes(16, 'little')}[reason]
    return '0x' + (amount.to_bytes(16, 'little') + bytes([status, reason]) + extra).hex()


def metadata(precision=6):
    return '0x' + (bytes(16) + b'\x18dotUSD\x18dotUSD' + bytes([precision, 0])).hex()


class TreasuryDecodingTests(unittest.TestCase):
    def test_canonical_account_and_double_map_key(self):
        # SS58 address documented by the Polkadot Hub runtime's treasury.rs.
        self.assertEqual(TREASURY_ACCOUNT.hex(),
                         '6d6f646c70792f74727372790000000000000000000000000000000000000000')
        # Independently queried through Parity WSS at finalized block 22281818.
        self.assertEqual(account_key(),
                         '0x682a59d51ab9e48a8c8cc418ff9708d2b'
                         '99d880ec681799c0cf30e8886371da99'
                         '74267ef3a34a5b57b06f26556087d7dc11e0000'
                         '5ecffd7b6c0f78751baa9d281e0bfa3a'
                         '6d6f646c70792f74727372790000000000000000000000000000000000000000')

    def test_full_u128_and_every_status_and_existence_reason(self):
        for amount in (0, 123456789, 2 ** 128 - 1):
            for status in range(3):
                for reason in range(5):
                    with self.subTest(amount=amount, status=status, reason=reason):
                        self.assertEqual(account_balance(account(amount, status, reason)), amount)
        self.assertEqual(account_balance(None), 0)

    def test_malformed_accounts_are_not_zero_or_plausible_balances(self):
        malformed = [False, 123, '', '0x', 'ff' + account(123)[2:],
                     '0x' + bytes(16).hex(), account(123, status=3),
                     '0x' + (bytes(16) + b'\x00\x05').hex(),
                     account(123) + ' ', '0xzz']
        for reason in range(5):
            encoded = account(123, reason=reason)
            malformed.extend((encoded[:-2], encoded + '00'))
        for encoded in malformed:
            with self.subTest(encoded=encoded), self.assertRaises(ValueError):
                account_balance(encoded)

    def rpc(self):
        rpc = Mock()
        rpc.block_hash.return_value = 'pinned-block'
        rpc.storage.return_value = {
            TIMESTAMP: '0x009ba873a1010000',
            account_key(): '0x102700000000000000000000000000000001',
            metadata_key(ASSET): metadata(),
            asset_key(ASSET): '0x' + (
                bytes(range(128)) + (1234567).to_bytes(16, 'little') +
                bytes(32) + b'\x01' + bytes(12) + b'\x00').hex(),
        }
        return rpc

    def test_real_pinned_fixture_uses_metadata_precision_and_chain_timestamp(self):
        rpc = self.rpc()
        self.assertEqual(read_history(rpc, 22281818, treasury=True),
                         {'treasury': (1792941792., 0.01, 22281818)})
        rpc.block_hash.assert_called_once_with(22281818)
        rpc.storage.assert_called_once_with(
            'pinned-block', [TIMESTAMP, account_key(), metadata_key(ASSET)])
        rpc.storage.return_value[metadata_key(ASSET)] = metadata(3)
        self.assertEqual(read_history(rpc, 22281818, treasury=True)['treasury'][1], 10.)

    def test_shared_block_hash_and_millisecond_timestamp_skip_redundant_reads(self):
        rpc = self.rpc()
        del rpc.storage.return_value[TIMESTAMP]
        self.assertEqual(read_history(rpc, 42, treasury=True, at='shared-block', timestamp=1234567),
                         {'treasury': (1234.567, 0.01, 42)})
        rpc.block_hash.assert_not_called()
        rpc.storage.assert_called_once_with('shared-block', [account_key(), metadata_key(ASSET)])

    def test_both_series_share_exactly_one_storage_query(self):
        rpc = self.rpc()
        self.assertEqual(read_history(rpc, 42, issuance=True, treasury=True), {
            'issuance': (1792941792., 1.234567, 42),
            'treasury': (1792941792., 0.01, 42),
        })
        rpc.block_hash.assert_called_once_with(42)
        rpc.storage.assert_called_once_with(
            'pinned-block', [TIMESTAMP, asset_key(ASSET), account_key(), metadata_key(ASSET)])

    def test_issuance_only_omits_account_and_reuses_shared_context(self):
        rpc = self.rpc()
        del rpc.storage.return_value[account_key()]
        del rpc.storage.return_value[TIMESTAMP]
        self.assertEqual(
            read_history(rpc, 42, issuance=True, at='shared-block', timestamp=1234567),
            {'issuance': (1234.567, 1.234567, 42)})
        rpc.block_hash.assert_not_called()
        rpc.storage.assert_called_once_with(
            'shared-block', [asset_key(ASSET), metadata_key(ASSET)])

    def test_no_requested_series_does_no_rpc_work(self):
        rpc = self.rpc()
        self.assertEqual(read_history(rpc, 42), {})
        rpc.block_hash.assert_not_called()
        rpc.storage.assert_not_called()

    def test_decode_failure_preserves_other_series(self):
        for failed, key, good, amount in (
                ('issuance', asset_key(ASSET), 'treasury', 0.01),
                ('treasury', account_key(), 'issuance', 1.234567)):
            rpc = self.rpc()
            rpc.storage.return_value[key] = '0x'
            result = read_history(rpc, 42, issuance=True, treasury=True)
            with self.subTest(failed=failed):
                self.assertIsInstance(result[failed], ValueError)
                self.assertEqual(result[good], (1792941792., amount, 42))
                self.assertEqual(rpc.storage.call_count, 1)

    def test_only_explicitly_absent_account_is_zero(self):
        rpc = self.rpc()
        rpc.storage.return_value[account_key()] = None
        self.assertEqual(read_history(rpc, 42, treasury=True)['treasury'][1], 0.)
        for key in (TIMESTAMP, metadata_key(ASSET)):
            rpc = self.rpc()
            rpc.storage.return_value[key] = None
            with self.subTest(key=key), self.assertRaises(ValueError):
                read_history(rpc, 42, treasury=True)
        rpc = self.rpc()
        del rpc.storage.return_value[account_key()]
        self.assertIsInstance(read_history(rpc, 42, treasury=True)['treasury'], KeyError)

    def test_malformed_timestamps_and_metadata_fail(self):
        for encoded in ('0x', 'ff009ba873a1010000', '0x009ba873a10100',
                        '0x009ba873a101000000', '0x009ba873a10100zz'):
            rpc = self.rpc()
            rpc.storage.return_value[TIMESTAMP] = encoded
            with self.subTest(encoded=encoded), self.assertRaises(ValueError):
                read_history(rpc, 42, treasury=True)
        rpc = self.rpc()
        rpc.storage.return_value[metadata_key(ASSET)] = 'ff' + metadata()[2:]
        with self.assertRaises(ValueError):
            read_history(rpc, 42, treasury=True)


class TreasuryHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'nested' / 'repairs.db'
        self.now = 1_700_001_000
        clock = patch('issuance.time.time', return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def test_restore_keeps_seven_days_in_memory_and_all_rows_on_disk(self):
        history = TreasuryHistory(self.path)
        rows = [(self.now - RETENTION, 1., 1), (self.now - RETENTION + 1, 2., 2),
                (self.now - 100, 0., 3)]
        history.merge(rows, now=self.now)
        self.assertEqual(history.snapshot()['rows'], tuple(rows[1:]))
        restored = TreasuryHistory(self.path)
        restored.restore()
        restored.restore()
        self.assertEqual(restored.snapshot()['rows'], tuple(rows[1:]))
        with closing(restored.database()) as db:
            self.assertEqual(db.execute(
                'SELECT timestamp,balance,number FROM treasury ORDER BY number').fetchall(), rows)
        with patch('issuance.time.time', return_value=self.now + RETENTION):
            self.assertEqual(restored.snapshot()['rows'], ())
            expired = TreasuryHistory(self.path)
            expired.restore()
            self.assertEqual(expired.snapshot()['rows'], ())
        with closing(expired.database()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM treasury').fetchone()[0], 3)

    def test_merge_restores_existing_rows_sorts_deduplicates_and_corrects(self):
        first = (self.now - 30, 1., 1)
        second = (self.now - 20, 2., 2)
        third = (self.now - 20, 3., 3)  # Equal chain timestamps are valid.
        TreasuryHistory(self.path).merge([second, first])
        history = TreasuryHistory(self.path)
        correction = (self.now - 20, 4., 2)
        history.merge(iter([third, correction, third]))
        self.assertEqual(history.snapshot()['rows'], (first, correction, third))
        restored = TreasuryHistory(self.path)
        restored.restore()
        self.assertEqual(restored.snapshot()['rows'], history.snapshot()['rows'])
        with closing(history.database()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM treasury').fetchone()[0], 3)

    def test_backwards_timestamp_rejects_entire_batch_and_preserves_disk(self):
        history = TreasuryHistory(self.path)
        first = (self.now - 10, 1., 1)
        history.merge([first])
        with self.assertRaisesRegex(ValueError, 'moved backwards'):
            history.merge([(self.now - 20, 2., 2), (self.now, 3., 3)])
        self.assertEqual(history.snapshot()['rows'], (first,))
        with closing(history.database()) as db:
            self.assertEqual(db.execute('SELECT number FROM treasury').fetchall(), [(1,)])

    def test_database_failure_preserves_memory_and_can_retry(self):
        history = TreasuryHistory(self.path)
        first, second = (self.now - 20, 1., 1), (self.now - 10, 2., 2)
        history.merge([first])
        with patch.object(history, 'database', side_effect=OSError('Disk unavailable')):
            with self.assertRaises(OSError):
                history.merge([second])
        self.assertEqual(history.snapshot()['rows'], (first,))
        history.merge([second])
        self.assertEqual(history.snapshot()['rows'], (first, second))

    def test_correction_outside_memory_window_is_still_persisted(self):
        history = TreasuryHistory(self.path)
        history.merge([(self.now - 10, 1., 1)])
        old = (self.now - RETENTION, 2., 1)
        history.merge([old])
        self.assertEqual(history.snapshot()['rows'], ())
        with closing(history.database()) as db:
            self.assertEqual(db.execute('SELECT timestamp,balance,number FROM treasury').fetchall(),
                             [old])

    def test_no_history_configuration_returns_usable_empty_selection(self):
        history = TreasuryHistory()
        first = history.selection(self.now - 60, self.now)
        self.assertEqual(first['rows'], ())
        self.assertIsNone(first['error'])
        self.assertTrue(first['reset'])
        unchanged = history.selection(self.now - 60, self.now, first['cursor'])
        self.assertFalse(unchanged['reset'])
        self.assertEqual(unchanged['rows'], ())

    def test_revision_deltas_include_repairs_corrections_and_zero_balances(self):
        history = TreasuryHistory(self.path)
        first = (self.now - 30, 1., 20)
        last = (self.now - 10, 3., 40)
        history.merge([first, last])
        initial = history.selection(self.now - 60, self.now)
        self.assertIsNone(initial['error'])
        self.assertTrue(initial['reset'])
        history.merge([first, last])
        unchanged = history.selection(self.now - 60, self.now, initial['cursor'])
        self.assertEqual(unchanged['rows'], ())
        self.assertEqual(unchanged['cursor'], initial['cursor'])
        repair = (self.now - 40, 0., 10)
        correction = (self.now - 30, 2., 20)
        history.merge([repair, correction])
        correction = (self.now - 30, 4., 20)
        history.merge([correction])
        changed = history.selection(self.now - 60, self.now, initial['cursor'])
        self.assertFalse(changed['reset'])
        self.assertEqual(changed['rows'], (repair, correction))
        self.assertNotEqual(changed['cursor'], initial['cursor'])
        bounded = history.selection(self.now - 40, self.now - 30, initial['cursor'])
        self.assertEqual(bounded['rows'], (correction,))
        with history.lock:
            history.error = 'Treasury history temporarily unavailable.'
        failed = history.selection(self.now - 60, self.now, changed['cursor'])
        self.assertEqual(failed['rows'], ())
        self.assertEqual(failed['error'], history.error)

    def test_cursor_expiry_and_restoration_reset_only_this_series(self):
        with patch('issuance.MAX_CHANGES', 2):
            history = TreasuryHistory(self.path)
        empty = history.selection(self.now - 60, self.now)
        self.assertIn('Loading', empty['error'])
        rows = [(self.now - 30, 1., 1), (self.now - 20, 2., 2), (self.now - 10, 3., 3)]
        history.merge(rows[:1])
        first = history.selection(self.now - 60, self.now)
        history.merge(rows[1:])
        self.assertFalse(history.selection(self.now - 60, self.now, first['cursor'])['reset'])
        expired = history.selection(self.now - 60, self.now, empty['cursor'])
        self.assertTrue(expired['reset'])
        self.assertEqual(expired['rows'], tuple(rows))
        restored = TreasuryHistory(self.path)
        restored.restore()
        for cursor in (None, '', 'invalid', first['cursor'],
                       f'{restored.instance}:999999', f'{restored.instance}:-1',
                       f'{restored.instance}:' + '9' * 100):
            with self.subTest(cursor=cursor):
                result = restored.selection(self.now - 30, self.now - 10, cursor)
                self.assertTrue(result['reset'])
                self.assertEqual(result['rows'], tuple(rows[1:]))
                self.assertIsNone(result['error'])

    def test_historical_corrections_reset_without_mutating_live_journal(self):
        history = TreasuryHistory(self.path)
        old = self.now - 10 * RETENTION
        rows = [(old + 10, 1., 1), (old + 20, 0., 2), (old + 20, 3., 3),
                (self.now - 10, 4., 4)]
        history.merge(rows)
        recent, revision, changes = history.rows, history.revision, tuple(history.changes)
        first = history.selection(old + 10, old + 20)
        self.assertEqual(first['rows'], tuple(rows[1:3]))
        self.assertTrue(first['reset'])
        history.merge([(old + 20, 2., 2), (old + 30, 3., 3)])
        corrected = history.selection(old + 10, old + 20, first['cursor'])
        self.assertEqual(corrected['rows'], ((old + 20, 2., 2),))
        self.assertTrue(corrected['reset'])
        self.assertEqual(corrected['cursor'], first['cursor'])
        self.assertEqual((history.rows, history.revision, tuple(history.changes)),
                         (recent, revision, changes))
        restored = TreasuryHistory(self.path)
        self.assertEqual(restored.selection(old + 10, old + 20)['rows'], corrected['rows'])
        self.assertEqual(restored.rows, ())

    def test_historical_window_crosses_retention_without_expanding_memory(self):
        history = TreasuryHistory(self.path)
        cutoff = self.now - RETENTION
        rows = [(cutoff - 10, 1., 1), (cutoff, 2., 2), (cutoff + 10, 3., 3)]
        history.merge(rows)
        self.assertEqual(history.selection(cutoff - 10, cutoff + 10)['rows'], tuple(rows[1:]))
        self.assertEqual(history.rows, (rows[2],))

    def test_recent_and_historical_caps_reject_instead_of_truncating(self):
        history = TreasuryHistory(self.path)
        old = self.now - 10 * RETENTION
        historical = [(old + number, float(number), number) for number in range(1, 5)]
        recent = [(self.now - 10 + number, float(number), number + 10) for number in range(1, 5)]
        history.merge(historical + recent)
        cursor = history.selection(self.now - 10, self.now)['cursor']
        statements = []
        database = history.database
        def traced_database():
            self.assertFalse(history.lock.locked())
            db = database()
            db.set_trace_callback(statements.append)
            return db
        with patch('issuance.MAX_SAMPLES', 2):
            for value in (None, cursor):
                with self.subTest(cursor=value), self.assertRaisesRegex(ValueError, 'Treasury window exceeds 2'):
                    history.selection(self.now - 10, self.now, value)
            with patch.object(history, 'database', side_effect=traced_database):
                self.assertEqual(history.selection(old, old + 2)['rows'], tuple(historical[:2]))
                with self.assertRaisesRegex(ValueError, 'Treasury window exceeds 2'):
                    history.selection(old, old + 4)
        queries = [statement for statement in statements if statement.startswith('SELECT')]
        self.assertEqual(len(queries), 2)
        self.assertTrue(all(query.endswith('LIMIT 3') for query in queries))
