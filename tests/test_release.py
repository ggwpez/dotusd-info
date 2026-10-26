"""Require a fresh release hash and idempotent preparation before tests."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import release


class ReleaseTests(unittest.TestCase):
    def test_checked_in_release_matches_sources(self):
        release.check()

    def test_image_copies_every_release_file_through_the_build_context(self):
        # A file missing from either list fails the CI image build, not the tests.
        copied = {name for line in (release.ROOT / 'Dockerfile').read_text().splitlines()
                  if line.startswith('COPY ') for name in line.split()[1:-1]}
        allowed = {line[1:] for line in (release.ROOT / '.dockerignore').read_text().splitlines()
                   if line.startswith('!')}
        self.assertLessEqual(set(release.RELEASE_FILES) | {'VERSION'}, copied)
        self.assertLessEqual(copied, allowed)

    def test_preparation_versions_changed_sources_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, body in release.read_sources().items():
                (root / name).write_bytes(body)
            (root / 'VERSION').write_text('1.2.3+' + release.source_hash(release.read_sources(root)) + '\n')
            (root / 'app.js').write_bytes(b'changed application')
            result = subprocess.run([sys.executable, str(root / 'release.py'), '--check'],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            result = subprocess.run([sys.executable, str(root / 'release.py'), '--prepare', 'patch'],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), release.check(root))
            self.assertTrue(release.read_version(root).startswith('1.2.4+'))
            prepared = release.read_version(root)
            self.assertEqual(release.prepare(root=root), prepared)
            self.assertEqual(release.prepare('minor', root), prepared)
            (root / 'app.js').write_bytes(b'new feature')
            self.assertTrue(release.prepare('minor', root).startswith('1.3.0+'))
            release.check(root)

    def test_invalid_version_is_rejected_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'VERSION'
            path.write_text('invalid\n')
            with self.assertRaises(ValueError):
                release.prepare(root=root)
            self.assertEqual(path.read_text(), 'invalid\n')
