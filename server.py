#!/usr/bin/env python3
"""Read-only dotUSD peg monitor backed by finalized Asset Hub pool history."""
import argparse
import base64
import hashlib
import heapq
from bisect import bisect_left, bisect_right
from collections import deque, OrderedDict
from concurrent.futures import Future
from datetime import date
import html
from itertools import islice
import logging
from contextlib import closing
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from archive_reader import Unavailable, price_time, read_sample
from gap_repair import GapRepair, price_database
from hub_quotes import HubChain, RPC
from issuance import HISTORY_READ_LOCK, IssuanceCache, IssuanceWorker
from release import read_sources, read_version, source_hash
from treasury import TreasuryHistory

ROOT = Path(__file__).resolve().parent
HISTORY_SECONDS = 7 * 86400
MAX_SAMPLES = 350000
LIVE_BLOCKS = 15
SUMMARY_SAMPLES = 256
# Finality normally lags about a minute; matches the chart's "Data delayed" cutoff.
HEALTH_STALE_SECONDS = 180

CHANGELOG_ENTRIES = 10
BELL = ('<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<path d="M6 9a6 6 0 0 1 12 0c0 5 2 6.5 2 6.5H4S6 14 6 9"/><path d="M10 19a2 2 0 0 0 4 0"/></svg>')


def parse_changelog(text):
    """Strict release sections: `## X.Y.Z - YYYY-MM-DD`, then `- ` entries."""
    releases, entry = [], None
    for number, line in enumerate(text.splitlines(), 1):
        heading = re.fullmatch(r'## (\d+)\.(\d+)\.(\d+) - (\d{4}-\d{2}-\d{2})', line)
        if heading:
            version = tuple(map(int, heading.groups()[:3]))
            try:
                day = date.fromisoformat(heading[4])
            except ValueError:
                raise ValueError(f'CHANGELOG.md:{number}: invalid date') from None
            if releases and not releases[-1]['changes']:
                raise ValueError(f'CHANGELOG.md:{number}: previous release has no entries')
            if releases and (version >= releases[-1]['key'] or day > releases[-1]['date']):
                raise ValueError(f'CHANGELOG.md:{number}: releases must be newest first')
            releases.append({'key': version, 'version': '.'.join(map(str, version)),
                             'date': day, 'changes': []})
            entry = None
        elif line.startswith('## '):
            raise ValueError(f'CHANGELOG.md:{number}: expected "## X.Y.Z - YYYY-MM-DD"')
        elif not releases or not line.strip():
            entry = None if releases else entry
        elif line.startswith('- ') and line[2:].strip():
            releases[-1]['changes'].append(line[2:].strip())
            entry = len(releases[-1]['changes']) - 1
        elif line.startswith('  ') and entry is not None:
            releases[-1]['changes'][entry] += ' ' + line.strip()
        else:
            raise ValueError(f'CHANGELOG.md:{number}: expected "- " entry or indented continuation')
    if releases and not releases[-1]['changes']:
        raise ValueError('CHANGELOG.md: last release has no entries')
    return releases


def render_changelog(releases, limit=CHANGELOG_ENTRIES):
    """Server-rendered menu; the client only tracks the unseen marker."""
    items = [(release, change) for release in releases for change in release['changes']][:limit]
    if not items:
        return ''
    rows = ''.join(
        f'<li><div class="changelog-meta"><span>{release["version"]}</span>'
        f'<time datetime="{release["date"].isoformat()}">'
        f'{release["date"]:%b} {release["date"].day}, {release["date"].year}</time></div>'
        f'<p>{html.escape(change)}</p></li>'
        for release, change in items)
    return (f'<details class="changelog" id="changelog" data-latest="{releases[0]["version"]}">'
            f'<summary aria-label="What\'s new" title="What\'s new">{BELL}'
            '<span class="changelog-dot" id="changelog-dot" hidden></span></summary>'
            f'<div class="changelog-menu"><h2>What\'s new</h2><ol>{rows}</ol></div></details>')


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
    html = html.replace(b'__DETAILS__', details).replace(
        b'__CHANGELOG__', render_changelog(parse_changelog(files['CHANGELOG.md'].decode())).encode())
    for key, value in values.items():
        html = html.replace(f'__{key}__'.encode(), value.encode())
    html = html.replace(b'<p></p>\n', b'')
    assets['index.html'] = html.replace(
        b'__FAVICON__', b'data:image/svg+xml;base64,' + base64.b64encode(files['favicon.svg'])).replace(
        b'<!-- INLINE_STYLE -->', b'<style>' + assets['style.css'] + b'</style>').replace(
        b'<!-- INLINE_SCRIPT -->', b'<script type="module">' + assets['inline.js'] + b'</script>')
    return version, assets


