set positional-arguments

# List available commands.
default:
    @just --list

# Create the local Python environment and install dependencies.
setup:
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt

# Run locally using the public Asset Hub archive RPC; forwards server.py options.
run *args:
    .venv/bin/python server.py "$@"

# Preview on port 5137; PYTHON can select an existing environment.
preview *args:
    "{{env_var_or_default('PYTHON', '.venv/bin/python')}}" server.py --port 5137 --no-browser "$@"

# Prepare changed application versions, then run the local test suite.
test bump='patch':
    .venv/bin/python release.py --prepare "$1"
    .venv/bin/python -m unittest discover -s tests -v

# Deploy the committed release and require its public version within 60 seconds.
deploy host='server' version_url='https://dotusd.info/version':
    ./deploy/deploy.sh "$1" "$2"

# Show the deployed service's status.
status host='server':
    ssh "$1" systemctl status dotusd-info --no-pager

# Show the deployed service's latest 50 log entries.
logs host='server':
    ssh "$1" journalctl -u dotusd-info -n 50 --no-pager
