import unittest
import io
import json
from unittest.mock import Mock, patch

from hub_quotes import HubChain, HubQuotes, HubRpc, TIMESTAMP


def block(number, stamp, dotusd=None, usdt=None):
    return dict(number=number, timestamp=stamp, trades={
        '7873': [] if dotusd is None else [{'quote': dotusd}],
        '1984': [] if usdt is None else [{'quote': usdt}]})


class HubQuotesTests(unittest.TestCase):
    def chain(self, records, head):
        chain = Mock()
        chain.finalized.return_value = head
        chain.block.side_effect = records.__getitem__
        chain.reserve.return_value = '2'
        return chain

    def test_independent_latest_trades_catchup_and_same_head(self):
        records = {10: block(10, 100000, dotusd='2.5'),
                   9: block(9, 98000, usdt='2'),
                   11: block(11, 102000, usdt='3'),
                   12: block(12, 104000)}
        chain = self.chain(records, 10)
        source = HubQuotes(chain)
        self.assertEqual(source.read(), (100, {'1984': '2', '7873': '2.5'}, 10))
        self.assertIsNone(source.read())
        chain.finalized.return_value = 12
        self.assertEqual(source.read(), (104, {'1984': '3', '7873': '2.5'}, 12))
        self.assertEqual([c.args[0] for c in chain.block.call_args_list], [10, 9, 11, 12])
        self.assertEqual(chain.reserve.call_args_list[-1].args, (12, 7873))

    def test_bootstrap_waits_for_window_boundary_and_expires_trades(self):
        records = {10: block(10, 100000000, dotusd='2.5'),
                   9: block(9, 100000000-86400000, usdt='3'),
                   11: block(11, 100002000)}
        chain = self.chain(records, 10)
        source = HubQuotes(chain)
        source.BLOCKS_PER_POLL = 1
        self.assertIsNone(source.read())
        chain.reserve.assert_not_called()
        self.assertEqual(source.read(), (100000, {'1984': '3', '7873': '2.5'}, 10))
        chain.finalized.return_value = 11
        self.assertEqual(source.read(), (100002, {'1984': '2', '7873': '2.5'}, 11))

    def test_missing_leg_requires_full_window_before_reserve_fallback(self):
        records = {10: block(10, 100000000, dotusd='2.5'),
                   9: block(9, 100000000-86400001, usdt='9')}
        source = HubQuotes(self.chain(records, 10))
        self.assertEqual(source.read(), (100000, {'1984': '2', '7873': '2.5'}, 10))

    def test_rpc_failure_retries_failed_block_and_never_skips_gap(self):
        records = {10: block(10, 100000, dotusd='2', usdt='2'),
                   11: block(11, 102000, dotusd='2.5'),
                   12: block(12, 104000, usdt='3')}
        chain = self.chain(records, 10)
        source = HubQuotes(chain)
        source.read()
        chain.finalized.return_value = 12
        chain.block.side_effect = OSError('pruned')
        with self.assertRaises(OSError):
            source.read()
        self.assertEqual(source.cursor, 10)
        chain.block.side_effect = records.__getitem__
        source.BLOCKS_PER_POLL = 1
        self.assertIsNone(source.read())
        self.assertEqual(source.read(), (104, {'1984': '3', '7873': '2.5'}, 12))
        chain.finalized.return_value = 11
        with self.assertRaisesRegex(ValueError, 'backwards'):
            source.read()

    def test_reserve_failure_does_not_mark_snapshot_published(self):
        source = HubQuotes(self.chain({10: block(10, 100000, dotusd='2', usdt='3')}, 10))
        source.chain.reserve.side_effect = OSError('RPC timeout')
        with self.assertRaises(OSError):
            source.read()
        self.assertIsNone(source.published)
        source.chain.reserve.side_effect = None
        self.assertEqual(source.read(), (100, {'1984': '3', '7873': '2'}, 10))

    def test_wrong_chain_rejected_before_reading_head(self):
        chain = HubChain()
        chain.rpc = Mock()
        chain.rpc.call.return_value = 'wrong genesis'
        with self.assertRaisesRegex(ValueError, 'Wrong chain'):
            chain.finalized()
        chain.rpc.call.assert_called_once_with('chain_getBlockHash', [0])