def time_window(params):
    end = float(params.get('end', [time.time()])[0])
    start = float(params.get('start', [end - 3600])[0])
    if not all(map(math.isfinite, (start, end))) or not 1 <= end - start <= 604800 or start < 0 or end > time.time() + 60:
        raise ValueError('Choose a time window between one second and seven days.')
    return start, end


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


def merge_price_rows(earlier, later):
    """Merge sorted streams; later rows win equal timestamps without copying."""
    pending = None
    for row in heapq.merge(earlier, later, key=lambda row: row[0]):
        if pending is not None and pending[0] != row[0]:
            yield pending
        pending = row
    if pending is not None:
        yield pending


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
        yield encode({'encoding': 'ranges-stream-v2', 'fetchedAt': fetched_at,
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


def historic_response(chunks, histories, streaming, raw=False):
    """Attach fresh, losslessly packed state without caching it with prices."""
    if not histories:
        yield from chunks
        return
    encoder = json.JSONEncoder(allow_nan=False, separators=(',', ':'))
    metadata = {name: {key: value for key, value in history.items() if key != 'rows'}
                for name, history in histories.items()}
    if streaming:
        prices = iter(chunks) if chunks is not None else iter(())
        first = json.loads(next(prices)) if chunks is not None else {'encoding': 'ranges-stream-v2'}
        first.update(metadata)
        yield encoder.encode(first).encode() + b'\n'
        # Prices can draw immediately; state commits atomically at end of stream.
        for chunk in prices:
            if chunk != b'{"done":true}\n':
                yield chunk
        for name, history in histories.items():
            rows = history['rows']
            for offset in range(0, len(rows), 1024):
                batch = {'series': name, 'ranges': list(pack_ranges(rows[offset:offset + 1024]))}
                yield encoder.encode(batch).encode() + b'\n'
        yield b'{"done":true}\n'
        return

    yield b'{'
    for index, (name, history) in enumerate(histories.items()):
        yield ((',' if index else '') + encoder.encode(name) + ':{').encode()
        fields = metadata[name] if raw else {'encoding': 'ranges-v1', **metadata[name]}
        yield encoder.encode(fields)[1:-1].encode()
        yield b',"rows":[' if raw else b',"ranges":['
        records = iter(history['rows']) if raw else pack_ranges(history['rows'])
        first = True
        while batch := list(islice(records, 256)):
            yield (b'' if first else b',') + encoder.encode(batch)[1:-1].encode()
            first = False
        yield b']}'
    if chunks is None:
        yield b'}'
    else:
        prices = iter(chunks)
        yield b',' + next(prices)[1:]
        yield from prices


class HistoryCache:
    """Background writers publish history; HTTP readers never access the RPC."""
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
        # (block, chain seconds) of the newest finalized head read by ingestion.
        self.finalized = None

    def _stored_rows(self, start, end, history=()):
        rows = {}

        def remember(cursor, divisor=1):
            index = bisect_right(history, start, key=lambda row: row[0])
            for count, row in enumerate(cursor, 1):
                if count > MAX_SAMPLES:
                    raise ValueError('Too many price samples; request a smaller window.')
                if divisor != 1:
                    row = (row[0] / divisor, row[1], row[2])
                stamp, _, number = row
                while index < len(history) and history[index][0] < stamp:
                    index += 1
                if (number is not None and index < len(history) and
                        history[index][0] == stamp and history[index][2] == number):
                    # This block already has a cached correction. Do not retain
                    # another tuple for it, even when its stored price differs.
                    rows.pop(number, None)
                else:
                    rows[number] = row

        if self.history_path is not None and self.history_path.exists():
            with closing(sqlite3.connect(
                    self.history_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='repairs'").fetchone():
                    remember(db.execute(
                        'SELECT sample_time,price,number FROM repairs '
                        'WHERE sample_time>? AND sample_time<=? ORDER BY sample_time LIMIT ?',
                        (start, end, MAX_SAMPLES + 1)))
        if self.archive is not None and self.archive.exists():
            with closing(sqlite3.connect(
                    self.archive.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='samples'").fetchone():
                    return rows
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
                    remember(db.execute(
                        'SELECT sample_time,price,number FROM samples '
                        'WHERE number>=? AND number<? AND price IS NOT NULL ORDER BY number LIMIT ?',
                        (lower, upper, MAX_SAMPLES + 1)), divisor=1000)
        return rows

    def merge(self, rows, now, mark_fresh=True, replace=False, missing_only=False):
        # Serialize writers, but let readers use the previous immutable snapshot
        # while a replacement is built. Only publication takes the shared lock.
        with self.writer_lock:
            with self.lock:
                previous = () if replace else self.rows
            cutoff = now - HISTORY_SECONDS
            left = bisect_right(previous, cutoff, key=lambda row: row[0])
            rows = [tuple(row) for row in rows]
            candidates = {row[2] for row in rows} if missing_only else set()
            present = {row[2] for row in islice(previous, left, None)
                       if row[2] in candidates} if missing_only else set()
            repaired = {} if replace else self.repaired
            edits = {}
            changed_blocks = set()

            def forget(row):
                changed_blocks.add(row[2])

            for row in islice(previous, left):
                forget(row)

            def original(stamp):
                index = bisect_left(previous, stamp, lo=left, key=lambda row: row[0])
                return previous[index] if index < len(previous) and previous[index][0] == stamp else None

            def change(stamp, row):
                before = original(stamp)
                current = edits.get(stamp, before)
                if current is not None:
                    forget(current)
                if row == before:
                    edits.pop(stamp, None)
                else:
                    edits[stamp] = row

            for row in rows:
                stamp, _, block = row
                if not cutoff < stamp <= now:
                    continue
                if missing_only:
                    if block in present or edits.get(stamp, original(stamp)) is not None:
                        continue
                    change(stamp, row)
                    repaired[block] = stamp
                    present.add(block)
                else:
                    # Live data supersedes restored evidence by block even
                    # when its estimated timestamp differs.
                    old = repaired.pop(block, None) if block is not None else None
                    if old is not None:
                        change(old, None)
                    change(stamp, row)
            if edits:
                retained = (row for row in islice(previous, left, None) if row[0] not in edits)
                additions = sorted((row for row in edits.values() if row is not None),
                                   key=lambda row: row[0])
                replacement = tuple(heapq.merge(retained, additions, key=lambda row: row[0]))
            else:
                replacement = previous[left:] if left else previous
            if len(replacement) > MAX_SAMPLES:
                for row in islice(replacement, len(replacement) - MAX_SAMPLES):
                    forget(row)
                replacement = replacement[-MAX_SAMPLES:]
            for block in changed_blocks:
                stamp = repaired.get(block)
                if stamp is None:
                    continue
                index = bisect_left(replacement, stamp, key=lambda row: row[0])
                if (index == len(replacement) or replacement[index][0] != stamp or
                        replacement[index][2] != block):
                    repaired.pop(block, None)
            self.repaired = repaired
            summaries = (self.summaries if not replace and replacement is previous
                         else HistorySummaries(replacement))
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
            with HISTORY_READ_LOCK:
                saved = self._stored_rows(query_start, query_end, history)
            # Consult every cached block, not just the selected timestamps:
            # corrections can move a persisted sample outside this window.
            for row in history:
                if row[2] is not None:
                    saved.pop(row[2], None)
            if saved:
                stored = sorted(saved.values(), key=lambda row: row[0])
                del saved
                selected_left = bisect_right(history, query_start, key=lambda row: row[0])
                selected_right = bisect_right(history, query_end, key=lambda row: row[0])
                history = tuple(merge_price_rows(stored, islice(history, selected_left, selected_right)))
                if len(history) > MAX_SAMPLES:
                    raise ValueError('Too many price samples; request a smaller window.')
                summaries = HistorySummaries(history)
                generation = None
            history_ready = query_end <= cutoff or history_ready
            start, end = max(start, query_start), min(end, query_end)
        else:
            if fetched_at is None and not history_ready:
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
        rows = (list(islice(history, left, right)) if after_block is None else
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
    def __init__(self, cache, chain=None, archive=None):
        self.cache = cache
        self.chain = chain or HubChain()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='price-history', daemon=True)
        self.listener = threading.Thread(target=self.listen, name='price-finality', daemon=True)
        self.loader = threading.Thread(target=self.restore, name='price-restore', daemon=True)
        self.changed = threading.Condition()
        self.target = None
        self.archive = archive if archive is not None else cache.archive
        self.archive_seq = 0
        self.archive_ready = False
        self.restore_before = None
        self.restored = cache.history_path is None
        self.head = None
        self.cursor = None
        self.processed = set()

    def saved_state(self, head):
        """Seek durable progress and table tips without scanning retained history."""
        with self.cache.lock:
            tip = max((row[2] for row in self.cache.rows if row[2] is not None), default=0)
        cursor = None
        finalized = None
        for path, table in ((self.cache.history_path, 'repairs'), (self.archive, 'samples')):
            if path is None or not path.exists():
                continue
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro',
                                         uri=True, timeout=1)) as db:
                if db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
                    row = db.execute(f'SELECT number FROM {table} ORDER BY number DESC LIMIT 1').fetchone()
                    if row is not None:
                        tip = max(tip, row[0])
                        if row[0] == head and any(
                                column[1] == 'timestamp' for column in db.execute(f'PRAGMA table_info({table})')):
                            stamp = db.execute(f'SELECT timestamp FROM {table} WHERE number=?',
                                               (head,)).fetchone()[0]
                            finalized = (head, stamp / 1000)
                if table == 'repairs' and db.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='price_progress'").fetchone():
                    row = db.execute('SELECT number FROM price_progress WHERE id=1').fetchone()
                    if row is not None:
                        cursor = row[0]
                        tip = max(tip, cursor)
        if head < tip:
            raise ValueError('Hub finalized head moved backwards')
        if finalized is not None:
            with self.cache.lock:
                self.cache.finalized = finalized
        # Older installations wrote tip-first without a cursor. Revisit their
        # bounded retained tail before advancing; genuinely new installs seed 15.
        return cursor if cursor is not None else max(0, (tip or head) - LIVE_BLOCKS)

    def batch_rows(self, first, last, head):
        """Read one bounded range and its immutable neighboring price clocks."""
        rows = {}
        for path, table, divisor in ((self.cache.history_path, 'repairs', 1),
                                     (self.archive, 'samples', 1000)):
            if path is None or not path.exists():
                continue
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro',
                                         uri=True, timeout=1)) as db:
                if not db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
                    continue
                select = f'SELECT sample_time,price,number FROM {table} WHERE price IS NOT NULL AND '
                saved = list(db.execute(select + 'number>=? AND number<=?', (first, last)))
                for clause, args in (
                        ('number<? ORDER BY number DESC LIMIT 1', (first,)),
                        ('number>? ORDER BY number LIMIT 1', (last,)),
                        ('number=?', (head,))):
                    saved.extend(db.execute(select + clause, args))
                rows.update((number, (stamp / divisor, price, number))
                            for stamp, price, number in saved)
        with self.cache.lock:
            history = self.cache.rows
        lower = upper = None
        for row in history:
            number = row[2]
            if number is None:
                continue
            if number > head:
                raise ValueError('Hub finalized head moved backwards')
            if first <= number <= last or number == head or number in rows:
                rows[number] = row
            elif number < first and (lower is None or number > lower[2]):
                lower = row
            elif number > last and (upper is None or number < upper[2]):
                upper = row
        for row in (lower, upper):
            if row is not None:
                rows[row[2]] = row
        return rows

    @staticmethod
    def save_progress(db, cursor):
        db.execute('''CREATE TABLE IF NOT EXISTS price_progress (
            id INTEGER PRIMARY KEY CHECK(id=1), number INTEGER NOT NULL)''')
        db.execute('INSERT OR REPLACE INTO price_progress VALUES(1,?)', (cursor,))

    def ingest(self, head):
        """Consume a notified target in bounded batches; return whether caught up."""
        if self.head is not None and head < self.head:
            raise ValueError('Hub finalized head moved backwards')
        initial = self.cursor is None
        if self.cursor is None:
            self.chain.verify()
            cursor = self.saved_state(head)
            # Record the lower boundary before a tip-first read can persist a
            # newer sample. A crash must not mistake that sample for completion.
            if self.cache.history_path is not None:
                with closing(price_database(self.cache.history_path)) as db, db:
                    self.save_progress(db, cursor)
            self.cursor = cursor
        self.head = head
        if self.cursor >= head:
            if initial:
                existing = self.batch_rows(max(1, head - LIVE_BLOCKS + 1), head, head)
                self.cache.merge(existing.values(), time.time(), mark_fresh=head in existing)
            return True
        last = min(head, self.cursor + LIVE_BLOCKS)
        if head > last:
            last -= 1  # Reserve one read for the newest finalized tip.
        existing = self.batch_rows(self.cursor + 1, last, head)
        numbers = sorted(existing)
        incoming = list(existing.values())
        prices = []
        completed = set()
        finalized = None
        try:
            # Read the newest tip first, then fill this contiguous batch
            # backwards between preserved clocks. This also fits repeated
            # chain timestamps without pushing estimates into the future.
            pending = [head]
            pending.extend(range(last, self.cursor, -1))
            for number in dict.fromkeys(pending):
                if self.stop.is_set():
                    break
                if number in existing or number in self.processed:
                    completed.add(number)
                    continue
                try:
                    _, at, timestamp, price, reserves = read_sample(self.chain.rpc, number)
                except Unavailable:
                    completed.add(number)
                    continue
                if number == head:
                    finalized = (number, timestamp / 1000)
                if price is not None:
                    index = bisect_left(numbers, number)
                    lower = (numbers[index - 1], existing[numbers[index - 1]][0]) if index else None
                    upper = (numbers[index], existing[numbers[index]][0]) if index < len(numbers) else None
                    stamp = price_time(number, timestamp, lower, upper, now=time.time())
                    prices.append((number, at, timestamp, stamp, price, reserves))
                    existing[number] = (stamp, price, number)
                    numbers.insert(index, number)
                completed.add(number)
        finally:
            # Persist every successful sample even if a later read fails. The
            # cursor only crosses blocks actually processed, including genuine
            # missing-pool prices, which remain gaps rather than fabricated rows.
            processed = self.processed | completed
            cursor = self.cursor
            while cursor + 1 in processed:
                cursor += 1
            if self.cache.history_path is not None:
                with closing(price_database(self.cache.history_path)) as db, db:
                    db.executemany('INSERT OR IGNORE INTO repairs VALUES(?,?,?,?,?,?)', prices)
                    self.save_progress(db, cursor)
                    for row in prices:
                        incoming.append(db.execute(
                            'SELECT sample_time,price,number FROM repairs WHERE number=?',
                            (row[0],)).fetchone())
            else:
                incoming.extend((row[3], row[4], row[0]) for row in prices)
            self.cache.merge(incoming, time.time(), mark_fresh=head in existing)
            self.cursor = cursor
            self.processed = {number for number in processed if number > cursor}
            if finalized is not None:
                with self.cache.lock:
                    self.cache.finalized = finalized
        return self.cursor >= head

    def load_history(self):
        if self.restored:
            return True
        with closing(price_database(self.cache.history_path)) as db:
            rows = db.execute(
                'SELECT sample_time,price,number FROM repairs WHERE sample_time>? '
                'AND number<? ORDER BY number DESC LIMIT 2000',
                (time.time() - HISTORY_SECONDS,
                 self.restore_before if self.restore_before is not None else 2**63 - 1)).fetchall()
        if rows:
            self.cache.merge(rows, time.time(), mark_fresh=False, missing_only=True)
            self.restore_before = rows[-1][2]
        self.restored = len(rows) < 2000
        return self.restored

    def load_archive(self):
        if self.archive is None or not self.archive.exists():
            return True
        with closing(sqlite3.connect(self.archive.resolve().as_uri() + '?mode=ro',
                                     uri=True, timeout=1)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='samples'").fetchone():
                return True
            rows = db.execute(
                'SELECT seq,sample_time,price,number FROM samples '
                'WHERE seq>? AND sample_time>? ORDER BY seq LIMIT 2000',
                (self.archive_seq, (time.time() - HISTORY_SECONDS) * 1000)).fetchall()
            if len(rows) < 2000:
                self.archive_seq = db.execute('SELECT COALESCE(MAX(seq),0) FROM samples').fetchone()[0]
            else:
                self.archive_seq = rows[-1][0]
        if rows:
            self.cache.merge([(stamp / 1000, price, number) for _, stamp, price, number in rows
                              if price is not None], time.time(), mark_fresh=False, missing_only=True)
        return len(rows) < 2000

    def listen(self):
        delay = 1
        while not self.stop.is_set():
            try:
                with closing(self.chain.finalized_heads(self.stop)) as heads:
                    for head in heads:
                        if self.stop.is_set():
                            break
                        with self.changed:
                            if self.target is not None and head < self.target:
                                raise ValueError('Hub finalized head moved backwards')
                            if self.target is None or head > self.target:
                                self.target = head
                                delay = 1
                                self.changed.notify_all()
                    if not self.stop.is_set():
                        raise ConnectionError('Hub finalized-head subscription ended')
            except Exception:
                if not self.stop.is_set():
                    logging.exception('Finalized Hub subscription failed')
            if self.stop.wait(delay):
                return
            delay = min(delay * 2, 30)

    def restore(self):
        while not self.stop.is_set():
            failed = False
            try:
                self.load_history()
                self.archive_ready = self.load_archive()
            except (sqlite3.Error, OSError):
                failed = True
                logging.exception('Saved price history load failed')
            with self.cache.lock:
                self.cache.history_ready = self.archive_ready and self.restored
            # Only local SQLite refreshes are periodic; subscription startup and
            # live ingestion do not depend on historical loading or its failures.
            self.stop.wait(6 if failed or (self.archive_ready and self.restored) else 0.05)

    def run(self):
        self.listener.start()
        self.loader.start()
        delay = 1
        retry_at = 0
        try:
            while not self.stop.is_set():
                with self.changed:
                    head = self.target
                    if (head is None or self.cursor is not None and self.cursor >= head or
                            time.monotonic() < retry_at):
                        self.changed.wait(timeout=.25)
                        continue
                try:
                    self.ingest(head)
                    delay = 1
                    retry_at = 0
                except Exception:
                    logging.exception('Finalized Hub price ingestion failed')
                    retry_at = time.monotonic() + delay
                    delay = min(delay * 2, 30)
        finally:
            self.stop.set()
            self.listener.join()
            self.loader.join()

    def shutdown(self):
        self.stop.set()
        with self.changed:
            self.changed.notify_all()
        self.thread.join(timeout=25)


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
        self.treasury = TreasuryHistory()
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
        if url.path == '/health':
            finalized = self.server.cache.finalized
            age = None if finalized is None else max(0., time.time() - finalized[1])
            status = 'starting' if age is None else 'stale' if age > HEALTH_STALE_SECONDS else 'ok'
            self.respond(200 if status == 'ok' else 503, {
                'status': status, 'finalizedBlock': finalized and finalized[0],
                'finalizedAgeSeconds': None if age is None else round(age, 1),
                'staleAfterSeconds': HEALTH_STALE_SECONDS})
            return
        if url.path == '/api/historic':
            try:
                params = urllib.parse.parse_qs(url.query, max_num_fields=20, keep_blank_values=True)
                selected = params.get('series', [])
                series = selected[0].split(',') if len(selected) == 1 else []
                if (len(set(series)) != len(series) or
                        not set(series) <= {'price', 'ti', 'treasury'} or 'price' not in series):
                    raise ValueError('Only price data is currently available. Use series=price.')
                # Older clients may still select disabled state series; serve only prices.
                series = ['price']
                raw = params.get('format', ['ranges-v1'])[0] == 'raw'
                streaming = params.get('format', [''])[0] == 'stream'
                histories = {}
                end = None
                for name, history in (('ti', self.server.issuance), ('treasury', self.server.treasury)):
                    if name not in series:
                        continue
                    start_name, end_name, cursor_name = name + 'Start', name + 'End', name + 'Cursor'
                    if any(len(params.get(key, [])) > 1 for key in (start_name, end_name, cursor_name)):
                        raise ValueError(f'Choose one {history.label.lower()} window and cursor.')
                    state_end = float(params.get(end_name, params.get('end', [time.time()]))[0])
                    state_start = float(params.get(start_name, params.get('start', [state_end - 3600]))[0])
                    if end_name not in params and 'end' not in params:
                        state_start = max(state_start, state_end - HISTORY_SECONDS)
                    state_start, state_end = time_window({'start': [state_start], 'end': [state_end]})
                    histories[name] = history.selection(
                        state_start, state_end, params.get(cursor_name, [None])[0])
                    end = state_end if end is None else max(end, state_end)
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
                logging.exception('Historical state read failed')
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
                    if not histories:
                        self.respond(503, {'error': 'Price history is warming up. Please try again shortly.'})
                        return
                    chunks = encode_response((), None, False, None, raw=raw, streaming=streaming)
            chunks = historic_response(chunks, histories, streaming, raw=raw)
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
    parser.add_argument('--hub-rpc', default=RPC,
                        help='Polkadot Asset Hub HTTP archive RPC for finalized prices and state history')
    parser.add_argument('--hub-ws',
                        help='Asset Hub finalized-head WebSocket URL (default: derived from --hub-rpc)')
    parser.add_argument('--archive', type=Path, default=Path('/var/lib/dotusd-info/archive.sqlite3'))
    parser.add_argument('--gap-repair-history', type=Path,
                        help='Persist live prices, issuance and Treasury indefinitely; gradually recover recent history')
    parser.add_argument('--no-browser', action='store_true', help='Serve without opening a browser')
    args = parser.parse_args()
    server = CacheServer((args.bind, args.port), Handler)
    server.cache = HistoryCache(archive=args.archive, history_path=args.gap_repair_history)
    server.issuance = IssuanceCache(history_path=args.gap_repair_history)
    server.treasury = TreasuryHistory(args.gap_repair_history)
    if args.gap_repair_history:
        # Establish WAL before concurrent workers open a newly created database.
        with closing(price_database(args.gap_repair_history)):
            pass
    worker = None
    repair = None
    issuance = None
    try:
        worker = HistoryWorker(server.cache, chain=HubChain(args.hub_rpc, ws_endpoint=args.hub_ws),
                               archive=args.archive)
        worker.thread.start()
        issuance = IssuanceWorker(server.issuance, chain=HubChain(args.hub_rpc))
        issuance.thread.start()
        if args.gap_repair_history:
            repair = GapRepair(server.cache, args.gap_repair_history,
                               chain=HubChain(args.hub_rpc), issuance=server.issuance,
                               treasury=server.treasury)
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
            worker.shutdown()
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
