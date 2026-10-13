#!/usr/bin/env python3
"""Import local DEX reconstruction batches sequentially, with production health gates.

Runs outside production. At most 1,024 samples per import, at least 15 seconds
between imports, and never overwrites an unconsumed batch. Resume uses local markers.
"""
import argparse
import json
from pathlib import Path
import subprocess
import time

REMOTE_GATE = '''sudo -n python3 - <<'REMOTE'
import json, pathlib, subprocess, time, urllib.request
pending = pathlib.Path('/var/lib/dotusd-quotes/backfill.json')
if pending.exists():
    raise SystemExit(2)
used = int(subprocess.check_output(['systemctl','show','dotusd-info','--property=MemoryCurrent','--value']))
if used > 180 * 1024 * 1024:
    raise SystemExit(3)
now = time.time()
for route in ('/api/prices', '/api/coingecko/prices'):
    with urllib.request.urlopen(f'http://127.0.0.1:3001{route}?start={now-300}&end={now}&format=raw', timeout=3) as response:
        data = json.load(response)
        if response.status != 200 or now - (data.get('fetchedAt') or 0) > 180:
            raise SystemExit(4)
REMOTE'''
REMOTE_INSTALL = '''sudo -n sh -c 'test ! -e /var/lib/dotusd-quotes/backfill.json && install -o $(stat -c %u /var/lib/dotusd-quotes/history.json) -g $(stat -c %g /var/lib/dotusd-quotes/history.json) -m 0600 /tmp/dotusd-dex-import.json /var/lib/dotusd-quotes/backfill.pending && mv /var/lib/dotusd-quotes/backfill.pending /var/lib/dotusd-quotes/backfill.json' '''


def validate(payload):
    rows = payload['samples']
    if payload.get('source') != 'dex-last-trade-usdt-backfill-v2' or not 1 <= len(rows) <= 1024:
        raise ValueError('Invalid import batch')
    if len(rows) != len(payload['evidence']):
        raise ValueError('Missing import evidence')
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--host', default='server')
    parser.add_argument('--expected-samples', type=int, required=True)
    parser.add_argument('--max-memory-mib', type=int, default=180,
                        help='Pause imports above this measured service memory usage')
    args = parser.parse_args()
    if not 1 <= args.max_memory_mib <= 4096:
        parser.error("--max-memory-mib must be between 1 and 4096")
    gate = REMOTE_GATE.replace("180 * 1024 * 1024", f"{args.max_memory_mib} * 1024 * 1024")
    def remote(command):
        return subprocess.run(['ssh', '-o', 'ConnectTimeout=10', args.host, command], timeout=30, check=False)
    while True:
        paths = sorted(args.directory.glob('*.json'), reverse=True)
        done = sum(validate(json.loads(p.read_text())) for p in paths if p.with_suffix('.imported').exists())
        if done >= args.expected_samples:
            print(f'Complete: {done} samples imported', flush=True)
            remote('rm -f /tmp/dotusd-dex-import.json')
            return
        pending = [p for p in paths if not p.with_suffix('.imported').exists()]
        if pending and remote(gate).returncode == 0:
            path = pending[0]
            count = validate(json.loads(path.read_text()))
            subprocess.run(['scp', '-q', str(path), args.host+':/tmp/dotusd-dex-import.json'], timeout=30, check=True)
            if remote(REMOTE_INSTALL).returncode != 0:
                raise RuntimeError('Import staging failed')
            # Wait for consumption, persistence and healthy normal-price responses.
            deadline = time.monotonic() + 120
            while remote(gate).returncode != 0:
                if time.monotonic() > deadline:
                    raise TimeoutError('Import not consumed or production unhealthy; stopping')
                time.sleep(15)
            path.with_suffix('.imported').write_text(str(time.time()))
            print(f'Imported {done+count}/{args.expected_samples}: {path.name}', flush=True)
        time.sleep(15)


if __name__ == '__main__':
    main()
