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
from treasury import TreasuryHistory
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
        self.http.treasury = TreasuryHistory(self.path)
        self.http.treasury.merge([(self.old + n * 2, float(n * 10), n) for n in (9, 10, 11, 12)])
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
        query = f'series=price,ti,treasury&format=raw&start={self.old+18}&end={self.old+24}'
        with patch('hub_quotes.HubRpc.call', side_effect=AssertionError('Request-triggered RPC')):
            body = self.get(query)
            self.assertEqual(body['samples'], [[self.old+20, 1.01, 10],
                                               [self.old+22, 1.011, 11],
                                               [self.old+24, 1.12, 12]])
            self.assertEqual(body['ti']['rows'], [[self.old+n*2, n*100., n] for n in (10, 11, 12)])
            self.assertEqual(body['treasury']['rows'], [[self.old+n*2, n*10., n] for n in (10, 11, 12)])
            self.assertTrue(body['treasury']['reset'])
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
        for series in ('price', 'ti', 'treasury', 'price,ti', 'price,treasury', 'ti,treasury', 'price,ti,treasury'):
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

    def test_treasury_historical_formats_independent_bounds_and_corrections(self):
        bounds = (f'&start={self.old+18}&end={self.old+24}'
                  f'&tiStart={self.old+18}&tiEnd={self.old+20}'
                  f'&treasuryStart={self.old+20}&treasuryEnd={self.old+24}')
        for series in ('treasury', 'ti,treasury', 'price,treasury', 'price,ti,treasury'):
            for encoding in ('raw', 'ranges-v1', 'stream'):
                with self.subTest(series=series, encoding=encoding):
                    query = f'series={series}&format={encoding}' + bounds
                    with urlopen(self.base + query) as response:
                        if encoding == 'stream':
                            messages = [json.loads(line) for line in response]
                            body = messages[0]
                            self.assertEqual(messages[-1], {'done': True})
                        else:
                            body = json.load(response)
                    self.assertEqual(body['treasury']['rows'],
                                     [[self.old+n*2, n*10., n] for n in (11, 12)])
                    self.assertTrue(body['treasury']['reset'])
                    if 'ti' in series:
                        self.assertEqual(body['ti']['rows'], [[self.old+20, 1000., 10]])
        first = self.get('series=treasury' + bounds)['treasury']
        self.http.treasury.merge([(self.old + 22, 0., 11), (self.old + 26, 120., 12)])
        corrected = self.get('series=treasury' + bounds +
                             '&treasuryCursor=' + first['cursor'])['treasury']
        self.assertEqual(corrected['rows'], [[self.old+22, 0., 11]])
        self.assertTrue(corrected['reset'])
        self.assertEqual(corrected['cursor'], first['cursor'])
        self.assertEqual(self.http.treasury.rows, ())
