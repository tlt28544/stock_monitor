#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"
env_file="${STOCK_MONITOR_ENV_FILE:-$project_dir/.env}"
if [[ ! -f "$env_file" && -z "${STOCK_MONITOR_ENV_FILE:-}" && -f /home/admin/.openclaw/.env ]]; then
    env_file=/home/admin/.openclaw/.env
fi
if [[ -f "$env_file" ]]; then
    set -a
    source "$env_file"
    set +a
fi
exec "${STOCK_MONITOR_PYTHON:-$project_dir/.venv/bin/python}" "$project_dir/orchestrator.py" "$@"
