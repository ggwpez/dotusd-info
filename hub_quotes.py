"""Bounded Hub archive RPC and verified finalized-head subscriptions."""
import json
import time
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from websockets.sync.client import connect

from archive_reader import Unavailable

RPC = 'https://polkadot-asset-hub-rpc.polkadot.io'
GENESIS = '0x68d56f15f85d3136970ec16946040bc1752654e906147f7e43e9d539d7c3de2f'
MAX_RPC_BYTES = 16 * 1024 * 1024
MAX_HEAD_BYTES = 64 * 1024
WS_RECEIVE_TIMEOUT = 0.25
WS_RPC_TIMEOUT = 10


class _SubscriptionStopped(Exception):
    """Interrupt bootstrap or receipt when the owner requests shutdown."""


class _FinalizedSubscription:
    def __init__(self, websocket, stop):
        self.websocket = websocket
        self.stop = stop
        self.request_id = 0
        self.subscription = None
        self.last_notification = None

    def receive(self, deadline=None):
        while not self.stop.is_set():
            timeout = WS_RECEIVE_TIMEOUT
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Hub WebSocket RPC response timed out')
                timeout = min(timeout, remaining)
            try:
                raw = self.websocket.recv(timeout=timeout)
            except TimeoutError:
                continue
            if self.stop.is_set():
                break
            message = json.loads(raw)
            if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                raise ValueError('Invalid Hub WebSocket JSON-RPC message')
            return message
        raise _SubscriptionStopped()

    def call(self, method, params):
        if self.stop.is_set():
            raise _SubscriptionStopped()
        self.request_id += 1
        self.websocket.send(json.dumps(dict(
            jsonrpc='2.0', id=self.request_id, method=method, params=params)))
        deadline = time.monotonic() + WS_RPC_TIMEOUT
        while True:
            message = self.receive(deadline)
            if 'id' not in message:
                # The subscription is already live while bootstrap replies arrive.
                # Remember its highest head without an unbounded notification queue.
                self.notification(message)
                continue
            if type(message['id']) is not int or message['id'] != self.request_id:
                raise ValueError('Invalid Hub WebSocket response ID')
            if 'error' in message:
                raise ValueError(f'Hub WebSocket RPC failed: {message["error"]}')
            if 'result' not in message or 'method' in message or 'params' in message:
                raise ValueError('Invalid Hub WebSocket RPC response')
            return message['result']

    def notification(self, message):
        params = message.get('params')
        if ('id' in message or 'error' in message or 'result' in message
                or message.get('method') != 'chain_finalizedHead'
                or not isinstance(params, dict) or 'error' in params
                or 'result' not in params):
            raise ValueError('Invalid finalized-head notification')
        subscription = params.get('subscription')
        if (self.subscription is None
                or type(subscription) is not type(self.subscription)
                or subscription != self.subscription):
            raise ValueError('Invalid finalized-head subscription ID')
        number = self.header_number(params['result'])
        if self.last_notification is not None and number < self.last_notification:
            raise ValueError('Finalized head moved backwards')
        self.last_notification = number
        return number

    @staticmethod
    def header_number(header):
        number = header.get('number') if isinstance(header, dict) else None
        if (not isinstance(number, str) or not number.startswith('0x')
                or not number[2:] or any(c not in '0123456789abcdefABCDEF'
                                        for c in number[2:])):
            raise ValueError('Invalid finalized-head block number')
        return int(number, 16)


class HubRpc:
    """Sequential HTTP calls with a per-client pause, timeout and response cap."""
    def __init__(self, endpoint, delay=0.1):
        self.endpoint = endpoint
        self.delay = delay

    def call(self, method, params):
        time.sleep(self.delay)
        request = urllib.request.Request(self.endpoint, json.dumps(dict(
            jsonrpc='2.0', id=1, method=method, params=params)).encode(),
            {'Content-Type': 'application/json', 'User-Agent': 'dotusd-info/1.0'})
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read(MAX_RPC_BYTES + 1)
        if len(body) > MAX_RPC_BYTES:
            raise ValueError('Hub RPC response exceeds size limit')
        result = json.loads(body)
        if result.get('id') != 1 or 'error' in result:
            raise ValueError(f'Hub RPC failed: {result.get("error", "invalid response ID")}')
        return result['result']

    def block_hash(self, number):
        at = self.call('chain_getBlockHash', [number])
        if at is None:
            raise Unavailable(f'No retained hash for block {number}')
        return at

    def storage(self, at, keys):
        result = self.call('state_queryStorageAt', [keys, at])
        if len(result) != 1 or result[0]['block'] != at:
            raise ValueError('Storage response is not pinned to the requested block')
        return dict(result[0]['changes'])


class HubChain:
    def __init__(self, endpoint=RPC, ws_endpoint=None):
        self.rpc = HubRpc(endpoint)
        if ws_endpoint is None:
            url = urlsplit(endpoint)
            if url.scheme not in ('http', 'https'):
                raise ValueError('Hub RPC endpoint must use HTTP or HTTPS')
            ws_endpoint = urlunsplit(url._replace(
                scheme='wss' if url.scheme == 'https' else 'ws'))
        self.ws_endpoint = ws_endpoint
        self.verified = False

    def verify(self):
        """Verify the HTTP state/archive endpoint once, independently of WS."""
        if not self.verified:
            if self.rpc.call('chain_getBlockHash', [0]) != GENESIS:
                raise ValueError('Wrong chain: expected Polkadot Hub')
            self.verified = True

    def finalized(self):
        self.verify()
        at = self.rpc.call('chain_getFinalizedHead', [])
        number = int(self.rpc.call('chain_getHeader', [at])['number'], 16)
        return number

    def finalized_heads(self, stop):
        """Yield increasing finality from one verified WebSocket connection.

        Subscribe before reading the current head, so even a quiet chain gets an
        initial value and finality advancing during bootstrap cannot be missed.
        Bootstrap notifications may coalesce; callers must ingest every block
        between yielded heads. Disconnects and protocol errors belong to the
        caller's reconnect policy, never an HTTP polling fallback.
        """
        if stop.is_set():
            return
        try:
            with connect(self.ws_endpoint, open_timeout=5, close_timeout=1,
                         ping_interval=20, ping_timeout=20,
                         max_size=MAX_HEAD_BYTES, max_queue=16) as websocket:
                subscription = _FinalizedSubscription(websocket, stop)
                # HTTP verification doesn't authenticate this endpoint or a new
                # connection after a reconnect.
                if subscription.call('chain_getBlockHash', [0]) != GENESIS:
                    raise ValueError('Wrong chain: expected Polkadot Hub')
                identity = subscription.call('chain_subscribeFinalizedHeads', [])
                if (type(identity) not in (str, int) or identity == ''
                        or (type(identity) is int and identity < 0)):
                    raise ValueError('Invalid finalized-head subscription ID')
                subscription.subscription = identity
                at = subscription.call('chain_getFinalizedHead', [])
                if (not isinstance(at, str) or len(at) != 66
                        or not at.startswith('0x')
                        or any(c not in '0123456789abcdefABCDEF' for c in at[2:])):
                    raise ValueError('Invalid finalized-head block hash')
                head = subscription.header_number(
                    subscription.call('chain_getHeader', [at]))
                if stop.is_set():
                    return
                yield head
                if stop.is_set():
                    return
                pending = subscription.last_notification
                if pending is not None and pending > head:
                    head = pending
                    yield head
                while not stop.is_set():
                    number = subscription.notification(subscription.receive())
                    if number > head:
                        head = number
                        yield head
        except _SubscriptionStopped:
            return
