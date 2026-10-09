#!/usr/bin/env python3
"""Read-only dotUSD peg monitor backed by finalized Asset Hub pool history."""
import argparse
import hashlib
from bisect import bisect_right
import logging
from contextlib import closing, contextmanager
import json
import math
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HISTORY_SECONDS = 7 * 86400
MAX_SAMPLES = 350000
FETCH_SECONDS = 6 * 3600
MAX_UPSTREAM_BYTES = 4 * 1024 * 1024

METRIC = '{__name__=~"arb_peg_dotusd_usdt|arb_peg_block_number",clock="estimated_2s"}'


@contextmanager
def price_source(upstream=None, ssh_host='server'):
    """Own only the tunnel we launch; never replace another local listener."""
    if upstream:
        yield upstream.rstrip('/')
        return
    if not ssh_host or ssh_host.startswith('-'):
        raise ValueError('Invalid SSH host')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    command = [
        'ssh', '-N', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
        '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15',
        '-o', 'ServerAliveCountMax=3',
        '-L', f'127.0.0.1:{port}:127.0.0.1:9090', ssh_host,
    ]
    tunnel = subprocess.Popen(command)
    base = f'http://127.0.0.1:{port}'
    http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if tunnel.poll() is not None:
                raise RuntimeError(f'SSH tunnel exited ({tunnel.returncode}); check ssh {ssh_host}')
            try:
                with http.open(base + '/-/ready', timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError('Prometheus did not become reachable through SSH within 20 seconds')
        print(f'Peg history connected through SSH to {ssh_host}.', flush=True)
        yield base + '/api/v1'
    finally:
        if tunnel.poll() is None:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel.kill()
                tunnel.wait()


def time_window(params):
    end = float(params.get('end', [time.time()])[0])
    start = float(params.get('start', [end - 3600])[0])
    if not all(map(math.isfinite, (start, end))) or not 1 <= end - start <= 604800 or start < 0 or end > time.time() + 60:
        raise ValueError('Choose a time window between one second and seven days.')
    return start, end


def query_window(params):
    start, end = time_window(params)
    # An instant range-vector query returns original samples, with no step
    # resampling or Prometheus lookback that could fill collection gaps.
    return urllib.parse.urlencode({'query': f'{METRIC}[{math.ceil((end-start)*1000)}ms]', 'time': end, 'timeout': '15s'})


def samples(payload):
    if payload.get('status') != 'success' or payload.get('data', {}).get('resultType') != 'matrix':
        raise ValueError('Price source returned an invalid response.')
    series = payload['data']['result']
    prices = [s for s in series if s.get('metric', {}).get('__name__') == 'arb_peg_dotusd_usdt']
    blocks = [s for s in series if s.get('metric', {}).get('__name__') == 'arb_peg_block_number']
    if len(prices) > 1 or len(blocks) > 1:
        raise ValueError('Multiple price series returned; select a single market at the source.')
    numbers = {}
    for stamp, raw in blocks[0]['values'] if blocks else []:
        number = float(raw)
        if math.isfinite(number) and number.is_integer():
            numbers[stamp] = int(number)
    rows = []
    for stamp, raw in prices[0]['values'] if prices else []:
        price = float(raw)
        if math.isfinite(price) and price > 0:
            rows.append([stamp, price, numbers.get(stamp)])
    return rows


class HistoryCache:
    """One writer fetches history; HTTP readers never access the upstream."""
    def __init__(self):
        self.lock = threading.Lock()
        self.rows = []
        self.fetched_at = None

    def merge(self, rows, now, mark_fresh=True):
        with self.lock:
            # Merge by timestamp to deduplicate overlapping refreshes and preserve gaps.
            by_time = {row[0]: row for row in self.rows if row[0] > now - HISTORY_SECONDS}
            by_time.update({row[0]: tuple(row) for row in rows
                            if now - HISTORY_SECONDS < row[0] <= now})
            self.rows = [by_time[t] for t in sorted(by_time)][-MAX_SAMPLES:]
            if mark_fresh:
                self.fetched_at = now

    def read(self, start, end):
        with self.lock:
            if self.fetched_at is None:
                raise RuntimeError('History is warming up')
            start = max(start, time.time() - HISTORY_SECONDS)
            left = bisect_right(self.rows, start, key=lambda row: row[0])
            right = bisect_right(self.rows, end, key=lambda row: row[0])
            return self.rows[left:right], self.fetched_at


class HistoryWorker:
    def __init__(self, cache, upstream, http=None, archive=None):
        self.cache = cache
        self.upstream = upstream
        self.http = http or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='price-history', daemon=True)
        self.cursor = None
        self.backfill_end = None
        self.archive = archive
        self.archive_seq = 0

    def fetch(self, start, end):
        query = query_window({'start': [start], 'end': [end]})
        with self.http.open(self.upstream + '/query?' + query, timeout=20) as response:
            body = response.read(MAX_UPSTREAM_BYTES + 1)
        if len(body) > MAX_UPSTREAM_BYTES:
            raise ValueError('History response exceeds size limit')
        rows = samples(json.loads(body))
        if len(rows) > MAX_SAMPLES:
            raise ValueError('Too many history samples')
        self.cache.merge(rows, time.time())

    def poll(self):
        now = time.time()
        if self.cursor is None:
            # Make recent prices available first; seed older history in bounded chunks.
            self.fetch(now - 3600, now)
            self.cursor = now
            self.backfill_end = now - 3600
        else:
            start = max(self.cursor - 300, now - HISTORY_SECONDS)
            while start < now and not self.stop.is_set():
                end = min(now, start + FETCH_SECONDS)
                self.fetch(start, end)
                self.cursor = end
                start = end
        # One old-history chunk per cycle keeps live updates responsive.
        if self.backfill_end > now - HISTORY_SECONDS and not self.stop.is_set():
            start = max(now - HISTORY_SECONDS, self.backfill_end - FETCH_SECONDS)
            self.fetch(start, self.backfill_end)
            self.backfill_end = start

    def load_archive(self):
        if self.archive is None or not self.archive.exists():
            return
        with closing(sqlite3.connect(f'file:{self.archive}?mode=ro', uri=True, timeout=1)) as db:
            rows = db.execute('SELECT seq,sample_time,price,number FROM samples WHERE seq>? ORDER BY seq LIMIT 2000',
                              (self.archive_seq,)).fetchall()
        if rows:
            self.cache.merge([(stamp / 1000, price, number) for _, stamp, price, number in rows
                              if price is not None], time.time(), mark_fresh=False)
            self.archive_seq = rows[-1][0]

    def run(self):
        while not self.stop.is_set():
            try:
                self.poll()
            except Exception:
                logging.exception('Background price history refresh failed')
            try:
                self.load_archive()
            except (sqlite3.Error, OSError):
                logging.exception('Archive history load failed')
            self.stop.wait(6)


