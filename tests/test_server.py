"""Validate raw history semantics and the narrow read-only HTTP surface."""
import importlib.util
import json
import io
import math
from itertools import permutations
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
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

spec = importlib.util.spec_from_file_location('dotusd_server', Path(__file__).resolve().parents[1] / 'server.py')
page = importlib.util.module_from_spec(spec)
spec.loader.exec_module(page)


def unpack_ranges(records):
    if not records:
        return []
    stamp, block, value = records[0]
    rows = [[stamp, value, block]]
    for record in records[1:]:
        count, dt, db = record[:3]
        if count == 0:
            stamp, block, value = dt, db, record[3]
            rows.append([stamp, value, block])
            continue
        if len(record) == 4:
            value = record[3]
        rows.extend([stamp + i * dt, value, block + i * db] for i in range(1, count + 1))
        stamp += count * dt
        block += count * db
    return rows


def decode_history(body):
    for name in ('ti', 'treasury'):
        history = body.get(name)
        if history is not None and 'ranges' in history:
            assert history.pop('encoding') == 'ranges-v1'
            history['rows'] = unpack_ranges(history.pop('ranges'))
    return body


def decode_stream(lines):
    messages = [json.loads(line) for line in lines]
    metadata = messages[0]
    assert metadata['encoding'] == 'ranges-stream-v2'
    for name in ('ti', 'treasury'):
        if name in metadata:
            assert 'rows' not in metadata[name]
            metadata[name]['rows'] = []
    output = [metadata]
    for message in messages[1:]:
        if 'series' in message:
            metadata[message['series']]['rows'].extend(unpack_ranges(message['ranges']))
        else:
            output.append(message)
    return output


