"""Deployment verification retries stale or unavailable releases with a deadline."""
import subprocess
import unittest
from unittest.mock import patch

from deploy import check_version


class DeploymentTests(unittest.TestCase):
    def verify(self, responses):
        self.elapsed = 0
        self.attempts = []

        def sleep(seconds):
            self.elapsed += seconds

        def request(*args, **kwargs):
            self.attempts.append(self.elapsed)
            self.assertLessEqual(kwargs['timeout'], 25 - self.elapsed)
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

    def test_stale_version_fails_after_25_seconds(self):
        with self.assertRaisesRegex(TimeoutError, '25s.*Version mismatch.*received old'):
            self.verify(iter(['{"version":"old"}'] * 5))
        self.assertEqual(self.attempts, [0, 5, 10, 15, 20])
        self.assertEqual(self.elapsed, 25)

    def test_unavailable_endpoint_fails_after_25_seconds(self):
        with self.assertRaisesRegex(TimeoutError, '25s.*Endpoint request timed out'):
            self.verify(iter([subprocess.TimeoutExpired('curl', 5)] * 5))
        self.assertEqual(self.elapsed, 25)

    def test_missing_version_is_not_a_success(self):
        with self.assertRaisesRegex(TimeoutError, 'Response has no version string'):
            self.verify(iter(['{}'] * 5))
