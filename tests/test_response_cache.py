"""Response reuse, freshness, bounded retention and concurrent miss behavior."""
import importlib.util
import json
from pathlib import Path
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('response_server', Path(__file__).resolve().parents[1] / 'server.py')
page = importlib.util.module_from_spec(spec)
spec.loader.exec_module(page)


class ResponseCacheTests(unittest.TestCase):
    def test_lru_byte_limits_and_ttl_not_extended_by_hits(self):
        cache = page.ResponseCache(max_entries=2, max_bytes=5, max_entry_bytes=4)
        with patch.object(page.time, 'monotonic', return_value=100) as clock:
            cache.get('a', lambda: [b'aa'])
            cache.get('b', lambda: [b'bb'])
            clock.return_value = 105
            cache.get('a', Mock(side_effect=AssertionError('unexpected rebuild')))
            cache.get('c', lambda: [b'ccc'])
            self.assertEqual(list(cache.entries), ['a', 'c'])
            self.assertEqual(cache.bytes, 5)
            clock.return_value = 106
            cache.get('d', lambda: [b'd'])
            self.assertEqual(list(cache.entries), ['c', 'd'])
            self.assertEqual(cache.bytes, 4)
            self.assertIsNone(cache.get('huge', lambda: [b'12345']))
            self.assertNotIn('huge', cache.entries)
            self.assertEqual(cache.bytes, 4)

    def test_entry_limit(self):
        cache = page.ResponseCache(max_entries=2)
        for key in range(10):
            cache.get(key, lambda: [b'a'])
        self.assertEqual(list(cache.entries), [8, 9])
        self.assertEqual(cache.bytes, 2)

    def test_concurrent_miss_builds_once_and_failure_releases_waiters(self):
        for fail in (False, True):
            cache = page.ResponseCache()
            entered, waiting, release = threading.Event(), threading.Event(), threading.Event()
            def build():
                entered.set()
                self.assertTrue(release.wait(2))
                if fail:
                    raise ValueError('encoding failed')
                return [b'encoded']
            builder = Mock(side_effect=build)
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(cache.get, 'same', builder)
                try:
                    self.assertTrue(entered.wait(2))
                    future = cache.pending['same']
                    original = future.result
                    def wait():
                        waiting.set()
                        return original(timeout=2)
                    with patch.object(future, 'result', side_effect=wait):
                        second = pool.submit(cache.get, 'same', builder)
                        self.assertTrue(waiting.wait(2))
                        release.set()
                        if fail:
                            for result in (first, second):
                                with self.assertRaisesRegex(ValueError, 'encoding failed'):
                                    result.result(timeout=2)
                        else:
                            self.assertIs(first.result(timeout=2), second.result(timeout=2))
                finally:
                    release.set()
            self.assertEqual(builder.call_count, 1)
            self.assertFalse(cache.pending)
            if fail:
                self.assertEqual(cache.get('same', lambda: [b'retry']), (b'retry',))

    def test_equivalent_windows_share_bytes_but_cursors_and_extremes_do_not(self):
        cache = page.HistoryCache()
        now = time.time()
        cache.merge([(now-30, .9, 1), (now-20, 1, 2), (now-10, 1.1, 3)], now)
        def response(start, end, cursor=None, window=None):
            return cache.response(start, end, window, cursor, streaming=True)
        a = response(now-40, now, window=(now-40, now))
        self.assertIs(a, response(now-39, now+0.1, window=(now-39, now+0.1)))
        self.assertIsNot(a, response(now-25, now, window=(now-25, now)))
        self.assertIsNot(response(now-15, now, window=(now-40, now)),
                         response(now-15, now, window=(now-25, now)))
        for cursor in (None, 1, 2, 3):
            for streaming in (False, True):
                actual = b''.join(cache.response(now-40, now, (now-40, now), cursor, streaming=streaming))
                expected = b''.join(page.encode_response(*cache.read(now-40, now, (now-40, now), cursor), streaming=streaming))
                self.assertEqual(actual, expected)

    def test_publication_and_readiness_invalidate_immediately(self):
        cache = page.HistoryCache()
        now = time.time()
        cache.merge([(now-10, 1, 1)], now)
        def response():
            return cache.response(now-20, now, streaming=True)
        first = response()
        cache.history_ready = True
        ready = response()
        self.assertIsNot(first, ready)
        self.assertTrue(json.loads(ready[0])['historyReady'])
        cache.merge([(now-10, 2, 1)], now, mark_fresh=False)
        corrected = response()
        self.assertNotEqual(ready, corrected)
        cache.merge([], now + 1)
        self.assertNotEqual(corrected, response())

    def test_retention_clock_and_oversized_raw_fallback(self):
        cache = page.HistoryCache()
        now = time.time()
        cache.merge([(now-page.HISTORY_SECONDS+1, 1, 1), (now-1, 2, 2)], now)
        with patch.object(page.time, 'time', return_value=now) as clock:
            first = cache.response(0, now)
            clock.return_value = now + 2
            self.assertNotEqual(first, cache.response(0, now))
            cache.responses.max_entry_bytes = 1
            for raw in (False, True):
                for streaming in (False, True):
                    actual = b''.join(cache.response(now-2, now, raw=raw, streaming=streaming))
                    expected = b''.join(page.encode_response(*cache.read(now-2, now), raw=raw, streaming=streaming))
                    self.assertEqual(actual, expected)
