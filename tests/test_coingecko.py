"""CoinGecko ingestion and shared HTTP/rendering contract, without network calls."""
import io
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.request import urlopen

from test_server import page


class CoinGeckoTests(unittest.TestCase):
    def test_fetch_validates_prices_and_keeps_actual_timestamps(self):
        http = Mock()
        cache = page.HistoryCache()
        worker = page.CoinGeckoWorker(cache, http=http, api_key='test-key')
        http.open.return_value = io.BytesIO(json.dumps({'prices': [[1700000000123, .999123456789]]}).encode())
        self.assertEqual(worker.fetch(1), [(1700000000.123, .999123456789, None)])
        request = http.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.coingecko.com/api/v3/coins/dotusd/market_chart?vs_currency=usd&days=1&precision=full')
        self.assertEqual(request.get_header('X-cg-demo-api-key'), 'test-key')
        for payload in ({'error': 'rate limited'}, {'prices': [[1, None]]},
                        {'prices': [[1, float('nan')]]}, {'prices': [[1, 0]]},
                        {'prices': [[True, 1]]}, {'prices': [[1, '1']]}, {'prices': [[1]]}):
            with self.subTest(payload=payload):
                http.open.return_value = io.BytesIO(json.dumps(payload).encode())
                with self.assertRaises(ValueError):
                    worker.fetch(1)
        http.open.return_value = io.BytesIO(b' ' * (page.MAX_UPSTREAM_BYTES + 1))
        with self.assertRaisesRegex(ValueError, 'size limit'):
            worker.fetch(1)
        http.open.return_value = io.BytesIO(b'{"prices":[]}')
        self.assertEqual(worker.fetch(1), [])

    def test_snapshot_refresh_corrections_empty_history_and_failure(self):
        now = 1800000000
        cache = page.HistoryCache()
        worker = page.CoinGeckoWorker(cache)
        older = [(now - 90000, .998, None), (now - 600, .97, None)]
        recent = [(now - 600, 1.001, None), (now - 1, .999, None)]
        worker.fetch = Mock(side_effect=[older, recent])
        with patch.object(page.time, 'time', return_value=now):
            worker.poll()
        self.assertEqual(cache.rows, tuple([older[0]] + recent))
        self.assertTrue(cache.history_ready)
        self.assertEqual([call.args for call in worker.fetch.call_args_list], [(7,), (1,)])
        # The hourly fetch is reused; a revised response replaces transient last points.
        worker.fetch = Mock(return_value=[(now - 600, 1.002, None), (now + 299, 1, None)])
        with patch.object(page.time, 'time', return_value=now + 300):
            worker.poll()
        worker.fetch.assert_called_once_with(1)
        self.assertEqual(cache.rows, (older[0], (now - 600, 1.002, None), (now + 299, 1, None)))
        before = cache.rows, cache.fetched_at
        worker.fetch.side_effect = OSError('upstream unavailable')
        with self.assertRaises(OSError), patch.object(page.time, 'time', return_value=now + 600):
            worker.poll()
        self.assertEqual((cache.rows, cache.fetched_at), before)
        worker.fetch = Mock(return_value=[])
        with patch.object(page.time, 'time', return_value=now + 3600):
            worker.poll()
        self.assertEqual(cache.rows, ())
        self.assertEqual(cache.fetched_at, now + 3600)

    def test_pages_share_assets_and_api_keeps_sources_separate(self):
        server = page.CacheServer(('127.0.0.1', 0), page.Handler)
        server.cache = page.HistoryCache()
        now = int(time.time())
        server.cache.merge([(now - 60, .98, 42)], now)
        cg_rows = [(now - 600, .999, None), (now - 300, 1.001, None)]
        server.coingecko_cache.merge(cg_rows, now)
        server.coingecko_cache.history_ready = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            for path, source, quote in [('/', 'pools', 'USDT'), ('/coingecko', 'coingecko', 'USD'),
                                        ('/coingecko/', 'coingecko', 'USD')]:
                with urlopen(base + path) as response:
                    body = response.read()
                    self.assertIn(f'data-price-source="{source}"'.encode(), body)
                    self.assertIn(f'Relative to 1.00 {quote}'.encode(), body)
                    self.assertIn(b'<script type="module">' + server.assets['inline.js'], body)
                    self.assertIn(b'<style>' + server.assets['style.css'], body)
                    self.assertNotIn(b'__QUOTE__', body)
                    self.assertIn("'sha256-", response.headers['Content-Security-Policy'])
            params = f'?start={now-1000}&end={now}&windowStart={now-1000}&windowEnd={now}'
            with urlopen(base + '/api/coingecko/prices' + params + '&format=stream') as response:
                records = [json.loads(line) for line in response]
            self.assertEqual(records[0]['extremes']['low'], list(cg_rows[0]))
            self.assertEqual(records[1]['ranges'], list(page.pack_ranges(cg_rows)))
            self.assertEqual(records[-1], {'done': True})
            with urlopen(base + '/api/prices' + params + '&format=raw') as response:
                self.assertEqual(json.load(response)['samples'], [[now - 60, .98, 42]])
            server.coingecko_cache.merge([], now, replace=True)
            with urlopen(base + '/api/coingecko/prices' + params + '&format=stream') as response:
                records = [json.loads(line) for line in response]
            self.assertTrue(records[0]['historyReady'])
            self.assertIsNone(records[0]['extremes'])
            self.assertEqual(records[1:], [{'done': True}])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
