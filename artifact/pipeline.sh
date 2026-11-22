#!/usr/bin/env bash
# Single entrypoint for the scx_fcp artifact.
set -euo pipefail

ARTIFACT=$(cd "$(dirname "$0")" && pwd)
if [ -f "$ARTIFACT/config.env" ]; then
  set -a
  . "$ARTIFACT/config.env"
  set +a

  echo "============================================"
  echo "CONFIG_FILE=$ARTIFACT/config.env"
  for var in $(grep -E '^(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' "$ARTIFACT/config.env" | sed -E 's/^export[[:space:]]+//' | cut -d= -f1); do
    echo "$var=${!var}"
  done
  echo "============================================"
fi
ROOT=$(cd "$ARTIFACT/.." && pwd)
SCRIPTS="$ARTIFACT/scripts"
INTERNAL="$SCRIPTS/internal"

usage() {
  cat <<'EOF'
Usage: ./artifact/pipeline.sh <command> [options]

Commands:
  build                 build scheduler for the selected platform
  launch                launch Cuttlefish or prepare the configured device
  smoke                 one JankBench ID across supported policies
  full [--quick]        full 14-test matrix; --quick = smoke one cell
  micro [--suite SUITE] microbenchmark suite
                        SUITE=fcp|schbench|all (default: fcp)
                        CF: 4/8/16/32 vCPU, sym+dl, 10 runs
                        device: fixed SoC, 10 runs, no CPU matrix
                        --quick = one cell, one run
  overhead [--quick]    eBPF-overhead; default suite=all
                        --suite jankbench|uibench|all
                        --quick = one cross-check run
  overhead-analyze DIR  aggregate per-run bpf_overhead.json files
  resume --out DIR      resume an existing full campaign
  analyze DIR           integrate a full campaign (optional --out-dir DIR)
  help                  this message

Environment:
  PLATFORM              x86_64 (default) or arm64
  SCX_ROOT, BIN_DIR     scheduler source/build paths
  CF_HOME, KERNEL, INITRAMFS, SERIAL
  CPUS, MEMORY_MB
  OUT                   campaign output directory
EOF
}

cmd=${1:-help}
shift || true

case "$cmd" in
  build)
    export BUILD_BASELINES=${BUILD_BASELINES:-1}
    export BIN_DIR=${BIN_DIR:-$ARTIFACT/build/bin}
    exec "$ROOT/scripts/build_static.sh" "$@"
    ;;
  launch)
    case "$PLATFORM" in
      x86_64) exec "$SCRIPTS/launch_cuttlefish.sh" "$@" ;;
      arm64) exec "$SCRIPTS/launch_device.sh" "$@" ;;
      *) echo "unsupported PLATFORM: $PLATFORM" >&2; exit 2 ;;
    esac
    ;;
  smoke)
    OUT=${OUT:-"$ARTIFACT/results/smoke_${PLATFORM}_$(date +%Y%m%d-%H%M%S)"}
    if [ "$PLATFORM" = x86_64 ]; then
      export OUT
      exec "$INTERNAL/run_full_matrix_14.sh" --smoke --out "$OUT" --runs 1 --jank-tests 0 --skip-uibench "$@"
    elif [ "$PLATFORM" = arm64 ]; then
      while [ $# -gt 0 ]; do
        case "$1" in
          --out) OUT=${2:?missing --out directory}; shift 2 ;;
          *) echo "unknown arm64 smoke argument: $1" >&2; exit 2 ;;
        esac
      done
      [ ! -e "$OUT" ] || { echo "error: smoke output already exists: $OUT" >&2; exit 2; }
      export OUT
      "$SCRIPTS/launch_device.sh"
      DEVICE_POLICIES=(eevdf scx_fcp scx_fcp_v_a_depth3_tgid1ms scx_p2dq_pelt_off scx_lavd)
      for policy in "${DEVICE_POLICIES[@]}"; do
        BM_ID=0 RUN_COUNT=1 REBOOT=0 RESULTS_ROOT="$OUT" BIN_DIR="$BIN_DIR" \
          SERIAL="$SERIAL" ADB="$ADB" \
          bash "$ROOT/tests/jankbench/run_jankbench.sh" -p "$policy" -n 1
      done
      python3 "$ROOT/tests/jankbench/summarize_jankbench.py" "$OUT" --out-dir "$OUT"
      python3 - "$OUT/summary.json" "$OUT" <<'PY'
