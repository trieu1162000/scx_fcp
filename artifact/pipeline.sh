#!/usr/bin/env bash
# Single entrypoint for the scx_fcp artifact.
set -euo pipefail

ARTIFACT=$(cd "$(dirname "$0")" && pwd)
SCRIPTS="$ARTIFACT/scripts"
INTERNAL="$SCRIPTS/internal"

usage() {
  cat <<'EOF'
Usage: ./artifact/pipeline.sh <command> [options]

Commands:
  build                 build scheduler, baselines, synthetic bench
  launch                launch Cuttlefish (CPUS=4/8/16/32)
  smoke                 one JankBench id, no UiBench
  full                  full 14-test matrix (default RUNS=10)
  resume --out DIR      resume an existing campaign
  analyze --out DIR     integrate/validate a campaign
  help                  this message

Environment:
  SCX_ROOT, BIN_DIR     scheduler/build paths
  CF_HOME, KERNEL, INITRAMFS, SERIAL
  CPUS, MEMORY_MB
  OUT                   campaign output directory
EOF
}

cmd=${1:-help}
shift || true

case "$cmd" in
  build)
    exec "$INTERNAL/build_static.sh" "$@"
    ;;
  launch)
    exec "$SCRIPTS/launch_cuttlefish.sh" "$@"
    ;;
  smoke)
    OUT=${OUT:-"$ARTIFACT/results/smoke"}
    exec "$INTERNAL/run_full_matrix_14.sh" --out "$OUT" --runs 1 --jank-tests 0 --skip-uibench "$@"
    ;;
  full)
    exec "$INTERNAL/run_full_matrix_14.sh" "$@"
    ;;
  resume)
    exec "$INTERNAL/run_full_matrix_14.sh" --resume "$@"
    ;;
  analyze)
    exec python3 "$INTERNAL/integrate_full14.py" "$@"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "unknown command: $cmd" >&2
    usage >&2
    exit 2
    ;;
esac
