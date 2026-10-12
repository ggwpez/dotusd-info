"""CoinGecko ingestion and shared HTTP/rendering contract, without network calls."""
import io
import json
import threading
import time
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import urlopen

from test_server import page


class CoinGeckoTests(unittest.TestCase):
    def test_fetch_bounds_and_keeps_api_key_on_coingecko_only(self):
        http = Mock()
        worker = page.CoinGeckoWorker(page.HistoryCache(), http=http, api_key='test-key')
        for url in (worker.DOT_URL, worker.DEX_URL):
            http.open.return_value = io.BytesIO(b'{}')
            self.assertEqual(worker.fetch(url), {})
            request = http.open.call_args.args[0]
            self.assertEqual(request.get_header('X-cg-demo-api-key'), 'test-key' if url == worker.DOT_URL else None)
        http.open.return_value = io.BytesIO(b' ' * (page.MAX_UPSTREAM_BYTES + 1))
        with self.assertRaisesRegex(ValueError, 'size limit'):
            worker.fetch(worker.DOT_URL)

    def test_quote_orientation_persistence_and_failed_refresh(self):
        now = 1800000000
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            cache = page.HistoryCache()
            worker = page.CoinGeckoWorker(cache, history_path=path)
            reference = {'polkadot': {'usd': 2.02, 'last_updated_at': now - 60}}
            ticker = {'ticker_id': 'DOT_7873', 'base_currency': 'DOT', 'target_currency': '7873', 'last_price': '2'}
            worker.fetch = Mock(side_effect=[reference, [ticker]])
            with patch.object(page.time, 'time', return_value=now):
                worker.poll()
            self.assertEqual(cache.rows, ((now, 1.01, None),))
            self.assertTrue(cache.history_ready)
            loaded = page.HistoryCache()
            with patch.object(page.time, 'time', return_value=now + 300):
                page.CoinGeckoWorker(loaded, history_path=path).load_history()
            self.assertEqual(loaded.rows, cache.rows)
            self.assertEqual(loaded.fetched_at, now)  # restarting does not refresh old quotes
            before = cache.rows, cache.fetched_at, path.read_bytes()
            for response in ([], [ticker, ticker], [dict(ticker, last_price='NaN')],
                             [dict(ticker, last_price='0')], [dict(ticker, last_price=True)],
                             [dict(ticker, target_currency='1984')]):
                worker.fetch = Mock(side_effect=[reference, response])
                with self.assertRaises(ValueError), patch.object(page.time, 'time', return_value=now+300):
                    worker.poll()
                self.assertEqual((cache.rows, cache.fetched_at, path.read_bytes()), before)
            worker.fetch = Mock(side_effect=[reference, [ticker]])
            with self.assertRaisesRegex(ValueError,'stale'), patch.object(page.time, 'time', return_value=now+1000):
                worker.poll()
            self.assertEqual((cache.rows, cache.fetched_at, path.read_bytes()), before)

    def test_history_is_observed_and_trimmed_not_fabricated(self):
        now = 1800000000
        cache = page.HistoryCache()
        cache.merge([(now-604799, .99, None)], now-300)
        worker = page.CoinGeckoWorker(cache)
        worker.fetch = Mock(side_effect=[{'polkadot': {'usd': 1, 'last_updated_at': now+300}},
            [{'ticker_id': 'DOT_7873', 'base_currency': 'DOT', 'target_currency': '7873', 'last_price': '1'}]])
        with patch.object(page.time, 'time', return_value=now+300):
            worker.poll()
        self.assertEqual(cache.rows, ((now+300, 1., None),))

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