class PageTests(unittest.TestCase):
    def test_large_state_history_streams_bounded_chunks_without_losing_rows(self):
        rows = [[1_700_000_000 + n // 2, 123.45 if n < 3000 else 125.67, n] for n in range(4097)]
        rows[2001][0] += .125
        histories = {
            name: {'rows': rows, 'cursor': 'instance:4097', 'reset': True, 'error': None}
            for name in ('ti', 'treasury')
        }
        for streaming in (False, True):
            for include_price in (False, True):
                with self.subTest(streaming=streaming, include_price=include_price):
                    price = (page.encode_response(rows[:1], 1_700_000_001, True, None,
                                                  raw=True, streaming=streaming)
                             if include_price else None)
                    chunks = list(page.historic_response(price, histories, streaming))
                    # A large state response must not require a whole-history
                    # encoded buffer before the first network write.
                    self.assertLess(max(map(len, chunks)), 64 * 1024)
                    encoded = b''.join(chunks)
                    if streaming:
                        wire = encoded.splitlines()
                        header = json.loads(wire[0])
                        self.assertLess(len(wire[0]), 1024)
                        self.assertNotIn('rows', header['ti'])
                        records = decode_stream(wire)
                        result = records[0]
                        self.assertEqual(records[-1], {'done': True})
                    else:
                        result = decode_history(json.loads(encoded))
                    for name in histories:
                        self.assertEqual(result[name], histories[name])
                    if include_price and not streaming:
                        self.assertEqual(result['samples'], rows[:1])

    def test_historic_serves_only_prices_even_with_saved_state(self):
        server = page.CacheServer(('127.0.0.1', 0), page.Handler)
        server.cache = page.HistoryCache()
        now = time.time()
        prices = [[now - 20, 1.01, 40]]
        server.cache.merge(prices, now)
        server.cache.history_ready = True
        directory = tempfile.TemporaryDirectory()
        server.issuance = page.IssuanceCache(Path(directory.name) / 'issuance.db')
        rows = [[now - 50, 100., 10], [now - 30, 120., 30], [now - 10, 140., 50]]
        server.issuance.merge(rows, live=True)
        server.treasury = page.TreasuryHistory(Path(directory.name) / 'issuance.db')
        treasury_rows = [[now - 45, 0., 15], [now - 25, 60., 35], [now - 5, 70., 55]]
        server.treasury.merge(treasury_rows)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}/api/historic?'
        bounds = (f'&start={now-60}&end={now}&tiStart={now-50}&tiEnd={now-10}'
                  f'&treasuryStart={now-45}&treasuryEnd={now-5}')
        combinations = [','.join(names) for length in (1, 2, 3)
                        for names in permutations(('price', 'ti', 'treasury'), length)]
        try:
            for series in combinations:
                for encoding in ('raw', 'ranges-v1', 'stream'):
                    with self.subTest(series=series, encoding=encoding):
                        route = base + f'series={series}&format={encoding}' + bounds
                        if 'price' not in series.split(','):
                            with self.assertRaises(HTTPError) as error:
                                urlopen(route)
                            self.assertEqual(error.exception.code, 400)
                            self.assertIn('series=price', json.load(error.exception)['error'])
                            error.exception.close()
                            continue
                        with urlopen(route) as response:
                            if encoding == 'stream':
                                messages = decode_stream(response)
                                body = messages[0]
                                self.assertEqual(messages[-1], {'done': True})
                                self.assertEqual(messages[1:-1], [{'ranges': list(page.pack_ranges(prices))}])
                            else:
                                body = decode_history(json.load(response))
                                self.assertEqual(body['samples' if encoding == 'raw' else 'ranges'],
                                                 prices if encoding == 'raw' else list(page.pack_ranges(prices)))
                            self.assertTrue(body['historyReady'])
                            self.assertEqual(body['fetchedAt'], now)
                            self.assertNotIn('ti', body)
                            self.assertNotIn('treasury', body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            directory.cleanup()

    def test_health_reports_finalized_head_age_and_fails_when_stale(self):
        server = page.CacheServer(('127.0.0.1', 0), page.Handler)
        server.cache = page.HistoryCache()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def health():
            try:
                with urlopen(f'http://127.0.0.1:{server.server_port}/health') as response:
                    return response.status, response.headers['Cache-Control'], json.load(response)
            except HTTPError as error:
                with error:
                    return error.code, error.headers['Cache-Control'], json.load(error)
        now = 2_000_000_000
        try:
            with patch.object(page.time, 'time', return_value=now):
                self.assertEqual(health(), (503, 'no-store', {
                    'status': 'starting', 'finalizedBlock': None,
                    'finalizedAgeSeconds': None, 'staleAfterSeconds': 180}))
                for age, code, status in ((61.24, 200, 'ok'), (180, 200, 'ok'),
                                          (180.04, 503, 'stale'), (-3, 200, 'ok')):
                    with self.subTest(age=age):
                        server.cache.finalized = (22_326_346, now - age)
                        self.assertEqual(health(), (code, 'no-store', {
                            'status': status, 'finalizedBlock': 22_326_346,
                            'finalizedAgeSeconds': round(max(0, age), 1), 'staleAfterSeconds': 180}))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_historic_requires_valid_series_and_removes_old_endpoints(self):
        server = page.CacheServer(('127.0.0.1', 0), page.Handler)
        server.cache = page.HistoryCache()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        now = time.time()
        invalid = ('', 'series=', 'series=issuance', 'series=price,unknown',
                   'series=price,price', 'series=price&series=ti', 'series=price,',
                   'series=ti&tiStart=nan', 'series=ti&tiStart=0&tiEnd=604801',
                   f'series=ti&tiStart={now}&tiEnd={now-1}',
                   f'series=ti&tiStart={now-10}&tiStart={now-20}',
                   f'series=ti&tiStart={now-10}&tiEnd={now}&tiEnd={now}',
                   f'series=ti&tiStart={now-10}&tiCursor=a&tiCursor=b',
                   'series=treasury,treasury', 'series=price,ti,treasury,unknown',
                   'series=treasury&treasuryStart=nan',
                   'series=treasury&treasuryStart=0&treasuryEnd=604801',
                   f'series=treasury&treasuryStart={now}&treasuryEnd={now-1}',
                   f'series=treasury&treasuryStart={now-10}&treasuryStart={now-20}',
                   f'series=treasury&treasuryStart={now-10}&treasuryEnd={now}&treasuryEnd={now}',
                   f'series=treasury&treasuryCursor=a&treasuryCursor=b')
        try:
            for query in invalid:
                with self.subTest(query=query), self.assertRaises(HTTPError) as error:
                    urlopen(base + '/api/historic?' + query)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
            for removed in ('/api/prices', '/api/issuance'):
                with self.subTest(removed=removed), self.assertRaises(HTTPError) as error:
                    urlopen(base + removed)
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_disabled_state_does_not_read_storage_or_bypass_price_warmup(self):
        server = page.CacheServer(('127.0.0.1', 0), page.Handler)
        server.cache = page.HistoryCache()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}/api/historic?'
        try:
            with patch.object(server.issuance, 'selection', side_effect=AssertionError('TI read')), \
                    patch.object(server.treasury, 'selection', side_effect=AssertionError('Treasury read')):
                for series, status in (('price', 503), ('ti', 400), ('treasury', 400),
                                       ('price,ti,treasury', 503)):
                    with self.subTest(series=series), self.assertRaises(HTTPError) as error:
                        urlopen(base + f'series={series}')
                    self.assertEqual(error.exception.code, status)
                    error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_startpoint_uses_blocks_and_live_requests_have_no_client_cutoff(self):
        server = page.CacheServer(('127.0.0.1', 0), page.Handler)
        server.cache = page.HistoryCache()
        now = time.time()
        stamp = now - 10.123456
        rows = [[stamp, .99, 10], [math.nextafter(stamp, math.inf), 1.01, 11],
                [now + 1, 1., 12]]
        server.cache.merge(rows, now + 1)
        server.cache.history_ready = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            route = '/api/historic'
            for encoding in ('raw', 'ranges-v1', 'stream'):
                query = f'?series=price&startpoint=10&windowStart={now-604800}&format={encoding}'
                with urlopen(base + route + query) as response:
                    self.assertGreaterEqual(float(response.headers['X-History-End']), rows[-1][0])
                    if encoding == 'stream':
                        metadata, batch, done = decode_stream(response)
                        self.assertEqual(batch['ranges'], list(page.pack_ranges(rows[1:])))
                        self.assertEqual(done, {'done': True})
                    else:
                        metadata = decode_history(json.load(response))
                        self.assertEqual(metadata['samples' if encoding == 'raw' else 'ranges'],
                                         rows[1:] if encoding == 'raw' else list(page.pack_ranges(rows[1:])))
                    self.assertEqual(metadata['extremes'], {'low': rows[0], 'high': rows[1]})
            # A rounded timestamp start must not cut off a newer block.
            with urlopen(base + route + f'?series=price&startpoint=10&start={now}&end={now+2}&format=raw') as response:
                self.assertEqual(decode_history(json.load(response))['samples'], rows[1:])
            with urlopen(base + route + f'?series=price&startpoint=11&end={now}&format=raw') as response:
                self.assertEqual(decode_history(json.load(response))['samples'], [])
            for value in ('', '-1', '1.5', 'NaN', '9007199254740992', '10&startpoint=11', '10&afterBlock=10'):
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + route + '?series=price&startpoint=' + value)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
            for removed in ('/coingecko', '/coingecko/', '/api/coingecko/prices'):
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + removed)
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_changelog_lists_latest_ten_entries_and_rejects_malformed_sections(self):
        text = '# Changelog\n\nFree text.\n\n' + ''.join(
            f'## 1.{n}.0 - 2026-10-{10 + n:02d}\n\n- Change {n}a <b>\n- Change {n}b\n  continued\n\n'
            for n in range(8, 0, -1))
        releases = page.parse_changelog(text)
        self.assertEqual([r['version'] for r in releases][:2], ['1.8.0', '1.7.0'])
        self.assertEqual(releases[0]['changes'], ['Change 8a <b>', 'Change 8b continued'])
        menu = page.render_changelog(releases)
        self.assertIn('data-latest="1.8.0"', menu)
        self.assertEqual(menu.count('<li>'), 10)
        self.assertIn('Change 8a &lt;b&gt;', menu)
        self.assertIn('Change 4b', menu)
        self.assertNotIn('Change 3a', menu)
        self.assertIn('Oct 18, 2026', menu)
        self.assertEqual(page.render_changelog(page.parse_changelog('# Changelog\n')), '')
        for bad in ('## 1.0.0 - 2026-10-01\n',
                    '## 1.0.0 - 2026-02-30\n- x\n',
                    '## 1.0.0 2026-10-01\n- x\n',
                    '## 1.0.0 - 2026-10-01\n- x\n## 1.1.0 - 2026-09-01\n- y\n',
                    '## 1.1.0 - 2026-10-01\n- x\n## 1.0.0 - 2026-10-02\n- y\n',
                    '## 1.1.0 - 2026-10-01\n- x\n## 1.0.0 - 2026-09-01\n\n## 0.9.0 - 2026-09-01\n- y\n',
                    '## 1.0.0 - 2026-10-01\nloose text\n'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                page.parse_changelog(bad)

    def test_release_version_tracks_code_and_freezes_assets(self):
        names = (*page.read_sources().keys(), 'VERSION')
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
                        (root / name).write_bytes(
                            b'999.0.0+' + original.split(b'+')[1] if name == 'VERSION' else original + b'\n')
                        self.assertNotEqual(page.load_release()[0], version, name)
                        self.assertEqual(server.version, version)
                        self.assertEqual(server.assets, assets)
                        (root / name).write_bytes(original)
                finally:
                    server.server_close()

    def test_deployed_commit_identity_requires_intact_release(self):
        commit = '1afc186cfb49452d233917e5d87373b6e1a15b6e'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (*page.read_sources().keys(), 'VERSION'):
                (root / name).write_bytes((page.ROOT / name).read_bytes())
            marker = root / 'DEPLOY_COMMIT'
            marker.write_text(commit + '\n')
            expected = page.read_version(root).split('+')[0] + '+' + commit[:7]
            with patch.object(page, 'ROOT', root):
                server = page.CacheServer(('127.0.0.1', 0), page.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = f'http://127.0.0.1:{server.server_port}'
                try:
                    with urlopen(base + '/version') as response:
                        self.assertEqual(decode_history(json.load(response)), {'version': expected})
                        self.assertEqual(response.headers['X-App-Version'], expected)
                    with urlopen(base) as response:
                        self.assertIn(expected.encode(), response.read())
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()
                for invalid in ('', commit[:12], 'g' * 40):
                    marker.write_text(invalid)
                    with self.assertRaisesRegex(ValueError, 'full Git commit'):
                        page.load_release()
                marker.write_text(commit)
                with (root / 'app.js').open('ab') as source:
                    source.write(b'\n')
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    page.load_release()

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
            with urlopen(base + f'/api/historic?series=price&start={now-20001}&end={now}&windowStart={now-20001}&windowEnd={now}&format=stream') as response:
                self.assertEqual(response.headers['Content-Type'], 'application/x-ndjson')
                messages = decode_stream(response)
            self.assertEqual(messages[0]['encoding'], 'ranges-stream-v2')
            self.assertTrue(messages[0]['historyReady'])
            self.assertEqual(messages[0]['extremes']['high'][1], 1.002)
            self.assertEqual(messages[1]['ranges'], list(page.pack_ranges(rows[:8192])))
            self.assertEqual(messages[2]['ranges'], list(page.pack_ranges(rows[8192:])))
            self.assertEqual(messages[3:], [{'done': True}])
            with urlopen(base + f'/api/historic?series=price&start={now-1}&end={now}&format=stream') as response:
                messages = decode_stream(response)
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[-1], {'done': True})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_retired_source_flags_are_rejected(self):
        for flag in ('--ssh-host', '--upstream', '--archive-only'):
            with self.subTest(flag=flag), patch('sys.argv', ['server.py', flag]), \
                    patch('sys.stderr', io.StringIO()), patch.object(page, 'CacheServer') as server:
                with self.assertRaises(SystemExit) as error:
                    page.main()
                self.assertEqual(error.exception.code, 2)
                server.assert_not_called()

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
        base = (f'http://127.0.0.1:{server.server_port}/api/historic?series=price&'
                f'start={now-1000}&end={now}&windowStart={now-1000}&windowEnd={now}')
        try:
            for format in ('raw', 'ranges-v1', 'stream'):
                with self.subTest(format=format):
                    with urlopen(base + f'&format={format}&afterBlock=11') as response:
                        if format == 'stream':
                            metadata, batch, done = decode_stream(response)
                            self.assertEqual(batch['ranges'], list(page.pack_ranges(rows[2:])))
                            self.assertEqual(done, {'done': True})
                        else:
                            metadata = decode_history(json.load(response))
                            key = 'samples' if format == 'raw' else 'ranges'
                            expected = rows[2:] if format == 'raw' else list(page.pack_ranges(rows[2:]))
                            self.assertEqual(metadata[key], expected)
                        self.assertEqual(metadata['extremes'], {'low': rows[0], 'high': rows[1]})
                        self.assertTrue(metadata['historyReady'])
            with urlopen(base + '&format=stream&afterBlock=12') as response:
                messages = decode_stream(response)
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[-1], {'done': True})
            # Catch up beyond the former five-minute overlap, using the same cursor.
            newer = [[now - 600, 1, 13], [now - 2, .999, 14]]
            server.cache.merge(newer, now)
            with urlopen(base + '&format=raw&afterBlock=12') as response:
                self.assertEqual(decode_history(json.load(response))['samples'], newer)
            for cursor in ('', '-1', '1.5', 'NaN', '9007199254740992', '12&afterBlock=13'):
                with self.subTest(cursor=cursor), self.assertRaises(HTTPError) as error:
                    urlopen(base + '&afterBlock=' + cursor)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_bounded_history_window(self):
        self.assertEqual(page.time_window({'start': ['100'], 'end': ['200']}), (100, 200))
        for start, end in [('nan', '200'), ('0', 'inf'), ('200', '100'), ('0', '604801')]:
            with self.assertRaises(ValueError):
                page.time_window({'start': [start], 'end': [end]})

    def test_http_assets_errors_and_no_arbitrary_proxy(self):
        server = page.CacheServer(('127.0.0.1', 0), page.Handler)
        server.cache = page.HistoryCache()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            # Deployment checks work even while history is warming.
            for route in ('/version', '/api/version'):
                with urlopen(base + route) as response:
                    self.assertEqual(decode_history(json.load(response)), {'version': server.version})
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
            for path, code in [('/api/historic?series=price&start=nan', 400), ('/../../.envrc', 404), ('/api/v1/query', 404)]:
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + path)
                self.assertEqual(error.exception.code, code)
                error.exception.close()
            with self.assertRaises(HTTPError) as error:
                urlopen(base + '/api/historic?series=price')
            self.assertEqual(error.exception.code, 503)
            self.assertEqual(error.exception.headers['X-App-Version'], server.version)
            error.exception.close()
            now = time.time()
            server.cache.merge([[now - 10, 1.001, 50]], now)
            with urlopen(base + '/api/historic?series=price') as response:
                packed = decode_history(json.load(response))
                self.assertEqual(packed['encoding'], 'ranges-v1')
                self.assertEqual(packed['ranges'], [[now - 10, 50, 1.001]])
                self.assertNotIn('samples', packed)
            with urlopen(base + '/ranges.js') as response:
                self.assertIn(b'export class RangeSeries', response.read())
            # There is no upstream client on the server. Concurrent reads still work.
            def read():
                with urlopen(base + '/api/historic?series=price&format=raw') as response:
                    self.assertEqual(response.headers['X-App-Version'], server.version)
                    return decode_history(json.load(response))
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

    def test_small_live_merges_share_unchanged_rows_and_retire_repaired_timestamps(self):
        cache = page.HistoryCache()
        now = time.time()
        rows = [(now - 2000 + number, 1., number) for number in range(1000)]
        cache.merge(rows, now, missing_only=True)
        snapshot, summaries = cache.rows, cache.summaries
        cache.merge([rows[-1]], now)
        self.assertIs(cache.rows, snapshot)
        self.assertIs(cache.summaries, summaries)
        self.assertNotIn(999, cache.repaired)
        corrected = (now - 5, 1.02, 998)
        cache.merge([corrected], now)
        self.assertEqual(cache.rows, (*rows[:998], rows[-1], corrected))
        self.assertTrue(all(row is original for row, original in zip(cache.rows[:998], snapshot)))
        self.assertNotIn(998, cache.repaired)
        cache.merge([(now - 4, .99, 997), (now - 3, 1., 1001)], now, missing_only=True)
        self.assertEqual(cache.rows[-1], (now - 3, 1., 1001))
        self.assertEqual(cache.repaired[997], rows[997][0])
        self.assertEqual(cache.repaired[1001], now - 3)
        cache.merge([], now, replace=True)
        self.assertEqual(cache.rows, ())
        self.assertEqual(cache.repaired, {})
        self.assertEqual(cache.summaries.chunks, ())

    def test_batch_corrections_preserve_repaired_block_and_timestamp_precedence(self):
        cache = page.HistoryCache()
        now = time.time()
        original = (now - 20, 1., 10)
        cache.merge([original], now, missing_only=True)
        # The later correction of restored block 10 removes its old timestamp,
        # including a preceding update at that timestamp in the same batch.
        corrected = (now - 10, 1.01, 10)
        cache.merge([(original[0], .99, 11), corrected], now)
        self.assertEqual(cache.rows, (corrected,))
        self.assertEqual(cache.repaired, {})

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
