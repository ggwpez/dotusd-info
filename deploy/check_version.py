#!/usr/bin/env python3
"""Wait up to 25 seconds for the public site to serve the expected release."""
import argparse
import json
import subprocess
import time


def wait_for_version(expected, url):
    deadline = time.monotonic() + 25
    last_error = 'No response'
    while time.monotonic() < deadline:
        started = time.monotonic()
        remaining = deadline - started
        if remaining <= 0:
            break
        try:
            # curl's total timeout also bounds DNS, TLS, and slow response bodies.
            result = subprocess.run(
                ['curl', '--fail', '--silent', '--show-error', '--max-time',
                 str(min(5, remaining)), '--header', 'Cache-Control: no-cache', url],
                capture_output=True, text=True, timeout=remaining, check=True)
            payload = json.loads(result.stdout)
            actual = payload.get('version') if isinstance(payload, dict) else None
            if not isinstance(actual, str):
                raise ValueError('Response has no version string')
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
    raise TimeoutError(f'Deployment verification timed out after 25s at {url}. {last_error}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('expected', help='version recorded before deployment')
    parser.add_argument('url', nargs='?', default='https://dotusd.info/version')
    args = parser.parse_args()
    try:
        wait_for_version(args.expected, args.url)
    except TimeoutError as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
