"""Validate raw history semantics and the narrow read-only HTTP surface."""
import importlib.util
import json
import io
import math
import random
import queue
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import tempfile
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

spec = importlib.util.spec_from_file_location('dotusd_server', Path(__file__).resolve().parents[1] / 'server.py')
page = importlib.util.module_from_spec(spec)
spec.loader.exec_module(page)


class PageTests(unittest.TestCase):
    def test_startpoint_uses_blocks_and_live_requests_have_no_client_cutoff(self):
        server = page.CacheServer(('127.0.0.1', 0), page.Handler)
        server.cache = page.HistoryCache()
        now = time.time()
        stamp = now - 10.123456
        rows = [[stamp, .99, 10], [math.nextafter(stamp, math.inf), 1.01, 11],
                [now + 1, 1., 12]]
        for cache in (server.cache, server.coingecko_cache):
            cache.merge(rows, now + 1)
            cache.history_ready = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            for route in ('/api/prices', '/api/coingecko/prices'):
                for encoding in ('raw', 'ranges-v1', 'stream'):
                    query = f'?startpoint=10&windowStart={now-604800}&format={encoding}'
                    with urlopen(base + route + query) as response:
                        self.assertGreaterEqual(float(response.headers['X-History-End']), rows[-1][0])
                        if encoding == 'stream':
                            metadata, batch, done = [json.loads(line) for line in response]
                            self.assertEqual(batch['ranges'], list(page.pack_ranges(rows[1:])))
                            self.assertEqual(done, {'done': True})
                        else:
                            metadata = json.load(response)
                            self.assertEqual(metadata['samples' if encoding == 'raw' else 'ranges'],
                                             rows[1:] if encoding == 'raw' else list(page.pack_ranges(rows[1:])))
                        self.assertEqual(metadata['extremes'], {'low': rows[0], 'high': rows[1]})
                # A rounded timestamp start must not cut off a newer block.
                with urlopen(base + route + f'?startpoint=10&start={now}&end={now+2}&format=raw') as response:
                    self.assertEqual(json.load(response)['samples'], rows[1:])
                with urlopen(base + route + f'?startpoint=11&end={now}&format=raw') as response:
                    self.assertEqual(json.load(response)['samples'], [])
                for value in ('', '-1', '1.5', 'NaN', '9007199254740992', '10&startpoint=11', '10&afterBlock=10'):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(base + route + '?startpoint=' + value)
                    self.assertEqual(error.exception.code, 400)
                    error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_release_version_tracks_code_and_freezes_assets(self):
        names = ('server.py', 'gap_repair.py', 'archive_reader.py', 'hub_quotes.py', 'backfill_quotes.py', 'about-dotusd.html', 'coingecko-details.html', 'index.html', 'app.js', 'ranges.js', 'style.css', 'favicon.svg', 'social-preview.png')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in names:
                (root / name).write_bytes((page.ROOT / name).read_bytes())
            with patch.object(page, 'ROOT', root):
                version, assets = page.load_release()
                self.assertEqual(page.load_release(), (version, assets))
                self.assertIn(version.encode(), assets['app.js'])
                self.assertNotIn(b'__APP_VERSION__', assets['app.js'])
                self.assertIn(f'content="{version}"'.encode(), assets['index.html'])
                self.assertIn(b'<style>' + assets['style.css'] + b'</style>', assets['index.html'])
                self.assertIn(b'<script type="module">' + assets['inline.js'], assets['index.html'])
                self.assertNotIn(b'import ', assets['inline.js'])
                self.assertNotIn(b'__APP_VERSION__', assets['index.html'])
                server = page.CacheServer(('127.0.0.1', 0), page.Handler)
                try:
                    for name in names:
                        original = (root / name).read_bytes()
                        (root / name).write_bytes(original + b'\n')
                        self.assertNotEqual(page.load_release()[0], version, name)
                        self.assertEqual(server.version, version)
                        self.assertEqual(server.assets, assets)
                        (root / name).write_bytes(original)
                finally:
                    server.server_close()

    def test_progressive_stream_batches_and_completion(self):
        server = page.CacheServer(('127.0.0.1', 0), page.Handler)
        server.cache = page.HistoryCache()
        now = int(time.time())
        rows = [[now - 20000 + i * 2, 1 + (i % 3) * .001, i] for i in range(9000)]
        server.cache.merge(rows, now)
        server.cache.history_ready = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(base + f'/api/prices?start={now-20001}&end={now}&windowStart={now-20001}&windowEnd={now}&format=stream') as response:
                self.assertEqual(response.headers['Content-Type'], 'application/x-ndjson')
                messages = [json.loads(line) for line in response]
            self.assertEqual(messages[0]['encoding'], 'ranges-stream-v1')
            self.assertTrue(messages[0]['historyReady'])
            self.assertEqual(messages[0]['extremes']['high'][1], 1.002)
            self.assertEqual(messages[1]['ranges'], list(page.pack_ranges(rows[:8192])))
            self.assertEqual(messages[2]['ranges'], list(page.pack_ranges(rows[8192:])))
            self.assertEqual(messages[3:], [{'done': True}])
            with urlopen(base + f'/api/prices?start={now-1}&end={now}&format=stream') as response:
                messages = [json.loads(line) for line in response]
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[-1], {'done': True})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_explicit_upstream_does_not_open_ssh(self):
        with patch.object(page.subprocess, 'Popen') as launch:
            with page.price_source('http://127.0.0.1:9090/api/v1/') as upstream:
                self.assertEqual(upstream, 'http://127.0.0.1:9090/api/v1')
            launch.assert_not_called()

    def test_live_block_cursor_across_formats_and_reconnects(self):
        server = page.CacheServer(('127.0.0.1', 0), page.Handler)
        server.cache = page.HistoryCache()
        now = int(time.time())
        rows = [[now - 900, .98, 10], [now - 800, 1.03, 11],
                [now - 700, 1.001, 12]]
        server.cache.merge(rows, now)
        server.cache.history_ready = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = (f'http://127.0.0.1:{server.server_port}/api/prices?'
                f'start={now-1000}&end={now}&windowStart={now-1000}&windowEnd={now}')
        try:
            for format in ('raw', 'ranges-v1', 'stream'):
                with self.subTest(format=format):
                    with urlopen(base + f'&format={format}&afterBlock=11') as response:
                        if format == 'stream':
                            metadata, batch, done = [json.loads(line) for line in response]
                            self.assertEqual(batch['ranges'], list(page.pack_ranges(rows[2:])))
                            self.assertEqual(done, {'done': True})
                        else:
                            metadata = json.load(response)
                            key = 'samples' if format == 'raw' else 'ranges'
                            expected = rows[2:] if format == 'raw' else list(page.pack_ranges(rows[2:]))
                            self.assertEqual(metadata[key], expected)
                        self.assertEqual(metadata['extremes'], {'low': rows[0], 'high': rows[1]})
                        self.assertTrue(metadata['historyReady'])
            with urlopen(base + '&format=stream&afterBlock=12') as response:
                messages = [json.loads(line) for line in response]
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[-1], {'done': True})
            # Catch up beyond the former five-minute overlap, using the same cursor.
            newer = [[now - 600, 1, 13], [now - 2, .999, 14]]
            server.cache.merge(newer, now)
            with urlopen(base + '&format=raw&afterBlock=12') as response:
                self.assertEqual(json.load(response)['samples'], newer)
            for cursor in ('', '-1', '1.5', 'NaN', '9007199254740992', '12&afterBlock=13'):
                with self.subTest(cursor=cursor), self.assertRaises(HTTPError) as error:
                    urlopen(base + '&afterBlock=' + cursor)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

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
            # Available even while history is warming; paused tabs use this endpoint.
            with urlopen(base + '/api/version') as response:
                self.assertEqual(json.load(response), {'version': server.version})
                self.assertEqual(response.headers['X-App-Version'], server.version)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
            with urlopen(base) as response:
                self.assertIn(b'Peg deviation', response.read())
                self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
                etag = response.headers['ETag']
                self.assertIn('must-revalidate', response.headers['Cache-Control'])
            with self.assertRaises(HTTPError) as cached:
                urlopen(Request(base, headers={'If-None-Match': etag}))
            self.assertEqual(cached.exception.code, 304)
            cached.exception.close()
            with urlopen(Request(base + '/social-preview.png', headers={'User-Agent': 'Twitterbot/1.0'})) as response:
                self.assertEqual(response.headers['Content-Type'], 'image/png')
                self.assertEqual(response.read(), server.assets['social-preview.png'])
            self.assertIn(b'name="twitter:card" content="summary_large_image"', server.assets['index.html'])
            self.assertIn(b'https://dotusd.info/social-preview.png', server.assets['index.html'])
            for path, code in [('/api/prices?start=nan', 400), ('/../../.envrc', 404), ('/api/v1/query', 404)]:
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + path)
                self.assertEqual(error.exception.code, code)
                error.exception.close()
            with self.assertRaises(HTTPError) as error:
                urlopen(base + '/api/prices')
            self.assertEqual(error.exception.code, 503)
            self.assertEqual(error.exception.headers['X-App-Version'], server.version)
            error.exception.close()
            now = time.time()
            server.cache.merge([[now - 10, 1.001, 50]], now)
            with urlopen(base + '/api/prices') as response:
                packed = json.load(response)
                self.assertEqual(packed['encoding'], 'ranges-v1')
                self.assertEqual(packed['ranges'], [[now - 10, 50, 1.001]])
                self.assertNotIn('samples', packed)
            with urlopen(base + '/ranges.js') as response:
                self.assertIn(b'export class RangeSeries', response.read())
            # There is no upstream client on the server. Concurrent reads still work.
            def read():
                with urlopen(base + '/api/prices?format=raw') as response:
                    self.assertEqual(response.headers['X-App-Version'], server.version)
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
    def test_partial_range_summaries_match_uncached_results(self):
        rng = random.Random(42)
        now = time.time()
        rows = [(now - 3000 + i * 2, rng.choice([.98, 1, 1.03]),
                 None if i % 17 == 0 else rng.randrange(2000)) for i in range(1300)]
        cache = page.HistoryCache()
        cache.merge(rows, now)
        summaries = cache.summaries.chunks
        self.assertEqual(len(summaries), math.ceil(len(rows) / page.SUMMARY_SAMPLES))
        bounds = [(0, 1300), (0, 256), (256, 512), (255, 257), (1299, 1300), (0, 0)]
        bounds += [sorted((rng.randrange(1301), rng.randrange(1301))) for _ in range(200)]
        for left, right in bounds:
            start, end = now - 3001 + left * 2, now - 3001 + right * 2
            window_rows = [row for row in rows if start < row[0] <= end]
            expected = ({'low': min(window_rows, key=lambda row: row[1]),
                         'high': max(window_rows, key=lambda row: row[1])} if window_rows else None)
            for cursor in (None, 0, 1000, 2000):
                with self.subTest(left=left, right=right, cursor=cursor):
                    result, _, _, extremes = cache.read(start, end, (start, end), cursor)
                    filtered = (window_rows if cursor is None else
                                [row for row in window_rows if row[2] is not None and row[2] > cursor])
                    self.assertEqual(result, filtered)
                    self.assertEqual(extremes, expected)
        # Arbitrary query windows never add cache entries.
        self.assertIs(cache.summaries.chunks, summaries)

    def test_summaries_replace_corrected_data_and_expire_with_history(self):
        cache = page.HistoryCache()
        now = time.time()
        with patch.object(page, 'MAX_SAMPLES', 520):
            cache.merge([(now - 1200 + i, 1, i) for i in range(1000)], now)
        self.assertEqual(len(cache.rows), 520)
        self.assertEqual(len(cache.summaries.chunks), 3)
        stamp = cache.rows[256][0]
        cache.merge([(stamp, .5, 2000)], now, mark_fresh=False)
        rows, _, _, extremes = cache.read(now - 1201, now, (now - 1201, now), 1500)
        self.assertEqual(rows, [(stamp, .5, 2000)])
        self.assertEqual(extremes['low'], rows[0])
        cache.merge([(stamp, 1, None)], now, mark_fresh=False)
        rows, _, _, extremes = cache.read(now - 1201, now, (now - 1201, now), 1500)
        self.assertEqual(rows, [])
        self.assertEqual(extremes['low'][1], 1)
        cache.merge([], now + page.HISTORY_SECONDS)
        self.assertEqual(cache.rows, ())
        self.assertEqual(cache.summaries.chunks, ())

    def test_slow_reader_does_not_block_readers_or_publication(self):
        cache = page.HistoryCache()
        now = time.time()
        original = (now - 10, 1, 50)
        replacement = (now - 10, 1.02, 50)
        cache.merge([original], now)
        entered, release = threading.Event(), threading.Event()
        bisect = page.bisect_right
        slow_thread = None

        def slow_bisect(*args, **kwargs):
            if threading.get_ident() == slow_thread:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('Reader was not released')
            return bisect(*args, **kwargs)

        def slow_read():
            nonlocal slow_thread
            slow_thread = threading.get_ident()
            return cache.read(now - 20, now, (now - 20, now))

        with ThreadPoolExecutor(max_workers=3) as pool, patch.object(page, 'bisect_right', slow_bisect):
            first = pool.submit(slow_read)
            try:
                self.assertTrue(entered.wait(2))
                second = pool.submit(cache.read, now - 20, now).result(timeout=2)
                self.assertEqual(second[0], [original])
                pool.submit(cache.merge, [replacement], now + 1).result(timeout=2)
                current = cache.read(now - 20, now)
                self.assertEqual(current[:2], ([replacement], now + 1))
            finally:
                release.set()
            # A reader must not mix old rows/extrema with a newly published timestamp.
            rows, fetched_at, _, extremes = first.result(timeout=2)
            self.assertEqual((rows, fetched_at), ([original], now))
            self.assertEqual(extremes, {'low': original, 'high': original})

    def test_merge_build_does_not_block_readers_or_lose_another_writer(self):
        cache = page.HistoryCache()
        now = time.time()
        original = (now - 10, 1, 50)
        additions = [(now - 8, 1.01, 51), (now - 6, .99, 52)]
        cache.merge([original], now)
        entered, release = threading.Event(), threading.Event()

        def slow_rows():
            entered.set()
            if not release.wait(5):
                raise TimeoutError('Writer was not released')
            yield additions[0]

        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(cache.merge, slow_rows(), now + 1)
            try:
                self.assertTrue(entered.wait(2))
                second = pool.submit(cache.merge, additions[1:], now + 2)
                rows = pool.submit(cache.read, now - 20, now).result(timeout=2)
                self.assertEqual(rows[:2], ([original], now))
            finally:
                release.set()
            first.result(timeout=2)
            second.result(timeout=2)
        self.assertEqual(cache.read(now - 20, now)[:2], ([original, *additions], now + 2))

    def test_merge_overlap_retention_and_range_boundaries(self):
        cache = page.HistoryCache()
        now = time.time()
        cache.merge([[now - 604801, 1, 1], [now - 20, 1, 2], [now - 10, 1, 4]], now)
        cache.merge([[now - 10, 1.01, 4], [now - 5, .99, 5]], now)
        rows, _, _, _ = cache.read(now - 20, now - 5)
        self.assertEqual(rows, [(now - 10, 1.01, 4), (now - 5, .99, 5)])
        self.assertEqual(len(cache.rows), 3)
        with patch.object(page, 'MAX_SAMPLES', 2):
            cache.merge([], now)
        self.assertEqual(len(cache.rows), 2)
        with patch.object(page.time, 'time', return_value=now + 604800):
            self.assertEqual(cache.read(0, now + 604800)[0], [])

    def test_chunk_extremes_cover_selected_window_not_just_chunk(self):
        now = time.time()
        cache = page.HistoryCache()
        cache.merge([[now-100, .98, 1], [now-80, 1.03, 2],
                     [now-10, 1.001, 3]], now)
        rows, _, _, extremes = cache.read(now-20, now, (now-200, now))
        self.assertEqual([r[2] for r in rows], [3])
        self.assertEqual(extremes['low'][2], 1)
        self.assertEqual(extremes['high'][2], 2)
        self.assertIsNone(cache.read(now-20, now, (now-300, now-200))[3])
        self.assertEqual(cache.read(now-20, now, (now-80, now))[3]['high'][2], 3)

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

    def test_delayed_collector_publication_recovers_beyond_overlap(self):
        now = time.time()
        cache = page.HistoryCache()
        cache.merge([[now, 1.0, 50]], now)
        http = Mock()
        worker = page.HistoryWorker(cache, 'http://unused', http)
        worker.cursor = now
        worker.backfill_end = now - page.HISTORY_SECONDS

        def response(rows):
            return io.BytesIO(json.dumps({'status': 'success', 'data': {
                'resultType': 'matrix', 'result': [
                    {'metric': {'__name__': metric},
                     'values': [[row[0], str(row[column])] for row in rows]}
                    for metric, column in [('arb_peg_dotusd_usdt', 1),
                                           ('arb_peg_block_number', 2)]
                ]}}).encode())

        # Queries succeed throughout an eight-minute collection outage.
        for elapsed in (6, 240, 480):
            http.open.return_value = response([[now, 1.0, 50]])
            with patch.object(page.time, 'time', return_value=now + elapsed):
                worker.poll()
        recovered = [[now + i * 2, 1.01, 50 + i] for i in range(1, 244)]
        http.open.return_value = response(recovered)
        with patch.object(page.time, 'time', return_value=now + 486):
            worker.poll()
        query = parse_qs(urlsplit(http.open.call_args.args[0]).query)
        self.assertIn('[786000ms]', query['query'][0])
        self.assertEqual([row[2] for row in cache.rows], list(range(50, 294)))

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


