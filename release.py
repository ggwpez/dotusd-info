#!/usr/bin/env python3
"""Bump and verify the version of the shipped application."""
import argparse
import hashlib
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
RELEASE_FILES = (
    'release.py', 'requirements.txt', 'server.py', 'gap_repair.py',
    'archive_reader.py', 'hub_quotes.py',
    'about-dotusd.html', 'index.html', 'app.js',
    'ranges.js', 'style.css', 'favicon.svg', 'social-preview.png',
)
VERSION_PATTERN = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\+([0-9a-f]{12})')


def source_hash(files):
    digest = hashlib.sha256()
    for name in RELEASE_FILES:
        digest.update(name.encode() + b'\0' + files[name] + b'\0')
    return digest.hexdigest()[:12]


def read_sources(root=ROOT):
    return {name: (root / name).read_bytes() for name in RELEASE_FILES}


def read_version(root=ROOT):
    version = (root / 'VERSION').read_text().strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError('VERSION must contain MAJOR.MINOR.PATCH+12-character SHA-256 prefix')
    return version


def check(root=ROOT):
    version = read_version(root)
    actual = source_hash(read_sources(root))
    if version.split('+')[1] != actual:
        raise ValueError('Release hash is stale. After finishing code changes, run '
                         '`python3 release.py --bump patch` (or --bump minor), then rerun tests.')
    return version


def bump(part, root=ROOT):
    version = read_version(root)
    major, minor, patch = map(int, version.split('+')[0].split('.'))
    if part == 'minor':
        minor, patch = minor + 1, 0
    elif part == 'patch':
        patch += 1
    else:
        raise ValueError('Choose a minor or patch bump')
    updated = f'{major}.{minor}.{patch}+{source_hash(read_sources(root))}'
    temporary = root / 'VERSION.tmp'
    temporary.write_text(updated + '\n')
    temporary.replace(root / 'VERSION')
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--check', action='store_true', help='fail if the release hash is stale')
    action.add_argument('--bump', choices=('minor', 'patch'), help='bump the number and refresh the hash')
    args = parser.parse_args()
    try:
        print(bump(args.bump) if args.bump else check())
    except (ValueError, OSError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