import json, sys
summary = json.load(open(sys.argv[1], encoding="utf-8"))
out = sys.argv[2]
expected = {
    "eevdf": ("disabled", None),
    "scx_fcp": ("enabled", "fcp"),
    "scx_fcp_v_a_depth3_tgid1ms": ("enabled", "fcp_zh"),
    "scx_p2dq_pelt_off": ("enabled", "p2dq"),
    "scx_lavd": ("enabled", "lavd"),
}

def ops_match(actual, prefix):
    if not isinstance(actual, str):
        return False
    if prefix == "fcp":
        return actual == "fcp" or (
            actual.startswith("fcp_") and not actual.startswith(("fcp_nh_", "fcp_zh_")))
    return actual == prefix or actual.startswith(prefix + "_")

details = []
for policy, (state, ops) in expected.items():
    rows = [row for row in summary.get("runs", [])
            if row.get("policy") == policy and str(row.get("run")) == "1"]
    if len(rows) != 1 or not rows[0].get("valid") or not rows[0].get("frames"):
        raise SystemExit(f"DEVICE_SMOKE_FAIL: {policy} result is invalid")
    path = f"{out}/{policy}/run-1/manifest.json"
    manifest = json.load(open(path, encoding="utf-8"))
    if manifest.get("policy") != policy or manifest.get("run_count") != 1 or \
            "EXTRA_ENABLED_BENCHMARK_IDS 0" not in manifest.get("benchmark_command", ""):
        raise SystemExit(f"DEVICE_SMOKE_FAIL: {policy} protocol mismatch")
    if manifest.get("scx_state_before") != state or \
            manifest.get("scx_state_during") != state:
        raise SystemExit(f"DEVICE_SMOKE_FAIL: {policy} scheduler was not active throughout the run")
    ops_before = manifest.get("scx_ops_before")
    ops_during = manifest.get("scx_ops_during")
    if ops is not None and (not ops_match(ops_before, ops) or not ops_match(ops_during, ops)):
        raise SystemExit(f"DEVICE_SMOKE_FAIL: {policy} scheduler ops did not match")
    log_path = f"{out}/{policy}/run-1/scheduler.log"
    if ops is not None and not open(log_path, encoding="utf-8").read().strip():
        raise SystemExit(f"DEVICE_SMOKE_FAIL: {policy} has no scheduler log evidence")
    details.append(
        f"{policy}(state={manifest['scx_state_during']},ops={ops_during or 'none'},"
        f"frames={rows[0]['frames']},log={log_path if ops is not None else 'no-sched-ext'})")
