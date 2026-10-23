"""Require a fresh release hash and verify the release bump workflow."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import release


class ReleaseTests(unittest.TestCase):
    def test_checked_in_release_matches_sources(self):
        release.check()

    def test_bumps_refresh_hash_and_check_rejects_stale_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, body in release.read_sources().items():
                (root / name).write_bytes(body)
            (root / 'VERSION').write_text('1.2.3+' + release.source_hash(release.read_sources(root)) + '\n')
            (root / 'app.js').write_bytes(b'changed application')
            result = subprocess.run([sys.executable, str(root / 'release.py'), '--check'],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('--bump patch', result.stderr)
            result = subprocess.run([sys.executable, str(root / 'release.py'), '--bump', 'patch'],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), release.check(root))
            self.assertTrue(release.read_version(root).startswith('1.2.4+'))
            self.assertTrue(release.bump('minor', root).startswith('1.3.0+'))
            release.check(root)

    def test_invalid_version_is_rejected_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'VERSION'
            path.write_text('invalid\n')
            with self.assertRaises(ValueError):
                release.bump('patch', root)
            self.assertEqual(path.read_text(), 'invalid\n')
