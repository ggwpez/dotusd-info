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

# Create the local Python environment and install dependencies.
setup:
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt

# Run locally; forwards options to server.py (defaults to SSH host "server").
run *args:
    .venv/bin/python server.py "$@"

# Preview on port 5137 without SSH; PYTHON can select an existing environment.
preview *args:
    "{{env_var_or_default('PYTHON', '.venv/bin/python')}}" server.py --port 5137 --upstream http://127.0.0.1:9090/api/v1 --no-browser "$@"

# Run the local test suite.
test:
    .venv/bin/python -m unittest discover -s tests -v

# Require a newer version, deploy, and verify it publicly within 25 seconds.
deploy host='server' version_url='https://dotusd.info/version':
    ./deploy/deploy.sh "$1" "$2"

# Show the deployed service's status.
status host='server':
    ssh "$1" systemctl status dotusd-info --no-pager

# Show the deployed service's latest 50 log entries.
logs host='server':
    ssh "$1" journalctl -u dotusd-info -n 50 --no-pager
