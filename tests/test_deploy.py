"""Deployment verification retries stale or unavailable releases with a deadline."""
import subprocess
import unittest
from unittest.mock import patch

from deploy import check_version


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
            check_version.wait_for_version('1.1.0+abcdef123456', 'https://example.org/version')

    def test_returns_immediately_on_matching_version(self):
        self.verify(iter(['{"version":"1.1.0+abcdef123456"}']))
        self.assertEqual(self.attempts, [0])
        self.assertEqual(self.elapsed, 0)

    def test_retries_unavailable_malformed_and_old_versions(self):
        self.verify(iter([
            subprocess.CalledProcessError(22, 'curl', stderr='HTTP 503'),
            'not JSON', '{"version":"old"}',
            '{"version":"1.1.0+abcdef123456"}',
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
            ['{"version":"old"}'] * 11 + ['{"version":"1.1.0+abcdef123456"}']))
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

    def test_preflight_rejects_same_number_even_with_different_hash(self):
        for deployed in ('1.1.0+abcdef123456', '1.1.0+000000000000', '1.2.0+abcdef123456'):
            with self.subTest(deployed=deployed), \
                 patch.object(check_version, 'fetch_version', return_value=deployed), \
                 self.assertRaisesRegex(ValueError, 'Refusing deployment.*must be newer'):
                check_version.require_newer_version('1.1.0+abcdef123456', 'https://example.org/version')

    def test_preflight_compares_numeric_versions_and_supports_existing_endpoint(self):
        with patch.object(check_version, 'fetch_version', return_value='1.9.0+abcdef123456') as fetch, \
             patch('builtins.print'):
            check_version.require_newer_version('1.10.0+abcdef123456', 'https://example.org/version')
            fetch.assert_called_once_with('https://example.org/api/version')

    def test_preflight_preserves_custom_endpoint(self):
        with patch.object(check_version, 'fetch_version', return_value='1.1.0+abcdef123456') as fetch, \
             patch('builtins.print'):
            check_version.require_newer_version('1.1.1+abcdef123456', 'https://example.org/custom')
            fetch.assert_called_once_with('https://example.org/custom')

    def test_preflight_refuses_unknown_remote_version(self):
        for error in (ValueError('invalid JSON'), OSError('curl missing'),
                      subprocess.TimeoutExpired('curl', 5)):
            with self.subTest(error=error), \
                 patch.object(check_version, 'fetch_version', side_effect=error), \
                 self.assertRaisesRegex(ValueError, 'Cannot verify.*refusing deployment'):
                check_version.require_newer_version('1.1.0+abcdef123456', 'https://example.org/version')
        with patch.object(check_version, 'fetch_version', return_value='bad-version'), \
             self.assertRaisesRegex(ValueError, 'Cannot verify.*Invalid release version'):
            check_version.require_newer_version('1.1.0+abcdef123456', 'https://example.org/version')
