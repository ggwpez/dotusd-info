#!/usr/bin/env python3
"""Read-only dotUSD peg monitor backed by finalized Asset Hub pool history."""
import argparse
import base64
import hashlib
from bisect import bisect_right
from collections import deque, OrderedDict
from concurrent.futures import Future
from itertools import islice
import logging
from contextlib import closing, contextmanager
import json
import math
import os
import re
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from issuance import IssuanceCache, IssuanceWorker
from release import read_sources, read_version, source_hash

ROOT = Path(__file__).resolve().parent
HISTORY_SECONDS = 7 * 86400
MAX_SAMPLES = 350000
FETCH_SECONDS = 6 * 3600
MAX_UPSTREAM_BYTES = 4 * 1024 * 1024
SUMMARY_SAMPLES = 256


def load_release():
    """Freeze assets and their version together until the server restarts."""
    files = read_sources(ROOT)
    recorded = read_version(ROOT)
    checksum = source_hash(files)
    commit_path = ROOT / 'DEPLOY_COMMIT'
    if commit_path.exists():
        identity = commit_path.read_text().strip()
        if not re.fullmatch(r'[0-9a-f]{40}', identity):
            raise ValueError('DEPLOY_COMMIT must contain a full Git commit hash')
        if recorded.split('+')[1] != checksum:
            raise ValueError('Deployed source checksum does not match VERSION')
        identity = identity[:7]
    else:
        # Uncommitted local previews must still invalidate browser caches.
        identity = checksum
    version = recorded.split('+')[0] + '+' + identity
    assets = {name: files[name] for name in ('index.html', 'app.js', 'ranges.js', 'style.css', 'favicon.svg', 'social-preview.png')}
    assets['app.js'] = assets['app.js'].replace(b'__APP_VERSION__', version.encode())
    assets['index.html'] = assets['index.html'].replace(
        b'</head>', f'<meta name="app-version" content="{version}"></head>'.encode())
    # Keep source modules separate, but deliver one self-contained document.
    assets['inline.js'] = assets['ranges.js'] + b'\n' + assets['app.js'].split(b'\n', 1)[1]
    values = dict(QUOTE='USDT', PAGE_PATH='/', SOURCE_LABEL='PRICE MONITOR',
                  DESCRIPTION='Monitor dotUSD / USDT peg deviation on Polkadot Asset Hub using publicly available data.',
                  PRICE_LABEL='Spot price', SOURCE_NOTE='')
    html = assets['index.html'].replace(
        b'__APP_VERSION__', version.encode()).replace(
        b'__SHORT_VERSION__', version.encode())
    details = (b'<section class="explain-grid" aria-label="About dotUSD">\n' +
               files['about-dotusd.html'] + b'\n</section>')
    html = html.replace(b'__DETAILS__', details)
    for key, value in values.items():
        html = html.replace(f'__{key}__'.encode(), value.encode())
    html = html.replace(b'<p></p>\n', b'')
    assets['index.html'] = html.replace(
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


class HistorySummaries:
    """Bounded partial-range cache, rebuilt once per immutable history snapshot."""
    def __init__(self, rows):
        chunks = []
        for offset in range(0, len(rows), SUMMARY_SAMPLES):
            chunk = rows[offset:offset + SUMMARY_SAMPLES]
            chunks.append((min(chunk, key=lambda row: row[1]),
                           max(chunk, key=lambda row: row[1]),
                           max((row[2] for row in chunk if row[2] is not None), default=-1)))
        self.chunks = tuple(chunks)

    def extremes(self, rows, left, right):
        if left >= right:
            return None
        candidates = []
        for index in range(left // SUMMARY_SAMPLES, (right - 1) // SUMMARY_SAMPLES + 1):
            start, end = index * SUMMARY_SAMPLES, min((index + 1) * SUMMARY_SAMPLES, len(rows))
            if left <= start and end <= right:
                candidates.extend(self.chunks[index][:2])
            else:
                candidates.extend(rows[max(left, start):min(right, end)])
        return {'low': min(candidates, key=lambda row: row[1]),
                'high': max(candidates, key=lambda row: row[1])}

    def after(self, rows, left, right, block):
        result = []
        for index in range(left // SUMMARY_SAMPLES, (right - 1) // SUMMARY_SAMPLES + 1):
            # Works even with missing or out-of-order block numbers.
            if self.chunks[index][2] <= block:
                continue
            start = max(left, index * SUMMARY_SAMPLES)
            end = min(right, (index + 1) * SUMMARY_SAMPLES)
            result.extend(row for row in rows[start:end] if row[2] is not None and row[2] > block)
        return result


class ResponseCache:
    """Bounded six-second LRU; concurrent misses share one encoding job."""
    def __init__(self, max_entries=32, max_bytes=8 * 1024 * 1024, max_entry_bytes=4 * 1024 * 1024):
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.max_entry_bytes = max_entry_bytes
        self.entries = OrderedDict()
        self.pending = {}
        self.bytes = 0
        self.lock = threading.Lock()

    def get(self, key, build):
        with self.lock:
            now = time.monotonic()
            for expired in [k for k, (deadline, _, _) in self.entries.items() if deadline <= now]:
                self.bytes -= self.entries.pop(expired)[2]
            if key in self.entries:
                self.entries.move_to_end(key)
                return self.entries[key][1]
            future = self.pending.get(key)
            owner = future is None
            if owner:
                future = self.pending[key] = Future()
        if not owner:
            return future.result()
        try:
            chunks, size = [], 0
            for chunk in build():
                size += len(chunk)
                if size > min(self.max_entry_bytes, self.max_bytes):
                    # Oversized responses use the existing bounded streaming path.
                    result = None
                    break
                chunks.append(chunk)
            else:
                result = tuple(chunks)
            with self.lock:
                if result is not None:
                    while self.entries and (len(self.entries) >= self.max_entries or self.bytes + size > self.max_bytes):
                        self.bytes -= self.entries.popitem(last=False)[1][2]
                    self.entries[key] = (time.monotonic() + 6, result, size)
                    self.bytes += size
                future.set_result(result)
                del self.pending[key]
            return result
        except BaseException as error:
            with self.lock:
                future.set_exception(error)
                del self.pending[key]
            raise


def encode_response(rows, fetched_at, history_ready, extremes, raw=False, streaming=False):
    def encode(record):
        return json.dumps(record, allow_nan=False, separators=(',', ':')).encode()
    if streaming:
        yield encode({'encoding': 'ranges-stream-v1', 'fetchedAt': fetched_at,
                      'historyReady': history_ready, 'extremes': extremes}) + b'\n'
        for offset in range(0, len(rows), 8192):
            yield encode({'ranges': list(pack_ranges(rows[offset:offset + 8192]))}) + b'\n'
        yield b'{"done":true}\n'
    else:
        yield b'{"samples":[' if raw else b'{"encoding":"ranges-v1","ranges":['
        records = iter(rows) if raw else pack_ranges(rows)
        first = True
        while batch := list(islice(records, 256)):
            yield (b'' if first else b',') + encode(batch)[1:-1]
            first = False
        yield (b'],"fetchedAt":' + encode(fetched_at) + b',"historyReady":' + encode(history_ready) +
               b',"extremes":' + encode(extremes) + b'}')


def historic_response(chunks, ti, streaming):
    """Attach fresh issuance without caching it with the reusable price chunks."""
    if ti is None:
        yield from chunks
        return
    encoded = json.dumps(ti, allow_nan=False, separators=(',', ':')).encode()
    if chunks is None:
        if streaming:
            yield b'{"encoding":"ranges-stream-v1","ti":' + encoded + b'}\n'
            yield b'{"done":true}\n'
        else:
            yield b'{"ti":' + encoded + b'}'
        return
    chunks = iter(chunks)
    first = next(chunks)
    # Both price formats begin with one object; only its first chunk changes.
    yield b'{"ti":' + encoded + b',' + first[1:]
    yield from chunks


class HistoryCache:
    """One writer fetches history; HTTP readers never access the upstream."""
    def __init__(self, archive=None, history_path=None):
        self.lock = threading.Lock()
        self.writer_lock = threading.Lock()
        self.rows = ()
        self.summaries = HistorySummaries(self.rows)
        self.fetched_at = None
        self.history_ready = False
        self.generation = 0
        self.responses = ResponseCache()
        self.repaired = {}
        self.archive = archive
        self.history_path = history_path

    def _stored_rows(self, start, end):
        rows = {}
        if self.history_path is not None and self.history_path.exists():
            with closing(sqlite3.connect(
                    self.history_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='repairs'").fetchone():
                    saved = db.execute(
                        'SELECT sample_time,price,number FROM repairs '
                        'WHERE sample_time>? AND sample_time<=? ORDER BY sample_time LIMIT ?',
                        (start, end, MAX_SAMPLES + 1)).fetchall()
                    if len(saved) > MAX_SAMPLES:
                        raise ValueError('Too many price samples; request a smaller window.')
                    rows.update((row[2], row) for row in saved)
        if self.archive is not None and self.archive.exists():
            with closing(sqlite3.connect(
                    self.archive.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                first = db.execute('SELECT number FROM samples ORDER BY number LIMIT 1').fetchone()
                last = db.execute('SELECT number FROM samples ORDER BY number DESC LIMIT 1').fetchone()
                if first:
                    # Published archives validate contiguous blocks and strictly
                    # increasing sample times. Seek their existing number index,
                    # without scanning or modifying the read-only archive.
                    def boundary(stamp):
                        low, high = first[0], last[0] + 1
                        while low < high:
                            middle = (low + high) // 2
                            value = db.execute('SELECT sample_time FROM samples WHERE number=?',
                                               (middle,)).fetchone()[0]
                            if value <= stamp * 1000:
                                low = middle + 1
                            else:
                                high = middle
                        return low
                    lower, upper = boundary(start), boundary(end)
                    saved = db.execute(
                        'SELECT sample_time,price,number FROM samples '
                        'WHERE number>=? AND number<? AND price IS NOT NULL ORDER BY number LIMIT ?',
                        (lower, upper, MAX_SAMPLES + 1)).fetchall()
                    if len(saved) > MAX_SAMPLES:
                        raise ValueError('Too many price samples; request a smaller window.')
                    rows.update((number, (stamp / 1000, price, number))
                                for stamp, price, number in saved)
        return rows

    def merge(self, rows, now, mark_fresh=True, replace=False, missing_only=False):
        # Serialize writers, but let readers use the previous immutable snapshot
        # while a replacement is built. Only publication takes the shared lock.
        with self.writer_lock:
            with self.lock:
                previous = () if replace else self.rows
            # Merge by timestamp to deduplicate overlapping refreshes and preserve gaps.
            by_time = {row[0]: row for row in previous if row[0] > now - HISTORY_SECONDS}
            rows = list(rows)
            candidates = {row[2] for row in rows} if missing_only else set()
            present = {row[2] for row in by_time.values() if row[2] in candidates} if missing_only else set()
            repaired = {} if replace else dict(self.repaired)
            for row in rows:
                stamp, _, block = row
                if not now - HISTORY_SECONDS < stamp <= now:
                    continue
                if missing_only and (stamp in by_time or block in present):
                    continue
                # Late collector data supersedes a repair even if its estimated
                # timestamp differs; never keep two observations of one block.
                if missing_only:
                    repaired[block] = stamp
                    present.add(block)
                elif block is not None:
                    old = repaired.pop(block, None)
                    if old is not None:
                        by_time.pop(old, None)
                by_time[stamp] = tuple(row)
            replacement = tuple(by_time[t] for t in sorted(by_time)[-MAX_SAMPLES:])
            self.repaired = {block: stamp for block, stamp in repaired.items()
                             if replacement and stamp >= replacement[0][0] and
                             by_time.get(stamp, (None, None, None))[2] == block}
            summaries = HistorySummaries(replacement)
            with self.lock:
                self.rows = replacement
                self.summaries = summaries
                self.generation += 1
                if mark_fresh:
                    self.fetched_at = now

    def selection(self, start, end, window):
        with self.lock:
            history, fetched_at, history_ready = self.rows, self.fetched_at, self.history_ready
            summaries = self.summaries
            generation = self.generation
        cutoff = time.time() - HISTORY_SECONDS
        query_start, query_end = window if window is not None else (start, end)
        if query_start < cutoff and (self.archive is not None or self.history_path is not None):
            saved = self._stored_rows(query_start, query_end)
            # Collector samples supersede persisted prices by block, even when
            # a correction moves the timestamp outside the selected window.
            saved.update((row[2], row) for row in history if row[2] is not None and
                         (row[2] in saved or query_start < row[0] <= query_end))
            by_time = {row[0]: row for row in saved.values() if query_start < row[0] <= query_end}
            by_time.update((row[0], row) for row in history if query_start < row[0] <= query_end)
            if len(by_time) > MAX_SAMPLES:
                raise ValueError('Too many price samples; request a smaller window.')
            history = tuple(by_time[stamp] for stamp in sorted(by_time))
            summaries = HistorySummaries(history)
            generation = None
            history_ready = query_end <= cutoff or history_ready
        else:
            if fetched_at is None:
                raise RuntimeError('History is warming up')
            start = max(start, cutoff)
        left = bisect_right(history, start, key=lambda row: row[0])
        right = bisect_right(history, end, key=lambda row: row[0])
        bounds = None
        if window is not None:
            window_left = bisect_right(history, window[0], key=lambda row: row[0])
            window_right = bisect_right(history, window[1], key=lambda row: row[0])
            bounds = (window_left, window_right)
        return {
            'history': history,
            'summaries': summaries,
            'fetched_at': fetched_at,
            'history_ready': history_ready,
            'generation': generation,
            'left': left,
            'right': right,
            'bounds': bounds,
        }

    @staticmethod
    def read_selection(selection, after_block):
        history = selection['history']
        summaries = selection['summaries']
        left, right = selection['left'], selection['right']
        bounds = selection['bounds']
        extremes = summaries.extremes(history, *bounds) if bounds is not None else None
        rows = (list(history[left:right]) if after_block is None else
                summaries.after(history, left, right, after_block))
        return rows, selection['fetched_at'], selection['history_ready'], extremes

    def read(self, start, end, window=None, after_block=None):
        return self.read_selection(self.selection(start, end, window), after_block)

    def response(self, start, end, window=None, after_block=None, raw=False, streaming=False):
        selection = self.selection(start, end, window)
        # Different clocks can still select identical rows and summary windows.
        # The cursor remains part of the key: polls must never share another cursor's delta.
        key = (
            selection['generation'], selection['fetched_at'], selection['history_ready'],
            selection['left'], selection['right'], selection['bounds'], after_block, streaming,
        )
        def build():
            return encode_response(*self.read_selection(selection, after_block), raw=raw, streaming=streaming)
        if not raw and selection['generation'] is not None:
            cached = self.responses.get(key, build)
            if cached is not None:
                return cached
        return build()


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
        self.archive_ready = False

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
            # A successful query can still contain only stale samples while the
            # collector is offline. Keep querying from the last actual sample
            # so delayed publication cannot fall behind the overlap window.
            with self.cache.lock:
                latest = self.cache.rows[-1][0] if self.cache.rows else self.cursor
            start = max(min(self.cursor, latest) - 300, now - HISTORY_SECONDS)
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
            try:
                self.poll()
            except Exception:
                failed = True
                logging.exception('Background price history refresh failed')
            try:
                self.archive_ready = self.load_archive()
            except (sqlite3.Error, OSError):
                failed = True
                logging.exception('Archive history load failed')
            ready = (self.archive_ready and self.backfill_end is not None and
                     self.backfill_end <= time.time() - HISTORY_SECONDS)
            with self.cache.lock:
                self.cache.history_ready = ready
            # Stored history needs no six-second live polling delay during startup.
            self.stop.wait(6 if ready or failed else 0.05)


class CacheServer(HTTPServer):
    """Four fixed workers and a bounded FIFO for short request bursts."""
    handler_count = 4
    max_pending = 32
    queue_wait_seconds = 2
    request_queue_size = 64

    def __init__(self, address, handler):
        self.version, self.assets = load_release()
        script_hash = base64.b64encode(hashlib.sha256(self.assets['inline.js']).digest()).decode()
        style_hash = base64.b64encode(hashlib.sha256(self.assets['style.css']).digest()).decode()
        self.content_security_policy = (
            f"default-src 'self'; style-src 'self' 'sha256-{style_hash}'; "
            f"script-src 'self' 'sha256-{script_hash}'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'")
        self.pending = deque()
        self.work_ready = threading.Condition()
        self.closing = False
        self.workers = []
        self.issuance = IssuanceCache()
        super().__init__(address, handler)
        self.workers = [threading.Thread(target=self.process_requests, name=f'price-http-{i}', daemon=True)
                        for i in range(self.handler_count)]
        for worker in self.workers:
            worker.start()

    def reject_request(self, request):
        try:
            request.settimeout(1)
            request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nRetry-After: 6\r\nContent-Length: 0\r\n\r\n')
        except OSError:
            pass
        finally:
            self.shutdown_request(request)

    def process_request(self, request, client_address):
        with self.work_ready:
            if not self.closing and len(self.pending) < self.max_pending:
                self.pending.append((time.monotonic() + self.queue_wait_seconds, request, client_address))
                self.work_ready.notify()
                return
        self.reject_request(request)

    def process_requests(self):
        while True:
            with self.work_ready:
                self.work_ready.wait_for(lambda: self.closing or self.pending)
                if self.closing:
                    return
                deadline, request, client_address = self.pending.popleft()
            if time.monotonic() >= deadline:
                self.reject_request(request)
                continue
            try:
                request.settimeout(10)
                self.finish_request(request, client_address)
            except Exception:
                self.handle_error(request, client_address)
            finally:
                self.shutdown_request(request)

    def service_actions(self):
        # Expire waiters even if every worker is occupied by a slow client.
        # serve_forever checks at least every poll interval (normally 0.5s).
        expired = []
        with self.work_ready:
            now = time.monotonic()
            while self.pending and self.pending[0][0] <= now:
                expired.append(self.pending.popleft()[1])
        for request in expired:
            self.reject_request(request)

    def server_close(self):
        with self.work_ready:
            self.closing = True
            waiting = list(self.pending)
            self.pending.clear()
            self.work_ready.notify_all()
        super().server_close()
        for _, request, _ in waiting:
            self.shutdown_request(request)
        for worker in self.workers:
            worker.join()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path in ('/version', '/api/version'):
            self.respond(200, {'version': self.server.version})
            return
        if url.path == '/api/historic':
            try:
                params = urllib.parse.parse_qs(url.query, max_num_fields=12, keep_blank_values=True)
                selected = params.get('series', [])
                if len(selected) != 1 or selected[0] not in ('price', 'ti', 'price,ti', 'ti,price'):
                    raise ValueError('Choose series=price, ti, or price,ti.')
                series = selected[0].split(',')
                raw = params.get('format', ['ranges-v1'])[0] == 'raw'
                streaming = params.get('format', [''])[0] == 'stream'
                ti = None
                if 'ti' in series:
                    if any(len(params.get(name, [])) > 1 for name in ('tiStart', 'tiEnd', 'tiCursor')):
                        raise ValueError('Choose one issuance window and cursor.')
                    ti_end = float(params.get('tiEnd', params.get('end', [time.time()]))[0])
                    ti_start = float(params.get('tiStart', params.get('start', [ti_end - 3600]))[0])
                    if 'tiEnd' not in params and 'end' not in params:
                        ti_start = max(ti_start, ti_end - HISTORY_SECONDS)
                    ti_start, ti_end = time_window({'start': [ti_start], 'end': [ti_end]})
                    ti = self.server.issuance.selection(
                        ti_start, ti_end, params.get('tiCursor', [None])[0])
                end = ti_end if ti is not None else None
                if 'price' in series:
                    live = 'end' not in params
                    cache = self.server.cache
                    with cache.lock:
                        latest = cache.rows[-1][0] if cache.rows else 0
                    time_params = {**params, 'end': params.get('end', [max(time.time(), latest)])}
                    if live and 'start' in params:
                        time_params['start'] = [max(float(params['start'][0]), float(time_params['end'][0]) - HISTORY_SECONDS)]
                    if 'startpoint' in params:
                        # A block cursor replaces the timestamp lower bound. Keep
                        # time bounds for the viewport, retention and upper limit.
                        time_params['start'] = [float(time_params['end'][0]) - HISTORY_SECONDS]
                    start, end = time_window(time_params)
                    after_block = None
                    cursor_name = 'startpoint' if 'startpoint' in params else 'afterBlock'
                    if cursor_name in params:
                        value = params[cursor_name]
                        if len(value) != 1 or not value[0].isascii() or not value[0].isdigit():
                            raise ValueError('Invalid block cursor')
                        after_block = int(value[0])
                        if after_block > 9007199254740991:
                            raise ValueError('Invalid block cursor')
                        if 'startpoint' in params and 'afterBlock' in params:
                            raise ValueError('Choose one block cursor')
                    window = None
                    if 'windowStart' in params or 'windowEnd' in params:
                        if 'windowStart' not in params or ('windowEnd' not in params and not live):
                            raise ValueError('Both window bounds are required')
                        window_start = float(params['windowStart'][0])
                        if live:
                            window_start = max(window_start, end - HISTORY_SECONDS)
                        window = time_window({'start': [window_start], 'end': params.get('windowEnd', [end])})
                        if (not window[0] < end <= window[1] or
                                ('startpoint' not in params and start < window[0])):
                            raise ValueError('Chunk must lie within the window')
            except (ValueError, OverflowError) as error:
                self.respond(400, {'error': str(error)})
                return
            except (sqlite3.Error, OSError):
                logging.exception('Historical issuance read failed')
                self.respond(503, {'error': 'Stored history is temporarily unavailable.'})
                return
            chunks = None
            if 'price' in series:
                try:
                    # Do not clip a block cursor by a floating-point start time.
                    selection_start = end - HISTORY_SECONDS if 'startpoint' in params else start
                    chunks = cache.response(selection_start, end, window, after_block, raw, streaming)
                except ValueError as error:
                    self.respond(400, {'error': str(error)})
                    return
                except (sqlite3.Error, OSError):
                    logging.exception('Historical price read failed')
                    self.respond(503, {'error': 'Stored history is temporarily unavailable.'})
                    return
                except RuntimeError:
                    if ti is None:
                        self.respond(503, {'error': 'Price history is warming up. Please try again shortly.'})
                        return
                    chunks = encode_response((), None, False, None, raw=raw, streaming=streaming)
            chunks = historic_response(chunks, ti, streaming)
            self.send_headers(200, 'application/x-ndjson' if streaming else 'application/json', history_end=end)
            try:
                for chunk in chunks:
                    self.wfile.write(chunk)
                    if streaming:
                        self.wfile.flush()
            except (OSError, TimeoutError):
                pass
            return
        filename = {'/': 'index.html', '/app.js': 'app.js', '/ranges.js': 'ranges.js', '/style.css': 'style.css', '/social-preview.png': 'social-preview.png'}.get(url.path)
        if not filename:
            self.respond(404, {'error': 'Not found'})
            return
        mime = {'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8', 'png': 'image/png'}
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

    def send_headers(self, status, content_type, length=None, etag=None, history_end=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('X-App-Version', self.server.version)
        if history_end is not None:
            self.send_header('X-History-End', str(history_end))
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'public, max-age=0, must-revalidate' if etag else 'no-store')
        if etag:
            self.send_header('ETag', etag)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', self.server.content_security_policy)
        self.end_headers()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=3001)
    parser.add_argument('--ssh-host', default='server', help='SSH config host or user@host (default: server)')
    parser.add_argument('--upstream', help='Use an existing Prometheus API base instead of opening SSH')
    parser.add_argument('--archive', type=Path, default=Path('/var/lib/dotusd-info/archive.sqlite3'))
    parser.add_argument('--gap-repair-history', type=Path,
                        help='Persist issuance and price repairs indefinitely; gradually repair recent missing history')
    parser.add_argument('--hub-rpc', default='https://polkadot-asset-hub-rpc.polkadot.io',
                        help='Polkadot Hub HTTP archive RPC for recent issuance and pool-history gap repair')
    parser.add_argument('--no-browser', action='store_true', help='Serve without opening a browser')
    args = parser.parse_args()
    server = CacheServer((args.bind, args.port), Handler)
    server.cache = HistoryCache(archive=args.archive, history_path=args.gap_repair_history)
    server.issuance = IssuanceCache(history_path=args.gap_repair_history)
    worker = None
    repair = None
    issuance = None
    try:
        with price_source(args.upstream, args.ssh_host) as upstream:
            worker = HistoryWorker(server.cache, upstream, archive=args.archive)
            worker.thread.start()
            from hub_quotes import HubChain
            issuance = IssuanceWorker(server.issuance, chain=HubChain(args.hub_rpc))
            issuance.thread.start()
            if args.gap_repair_history:
                from gap_repair import GapRepair
                from treasury import TreasuryHistory
                repair = GapRepair(server.cache, args.gap_repair_history,
                                   chain=HubChain(args.hub_rpc), issuance=server.issuance,
                                   treasury=TreasuryHistory(args.gap_repair_history))
                repair.thread.start()
            browser_host = '127.0.0.1' if args.bind == '0.0.0.0' else args.bind
            url = f'http://{browser_host}:{server.server_port}'
            print(f'dotUSD: {url}', flush=True)
            if not args.no_browser:
                threading.Thread(target=webbrowser.open, args=(url,), kwargs={'new': 2}, daemon=True).start()
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if issuance is not None:
            issuance.stop.set()
        if repair is not None:
            repair.stop.set()
        if worker is not None:
            worker.stop.set()
            worker.thread.join(timeout=25)
        if repair is not None:
            repair.thread.join(timeout=15)
        if issuance is not None:
            issuance.thread.join(timeout=35)
        server.server_close()


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(f'dotUSD: {error}') from error
