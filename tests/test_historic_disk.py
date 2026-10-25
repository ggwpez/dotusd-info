"""Bounded historic requests read persisted windows without upstream work."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

import backfill
from gap_repair import GapRepair
from issuance import IssuanceCache
import server


class HistoricDiskTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'repairs.db'
        self.archive = Path(directory.name) / 'archive.db'
        self.now = int(time.time())
        self.old = self.now - 30 * 86400
        self.cache = server.HistoryCache(archive=self.archive, history_path=self.path)
        worker = GapRepair(self.cache, self.path)
        with closing(worker.database()) as db, db:
            db.executemany('INSERT INTO repairs VALUES(?,?,?,?,?,?)', [
                (n, f'h{n}', (self.old + n * 2) * 1000, self.old + n * 2, 1 + n / 100, '[]')
                for n in (10, 11, 12)])
        with closing(backfill.database(self.archive)) as db, db:
            # Sequence order need not match block/time order.
            db.executemany('INSERT INTO samples(number,hash,timestamp,sample_time,price,reserves) '
                           'VALUES(?,?,?,?,?,?)', [
                (n, f'h{n}', (self.old + n * 2) * 1000, (self.old + n * 2) * 1000,
                 1 + n / 1000, '[]') for n in (11, 10, 9)])
        self.http = server.CacheServer(('127.0.0.1', 0), server.Handler)
        self.http.cache = self.cache
        self.http.issuance = IssuanceCache(self.path)
        self.http.issuance.merge([(self.old + n * 2, float(n * 100), n) for n in (9, 10, 11, 12)])
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.base = f'http://127.0.0.1:{self.http.server_port}/api/historic?'

    def stop(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()

    def get(self, query):
        with urlopen(self.base + query) as response:
            return json.load(response)

    def test_old_combined_window_boundaries_precedence_and_cursor(self):
        query = f'series=price,ti&format=raw&start={self.old+18}&end={self.old+24}'
        with patch('hub_quotes.HubRpc.call', side_effect=AssertionError('Request-triggered RPC')):
            body = self.get(query)
            self.assertEqual(body['samples'], [[self.old+20, 1.01, 10],
                                               [self.old+22, 1.011, 11],
                                               [self.old+24, 1.12, 12]])
            self.assertEqual(body['ti']['rows'], [[self.old+n*2, n*100., n] for n in (10, 11, 12)])
            self.assertTrue(body['historyReady'])
            self.assertEqual(self.cache.rows, ())
            delta = self.get(query + '&startpoint=10')
            self.assertEqual(delta['samples'], body['samples'][1:])
            empty = self.get(f'series=price&format=raw&start={self.old-100}&end={self.old}')
            self.assertEqual(empty['samples'], [])

    def test_historical_formats_extremes_and_saved_corrections(self):
        bounds = f'&start={self.old+20}&end={self.old+24}&windowStart={self.old+18}&windowEnd={self.old+24}'
        body = self.get('series=price' + bounds)
        self.assertEqual(body['extremes']['low'], [self.old+20, 1.01, 10])
        self.assertEqual(body['extremes']['high'], [self.old+24, 1.12, 12])
        with urlopen(self.base + 'series=price&format=stream' + bounds) as response:
            messages = [json.loads(line) for line in response]
        self.assertEqual(messages[1]['ranges'], body['ranges'])
        self.assertEqual(messages[-1], {'done': True})
        with closing(self.http.issuance.database()) as db, db:
            db.execute('UPDATE repairs SET price=1.2 WHERE number=12')
        changed = self.get('series=price&format=raw' + bounds)
        self.assertEqual(changed['samples'][-1], [self.old+24, 1.2, 12])

    def test_duration_and_sample_caps_reject_instead_of_truncating(self):
        for series in ('price', 'ti', 'price,ti'):
            with self.subTest(series=series), self.assertRaises(HTTPError) as error:
                self.get(f'series={series}&start={self.old}&end={self.old+604801}')
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
            with patch('server.MAX_SAMPLES', 2), patch('issuance.MAX_SAMPLES', 2):
                with self.subTest(series=series), self.assertRaises(HTTPError) as error:
                    self.get(f'series={series}&start={self.old}&end={self.old+24}')
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
        body = self.get(f'series=price,ti&format=raw&start={self.old}&end={self.old+604800}')
        self.assertEqual([row[2] for row in body['samples']], [9, 10, 11, 12])

    def test_crossing_live_boundary_keeps_collector_corrections(self):
        recent = self.now - 10
        with closing(self.http.issuance.database()) as db, db:
            db.execute('INSERT INTO repairs VALUES(?,?,?,?,?,?)',
                       (100, 'h100', recent*1000, recent, 1.1, '[]'))
        self.cache.merge([(recent+1, 1.02, 100)], self.now)
        start = self.now - server.HISTORY_SECONDS - 5
        body = self.get(f'series=price&format=raw&start={start}&end={self.now-5}')
        self.assertEqual(body['samples'], [[recent+1, 1.02, 100]])
        self.assertEqual(self.cache.rows, ((recent+1, 1.02, 100),))
