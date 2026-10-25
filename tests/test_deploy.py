"""Deployment verification retries stale or unavailable releases with a deadline."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from deploy import check_version
import release


COMMIT = 'abcdef1234567890abcdef1234567890abcdef1234'
EXPECTED = f'1.1.0+{COMMIT[:7]}'


class DeploymentTests(unittest.TestCase):
    def verify(self, responses, wake_delays=()):
        self.elapsed = 0
        self.attempts = []
        self.timeouts = []
        wake_delays = iter(wake_delays)

        def sleep(seconds):
            self.elapsed += seconds + next(wake_delays, 0)

        def request(*args, **kwargs):
            self.attempts.append(self.elapsed)
            timeout = kwargs['timeout']
            self.timeouts.append(timeout)
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, min(5, 60 - self.elapsed))
            command = args[0]
            self.assertEqual(float(command[command.index('--max-time') + 1]), timeout)
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return subprocess.CompletedProcess(args[0], 0, response, '')

        with patch.object(check_version.time, 'monotonic', side_effect=lambda: self.elapsed), \
             patch.object(check_version.time, 'sleep', side_effect=sleep), \
             patch.object(check_version.subprocess, 'run', side_effect=request), \
             patch('builtins.print'):
            check_version.wait_for_version(EXPECTED, 'https://example.org/version')

    def test_returns_immediately_on_matching_version(self):
        self.verify(iter([json.dumps({'version': EXPECTED})]))
        self.assertEqual(self.attempts, [0])
        self.assertEqual(self.elapsed, 0)

    def test_retries_unavailable_malformed_and_old_versions(self):
        self.verify(iter([
            subprocess.CalledProcessError(22, 'curl', stderr='HTTP 503'),
            'not JSON', '{"version":"old"}',
            json.dumps({'version': EXPECTED}),
        ]))
        self.assertEqual(self.attempts, [0, 5, 10, 15])

    def test_stale_version_fails_after_60_seconds(self):
        with self.assertRaisesRegex(TimeoutError, '60s.*Version mismatch.*received old'):
            self.verify(iter(['{"version":"old"}'] * 12))
        self.assertEqual(self.attempts, list(range(0, 60, 5)))
        self.assertEqual(self.elapsed, 60)

    def test_unavailable_endpoint_fails_after_60_seconds(self):
        with self.assertRaisesRegex(TimeoutError, '60s.*Endpoint request timed out'):
            self.verify(iter([subprocess.TimeoutExpired('curl', 5)] * 12))
        self.assertEqual(self.attempts, list(range(0, 60, 5)))
        self.assertEqual(self.elapsed, 60)

    def test_recovers_late_in_verification_window(self):
        self.verify(iter(
            ['{"version":"old"}'] * 11 + [json.dumps({'version': EXPECTED})]))
        self.assertEqual(self.attempts, list(range(0, 60, 5)))
        self.assertEqual(self.elapsed, 55)

    def test_last_request_is_bounded_by_remaining_deadline(self):
        # A delayed wake-up leaves only two seconds for the final request.
        with self.assertRaisesRegex(TimeoutError, '60s.*Endpoint request timed out'):
            self.verify(
                iter([subprocess.TimeoutExpired('curl', 5)] * 12),
                wake_delays=[0] * 10 + [3])
        self.assertEqual(self.attempts, list(range(0, 55, 5)) + [58])
        self.assertEqual(self.timeouts, [5] * 11 + [2])
        self.assertEqual(self.elapsed, 60)

    def test_missing_version_is_not_a_success(self):
        with self.assertRaisesRegex(TimeoutError, 'Response has no version string'):
            self.verify(iter(['{}'] * 12))

    def test_postdeploy_requires_exact_commit_identity(self):
        for actual in (f'1.1.0+{COMMIT[:12]}', f'1.1.0+{"0" * 7}'):
            with self.subTest(actual=actual), \
                 self.assertRaisesRegex(TimeoutError, 'Version mismatch'):
                self.verify(iter([json.dumps({'version': actual})] * 12))

    def test_preflight_rejects_same_number_even_with_different_hash(self):
        for deployed in ('1.1.0+abcdef123456', f'1.1.0+{"0" * 7}', f'1.2.0+{COMMIT[:7]}'):
            with self.subTest(deployed=deployed), \
                 patch.object(check_version, 'fetch_version', return_value=deployed), \
                 self.assertRaisesRegex(ValueError, 'Refusing deployment.*must be newer'):
                check_version.require_newer_version(EXPECTED, 'https://example.org/version')

    def test_preflight_compares_numeric_versions_and_supports_existing_endpoint(self):
        for deployed in ('1.9.0+abcdef123456', f'1.9.0+{COMMIT[:7]}'):
            with self.subTest(deployed=deployed), \
                 patch.object(check_version, 'fetch_version', return_value=deployed) as fetch, \
                 patch('builtins.print'):
                check_version.require_newer_version(f'1.10.0+{COMMIT[:7]}', 'https://example.org/version')
                fetch.assert_called_once_with('https://example.org/api/version')

    def test_preflight_preserves_custom_endpoint(self):
        with patch.object(check_version, 'fetch_version', return_value='1.1.0+abcdef123456') as fetch, \
             patch('builtins.print'):
            check_version.require_newer_version(f'1.1.1+{COMMIT[:7]}', 'https://example.org/custom')
            fetch.assert_called_once_with('https://example.org/custom')

    def test_preflight_refuses_unknown_remote_version(self):
        for error in (ValueError('invalid JSON'), OSError('curl missing'),
                      subprocess.TimeoutExpired('curl', 5)):
            with self.subTest(error=error), \
                 patch.object(check_version, 'fetch_version', side_effect=error), \
                 self.assertRaisesRegex(ValueError, 'Cannot verify.*refusing deployment'):
                check_version.require_newer_version(EXPECTED, 'https://example.org/version')
        with patch.object(check_version, 'fetch_version', return_value='bad-version'), \
             self.assertRaisesRegex(ValueError, 'Cannot verify.*Invalid release version'):
            check_version.require_newer_version(EXPECTED, 'https://example.org/version')

    def test_preflight_rejects_unsupported_commit_lengths(self):
        for suffix in (COMMIT[:6], COMMIT):
            with self.subTest(suffix=suffix), \
                 patch.object(check_version, 'fetch_version', return_value=f'1.0.0+{suffix}'), \
                 self.assertRaisesRegex(ValueError, 'Invalid release version'):
                check_version.require_newer_version(EXPECTED, 'https://example.org/version')


class DeploymentSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        source = Path(__file__).resolve().parents[1]
        names = (*release.RELEASE_FILES, 'deploy/deploy.sh', 'deploy/check_version.py',
                 'deploy/dotusd-info.service', 'deploy/dotusd-info.caddy',
                 'backfill.py', 'publish_archive.py')
        for name in names:
            destination = self.repo / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, destination)
        checksum = release.source_hash(release.read_sources(self.repo))
        (self.repo / 'VERSION').write_text(f'1.1.0+{checksum}\n')
        self.git('init', '-b', 'master')
        self.git('add', '.')
        self.git('-c', 'user.name=Deploy Test', '-c', 'user.email=deploy@example.org',
                 '-c', 'commit.gpgsign=false', 'commit', '-m', 'Release')
        self.commit = self.git('rev-parse', 'HEAD').stdout.strip()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.log = self.root / 'transfer.json'
        self.stub('curl', """import json, os, sys
