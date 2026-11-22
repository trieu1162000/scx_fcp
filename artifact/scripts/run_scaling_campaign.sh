#!/usr/bin/env bash
# Run the microbenchmark matrix in one shot.
#
# CF: for each vCPU count and topology:
#   1. launch Cuttlefish,
#   2. attach each policy,
#   3. run RUNS samples of fcp_frame_bench,
#   4. reboot once with ProxyExec for the EEVDF+ProxyExec cell.
#
# Device: run one fixed-SoC cell per policy; CPU/topology lists are ignored.
# This is a host-side campaign. It does not modify the paper data. The output
# is a tree of per-run JSON files, suitable for the paper's scaling summary.
#
# Usage:
#   ./artifact/scripts/run_scaling_campaign.sh
#   CPUS_LIST="4 8 16" TOPOS="sym dl" RUNS=10 \
#     ./artifact/scripts/run_scaling_campaign.sh
#
# Environment:
#   CPUS_LIST    default "4 8 16 32"
#   TOPOS        default "sym dl"  (dl = SCHED_DEADLINE asymmetric)
#   RUNS         default 10
#   BENCH        fcp | schbench | all (default: fcp)
#   FRAMES       default 60
#   HZ           default 90
#   STAGES       default 4
#   STAGE_US     default 1800
#   OUT          default results/scaling_<timestamp>
#   CF_HOME      Cuttlefish host directory
#   SERIAL/ADB   ADB target
#   BIN_DIR      default artifact/build/bin
#   SCHBENCH_ARGS default '-m 1 -r 30 -w 5 -j -'
#
# The script skips a vCPU count larger than the host CPU count unless
# ALLOW_OVERSIZED=1. It does not fabricate missing cells.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
[ -f "$ROOT/artifact/config.env" ] && . "$ROOT/artifact/config.env"

CPUS_LIST=${CPUS_LIST:-"4 8 16 32"}
TOPOS=${TOPOS:-"sym dl"}
RUNS=${RUNS:-10}
FRAMES=${FRAMES:-60}
HZ=${HZ:-90}
STAGES=${STAGES:-4}
STAGE_US=${STAGE_US:-1800}
SERIAL=${SERIAL:-127.0.0.1:6520}
ADB=${ADB:-adb}
BIN_DIR=${BIN_DIR:-$ROOT/artifact/build/bin}
OUT=${OUT:-$ROOT/results/scaling_$(date +%Y%m%d-%H%M%S)}
ALLOW_OVERSIZED=${ALLOW_OVERSIZED:-0}
REMOTE=${REMOTE:-/data/local/tmp/scx}
# Absolute load gates are not CPU-count aware. 8.0 is a sensible default for
# a 16-vCPU host; override it for larger/smaller machines.
export QUIET_HOST=${QUIET_HOST:-8.0}
export QUIET_GUEST=${QUIET_GUEST:-2.0}

NORMAL_POLICIES=${NORMAL_POLICIES:-"eevdf scx_p2dq_pelt_off scx_lavd scx_fcp scx_fcp_behavioral"}
RUN_PROXY=${RUN_PROXY:-1}
BENCH=${BENCH:-fcp}
SCHBENCH_THREADS=${SCHBENCH_THREADS:-}
SCHBENCH_ARGS=${SCHBENCH_ARGS:-"-m 1 -r 30 -w 5 -j -"}

case "$BENCH" in
  fcp|schbench|all) ;;
  *) echo "invalid BENCH=$BENCH (use fcp, schbench, or all)" >&2; exit 2 ;;
esac

mkdir -p "$OUT"
LOG=$OUT/campaign.log
: > "$LOG"
log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG"; }
die() { log "FATAL $*"; exit 1; }

sh_() { "$ADB" -s "$SERIAL" shell "$@" 2>/dev/null | tr -d '\r'; }
state() { sh_ 'cat /sys/kernel/sched_ext/state 2>/dev/null' || true; }
ops() { sh_ 'cat /sys/kernel/sched_ext/root/ops 2>/dev/null' || true; }
host_nproc=$(nproc || echo 0)

