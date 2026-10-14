#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
report_date="${1:-$(TZ=Asia/Shanghai date +%F)}"
if (( $# )); then shift; fi
# Direct exec preserves Python's success/partial/failure exit code and progress markers.
exec bash "$project_dir/run_with_env.sh" --all --date "$report_date" "$@"