version = '1.0.0+abcdef123456' if sys.argv[-1].endswith('/api/version') else os.environ['EXPECTED_VERSION']
print(json.dumps({'version': version}))
""")
        self.stub('ssh', """import os, pathlib, sys
sys.stdin.read()
# Concurrent edits after snapshot creation must never reach the deployment.
(pathlib.Path(os.environ['TEST_REPO']) / 'server.py').write_text('concurrent edit\\n')
""")
        self.stub('rsync', """import json, os, pathlib, sys
if 'DEPLOY_COMMIT' in sys.argv:
    files = {name: pathlib.Path(name).read_text() for name in ('DEPLOY_COMMIT', 'VERSION', 'server.py')}
    pathlib.Path(os.environ['TRANSFER_LOG']).write_text(json.dumps({'cwd': os.getcwd(), 'files': files}))
""")

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, check=True,
                              capture_output=True, text=True)

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(f'#!{sys.executable}\n{body}')
        path.chmod(0o755)

    def deploy(self):
        env = dict(os.environ, PATH=f'{self.bin}{os.pathsep}{os.environ["PATH"]}',
                   PYTHON=sys.executable, TEST_REPO=str(self.repo),
                   TRANSFER_LOG=str(self.log), EXPECTED_VERSION=f'1.1.0+{self.commit[:7]}')
        return subprocess.run(['sh', 'deploy/deploy.sh', 'test-host', 'https://example.org/version'],
                              cwd=self.repo, env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=15)

    def assert_refused(self, message):
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stderr)
        self.assertFalse(self.log.exists())

    def test_refuses_unstaged_changes(self):
        (self.repo / 'server.py').write_text('modified\n')
        self.assert_refused('working-tree and index changes')

    def test_refuses_staged_changes(self):
        (self.repo / 'server.py').write_text('modified\n')
        self.git('add', 'server.py')
        self.assert_refused('working-tree and index changes')

    def test_refuses_untracked_files(self):
        (self.repo / 'untracked').write_text('not committed\n')
        self.assert_refused('working-tree and index changes')

    def test_refuses_other_branches_and_detached_head(self):
        self.git('checkout', '-b', 'feature')
        self.assert_refused('check out master')
        self.git('checkout', '--detach', self.commit)
        self.assert_refused('check out master')

    def test_refuses_committed_source_with_stale_release_checksum(self):
        (self.repo / 'server.py').write_text('changed without updating VERSION\n')
        self.git('add', 'server.py')
        self.git('-c', 'user.name=Deploy Test', '-c', 'user.email=deploy@example.org',
                 '-c', 'commit.gpgsign=false', 'commit', '-m', 'Stale release')
        self.assert_refused('Release hash is stale')

    def test_transfers_exact_commit_snapshot_and_cleans_temporary_identity(self):
        original = (self.repo / 'server.py').read_text()
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        transfer = json.loads(self.log.read_text())
        self.assertEqual(transfer['files']['DEPLOY_COMMIT'], self.commit + '\n')
        self.assertEqual(transfer['files']['server.py'], original)
        self.assertEqual(transfer['files']['VERSION'], (self.repo / 'VERSION').read_text())
        self.assertIn(f'Deployed 1.1.0+{self.commit[:7]}: verified', result.stdout)
        snapshot = Path(transfer['cwd'])
        self.assertNotIn(self.repo, snapshot.parents)
        self.assertFalse(snapshot.exists())
        self.assertFalse((self.repo / 'DEPLOY_COMMIT').exists())
