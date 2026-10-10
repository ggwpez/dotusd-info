#!/usr/bin/env python3
"""Read-only dotUSD peg monitor backed by finalized Asset Hub pool history."""
import argparse
import base64
import hashlib
from bisect import bisect_right
from itertools import islice
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


def load_release():
    """Freeze assets and their version together until the server restarts."""
    files = {name: (ROOT / name).read_bytes() for name in
             ('server.py', 'archive_reader.py', 'index.html', 'app.js', 'ranges.js', 'style.css', 'favicon.svg')}
    digest = hashlib.sha256()
    for name, body in files.items():
        digest.update(name.encode() + b'\0' + body + b'\0')
    version = digest.hexdigest()
    assets = {name: files[name] for name in ('index.html', 'app.js', 'ranges.js', 'style.css', 'favicon.svg')}
    assets['app.js'] = assets['app.js'].replace(b'__APP_VERSION__', version.encode())
    assets['index.html'] = assets['index.html'].replace(
        b'</head>', f'<meta name="app-version" content="{version}"></head>'.encode())
    # Keep source modules separate, but deliver one self-contained document.
    assets['inline.js'] = assets['ranges.js'] + b'\n' + assets['app.js'].split(b'\n', 1)[1]
    assets['index.html'] = assets['index.html'].replace(
        b'__FAVICON__', b'data:image/svg+xml;base64,' + base64.b64encode(files['favicon.svg'])).replace(
        b'<!-- INLINE_STYLE -->', b'<style>' + assets['style.css'] + b'</style>').replace(
        b'<!-- INLINE_SCRIPT -->', b'<script type="module">' + assets['inline.js'] + b'</script>')
    return version, assets

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


def pack_ranges(rows):
    """Delta runs: seed [time, block, price], then [count, dt, db, ?price].

    [0, time, block, price] resets the seed when a block is unknown or exact
    floating-point reconstruction is impossible. No price rounding is used.
    """
    if not rows:
        return
    t, price, block = rows[0]
    yield [t, block, price]
    i = 1
    while i < len(rows):
        stamp, value, number = rows[i]
        dt = stamp - t
        if block is None or number is None or t + dt != stamp:
            yield [0, stamp, number, value]
            t, price, block = stamp, value, number
            i += 1
            continue
        dt = int(dt) if dt == int(dt) else dt
        db = number - block
        count = 1
        while int(stamp) == stamp and int(dt) == dt and i + count < len(rows):
            next_t, next_price, next_block = rows[i + count]
            if (next_price != value or next_block != block + (count + 1) * db or
                    next_t != stamp + count * dt or next_t != t + (count + 1) * dt):
                break
            count += 1
        record = [count, dt, db]
        if value != price:
            record.append(value)
        yield record
        t, price, block = rows[i + count - 1]
        i += count


class HistoryCache:
    """One writer fetches history; HTTP readers never access the upstream."""
    def __init__(self):
        self.lock = threading.Lock()
        self.writer_lock = threading.Lock()
        self.rows = ()
        self.fetched_at = None
        self.history_ready = False

    def merge(self, rows, now, mark_fresh=True):
        # Serialize writers, but let readers use the previous immutable snapshot
        # while a replacement is built. Only publication takes the shared lock.
        with self.writer_lock:
            with self.lock:
                previous = self.rows
            # Merge by timestamp to deduplicate overlapping refreshes and preserve gaps.
            by_time = {row[0]: row for row in previous if row[0] > now - HISTORY_SECONDS}
            by_time.update({row[0]: tuple(row) for row in rows
                            if now - HISTORY_SECONDS < row[0] <= now})
            replacement = tuple(by_time[t] for t in sorted(by_time)[-MAX_SAMPLES:])
            with self.lock:
                self.rows = replacement
                if mark_fresh:
                    self.fetched_at = now

    def read(self, start, end, window=None, after_block=None):
        with self.lock:
            history, fetched_at, history_ready = self.rows, self.fetched_at, self.history_ready
        if fetched_at is None:
            raise RuntimeError('History is warming up')
        start = max(start, time.time() - HISTORY_SECONDS)
        left = bisect_right(history, start, key=lambda row: row[0])
        right = bisect_right(history, end, key=lambda row: row[0])
        extremes = None
        if window is not None:
            window_left = bisect_right(history, window[0], key=lambda row: row[0])
            window_right = bisect_right(history, window[1], key=lambda row: row[0])
            window_rows = history[window_left:window_right]
            if window_rows:
                extremes = {'low': min(window_rows, key=lambda row: row[1]),
                            'high': max(window_rows, key=lambda row: row[1])}
        rows = list(history[left:right])
        if after_block is not None:
            rows = [row for row in rows if row[2] is not None and row[2] > after_block]
        return rows, fetched_at, history_ready, extremes


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
        elif now - self.cursor >= 6:
            start = max(self.cursor - 300, now - HISTORY_SECONDS)
            while start < now and not self.stop.is_set():
                end = min(now, start + FETCH_SECONDS)
                self.fetch(start, end)
                self.cursor = end
                start = end
        # Bounded startup chunks share the worker with live updates.
        if self.backfill_end > now - HISTORY_SECONDS and not self.stop.is_set():
            start = max(now - HISTORY_SECONDS, self.backfill_end - FETCH_SECONDS)
            self.fetch(start, self.backfill_end)
            self.backfill_end = start

    def load_archive(self):
        if self.archive is None or not self.archive.exists():
            return True
        with closing(sqlite3.connect(f'file:{self.archive}?mode=ro', uri=True, timeout=1)) as db:
            rows = db.execute('SELECT seq,sample_time,price,number FROM samples WHERE seq>? ORDER BY seq LIMIT 2000',
                              (self.archive_seq,)).fetchall()
        if rows:
            self.cache.merge([(stamp / 1000, price, number) for _, stamp, price, number in rows
                              if price is not None], time.time(), mark_fresh=False)
            self.archive_seq = rows[-1][0]
        return len(rows) < 2000

    def run(self):
        while not self.stop.is_set():
            failed = False
            archive_ready = False
            try:
                self.poll()
            except Exception:
                failed = True
                logging.exception('Background price history refresh failed')
            try:
                archive_ready = self.load_archive()
            except (sqlite3.Error, OSError):
                failed = True
                logging.exception('Archive history load failed')
            ready = (archive_ready and self.backfill_end is not None and
                     self.backfill_end <= time.time() - HISTORY_SECONDS)
            with self.cache.lock:
                self.cache.history_ready = ready
            # Stored history needs no six-second live polling delay during startup.
            self.stop.wait(6 if ready or failed else 0.05)