[ -x "$BIN_DIR/scx_fcp" ] || die "missing scheduler binaries; run ./artifact/pipeline.sh build"
if [ "$BENCH" = fcp ] || [ "$BENCH" = all ]; then
  [ -x "$BIN_DIR/fcp_frame_bench" ] || die "missing $BIN_DIR/fcp_frame_bench; run ./artifact/pipeline.sh build"
fi
if [ "$BENCH" = schbench ] || [ "$BENCH" = all ]; then
  [ -x "$BIN_DIR/schbench" ] || die "missing $BIN_DIR/schbench; run ./artifact/pipeline.sh build"
fi

stop_sched() {
  for p in scx_fcp scx_fcp_behavioral scx_lavd scx_p2dq; do
    pid=$(sh_ "pidof $p" || true)
    [ -n "$pid" ] && sh_ "kill -TERM $pid" >/dev/null 2>&1 || true
  done
  local deadline=$((SECONDS + 30))
  while [ $SECONDS -lt $deadline ]; do
    [ "$(state)" = "disabled" ] && return 0
    sleep 1
  done
  log "WARNING sched_ext state=$(state) after stop"
  return 0
}

start_sched() {
  local policy=$1 bin args expected
  stop_sched
  if [ "$policy" = "eevdf" ] || [ "$policy" = "eevdf_proxy_exec" ]; then
    return 0
  fi
  case "$policy" in
    scx_p2dq_pelt_off) bin=scx_p2dq; args="--enable-pelt false"; expected=p2dq ;;
    scx_lavd)          bin=scx_lavd; args=""; expected=lavd ;;
    scx_fcp)           bin=scx_fcp; args=""; expected=fcp ;;
    scx_fcp_behavioral) bin=scx_fcp_behavioral; args=""; expected=fcp_zh ;;
    *) die "unknown policy $policy" ;;
  esac
  if { [ "$policy" = "scx_fcp" ] || [ "$policy" = "scx_fcp_behavioral" ]; } && [ -n "$BIG" ]; then
    args="$args --big-cpus $BIG"
  fi
  "$ADB" -s "$SERIAL" push "$BIN_DIR/$bin" "$REMOTE/$bin" >/dev/null
  sh_ "chmod 755 $REMOTE/$bin"
  sh_ "cd $REMOTE && ulimit -l unlimited && SCX_STATS_BASE_PATH=$REMOTE/stats nohup ./$bin $args >$REMOTE/${policy}.log 2>&1 </dev/null &"
  local deadline=$((SECONDS + 120))
  while [ $SECONDS -lt $deadline ]; do
    if [ "$(state)" = "enabled" ] && printf '%s' "$(ops)" | grep -q "$expected"; then
      return 0
    fi
    sleep 2
  done
  die "scheduler $policy did not reach enabled/$expected (state=$(state) ops=$(ops))"
}

run_cell() {
  local policy=$1 r dest fcp_root schbench_root
  dest=$CELL/$policy
  mkdir -p "$dest"
  start_sched "$policy"
  for r in $(seq 1 "$RUNS"); do
    if [ "$BENCH" = fcp ] || [ "$BENCH" = all ]; then
      if [ "$BENCH" = all ]; then fcp_root=$dest/fcp; else fcp_root=$dest; fi
      TARGET=device \
        SERIAL=$SERIAL \
        ADB=$ADB \
        BIN_DIR=$BIN_DIR \
        RESULTS_ROOT="$fcp_root" \
        SCHEDULER_LABEL="$policy" \
        FRAMES=$FRAMES \
        HZ=$HZ \
        STAGES=$STAGES \
        STAGE_US=$STAGE_US \
        BG_WORKERS=$BG \
        "$ROOT/tests/synthetic/run_fcp_frame_bench.sh" >>"$LOG" 2>&1 || die "fcp run failed $policy run=$r"
      log "done bench=fcp cpus=$c topo=$t policy=$policy run=$r"
    fi
    if [ "$BENCH" = schbench ] || [ "$BENCH" = all ]; then
      if [ "$BENCH" = all ]; then schbench_root=$dest/schbench; else schbench_root=$dest; fi
      TARGET=device \
        SERIAL=$SERIAL \
        ADB=$ADB \
        BIN_DIR=$BIN_DIR \
        RESULTS_ROOT="$schbench_root" \
        SCHEDULER_LABEL="$policy" \
        SCHBENCH_THREADS="$SCHBENCH_THREADS" \
        SCHBENCH_ARGS="$SCHBENCH_ARGS" \
        "$ROOT/tests/synthetic/run_schbench.sh" >>"$LOG" 2>&1 || die "schbench run failed $policy run=$r"
      log "done bench=schbench cpus=$c topo=$t policy=$policy run=$r"
    fi
  done
  stop_sched
}

