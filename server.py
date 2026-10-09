#!/usr/bin/env python3
"""Read-only dotUSD peg monitor backed by finalized Asset Hub pool history."""
import argparse
from contextlib import contextmanager
import json
import math
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
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


def query_window(params):
    end = float(params.get('end', [time.time()])[0])
    start = float(params.get('start', [end - 3600])[0])
    if not all(map(math.isfinite, (start, end))) or not 1 <= end - start <= 604800 or start < 0 or end > time.time() + 60:
        raise ValueError('Choose a time window between one second and seven days.')
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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == '/api/prices':
            try:
                query = query_window(urllib.parse.parse_qs(url.query))
            except (ValueError, OverflowError):
                self.respond(400, {'error': 'Invalid time window (maximum seven days).'})
                return
            try:
                with self.server.http.open(self.server.upstream + '/query?' + query, timeout=20) as response:
                    rows = samples(json.load(response))
                self.respond(200, {'samples': rows, 'fetchedAt': time.time()})
            except Exception as error:
                self.log_error('Price source unavailable: %s: %s', type(error).__name__, error)
                self.respond(502, {'error': 'Price history is temporarily unavailable. Please try again shortly.'})
            return
        filename = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}.get(url.path)
        if not filename:
            self.respond(404, {'error': 'Not found'})
            return
        mime = {'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}
        self.respond(200, (ROOT / filename).read_bytes(), mime[filename.split('.')[-1]])

    def respond(self, status, body, content_type='application/json'):
        if not isinstance(body, bytes):
            body = json.dumps(body, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=3001)
    parser.add_argument('--ssh-host', default='server', help='SSH config host or user@host (default: server)')
    parser.add_argument('--upstream', help='Use an existing Prometheus API base instead of opening SSH')
    parser.add_argument('--no-browser', action='store_true', help='Serve without opening a browser')
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with price_source(args.upstream, args.ssh_host) as upstream:
            server.upstream = upstream
            browser_host = '127.0.0.1' if args.bind == '0.0.0.0' else args.bind
            url = f'http://{browser_host}:{server.server_port}'
            print(f'dotUSD: {url}', flush=True)
            if not args.no_browser:
                threading.Thread(target=webbrowser.open, args=(url,), kwargs={'new': 2}, daemon=True).start()
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(f'dotUSD: {error}') from error
