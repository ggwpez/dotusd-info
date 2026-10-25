#!/usr/bin/env python3
"""Wait up to 60 seconds for the public site to serve the expected release."""
import argparse
import json
import re
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit



def fetch_version(url, timeout=5):
    # curl's total timeout also bounds DNS, TLS, and slow response bodies.
    timeout = min(5, timeout)
    result = subprocess.run(
        ['curl', '--fail', '--silent', '--show-error', '--max-time',
         str(timeout), '--header', 'Cache-Control: no-cache', url],
        capture_output=True, text=True, timeout=timeout, check=True)
    payload = json.loads(result.stdout)
    actual = payload.get('version') if isinstance(payload, dict) else None
    if not isinstance(actual, str):
        raise ValueError('Response has no version string')
    return actual


def version_number(version):
    match = re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\+[0-9a-f]{12}', version)
    if not match:
        raise ValueError(f'Invalid release version: {version!r}')
    return tuple(map(int, match.groups()))


def require_newer_version(expected, url):
    # The existing alias also works on releases predating the /version route.
    parts = urlsplit(url)
    if parts.path == '/version':
        url = urlunsplit(parts._replace(path='/api/version'))
    try:
        actual = fetch_version(url)
        local_number, remote_number = version_number(expected), version_number(actual)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise ValueError(f'Cannot verify deployed version at {url}; refusing deployment: {error}') from error
    if local_number <= remote_number:
        raise ValueError(
            f'Refusing deployment: local {expected} must be newer than deployed {actual}. '
            'Run `just test` (or `just test minor` for features), commit VERSION with '
            'the changes, then run `just deploy`.')
    print(f'Version upgrade verified: {actual} -> {expected}', flush=True)


def wait_for_version(expected, url):
    deadline = time.monotonic() + 60
    last_error = 'No response'
    while time.monotonic() < deadline:
        started = time.monotonic()
        remaining = deadline - started
        if remaining <= 0:
            break
        try:
            actual = fetch_version(url, remaining)
            if actual == expected:
                print(f'Deployed {expected}: verified at {url}', flush=True)
                return
            last_error = f'Version mismatch: expected {expected}, received {actual}'
        except subprocess.CalledProcessError as error:
            last_error = f'Endpoint unavailable: {error.stderr.strip()}'
        except subprocess.TimeoutExpired:
            last_error = 'Endpoint request timed out'
        except (ValueError, OSError) as error:
            last_error = f'Invalid version response: {error}'
        print(f'{last_error}; waiting for deployment', flush=True)
        delay = min(started + 5, deadline) - time.monotonic()
        if delay > 0:
            time.sleep(delay)
    raise TimeoutError(f'Deployment verification timed out after 60s at {url}. {last_error}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('expected', help='version recorded before deployment')
    parser.add_argument('url', nargs='?', default='https://dotusd.info/version')
    parser.add_argument('--before-deploy', action='store_true', help='refuse unchanged or older version numbers')
    args = parser.parse_args()
    try:
        if args.before_deploy:
            require_newer_version(args.expected, args.url)
        else:
            wait_for_version(args.expected, args.url)
    except (TimeoutError, ValueError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