if [ "${PLATFORM:-x86_64}" = "arm64" ]; then
  # Physical device: fixed SoC topology, no 4/8/16/32 matrix.
  RUN_PROXY=0
  CPUS_LIST=8
  TOPOS=device
  c=8
  t=device
  BG=6
  BIG=""
  CELL=$OUT/device
  mkdir -p "$CELL"
  log "launch device bg=$BG"
  CPUS=8 TOPO=sym PROXY_EXEC=0 SERIAL=$SERIAL ADB=$ADB \
    "$ROOT/artifact/pipeline.sh" launch >>"$LOG" 2>&1 || die "launch failed device"
  for policy in $NORMAL_POLICIES; do
    run_cell "$policy"
  done
else
  for c in $CPUS_LIST; do
    if [ "$c" -gt "$host_nproc" ] && [ "$ALLOW_OVERSIZED" != 1 ]; then
      log "SKIP ${c}-vCPU: host has only ${host_nproc} CPUs (ALLOW_OVERSIZED=1 to force)"
      continue
    fi
    case "$c" in 4|8|16|32) ;; *) die "CPUS must be 4, 8, 16, or 32 (got $c)" ;; esac
    BG=$((3 * c / 4))
    for t in $TOPOS; do
      case "$t" in sym|asym|dl) ;; *) die "unknown TOPO=$t (use sym, asym, or dl)" ;; esac
      if [ "$t" = "sym" ]; then
        BIG=$(seq -s, 0 $((c - 1)))
      else
        BIG=$(seq -s, $((c / 2)) $((c - 1)))
      fi
      CELL=$OUT/${c}vCPU_${t}
      mkdir -p "$CELL"
      log "launch cpus=$c topo=$t bg=$BG big=${BIG:-none}"
      CPUS=$c TOPO=$t PROXY_EXEC=0 SERIAL=$SERIAL ADB=$ADB \
        "$ROOT/artifact/pipeline.sh" launch >>"$LOG" 2>&1 || die "launch failed cpus=$c topo=$t"

      for policy in $NORMAL_POLICIES; do
        run_cell "$policy"
      done

      if [ "$RUN_PROXY" = 1 ]; then
        log "relaunch cpus=$c topo=$t proxy_exec=1"
        CPUS=$c TOPO=$t PROXY_EXEC=1 SERIAL=$SERIAL ADB=$ADB \
          "$ROOT/artifact/pipeline.sh" launch >>"$LOG" 2>&1 || die "proxy launch failed"
        run_cell "eevdf_proxy_exec"
      fi
    done
  done
fi

cat >"$OUT/CAMPAIGN.txt" <<EOF
scaling campaign
started: $(date -Iseconds)
cpus: $CPUS_LIST
topos: $TOPOS
runs_per_cell: $RUNS
frames_per_run: $FRAMES
hz: $HZ
stages: $STAGES
stage_us: $STAGE_US
bench: $BENCH
policies: $NORMAL_POLICIES eevdf_proxy_exec(optional)
results: $OUT
EOF
log "campaign complete: $OUT"
