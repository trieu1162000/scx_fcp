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
  overhead [--quick]    eBPF-overhead block; --quick = one cross-check run
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
  overhead)
    exec "$INTERNAL/run_bpf_overhead.sh" "$@"
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
