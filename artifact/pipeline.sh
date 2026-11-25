#!/usr/bin/env bash
# Unified CLI entrypoint for building, launching, running, and analyzing scx_fcp benchmarks.
set -euo pipefail

ARTIFACT=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$ARTIFACT/.." && pwd)
SCRIPTS="$ARTIFACT/scripts"

if [ -f "$ARTIFACT/config.env" ]; then
  set -a
  . "$ARTIFACT/config.env"
  set +a
fi

usage() {
  cat <<'EOF'
Usage: ./artifact/pipeline.sh <command> [options]

Commands:
  build                      Build static scx_fcp profiles, baselines, fcp_frame_bench, schbench
  launch                     Launch Cuttlefish (PLATFORM=x86_64) or stage physical device (PLATFORM=arm64)
  smoke [--out DIR]          Fast end-to-end validation across policies (micro + JankBench ID 0)
  micro [options]            Multi-core scaling microbenchmark suite (fcp_frame_bench + schbench)
                             Options: --cpus "4 8 16 32" --topos "sym dl" --runs 10 --suite fcp|schbench|all --quick --resume --out DIR
  macro [options]            Full 14-test AOSP macro suite (8 JankBench + 6 UiBenchJankTests)
  full  [options]            Alias for macro
                             Options: --topo dl|sym --runs 10 --bg-workers N --suite all|jankbench|uibench --quick --resume --out DIR
  overhead [options]         Isolated bpf_stats_enabled=1 eBPF struct_ops runtime overhead suite
                             Options: --topo dl|sym --runs 5 --suite all|jankbench|uibench --quick --resume --out DIR
  ablation [options]         Component ablation suite on JankBench (notoken, nofanout, behavioral, full)
                             Options: --topo dl --runs 10 --quick --resume --out DIR
  analyze DIR [--export DIR] Validate raw campaign(s), verify statistical invariants, and emit JSON/LaTeX summaries
  help                       Show this message
EOF
}

cmd=${1:-help}
shift || true

case "$cmd" in
  build)
    export BUILD_BASELINES=${BUILD_BASELINES:-1}
    export BIN_DIR=${BIN_DIR:-$ARTIFACT/build/bin/$PLATFORM}
    exec "$ROOT/scripts/build_static.sh" "$@"
    ;;
  launch)
    case "$PLATFORM" in
      x86_64) exec "$SCRIPTS/launch_cuttlefish.sh" "$@" ;;
      arm64)  exec "$SCRIPTS/launch_device.sh" "$@" ;;
      *) echo "unsupported PLATFORM: $PLATFORM" >&2; exit 2 ;;
    esac
    ;;
  smoke|micro|macro|overhead|ablation)
    exec python3 "$SCRIPTS/harness.py" "$cmd" "$@"
    ;;
  full)
    exec python3 "$SCRIPTS/harness.py" macro "$@"
    ;;
  resume)
    exec python3 "$SCRIPTS/harness.py" macro --resume "$@"
    ;;
  analyze|overhead-analyze)
    exec python3 "$SCRIPTS/analyze.py" "$@"
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