class RequestQueueTests(unittest.TestCase):
    def setUp(self):
        self.release = threading.Event()
        self.server = None

    def tearDown(self):
        self.release.set()
        if self.server:
            self.server.server_close()

    def make_server(self, handler, **limits):
        server_type = type('TestServer', (page.CacheServer,), limits)
        self.server = server_type(('127.0.0.1', 0), handler)
        return self.server

    def test_four_active_workers_and_bounded_waiters(self):
        started = queue.Queue()
        active, peak = 0, 0
        lock = threading.Lock()

        def handler(request, address, server):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            started.put(request)
            self.release.wait(5)
            with lock:
                active -= 1

        server = self.make_server(handler)
        for _ in range(4):
            server.process_request(Mock(), ('127.0.0.1', 1234))
            started.get(timeout=2)
        pending = [Mock() for _ in range(32)]
        for request in pending:
            server.process_request(request, ('127.0.0.1', 1234))
            request.sendall.assert_not_called()
        with server.work_ready:
            self.assertEqual(len(server.pending), 32)
        overflow = Mock()
        server.process_request(overflow, ('127.0.0.1', 1234))
        self.assertIn(b'503 Service Unavailable', overflow.sendall.call_args.args[0])
        overflow.close.assert_called_once()
        self.release.set()
        received = [started.get(timeout=2) for _ in pending]
        self.assertCountEqual(received, pending)
        self.assertEqual(peak, 4)
        self.assertEqual(len(server.workers), 4)

    def test_waiters_are_served_in_fifo_order(self):
        started = queue.Queue()

        def handler(request, address, server):
            started.put(request)
            self.release.wait(5)

        server = self.make_server(handler, handler_count=1)
        requests = [Mock() for _ in range(4)]
        server.process_request(requests[0], ('127.0.0.1', 1234))
        self.assertIs(started.get(timeout=2), requests[0])
        for request in requests[1:]:
            server.process_request(request, ('127.0.0.1', 1234))
        self.release.set()
        self.assertEqual([started.get(timeout=2) for _ in range(3)], requests[1:])

    def test_waiters_expire_even_when_every_worker_is_busy(self):
        started = threading.Event()

        def handler(request, address, server):
            started.set()
            self.release.wait(5)

        server = self.make_server(handler, handler_count=1)
        server.process_request(Mock(), ('127.0.0.1', 1234))
        self.assertTrue(started.wait(2))
        request = Mock()
        with patch.object(page.time, 'monotonic', return_value=100):
            server.process_request(request, ('127.0.0.1', 1234))
        with patch.object(page.time, 'monotonic', return_value=101):
            server.service_actions()
        request.sendall.assert_not_called()
        with patch.object(page.time, 'monotonic', return_value=102):
            server.service_actions()
        self.assertIn(b'503 Service Unavailable', request.sendall.call_args.args[0])
        request.close.assert_called_once()
        self.assertFalse(server.pending)

    def test_handler_failure_does_not_remove_a_worker(self):
        finished = threading.Event()
        broken = Mock()

        def handler(request, address, server):
            if request is broken:
                raise ValueError('bad request')
            finished.set()

        server = self.make_server(handler, handler_count=1)
        server.handle_error = Mock()
        server.process_request(broken, ('127.0.0.1', 1234))
        server.process_request(Mock(), ('127.0.0.1', 1234))
        self.assertTrue(finished.wait(2))
        server.handle_error.assert_called_once()
        broken.close.assert_called_once()
        self.assertTrue(server.workers[0].is_alive())

    def test_shutdown_closes_waiters_and_joins_workers(self):
        started, closed = threading.Event(), threading.Event()

        def handler(request, address, server):
            started.set()
            self.release.wait(5)

        server = self.make_server(handler, handler_count=1)
        server.process_request(Mock(), ('127.0.0.1', 1234))
        self.assertTrue(started.wait(2))
        waiting = Mock()
        waiting.close.side_effect = closed.set
        server.process_request(waiting, ('127.0.0.1', 1234))
        with ThreadPoolExecutor(max_workers=1) as pool:
            closing = pool.submit(server.server_close)
            try:
                self.assertTrue(closed.wait(2))
                waiting.close.assert_called_once()
            finally:
                self.release.set()
            closing.result(timeout=2)
        self.assertTrue(all(not worker.is_alive() for worker in server.workers))


if __name__ == '__main__':
    unittest.main()
