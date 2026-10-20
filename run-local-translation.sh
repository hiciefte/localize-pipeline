#!/usr/bin/env bash
# Full local workflow; publishes only with --publish or --publish-run.
set -euo pipefail
PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CALLER_CWD=$(pwd)
LOCALIZE_PYTHON="${LOCALIZE_PYTHON:-$PROJECT_ROOT/venv/bin/python}"
if [ ! -x "$LOCALIZE_PYTHON" ]; then
    echo "Create venv and install requirements-dev.txt, or set LOCALIZE_PYTHON." >&2
    exit 1
fi
# Keep the historical positional configuration argument; remaining flags belong
# to the shared workflow. Resolve before changing directory.
if [ "$#" -gt 0 ] && [[ "$1" != --* ]]; then
    CONFIG_PATH="$1"
    shift
    if [[ "$CONFIG_PATH" != /* ]]; then CONFIG_PATH="$CALLER_CWD/$CONFIG_PATH"; fi
    set -- --config "$CONFIG_PATH" "$@"
fi
# Module lookup must not alter the meaning of caller-relative --repo/state paths.
export PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "$LOCALIZE_PYTHON" -m localize.workflow "$@"
