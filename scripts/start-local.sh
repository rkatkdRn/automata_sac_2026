#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
if [[ ! -x .venv/bin/python ]]; then
  printf '%s\n' 'Create .venv and install requirements.txt first.' >&2
  exit 1
fi
exec .venv/bin/python scripts/run.py "$@"
