"""Validate raw history semantics and the narrow read-only HTTP surface."""
import importlib.util
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs
from urllib.request import urlopen
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
        server = ThreadingHTTPServer(('127.0.0.1', 0), page.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(base) as response:
                self.assertIn(b'Peg deviation', response.read())
                self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
            for path, code in [('/api/prices?start=nan', 400), ('/../../.envrc', 404), ('/api/v1/query', 404)]:
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + path)
                self.assertEqual(error.exception.code, code)
            with patch.object(page, 'samples', return_value=[[100, 1.001]]):
                class Upstream:
                    def open(self, *args, **kwargs):
                        import io
                        return io.StringIO('{}')
                server.http = Upstream()
                server.upstream = 'http://unused/api/v1'
                with urlopen(base + '/api/prices?start=100&end=200') as response:
                    self.assertEqual(json.load(response)['samples'], [[100, 1.001]])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