class CacheServer(ThreadingHTTPServer):
    """Bound active handlers, including slow readers, independently of systemd."""
    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.slots = threading.BoundedSemaphore(4)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nRetry-After: 6\r\nContent-Length: 0\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        request.settimeout(10)
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == '/api/prices':
            try:
                start, end = time_window(urllib.parse.parse_qs(url.query, max_num_fields=4))
            except (ValueError, OverflowError):
                self.respond(400, {'error': 'Invalid time window (maximum seven days).'})
                return
            try:
                rows, fetched_at = self.server.cache.read(start, end)
            except RuntimeError:
                self.respond(503, {'error': 'Price history is warming up. Please try again shortly.'})
                return
            # Stream bounded batches instead of building a second full JSON copy.
            self.send_headers(200, 'application/json')
            try:
                self.wfile.write(b'{"samples":[')
                for offset in range(0, len(rows), 256):
                    batch = json.dumps(rows[offset:offset + 256], allow_nan=False).encode()[1:-1]
                    self.wfile.write((b',' if offset else b'') + batch)
                self.wfile.write(b'],"fetchedAt":' + json.dumps(fetched_at).encode() + b'}')
            except (OSError, TimeoutError):
                pass
            return
        filename = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}.get(url.path)
        if not filename:
            self.respond(404, {'error': 'Not found'})
            return
        mime = {'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}
        body = (ROOT / filename).read_bytes()
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        content_type = mime[filename.split('.')[-1]]
        if etag in [tag.strip() for tag in self.headers.get('If-None-Match', '').split(',')]:
            self.send_headers(304, content_type, etag=etag)
        else:
            self.send_headers(200, content_type, len(body), etag=etag)
            self.wfile.write(body)

    def respond(self, status, body, content_type='application/json'):
        if not isinstance(body, bytes):
            body = json.dumps(body, allow_nan=False).encode()
        self.send_headers(status, content_type, len(body))
        self.wfile.write(body)

    def send_headers(self, status, content_type, length=None, etag=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'public, max-age=0, must-revalidate' if etag else 'no-store')
        if etag:
            self.send_header('ETag', etag)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=3001)
    parser.add_argument('--ssh-host', default='server', help='SSH config host or user@host (default: server)')
    parser.add_argument('--upstream', help='Use an existing Prometheus API base instead of opening SSH')
    parser.add_argument('--archive', type=Path, default=Path('/var/lib/dotusd-info/archive.sqlite3'))
    parser.add_argument('--no-browser', action='store_true', help='Serve without opening a browser')
    args = parser.parse_args()
    server = CacheServer((args.bind, args.port), Handler)
    server.cache = HistoryCache()
    worker = None
    try:
        with price_source(args.upstream, args.ssh_host) as upstream:
            worker = HistoryWorker(server.cache, upstream, archive=args.archive)
            worker.thread.start()
            browser_host = '127.0.0.1' if args.bind == '0.0.0.0' else args.bind
            url = f'http://{browser_host}:{server.server_port}'
            print(f'dotUSD: {url}', flush=True)
            if not args.no_browser:
                threading.Thread(target=webbrowser.open, args=(url,), kwargs={'new': 2}, daemon=True).start()
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if worker is not None:
            worker.stop.set()
            worker.thread.join(timeout=25)
        server.server_close()


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(f'dotUSD: {error}') from error
