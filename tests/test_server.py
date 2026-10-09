"""Validate raw history semantics and the narrow read-only HTTP surface."""
import importlib.util
import json
import io
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError
from urllib.parse import parse_qs
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

spec = importlib.util.spec_from_file_location('dotusd_server', Path(__file__).resolve().parents[1] / 'server.py')
page = importlib.util.module_from_spec(spec)
spec.loader.exec_module(page)


class PageTests(unittest.TestCase):
    def test_explicit_upstream_does_not_open_ssh(self):
        with patch.object(page.subprocess, 'Popen') as launch:
            with page.price_source('http://127.0.0.1:9090/api/v1/') as upstream:
                self.assertEqual(upstream, 'http://127.0.0.1:9090/api/v1')
            launch.assert_not_called()

    def test_failed_ssh_exits_without_serving_an_empty_page(self):
        with patch.object(page.subprocess, 'Popen') as launch:
            launch.return_value.poll.return_value = 255
            launch.return_value.returncode = 255
            with self.assertRaisesRegex(RuntimeError, 'SSH tunnel exited'):
                with page.price_source():
                    self.fail('A failed SSH tunnel must not be used')

    def test_owned_tunnel_closes_after_use(self):
        with patch.object(page.subprocess, 'Popen') as launch, patch.object(page.urllib.request, 'build_opener') as opener:
            launch.return_value.poll.return_value = None
            opener.return_value.open.return_value.__enter__.return_value.status = 200
            with page.price_source() as upstream:
                self.assertTrue(upstream.endswith('/api/v1'))
                self.assertIn('ExitOnForwardFailure=yes', launch.call_args.args[0])
            launch.return_value.terminate.assert_called_once()
            launch.return_value.wait.assert_called_once_with(timeout=5)

    def test_raw_samples_and_bounded_window(self):
        query = parse_qs(page.query_window({'start': ['100'], 'end': ['200']}))
        self.assertEqual(query['query'], ['{__name__=~"arb_peg_dotusd_usdt|arb_peg_block_number",clock="estimated_2s"}[100000ms]'])
        for start, end in [('nan', '200'), ('0', 'inf'), ('200', '100'), ('0', '604801')]:
            with self.assertRaises(ValueError):
                page.query_window({'start': [start], 'end': [end]})

    def test_gaps_and_original_timestamps_preserved(self):
        payload = {'status': 'success', 'data': {'resultType': 'matrix', 'result': [{'metric': {'__name__': 'arb_peg_dotusd_usdt'}, 'values': [[100, '1.001'], [102, 'NaN'], [200, '.999']]}, {'metric': {'__name__': 'arb_peg_block_number'}, 'values': [[100, '50'], [102, '51'], [200, '52']]}]}}
        self.assertEqual(page.samples(payload), [[100, 1.001, 50], [200, .999, 52]])
        payload['data']['result'] *= 2
        with self.assertRaises(ValueError):
            page.samples(payload)

    def test_http_assets_errors_and_no_arbitrary_proxy(self):
        server = page.CacheServer(('127.0.0.1', 0), page.Handler)
        server.cache = page.HistoryCache()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(base) as response:
                self.assertIn(b'Peg deviation', response.read())
                self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
                etag = response.headers['ETag']
                self.assertIn('must-revalidate', response.headers['Cache-Control'])
            with self.assertRaises(HTTPError) as cached:
                urlopen(Request(base, headers={'If-None-Match': etag}))
            self.assertEqual(cached.exception.code, 304)
            cached.exception.close()
            for path, code in [('/api/prices?start=nan', 400), ('/../../.envrc', 404), ('/api/v1/query', 404)]:
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + path)
                self.assertEqual(error.exception.code, code)
                error.exception.close()
            with self.assertRaises(HTTPError) as error:
                urlopen(base + '/api/prices')
            self.assertEqual(error.exception.code, 503)
            error.exception.close()
            now = time.time()
            server.cache.merge([[now - 10, 1.001, 50]], now)
            # There is no upstream client on the server. Concurrent reads still work.
            def read():
                with urlopen(base + '/api/prices') as response:
                    return json.load(response)
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(lambda _: read(), range(12)))
            for result in results:
                self.assertEqual(result['samples'], [[now - 10, 1.001, 50]])
                self.assertEqual(result['fetchedAt'], now)
                self.assertFalse(result['historyReady'])
            server.cache.history_ready = True
            self.assertTrue(read()['historyReady'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class CacheTests(unittest.TestCase):
    def test_merge_overlap_retention_and_range_boundaries(self):
        cache = page.HistoryCache()
        now = time.time()
        cache.merge([[now - 604801, 1, 1], [now - 20, 1, 2], [now - 10, 1, 4]], now)
        cache.merge([[now - 10, 1.01, 4], [now - 5, .99, 5]], now)
        rows, _, _ = cache.read(now - 20, now - 5)
        self.assertEqual(rows, [(now - 10, 1.01, 4), (now - 5, .99, 5)])
        self.assertEqual(len(cache.rows), 3)
        with patch.object(page, 'MAX_SAMPLES', 2):
            cache.merge([], now)
        self.assertEqual(len(cache.rows), 2)
        with patch.object(page.time, 'time', return_value=now + 604800):
            self.assertEqual(cache.read(0, now + 604800)[0], [])

    def test_worker_seeds_then_only_refreshes_incrementally(self):
        cache = page.HistoryCache()
        worker = page.HistoryWorker(cache, 'http://unused')
        worker.fetch = Mock()
        now = time.time()
        with patch.object(page.time, 'time', return_value=now):
            worker.poll()
        self.assertEqual(worker.fetch.call_args_list[0].args, (now - 3600, now))
        self.assertEqual(worker.fetch.call_args_list[1].args,
                         (now - 3600 - page.FETCH_SECONDS, now - 3600))
        with patch.object(page.time, 'time', return_value=now + 6):
            worker.poll()
        self.assertEqual(worker.fetch.call_args_list[2].args, (now - 300, now + 6))
        # Seeding stops once the retained window is covered.
        for _ in range(30):
            with patch.object(page.time, 'time', return_value=now + 12):
                worker.poll()
        worker.fetch.reset_mock()
        with patch.object(page.time, 'time', return_value=now + 18):
            worker.poll()
        worker.fetch.assert_called_once_with(now + 12 - 300, now + 18)

    def test_failure_keeps_cached_data_and_retry_cursor(self):
        now = time.time()
        cache = page.HistoryCache()
        cache.merge([[now - 1, 1.01, 50]], now)
        worker = page.HistoryWorker(cache, 'http://unused')
        worker.cursor = now
        worker.backfill_end = now - page.HISTORY_SECONDS
        worker.fetch = Mock(side_effect=OSError('offline'))
        with patch.object(page.time, 'time', return_value=now + 6):
            with self.assertRaises(OSError):
                worker.poll()
        self.assertEqual(worker.cursor, now)
        self.assertEqual(cache.read(now - 20, now)[0], [(now - 1, 1.01, 50)])

    def test_startup_drains_history_without_live_poll_delays(self):
        cache = page.HistoryCache()
        worker = page.HistoryWorker(cache, 'http://unused')
        worker.fetch = Mock()
        waits = []
        def wait(delay):
            waits.append(delay)
            if delay == 6:
                worker.stop.set()
        with patch.object(worker.stop, 'wait', side_effect=wait):
            worker.run()
        self.assertTrue(cache.history_ready)
        self.assertTrue(all(delay == 0.05 for delay in waits[:-1]))
        self.assertEqual(waits[-1], 6)
        self.assertLess(sum(waits[:-1]), 2)
        # One recent request, then 28 historical pages; no repeated live requests.
        self.assertEqual(worker.fetch.call_count, 29)

    def test_upstream_response_limit(self):
        http = Mock()
        http.open.return_value = io.BytesIO(b'x' * 101)
        cache = page.HistoryCache()
        worker = page.HistoryWorker(cache, 'http://unused', http)
        with patch.object(page, 'MAX_UPSTREAM_BYTES', 100):
            with self.assertRaisesRegex(ValueError, 'size limit'):
                worker.fetch(100, 200)
        self.assertIsNone(cache.fetched_at)

    def test_worker_fetch_preserves_raw_samples(self):
        now = time.time()
        payload = {'status': 'success', 'data': {'resultType': 'matrix', 'result': [
            {'metric': {'__name__': 'arb_peg_dotusd_usdt'}, 'values': [[now - 10, '1.01']]},
            {'metric': {'__name__': 'arb_peg_block_number'}, 'values': [[now - 10, '42']]}]}}
        http = Mock()
        http.open.return_value = io.BytesIO(json.dumps(payload).encode())
        cache = page.HistoryCache()
        worker = page.HistoryWorker(cache, 'http://unused', http)
        worker.fetch(now - 100, now)
        self.assertEqual(cache.read(now - 100, now)[0], [(now - 10, 1.01, 42)])

    def test_busy_server_rejects_without_starting_handler(self):
        server = page.CacheServer(('127.0.0.1', 0), page.Handler)
        connection = Mock()
        try:
            for _ in range(4):
                self.assertTrue(server.slots.acquire(False))
            server.process_request(connection, ('127.0.0.1', 1234))
            self.assertIn(b'503 Service Unavailable', connection.sendall.call_args.args[0])
            connection.close.assert_called_once()
        finally:
            server.server_close()


if __name__ == '__main__':
    unittest.main()
