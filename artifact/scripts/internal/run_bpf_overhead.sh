#!/usr/bin/env bash
# Run the eBPF-overhead block for the paper.
#
# Overhead is deliberately separate from the timing matrix: timing runs must
# keep kernel.bpf_stats_enabled=0 (validate_jankbench.py J11).  This runner
# enables BPF statistics only for its own JankBench runs and writes a separate
# results tree with bpf_overhead.json files.
#
# Usage:
#   ./artifact/pipeline.sh overhead --out DIR [--suite jankbench|uibench|all]
#   ./artifact/pipeline.sh overhead --quick
#
# The default suite is all: JankBench 0..7 plus all six UiBench tests.
#
# --quick runs one workload, one repetition, one iteration, and one policy
# (scx_fcp unless --policies is given). It is a cross-check, not paper data.
#
# Protocol defaults live in config.env: OVERHEAD_REPS, OVERHEAD_SUITE,
# OVERHEAD_BENCH_IDS, OVERHEAD_UI_TESTS, OVERHEAD_RUN_COUNT,
# OVERHEAD_WARMUP_ITERATIONS, OVERHEAD_POLICIES, and OVERHEAD_OUT.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ARTIFACT=$(cd "$HERE/../.." && pwd)
[ -f "$ARTIFACT/config.env" ] && . "$ARTIFACT/config.env"

ADB=${ADB:?config.env must define ADB}
SERIAL=${SERIAL:?config.env must define SERIAL}
REPS=${OVERHEAD_REPS:?config.env must define OVERHEAD_REPS}
SUITE=${OVERHEAD_SUITE:?config.env must define OVERHEAD_SUITE}
BENCH_IDS=${OVERHEAD_BENCH_IDS:?config.env must define OVERHEAD_BENCH_IDS}
UI_TESTS=${OVERHEAD_UI_TESTS:?config.env must define OVERHEAD_UI_TESTS}
RUN_COUNT=${OVERHEAD_RUN_COUNT:?config.env must define OVERHEAD_RUN_COUNT}
WARMUP_ITERATIONS=${OVERHEAD_WARMUP_ITERATIONS:?config.env must define OVERHEAD_WARMUP_ITERATIONS}
POLICIES=${OVERHEAD_POLICIES:?config.env must define OVERHEAD_POLICIES}
OUT=${OUT:-${OVERHEAD_OUT:?config.env must define OVERHEAD_OUT}}
PARSER=$ARTIFACT/scripts/internal/parse_bpf_overhead.py
QUICK=0
POLICIES_SET=0

usage() {
  sed -n '2,18p' "$0"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT=$2; shift 2 ;;
    --reps) REPS=$2; shift 2 ;;
    --suite) SUITE=$2; shift 2 ;;
    --bench-ids|--jank-tests) BENCH_IDS=$2; shift 2 ;;
    --ui-tests) UI_TESTS=$2; shift 2 ;;
    --policies) POLICIES=$2; POLICIES_SET=1; shift 2 ;;
    --run-count) RUN_COUNT=$2; shift 2 ;;
    --warmup) WARMUP_ITERATIONS=$2; shift 2 ;;
    --quick) QUICK=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [ "$QUICK" = 1 ]; then
  REPS=1
  BENCH_IDS=0
  UI_TESTS=bitmap-upload
  RUN_COUNT=1
  WARMUP_ITERATIONS=0
  [ "$POLICIES_SET" = 1 ] || POLICIES=scx_fcp
fi

case "$SUITE" in
  jankbench|uibench|all) ;;
  *) echo "invalid --suite '$SUITE' (use jankbench, uibench, or all)" >&2; exit 2 ;;
esac

[ -f "$ARTIFACT/scripts/internal/run_stage3_jankbench.sh" ] || {
  echo "missing run_stage3_jankbench.sh" >&2
  exit 2
}
[ -f "$PARSER" ] || {
  echo "missing parser: $PARSER" >&2
  exit 2
}

mkdir -p "$OUT"
LOG=$OUT/run.log
: > "$LOG"
log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG"; }
die() { log "FATAL $*"; exit 1; }
sh_() { "$ADB" -s "$SERIAL" shell "$@" 2>/dev/null | tr -d '\r'; }

"$ADB" -s "$SERIAL" get-state >/dev/null 2>&1 || die "device $SERIAL not reachable"
sh_ 'test -e /proc/sys/kernel/bpf_stats_enabled' || die "guest has no bpf_stats_enabled sysctl"
sh_ 'command -v bpftool >/dev/null 2>&1' || die "guest has no bpftool; cannot snapshot struct_ops run_time_ns/run_cnt"

log "overhead start out=$OUT suite=$SUITE reps=$REPS policies=$POLICIES"
log "topology=${TOPO:-unset} nproc=$(sh_ 'nproc') kernel=$(sh_ 'uname -r')"

if [ "$SUITE" = "jankbench" ] || [ "$SUITE" = "all" ]; then
  for bid in ${BENCH_IDS//,/ }; do
    root=$OUT/jankbench/id$bid
    mkdir -p "$root"
    log "JankBench id=$bid reps=$REPS run_count=$RUN_COUNT warmup=$WARMUP_ITERATIONS"
    rc=0
    BPF_STATS=1 VALIDATE=0 RUNS="$REPS" RUN_COUNT="$RUN_COUNT" \
      WARMUP_ITERATIONS="$WARMUP_ITERATIONS" BENCH_ID="$bid" \
      RESULTS_ROOT="$root" DRIVER_LOG="$root/driver.log" \
      bash "$ARTIFACT/scripts/internal/run_stage3_jankbench.sh" noreboot "$POLICIES" \
      >>"$LOG" 2>&1 || rc=$?
    [ "$rc" -eq 0 ] || log "WARNING run_stage3 id=$bid rc=$rc"

    parsed=0
    for run_dir in "$root"/*/run-*; do
      [ -d "$run_dir" ] || continue
      if python3 "$PARSER" "$run_dir" >>"$LOG" 2>&1; then
        parsed=$((parsed + 1))
      else
        log "WARNING could not parse $run_dir"
      fi
    done
    log "JankBench id=$bid parsed=$parsed run_dirs"
  done
fi

if [ "$SUITE" = "uibench" ] || [ "$SUITE" = "all" ]; then
  ui_root=$OUT/uibench
  mkdir -p "$ui_root"
  log "UiBench tests=$UI_TESTS reps=$REPS"
  BPF_STATS=1 VALIDATE="${UI_VALIDATE:-1}" \
    SERIAL="$SERIAL" ADB="$ADB" BIN_DIR="${BIN_DIR:-$ARTIFACT/build/bin}" \
    WORKLOADS_DIR="${WORKLOADS_DIR:-$ARTIFACT/workloads}" TOPO="${TOPO:-}" \
    bash "$ARTIFACT/scripts/internal/run_uibench_matrix.sh" \
      --out "$ui_root" --rounds 1 --reps "$REPS" \
      --tests "$UI_TESTS" --policies "$POLICIES" >>"$LOG" 2>&1 || \
    log "WARNING UiBench overhead matrix returned non-zero"
fi

count=$(find "$OUT" -name bpf_overhead.json -type f 2>/dev/null | wc -l)
log "overhead complete out=$OUT bpf_overhead.json=$count"
[ "$count" -gt 0 ] || die "no bpf_overhead.json records produced"
