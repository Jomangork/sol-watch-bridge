#!/bin/sh
# Start SOL-Watch from its own folder, using the virtualenv when there is one.
# Any arguments are passed on, e.g. ./run.sh --once
cd "$(dirname "$0")" || exit 1
if [ -x .venv/bin/python ]; then
    exec .venv/bin/python -u sol_watch.py "$@"
fi
exec python3 -u sol_watch.py "$@"
