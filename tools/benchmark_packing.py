"""Compare lossless sample encodings; never changes the production API.

Usage: python3 tools/benchmark_packing.py [--samples snapshot.json]
Python 3.14 adds optional zstd measurements. Sizes exclude shared metadata and
HTTP headers. Compression resets every six hours, like historical API chunks.
"""

import argparse
import gzip
import itertools
import json
import statistics
import time
import urllib.request
from collections import Counter
from pathlib import Path

try:
    from compression import zstd
except ImportError:
    zstd = None


def encode(rows, kind):
    if kind in ('current', 'compact'):
        return rows
    if kind == 'repeat':
        result, previous = [], None
        for t, price, block in rows:
            result.append([t, block] if price == previous else [t, price, block])
            previous = price
        return result
    if not rows:
        return []
    t, price, block = rows[0]
    result = [[t, block, price]]
    changes = []
    for a, b in zip(rows, rows[1:]):
        dt = b[0] - a[0]
        changes.append((int(dt) if dt == int(dt) else dt, b[2] - a[2], b[1]))
    groups = ([(1, change) for change in changes] if kind == 'delta' else
              [(sum(1 for _ in group), key) for key, group in itertools.groupby(changes)])
    for count, (dt, db, value) in groups:
        record = [dt, db] if kind == 'delta' else [count, dt, db]
        if value != price:
            record.append(value)
        result.append(record)
        price = value
    return result


def decode(records, kind):
    if kind in ('current', 'compact'):
        return records
    rows, price = [], None
    if kind == 'repeat':
        for record in records:
            if len(record) == 3:
                t, price, block = record
            else:
                t, block = record
            rows.append([t, price, block])
        return rows
    if not records:
        return []
    t, block, price = records[0]
    rows.append([t, price, block])
    for record in records[1:]:
        if kind == 'delta':
            count, dt, db, *value = [1, *record]
        else:
            count, dt, db, *value = record
        if value:
            price = value[0]
        for _ in range(count):
            t += dt
            block += db
            rows.append([t, price, block])
    return rows


def measure(rows):
    chunks = []
    for _, group in itertools.groupby(rows, lambda row: int((row[0] - rows[0][0]) // 21600)):
        chunks.append(list(group))
    result = {}
    for kind in ('current', 'compact', 'repeat', 'delta', 'runs'):
        totals = {'json': 0, 'gzip': 0, **({'zstd': 0} if zstd else {})}
        for chunk in chunks:
            packed = encode(chunk, kind)
            body = json.dumps(packed, allow_nan=False,
                              separators=None if kind == 'current' else (',', ':')).encode()
            assert decode(json.loads(body), kind) == chunk, f'{kind} failed round trip'
            totals['json'] += len(body)
            totals['gzip'] += len(gzip.compress(body, compresslevel=6, mtime=0))
            if zstd:
                totals['zstd'] += len(zstd.compress(body, level=3))
        result[kind] = totals
    return result


def project_week(rows):
    # Repeat the observed pattern, shifting timestamps and block numbers.
    # This is a size scenario, not a forecast of future trading activity.
    step = statistics.median(b[0] - a[0] for a, b in zip(rows, rows[1:]))
    duration = rows[-1][0] - rows[0][0] + step
    if duration == int(duration):
        duration = int(duration)
    block_span = rows[-1][2] - rows[0][2] + 1
    end = rows[0][0] + 604800
    output, cycle = [], 0
    while True:
        for t, price, block in rows:
            shifted = t + cycle * duration
            if shifted >= end:
                return output
            output.append([shifted, price, block + cycle * block_span])
        cycle += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=Path, help='JSON array of raw samples')
    args = parser.parse_args()
    if args.samples:
        rows = json.loads(args.samples.read_text())
    else:
        end = int(time.time())
        req = urllib.request.Request(
            f'https://dotusd.info/api/historic?series=price&start={end - 604800}&end={end}&format=raw',
            headers={'Accept-Encoding': 'gzip'})
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read()
            if response.headers.get('Content-Encoding') == 'gzip':
                body = gzip.decompress(body)
            payload = json.loads(body)
        if not payload['historyReady']:
            raise SystemExit('History is still loading; retry later.')
        rows = payload['samples']
    if len(rows) < 2 or rows[-1][0] <= rows[0][0]:
        raise SystemExit('Need at least two samples with increasing timestamps.')
    week = project_week(rows)
    price_runs = [sum(1 for _ in group) for _, group in itertools.groupby(rows, lambda r: r[1])]
    output = {
        'observed_samples': len(rows),
        'observed_hours': (rows[-1][0] - rows[0][0]) / 3600,
        'first_timestamp': rows[0][0], 'last_timestamp': rows[-1][0],
        'unchanged_price_fraction': (len(rows) - len(price_runs)) / (len(rows) - 1),
        'price_runs': len(price_runs), 'longest_price_run': max(price_runs),
        'median_price_run': statistics.median(price_runs),
        'timestamp_steps': dict(Counter(b[0] - a[0] for a, b in zip(rows, rows[1:]))),
        'projected_week_samples': len(week),
        'observed_bytes': measure(rows), 'projected_week_bytes': measure(week),
        'method': 'Six-hour chunks; gzip level 6, zstd level 3. Week repeats observed pattern. All formats round-trip exactly.',
    }
    print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
