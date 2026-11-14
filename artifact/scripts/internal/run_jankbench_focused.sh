#!/usr/bin/env bash
# Run one JankBench focused-comparison sample under one scheduling policy.
#
# Usage: [REBOOT=1] run_jankbench_focused.sh <policy> <run-number>
# Results go to artifact/results/jankbench_focused/<policy>/run-<n>.
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
export RESULTS_ROOT="${RESULTS_ROOT:-$ROOT/results/jankbench_focused}"
exec "$ROOT/scripts/internal/run_jankbench_pilot.sh" "$@"
