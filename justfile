set positional-arguments

# List available commands.
default:
    @just --list

# Bump the release version and refresh its hash (patch or minor).
release bump='patch':
    python3 release.py --bump "$1"

# Verify the recorded release hash matches the application.
check-version:
    python3 release.py --check
