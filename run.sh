#!/usr/bin/env bash
# crew-tui launcher (Linux/macOS). Creates no state; just uses the venv.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py="$here/.venv/bin/python"

if [ ! -x "$py" ]; then
    echo "Virtual environment not found at $py" >&2
    echo "Create it first:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

cd "$here"
exec "$py" main.py "$@"
