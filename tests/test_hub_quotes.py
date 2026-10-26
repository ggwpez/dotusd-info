import unittest
import io
import json
from threading import Event
from unittest.mock import Mock, patch

from websockets.exceptions import ConnectionClosed, ConnectionClosedError, ConnectionClosedOK

import hub_quotes
from archive_reader import Unavailable
from hub_quotes import GENESIS, HubChain, HubRpc


class HubChainTests(unittest.TestCase):
    def test_http_verification_is_cached_without_polling_finality(self):
        chain = HubChain()
        chain.rpc = Mock()
        chain.rpc.call.return_value = GENESIS
        chain.verify()
        chain.verify()
        self.assertTrue(chain.verified)
        chain.rpc.call.assert_called_once_with('chain_getBlockHash', [0])

    def test_failed_http_verification_is_not_cached(self):
        chain = HubChain()
        chain.rpc = Mock()
        chain.rpc.call.side_effect = ['wrong genesis', GENESIS]
        with self.assertRaisesRegex(ValueError, 'Wrong chain'):
            chain.verify()
        self.assertFalse(chain.verified)
        chain.verify()
        self.assertTrue(chain.verified)
        self.assertEqual(chain.rpc.call.call_count, 2)

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


class FinalizedHeadsTests(unittest.TestCase):
    HEAD_HASH = '0x' + 'a' * 64

    def setUp(self):
        connector = patch('hub_quotes.connect')
        self.connect = connector.start()
        self.addCleanup(connector.stop)
        self.websocket = Mock()
        self.connect.return_value.__enter__.return_value = self.websocket
        self.chain = HubChain('https://hub.example/rpc?token=secret')
        self.chain.rpc = Mock()
        self.stop = Event()

    @staticmethod
    def reply(request_id, result):
        return json.dumps(dict(jsonrpc='2.0', id=request_id, result=result))

    @staticmethod
    def notification(number, subscription='finality'):
        return json.dumps(dict(jsonrpc='2.0', method='chain_finalizedHead',
                               params=dict(subscription=subscription,
                                           result={'number': hex(number)})))

    def bootstrap(self, number=42, subscription='finality'):
        return [self.reply(1, GENESIS), self.reply(2, subscription),
                self.reply(3, self.HEAD_HASH), self.reply(4, {'number': hex(number)})]

    def stream(self, messages):
        self.websocket.recv.side_effect = messages
        stream = self.chain.finalized_heads(self.stop)
        self.addCleanup(stream.close)
        return stream

    def test_endpoint_derivation_preserves_http_and_explicit_override(self):
        self.assertEqual(self.chain.ws_endpoint, 'wss://hub.example/rpc?token=secret')
        chain = HubChain('http://localhost:9944/archive?key=a%2Fb')
        self.assertEqual(chain.rpc.endpoint, 'http://localhost:9944/archive?key=a%2Fb')
        self.assertEqual(chain.ws_endpoint, 'ws://localhost:9944/archive?key=a%2Fb')
        chain = HubChain('https://hub.example/archive',
                         ws_endpoint='wss://heads.example/live?key=x')
        self.assertEqual(chain.rpc.endpoint, 'https://hub.example/archive')
        self.assertEqual(chain.ws_endpoint, 'wss://heads.example/live?key=x')

    def test_quiet_chain_bootstraps_then_delivers_notifications_without_http(self):
        stream = self.stream(self.bootstrap() + [
            self.notification(42), self.notification(45)])
        self.assertEqual(next(stream), 42)
        self.assertEqual(next(stream), 45)
        sent = [json.loads(call.args[0]) for call in self.websocket.send.call_args_list]
        self.assertEqual([(m['id'], m['method'], m['params']) for m in sent], [
            (1, 'chain_getBlockHash', [0]),
            (2, 'chain_subscribeFinalizedHeads', []),
            (3, 'chain_getFinalizedHead', []),
            (4, 'chain_getHeader', [self.HEAD_HASH])])
        self.assertTrue(all(m['jsonrpc'] == '2.0' for m in sent))
        self.chain.rpc.call.assert_not_called()
        self.stop.set()
        self.assertEqual(list(stream), [])
        self.connect.return_value.__exit__.assert_called_once()

    def test_bootstrap_coalesces_interleaved_notifications_without_stale_order(self):
        responses = self.bootstrap()
        stream = self.stream(responses[:2] + [
            self.notification(40), self.notification(42), responses[2],
            self.notification(45), self.notification(46), responses[3],
            self.notification(46), self.notification(48)])
        self.assertEqual([next(stream), next(stream), next(stream)], [42, 46, 48])
        self.assertEqual(self.websocket.send.call_count, 4)

    def test_bootstrap_ahead_of_queued_notification_does_not_regress(self):
        stream = self.stream(self.bootstrap(45) + [
            self.notification(42), self.notification(45), self.notification(46)])
        self.assertEqual([next(stream), next(stream)], [45, 46])

    def test_websocket_genesis_is_checked_even_after_http_verification(self):
        self.chain.verified = True
        stream = self.stream([self.reply(1, 'wrong genesis')])
        with self.assertRaisesRegex(ValueError, 'Wrong chain'):
            next(stream)
        self.assertEqual(self.websocket.send.call_count, 1)
        self.chain.rpc.call.assert_not_called()

    def test_bootstrap_rejects_wrong_response_ids_errors_and_invalid_messages(self):
        invalid = [
            self.reply(2, GENESIS), self.reply(True, GENESIS), self.reply('1', GENESIS),
            json.dumps(dict(jsonrpc='2.0', id=1, error={'message': 'unavailable'})),
            json.dumps(dict(jsonrpc='2.0', id=1)),
            json.dumps(dict(jsonrpc='1.0', id=1, result=GENESIS)),
            json.dumps(dict(jsonrpc='2.0', id=1, result=GENESIS, method='wrong')),
            '[]', 'null', '{not JSON',
        ]
        for message in invalid:
            with self.subTest(message=message):
                with self.assertRaises(ValueError):
                    next(self.stream([message]))

    def test_subscription_response_requires_valid_identity_and_success(self):
        for identity in (None, True, False, '', -1, 1.5, [], {}):
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(ValueError, 'subscription ID'):
                    next(self.stream(self.bootstrap(subscription=identity)))
        with self.assertRaisesRegex(ValueError, 'RPC failed'):
            next(self.stream([self.reply(1, GENESIS), json.dumps(dict(
                jsonrpc='2.0', id=2, error={'message': 'subscriptions disabled'}))]))

    def test_notification_requires_matching_identity_and_expected_method(self):
        valid = json.loads(self.notification(43))
        wrong_method = dict(valid, method='chain_newHead')
        response = dict(valid, id=4)
        missing_header = dict(valid, params={'subscription': 'finality'})
        error = dict(valid, params={'subscription': 'finality', 'error': 'failed'})
        for message in [self.notification(43, 'other'), self.notification(43, None),
                        json.dumps(wrong_method), json.dumps(response),
                        json.dumps(missing_header), json.dumps(error)]:
            with self.subTest(message=message):
                stream = self.stream(self.bootstrap() + [message])
                self.assertEqual(next(stream), 42)
                with self.assertRaises(ValueError):
                    next(stream)
        # Python equality must not mistake JSON true or 1.0 for subscription 1.
        for identity in (True, 1.0, '1'):
            with self.subTest(identity=identity):
                stream = self.stream(self.bootstrap(subscription=1)
                                     + [self.notification(43, identity)])
                self.assertEqual(next(stream), 42)
                with self.assertRaisesRegex(ValueError, 'subscription ID'):
                    next(stream)

    def test_notification_identity_is_checked_during_bootstrap(self):
        responses = self.bootstrap()
        stream = self.stream(responses[:2] + [self.notification(43, 'other')])
        with self.assertRaisesRegex(ValueError, 'subscription ID'):
            next(stream)

    def test_header_numbers_and_pinned_bootstrap_hash_must_be_valid(self):
        for number in (None, True, 42, '42', '0x', '0x-1', '0x2_0', '0xgg'):
            with self.subTest(number=number):
                stream = self.stream(self.bootstrap()[:3]
                                     + [self.reply(4, {'number': number})])
                with self.assertRaisesRegex(ValueError, 'block number'):
                    next(stream)
        stream = self.stream(self.bootstrap()[:2] + [self.reply(3, None)])
        with self.assertRaisesRegex(ValueError, 'block hash'):
            next(stream)

    def test_backwards_notifications_raise_instead_of_emitting_stale_heads(self):
        stream = self.stream(self.bootstrap() + [
            self.notification(45), self.notification(44)])
        self.assertEqual([next(stream), next(stream)], [42, 45])
        with self.assertRaisesRegex(ValueError, 'backwards'):
            next(stream)

    def test_bootstrap_response_timeout_is_bounded(self):
        stream = self.stream([TimeoutError()])
        with patch('hub_quotes.time.monotonic', side_effect=[0, 0, 11]):
            with self.assertRaisesRegex(TimeoutError, 'response timed out'):
                next(stream)

    def test_stop_before_connect_and_during_idle_receive_or_bootstrap(self):
        self.stop.set()
        self.assertEqual(list(self.chain.finalized_heads(self.stop)), [])
        self.connect.assert_not_called()

        def stop_receiving(**kwargs):
            self.assertLessEqual(kwargs['timeout'], hub_quotes.WS_RECEIVE_TIMEOUT)
            self.stop.set()
            raise TimeoutError()

        self.stop.clear()
        stream = self.stream([])
        self.websocket.recv.side_effect = stop_receiving
        self.assertEqual(list(stream), [])

        self.stop.clear()
        stream = self.stream(self.bootstrap())
        self.assertEqual(next(stream), 42)
        self.websocket.recv.side_effect = stop_receiving
        self.assertEqual(list(stream), [])

    def test_clean_and_failed_disconnects_propagate_without_auto_reconnect(self):
        for error_type in (ConnectionClosedOK, ConnectionClosedError):
            for bootstrap in (False, True):
                with self.subTest(error_type=error_type, bootstrap=bootstrap):
                    self.connect.reset_mock()
                    messages = self.bootstrap() if bootstrap else []
                    stream = self.stream(messages + [error_type(None, None)])
                    if bootstrap:
                        self.assertEqual(next(stream), 42)
                    with self.assertRaises(ConnectionClosed):
                        next(stream)
                    self.connect.assert_called_once()

    def test_reconnect_reverifies_chain_and_replaces_subscription_identity(self):
        stream = self.stream(self.bootstrap() + [ConnectionClosedOK(None, None)])
        self.assertEqual(next(stream), 42)
        with self.assertRaises(ConnectionClosed):
            next(stream)
        stream = self.stream(self.bootstrap(45, 'new-finality') + [self.notification(46)])
        self.assertEqual(next(stream), 45)
        with self.assertRaisesRegex(ValueError, 'subscription ID'):
            next(stream)
        self.assertEqual(self.connect.call_count, 2)
        sent = [json.loads(call.args[0]) for call in self.websocket.send.call_args_list]
        self.assertEqual([m['method'] for m in sent].count('chain_getBlockHash'), 2)
        self.chain.rpc.call.assert_not_called()

    def test_quiet_receive_timeouts_do_not_poll_or_reconnect(self):
        stream = self.stream(self.bootstrap(subscription=0) + [
            TimeoutError(), TimeoutError(), self.notification(45, 0)])
        self.assertEqual([next(stream), next(stream)], [42, 45])
        self.assertEqual(self.websocket.send.call_count, 4)
        self.connect.assert_called_once()
        self.chain.rpc.call.assert_not_called()

    def test_stop_does_not_emit_coalesced_bootstrap_notifications(self):
        responses = self.bootstrap()
        stream = self.stream(responses[:3] + [self.notification(45), responses[3]])
        self.assertEqual(next(stream), 42)
        self.stop.set()
        self.assertEqual(list(stream), [])

    def test_correct_websocket_does_not_authenticate_http_state_endpoint(self):
        stream = self.stream(self.bootstrap())
        self.assertEqual(next(stream), 42)
        self.assertFalse(self.chain.verified)
        self.chain.rpc.call.return_value = 'wrong genesis'
        with self.assertRaisesRegex(ValueError, 'Wrong chain'):
            self.chain.verify()
        self.chain.rpc.call.assert_called_once_with('chain_getBlockHash', [0])