print("DEVICE_SMOKE_VALID: " + " ".join(details))
PY
    else
      echo "unsupported PLATFORM: $PLATFORM" >&2
      exit 2
    fi
    ;;
  full)
    case "$PLATFORM" in
      x86_64) exec "$INTERNAL/run_full_matrix_14.sh" "$@" ;;
      arm64)
        "$SCRIPTS/launch_device.sh"
        exec "$INTERNAL/run_full_matrix_14.sh" "$@"
        ;;
      *) echo "unsupported PLATFORM: $PLATFORM" >&2; exit 2 ;;
    esac
    ;;
  micro)
    SUITE=${MICRO_SUITE:-fcp}
    CPUS_LIST=${MICRO_CPUS_LIST:-"4 8 16 32"}
    TOPOS=${MICRO_TOPOS:-"sym dl"}
    RUNS=${MICRO_RUNS:-10}
    FRAMES=${MICRO_FRAMES:-60}
    HZ=${MICRO_HZ:-90}
    STAGES=${MICRO_STAGES:-4}
    STAGE_US=${MICRO_STAGE_US:-1800}
    NORMAL_POLICIES=${MICRO_POLICIES:-"eevdf scx_p2dq_pelt_off scx_lavd scx_fcp scx_fcp_behavioral"}
    RUN_PROXY=${MICRO_RUN_PROXY:-1}
    ALLOW_OVERSIZED=${MICRO_ALLOW_OVERSIZED:-0}
    SCHBENCH_THREADS=${MICRO_SCHBENCH_THREADS:-}
    SCHBENCH_ARGS=${MICRO_SCHBENCH_ARGS:-"-m 1 -r 30 -w 5 -j -"}
    OUT=${MICRO_OUT:-"$ARTIFACT/results/micro_$(date +%Y%m%d-%H%M%S)"}
    QUICK=0

    while [ $# -gt 0 ]; do
      case "$1" in
        --quick) QUICK=1; shift ;;
        --suite) SUITE=${2:?missing --suite value}; shift 2 ;;
        --schbench-threads) SCHBENCH_THREADS=${2:?missing --schbench-threads value}; shift 2 ;;
        --schbench-args) SCHBENCH_ARGS=${2:?missing --schbench-args value}; shift 2 ;;
        --cpus) CPUS_LIST=${2:?missing --cpus value}; shift 2 ;;
        --topos|--topology) TOPOS=${2:?missing --topos value}; shift 2 ;;
        --runs) RUNS=${2:?missing --runs value}; shift 2 ;;
        --frames) FRAMES=${2:?missing --frames value}; shift 2 ;;
        --hz) HZ=${2:?missing --hz value}; shift 2 ;;
        --stages) STAGES=${2:?missing --stages value}; shift 2 ;;
        --stage-us) STAGE_US=${2:?missing --stage-us value}; shift 2 ;;
        --policies) NORMAL_POLICIES=${2:?missing --policies value}; shift 2 ;;
        --out) OUT=${2:?missing --out value}; shift 2 ;;
        --no-proxy) RUN_PROXY=0; shift ;;
        --allow-oversized) ALLOW_OVERSIZED=1; shift ;;
        -h|--help)
          sed -n '/^  micro /,/^  overhead /p' "$0" | sed '$d'
          exit 0
          ;;
        *) echo "unknown micro argument: $1" >&2; exit 2 ;;
      esac
    done

    case "$SUITE" in
      fcp|schbench|all) ;;
      *) echo "invalid --suite '$SUITE' (use fcp, schbench, or all)" >&2; exit 2 ;;
    esac
    if [ "$SUITE" = all ]; then
      BENCH=all
    else
      BENCH=$SUITE
    fi

    if [ "$QUICK" = 1 ]; then
      CPUS_LIST=4
      TOPOS=sym
      RUNS=1
      RUN_PROXY=0
      if [ "$SUITE" = schbench ] || [ "$SUITE" = all ]; then
        SCHBENCH_ARGS="-m 1 -r 5 -w 0 -j -"
      fi
    fi

    export CPUS_LIST TOPOS RUNS FRAMES HZ STAGES STAGE_US
    export NORMAL_POLICIES RUN_PROXY ALLOW_OVERSIZED OUT
    export BENCH SCHBENCH_THREADS SCHBENCH_ARGS
    exec "$SCRIPTS/run_scaling_campaign.sh"
    ;;
  overhead)
    exec "$INTERNAL/run_bpf_overhead.sh" "$@"
    ;;
  overhead-analyze)
    exec python3 "$INTERNAL/aggregate_bpf_overhead.py" "$@"
    ;;
  resume)
    case "$PLATFORM" in
      x86_64) exec "$INTERNAL/run_full_matrix_14.sh" --resume "$@" ;;
      arm64)
        "$SCRIPTS/launch_device.sh"
        exec "$INTERNAL/run_full_matrix_14.sh" --resume "$@"
        ;;
      *) echo "unsupported PLATFORM: $PLATFORM" >&2; exit 2 ;;
    esac
    ;;
  analyze)
    case "$PLATFORM" in
      x86_64) exec python3 "$INTERNAL/integrate_full14.py" "$@" ;;
      arm64) exec python3 "$INTERNAL/integrate_full14.py" --policies "$POLICIES" "$@" ;;
      *) echo "unsupported PLATFORM: $PLATFORM" >&2; exit 2 ;;
    esac
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