class CacheServer(ThreadingHTTPServer):
    """Bound active handlers, including slow readers, independently of systemd."""
    def __init__(self, address, handler):
        self.version, self.assets = load_release()
        script_hash = base64.b64encode(hashlib.sha256(self.assets['inline.js']).digest()).decode()
        style_hash = base64.b64encode(hashlib.sha256(self.assets['style.css']).digest()).decode()
        self.content_security_policy = (
            f"default-src 'self'; style-src 'self' 'sha256-{style_hash}'; "
            f"script-src 'self' 'sha256-{script_hash}'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'")
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
        if url.path == '/api/version':
            self.respond(200, {'version': self.server.version})
            return
        if url.path == '/api/prices':
            try:
                params = urllib.parse.parse_qs(url.query, max_num_fields=6, keep_blank_values=True)
                raw = params.get('format', ['ranges-v1'])[0] == 'raw'
                streaming = params.get('format', [''])[0] == 'stream'
                start, end = time_window(params)
                after_block = None
                if 'afterBlock' in params:
                    value = params['afterBlock']
                    if len(value) != 1 or not value[0].isascii() or not value[0].isdigit():
                        raise ValueError('Invalid block cursor')
                    after_block = int(value[0])
                    if after_block > 9007199254740991:
                        raise ValueError('Invalid block cursor')
                window = None
                if 'windowStart' in params or 'windowEnd' in params:
                    if 'windowStart' not in params or 'windowEnd' not in params:
                        raise ValueError('Both window bounds are required')
                    window = time_window({'start': params['windowStart'], 'end': params['windowEnd']})
                    if not window[0] <= start < end <= window[1]:
                        raise ValueError('Chunk must lie within the window')
            except (ValueError, OverflowError):
                self.respond(400, {'error': 'Invalid time window (maximum seven days).'})
                return
            try:
                rows, fetched_at, history_ready, extremes = self.server.cache.read(start, end, window, after_block)
            except RuntimeError:
                self.respond(503, {'error': 'Price history is warming up. Please try again shortly.'})
                return
            if streaming:
                self.send_headers(200, 'application/x-ndjson')
                def send(record):
                    self.wfile.write(json.dumps(record, allow_nan=False, separators=(',', ':')).encode() + b'\n')
                    self.wfile.flush()
                try:
                    send({'encoding': 'ranges-stream-v1', 'fetchedAt': fetched_at,
                          'historyReady': history_ready, 'extremes': extremes})
                    # Independent packed batches can be decoded immediately, without round trips.
                    for offset in range(0, len(rows), 8192):
                        send({'ranges': list(pack_ranges(rows[offset:offset + 8192]))})
                    send({'done': True})
                except (OSError, TimeoutError):
                    pass
                return
            # Stream bounded batches instead of building a second full JSON copy.
            self.send_headers(200, 'application/json')
            try:
                self.wfile.write(b'{"samples":[' if raw else b'{"encoding":"ranges-v1","ranges":[')
                records = iter(rows) if raw else pack_ranges(rows)
                first = True
                while batch := list(islice(records, 256)):
                    body = json.dumps(batch, allow_nan=False, separators=(',', ':')).encode()[1:-1]
                    self.wfile.write((b'' if first else b',') + body)
                    first = False
                self.wfile.write(b'],"fetchedAt":' + json.dumps(fetched_at).encode() +
                                 b',"historyReady":' + json.dumps(history_ready).encode() +
                                 b',"extremes":' + json.dumps(extremes, allow_nan=False).encode() + b'}')
            except (OSError, TimeoutError):
                pass
            return
        filename = {'/': 'index.html', '/app.js': 'app.js', '/ranges.js': 'ranges.js', '/style.css': 'style.css'}.get(url.path)
        if not filename:
            self.respond(404, {'error': 'Not found'})
            return
        mime = {'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}
        body = self.server.assets[filename]
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
        self.send_header('X-App-Version', self.server.version)
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'public, max-age=0, must-revalidate' if etag else 'no-store')
        if etag:
            self.send_header('ETag', etag)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', self.server.content_security_policy)
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
