#!/usr/bin/env python3
"""Compare backend concurrency and bursts without sending load to production.

Uses a separate server process, a fixed fixture, and no upstream worker/Caddy.
Example: python3 tools/benchmark_requests.py --fixture /tmp/prices.json
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import http.client
import importlib.util
import json
import math
import multiprocessing
from pathlib import Path
import statistics
import threading
import time
from urllib.parse import urlencode


def serve(pipe, source, fixture, backlog):
    spec = importlib.util.spec_from_file_location('benchmark_server', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Alternative source files still use this checkout's release assets.
    module.ROOT = Path(__file__).resolve().parents[1]

    class QuietHandler(module.Handler):
        def log_message(self, *args):
            pass

    if backlog is not None:
        module.CacheServer.request_queue_size = backlog
    server = module.CacheServer(('127.0.0.1', 0), QuietHandler)
    data = json.loads(Path(fixture).read_text())
    rows = data['samples']
    server.cache = module.HistoryCache()
    now = time.time()
    server.cache.merge(rows, now)
    server.cache.history_ready = True
    end = rows[-1][0] + 1
    start = max(0, end - 604800)
    common = {'start': start, 'end': end, 'windowStart': start,
              'windowEnd': end, 'format': 'stream'}
    paths = {'page': '/', 'history': '/api/prices?' + urlencode(common),
             'poll': '/api/prices?' + urlencode(dict(common, afterBlock=rows[-1][2] - 3))}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    pipe.send((server.server_port, paths, len(rows)))
    try:
        pipe.recv()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def percentile(values, fraction):
    return round(sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)], 2) if values else None


def run(port, paths, concurrency, count, rounds):
    results = []
    elapsed = 0
    # 70% polls, 20% initial documents, 10% full-history downloads.
    mix = ['poll'] * 7 + ['page'] * 2 + ['history']
    for round_number in range(rounds):
        barrier = threading.Barrier(concurrency)

        def client(worker):
            samples = []
            barrier.wait(timeout=10)
            for request_number in range(count):
                kind = mix[(round_number * concurrency * count + worker * count + request_number) % len(mix)]
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
                started = time.perf_counter()
                try:
                    connection.request('GET', paths[kind])
                    response = connection.getresponse()
                    response.read()
                    code = str(response.status)
                except (OSError, http.client.HTTPException) as error:
                    code = type(error).__name__
                finally:
                    connection.close()
                samples.append((kind, code, (time.perf_counter() - started) * 1000))
            return samples

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for samples in pool.map(client, range(concurrency)):
                results.extend(samples)
        elapsed += time.perf_counter() - started

    def summarize(samples):
        success = [ms for _, code, ms in samples if code == '200']
        return {'statuses': dict(Counter(code for _, code, _ in samples)),
                'success_p50_ms': round(statistics.median(success), 2) if success else None,
                'success_p95_ms': percentile(success, .95),
                'all_p95_ms': percentile([ms for _, _, ms in samples], .95)}

    return {'concurrency': concurrency, 'requests': len(results),
            'elapsed_s': round(elapsed, 3),
            'successful_requests_per_s': round(sum(code == '200' for _, code, _ in results) / elapsed, 1),
            **summarize(results),
            'endpoints': {kind: summarize([r for r in results if r[0] == kind]) for kind in paths}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, required=True, help='Raw /api/prices JSON captured once')
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1] / 'server.py')
    parser.add_argument('--backlog', type=int, help='Override TCP backlog for a controlled comparison')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--requests-per-client', type=int, default=8)
    parser.add_argument('--concurrency', default='1,4,8,16,32,64')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    parent, child = multiprocessing.Pipe()
    process = multiprocessing.Process(target=serve, args=(child, args.source, args.fixture, args.backlog))
    process.start()
    report = {'source': str(args.source), 'backlog_override': args.backlog, 'rounds': args.rounds,
              'requests_per_client': args.requests_per_client, 'results': []}
    try:
        if not parent.poll(20):
            raise TimeoutError('Benchmark server did not start')
        port, paths, report['samples'] = parent.recv()
        # Warm release assets and the HTTP path before collecting measurements.
        run(port, paths, 1, 1, 1)
        for mode, count in [('steady', args.requests_per_client), ('burst', 1)]:
            for concurrency in map(int, args.concurrency.split(',')):
                result = {'mode': mode, **run(port, paths, concurrency, count, args.rounds)}
                report['results'].append(result)
                print(json.dumps(result), flush=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    finally:
        parent.send('stop')
        process.join(timeout=20)
        if process.is_alive():
            process.terminate()
            process.join()


if __name__ == '__main__':
    main()
