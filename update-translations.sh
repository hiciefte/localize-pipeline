#!/usr/bin/env bash
# Server adapter: the same full workflow, with publishing enabled by default.
set -euo pipefail
PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LOCALIZE_PYTHON="${LOCALIZE_PYTHON:-python3}"
export PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export TRANSLATOR_CONFIG_FILE="${TRANSLATOR_CONFIG_FILE:-$PROJECT_ROOT/config.yaml}"
export LOCALIZE_STATE_DIR="${LOCALIZE_STATE_DIR:-$PROJECT_ROOT/logs/state/workflow}"
publication_set=false
for arg in "$@"; do
    case "$arg" in --publish|--no-publish|--publish-run|--check) publication_set=true ;; esac
done
if [[ "${LOCALIZE_SMOKE_ONLY:-false}" == "true" ]]; then
    set -- --check "$@"
    publication_set=true
fi
if [ "$publication_set" = false ]; then set -- --publish "$@"; fi
exec "$LOCALIZE_PYTHON" -m localize.workflow "$@"
