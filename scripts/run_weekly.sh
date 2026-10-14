#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
report_date="${1:-$(TZ=Asia/Shanghai date +%F)}"
exec "$project_dir/.venv/bin/python" orchestrator.py --all --date "$report_date"
