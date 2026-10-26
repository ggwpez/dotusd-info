import unittest
import io
import json
from unittest.mock import Mock, patch

import hub_quotes
from archive_reader import Unavailable
from hub_quotes import GENESIS, HubChain, HubRpc


class HubChainTests(unittest.TestCase):
    def test_finality_verifies_chain_once_and_reads_each_head(self):
        chain = HubChain('https://hub.example/rpc')
        self.assertEqual(chain.rpc.endpoint, 'https://hub.example/rpc')
        chain.rpc = Mock()
        chain.rpc.call.side_effect = [GENESIS, 'head-one', {'number': '0x2a'},
                                     'head-two', {'number': '0x2b'}]
        self.assertEqual(chain.finalized(), 42)
        self.assertEqual(chain.finalized(), 43)
        self.assertEqual([call.args for call in chain.rpc.call.call_args_list], [
            ('chain_getBlockHash', [0]),
            ('chain_getFinalizedHead', []), ('chain_getHeader', ['head-one']),
            ('chain_getFinalizedHead', []), ('chain_getHeader', ['head-two'])])

    def test_wrong_chain_rejected_before_reading_head(self):
        chain = HubChain()
        chain.rpc = Mock()
        chain.rpc.call.return_value = 'wrong genesis'
        with self.assertRaisesRegex(ValueError, 'Wrong chain'):
            chain.finalized()
        chain.rpc.call.assert_called_once_with('chain_getBlockHash', [0])

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

    def test_rpc_response_size_is_bounded(self):
        rpc = HubRpc('https://hub.example', delay=0)
        with patch.object(hub_quotes, 'MAX_RPC_BYTES', 100), \
                patch('hub_quotes.urllib.request.urlopen', return_value=io.BytesIO(b'x' * 101)):
            with self.assertRaisesRegex(ValueError, 'size limit'):
                rpc.call('chain_getFinalizedHead', [])

    def test_block_hash_and_storage_stay_pinned(self):
        rpc = HubRpc('https://hub.example', delay=0)
        rpc.call = Mock(return_value=None)
        with self.assertRaises(Unavailable):
            rpc.block_hash(123)
        rpc.call.return_value = [{'block': 'h123', 'changes': [['key', 'value']]}]
        self.assertEqual(rpc.storage('h123', ['key']), {'key': 'value'})
        rpc.call.assert_called_with('state_queryStorageAt', [['key'], 'h123'])
        for result in ([], [{'block': 'wrong', 'changes': []}]):
            rpc.call.return_value = result
            with self.assertRaisesRegex(ValueError, 'pinned'):
                rpc.storage('h123', ['key'])