class HubChainTests(unittest.TestCase):
    def test_rpc_sends_only_json_rpc_to_configured_hub(self):
        rpc = HubRpc('https://hub.example/rpc', delay=0)
        with patch('hub_quotes.urllib.request.urlopen', return_value=io.BytesIO(
                b'{"jsonrpc":"2.0","id":1,"result":"0x123"}')) as request:
            self.assertEqual(rpc.call('chain_getFinalizedHead', []), '0x123')
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, 'https://hub.example/rpc')
        self.assertEqual(json.loads(sent.data), dict(jsonrpc='2.0', id=1,
                                                   method='chain_getFinalizedHead', params=[]))
        for body in (b'{"id":2,"result":"wrong"}', b'{"id":1,"error":{"message":"pruned"}}'):
            with patch('hub_quotes.urllib.request.urlopen', return_value=io.BytesIO(body)):
                with self.assertRaises(ValueError):
                    rpc.call('state_getStorage', [])

    def test_event_decoding_stays_pinned_to_requested_block(self):
        chain = HubChain()
        chain.rpc = Mock()
        chain.rpc.block_hash.return_value = 'finalized-hash'
        chain.decoder = Mock()
        chain.rpc.storage.return_value = {TIMESTAMP: '0x' + (123000).to_bytes(8, 'little').hex(),
                                         'events-key': '0x00'}
        chain.decoder.runtime_config.create_scale_object.return_value.decode.return_value = []
        storage_module, scale_module = Mock(), Mock()
        storage = storage_module.StorageKey.create_from_storage_function.return_value
        storage.to_hex.return_value = 'events-key'
        with patch.dict('sys.modules', {'substrateinterface.storage': storage_module,
                                       'scalecodec.base': scale_module}):
            result = chain.block(42)
        chain.decoder.init_runtime.assert_called_once_with(block_hash='finalized-hash')
        chain.decoder.create_storage_key.assert_not_called()
        chain.rpc.storage.assert_called_once_with('finalized-hash', [TIMESTAMP, 'events-key'])
        self.assertEqual(result, block(42, 123000))

    def test_failed_runtime_setup_keeps_decoder_and_invalidates_block(self):
        chain = HubChain()
        chain.rpc = Mock()
        chain.decoder = Mock()
        chain.decoder.init_runtime.side_effect = OSError('metadata unavailable')
        decoder = chain.decoder
        with patch('hub_quotes.gc.collect') as collect:
            with self.assertRaises(OSError):
                chain.block(42)
        collect.assert_called_once_with()
        self.assertIs(chain.decoder, decoder)
        self.assertIsNone(decoder.block_hash)
        self.assertIsNone(decoder.block_id)

    def test_real_runtime_initialization_recovers_at_each_failure_stage(self):
        try:
            from substrateinterface import SubstrateInterface
        except ImportError:
            self.skipTest('substrate-interface is not installed')

        for initial_version in (None, 1):
            for stage in ('rpc_request', 'get_block_runtime_version',
                          'get_block_metadata', 'reload_type_registry', 'get_constant'):
                with self.subTest(initial_version=initial_version, stage=stage):
                    chain = HubChain()
                    chain.rpc = Mock()
                    chain.rpc.block_hash.return_value = 'target'
                    decoder = SubstrateInterface(url='http://unused.invalid', auto_discover=False)
                    chain.decoder = decoder
                    decoder.runtime_version = initial_version
                    decoder.rpc_request = Mock(return_value={'result': {'parentHash': 'parent'}})
                    decoder.get_block_runtime_version = Mock(return_value={
                        'specVersion': 2, 'transactionVersion': 1})
                    metadata = Mock()
                    decoder.get_block_metadata = Mock(return_value=metadata)
                    decoder.reload_type_registry = Mock()
                    decoder.implements_scaleinfo = Mock(return_value=True)
                    decoder.runtime_config = Mock()
                    decoder.get_constant = Mock(return_value=None)
                    method = getattr(decoder, stage)
                    method.side_effect = TimeoutError('RPC timeout')
                    with self.assertRaises(TimeoutError):
                        chain.block(42)
                    self.assertIs(chain.decoder, decoder)
                    self.assertIsNone(decoder.block_hash)
                    self.assertIsNone(decoder.block_id)

                    method.side_effect = None
                    # Run the real library's retry and its early-return checks.
                    decoder.init_runtime(block_hash='target')
                    self.assertEqual(decoder.runtime_version, 2)
                    self.assertIs(decoder.metadata, metadata)
                    decoder.runtime_config.add_portable_registry.assert_called_with(metadata)
                    decoder.runtime_config.set_active_spec_version_id.assert_called_with(2)
                    decoder.get_constant.assert_called_with('System', 'SS58Prefix', block_hash='target')
                    # Metadata already parsed before a late failure is reused.
                    self.assertEqual(decoder.get_block_metadata.call_count,
                                     2 if stage == 'get_block_metadata' else 1)

    def test_timeout_on_unchanged_runtime_reuses_initialized_registry(self):
        try:
            from substrateinterface import SubstrateInterface
        except ImportError:
            self.skipTest('substrate-interface is not installed')
        for stage in ('rpc_request', 'get_block_runtime_version'):
            with self.subTest(stage=stage):
                chain = HubChain()
                chain.rpc = Mock()
                chain.rpc.block_hash.return_value = 'target'
                decoder = SubstrateInterface(url='http://unused.invalid', auto_discover=False)
                chain.decoder = decoder
                decoder.runtime_version = 1
                decoder.rpc_request = Mock(return_value={'result': {'parentHash': 'parent'}})
                decoder.get_block_runtime_version = Mock(return_value={'specVersion': 1})
                decoder.reload_type_registry = Mock()
                decoder.get_block_metadata = Mock()
                method = getattr(decoder, stage)
                for _ in range(3):
                    method.side_effect = TimeoutError('RPC timeout')
                    with self.assertRaises(TimeoutError):
                        chain.block(42)
                    method.side_effect = None
                    decoder.init_runtime(block_hash='target')
                    self.assertIs(chain.decoder, decoder)
                    self.assertEqual(decoder.runtime_version, 1)
                    decoder.block_hash = 'previous'
                decoder.reload_type_registry.assert_not_called()
                decoder.get_block_metadata.assert_not_called()

    def test_reserve_orientation_and_decimals(self):
        chain = HubChain()
        chain.rpc = Mock()
        chain.rpc.block_hash.return_value = 'finalized-hash'
        chain.decoder = Mock()
        encoded = '0x' + (bytes(18) + bytes([6, 0])).hex()
        chain.rpc.storage.return_value = {'metadata-key': encoded}
        with patch('hub_quotes.metadata_key', return_value='metadata-key'), patch(
                'hub_quotes.reserves', return_value=(10**10, 2_000_000)) as reserves:
            self.assertEqual(chain.reserve(42, 7873), '2')
        reserves.assert_called_once_with(chain.rpc, 'finalized-hash', 7873)
        chain.rpc.storage.assert_called_once_with('finalized-hash', ['metadata-key'])
        chain.rpc.storage.return_value = {'metadata-key': '0x' + (bytes(18) + bytes([12, 0])).hex()}
        with patch('hub_quotes.metadata_key', return_value='metadata-key'):
            with self.assertRaisesRegex(ValueError, 'decimals'):
                chain.reserve(42, 7873)
