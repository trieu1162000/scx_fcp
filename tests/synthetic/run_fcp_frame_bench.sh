#!/usr/bin/env bash
# Run the synthetic frame-pipeline benchmark and save its JSON record.
#
# Usage:
#   tests/synthetic/run_fcp_frame_bench.sh
#   TARGET=device SERIAL=127.0.0.1:6520 tests/synthetic/run_fcp_frame_bench.sh
#   FRAMES=300 HZ=90 SCHEDULER_LABEL=scx_fcp_behavioral tests/synthetic/run_fcp_frame_bench.sh
#
# Environment:
#   ADB             adb binary                  (default: adb)
#   SERIAL          device serial               (default: 127.0.0.1:6520)
#   SCX_ROOT        scx workspace               (default: $HOME/workspace/scx)
#   RESULTS_ROOT    result directory            (default: <repo>/results/synthetic)
#   BIN_DIR         benchmark binary directory  (default: $SCX_ROOT/target/release)
#   TARGET          host or device              (default: host)
#   SCHEDULER_LABEL label written to the JSON   (default: unknown)
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
ADB=${ADB:-adb}
SERIAL=${SERIAL:-127.0.0.1:6520}
SCX_ROOT=${SCX_ROOT:-$HOME/workspace/scx}
RESULTS_ROOT=${RESULTS_ROOT:-$REPO_ROOT/results/synthetic}
BIN_DIR=${BIN_DIR:-$SCX_ROOT/target/release}
TARGET=${TARGET:-host}
REMOTE=${REMOTE:-/data/local/tmp/scx}
SCHEDULER_LABEL=${SCHEDULER_LABEL:-unknown}
FRAMES=${FRAMES:-300}
HZ=${HZ:-60}
STAGES=${STAGES:-4}
STAGE_US=${STAGE_US:-1800}
BG_WORKERS=${BG_WORKERS:-16}

BENCH=$BIN_DIR/fcp_frame_bench
STAMP=$(date +%Y%m%d-%H%M%S)
OUT=$RESULTS_ROOT/${SCHEDULER_LABEL}/${TARGET}/run-$STAMP
mkdir -p "$OUT"

ARGS=(--scheduler "$SCHEDULER_LABEL" --frames "$FRAMES" --hz "$HZ"
      --stages "$STAGES" --stage-us "$STAGE_US" --bg-workers "$BG_WORKERS")

if [ ! -x "$BENCH" ]; then
    echo "building fcp_frame_bench in $REPO_ROOT/tools/scx_fcp_bench"
    make -C "$REPO_ROOT/tools/scx_fcp_bench"
    BENCH=$REPO_ROOT/tools/scx_fcp_bench/fcp_frame_bench
fi
[ -x "$BENCH" ] || { echo "missing benchmark binary $BENCH" >&2; exit 1; }

if [ "$TARGET" = device ]; then
    A=("$ADB" -s "$SERIAL")
    "$ADB" connect "$SERIAL" >/dev/null 2>&1 || true
    [ "$("${A[@]}" get-state 2>/dev/null | tr -d '\r')" = device ] || {
        echo "device $SERIAL not reachable" >&2; exit 1; }
    "${A[@]}" shell "mkdir -p $REMOTE" >/dev/null
    "${A[@]}" push "$BENCH" "$REMOTE/fcp_frame_bench" >/dev/null
    "${A[@]}" shell "chmod 755 $REMOTE/fcp_frame_bench" >/dev/null
    "${A[@]}" shell "cat /sys/kernel/sched_ext/state 2>/dev/null" | tr -d '\r' > "$OUT/scx_state.txt" || true
    "${A[@]}" shell "cat /sys/kernel/sched_ext/root/ops 2>/dev/null" | tr -d '\r' > "$OUT/scx_ops.txt" || true
    "${A[@]}" shell "$REMOTE/fcp_frame_bench ${ARGS[*]}" | tr -d '\r' > "$OUT/fcp_frame_bench.json"
    "${A[@]}" shell 'uname -r' | tr -d '\r' > "$OUT/kernel.txt"
    "${A[@]}" shell 'nproc' | tr -d '\r' > "$OUT/nproc.txt"
else
    cat /sys/kernel/sched_ext/state 2>/dev/null > "$OUT/scx_state.txt" || true
    cat /sys/kernel/sched_ext/root/ops 2>/dev/null > "$OUT/scx_ops.txt" || true
    "$BENCH" "${ARGS[@]}" > "$OUT/fcp_frame_bench.json"
    uname -r > "$OUT/kernel.txt"
    nproc > "$OUT/nproc.txt"
fi

export TARGET SERIAL SCHEDULER_LABEL FRAMES HZ STAGES STAGE_US BG_WORKERS
python3 - "$OUT/manifest.json" <<'PY'
import json
import os
import sys

out = os.path.dirname(os.path.abspath(sys.argv[1]))

def read(name, default=""):
    path = os.path.join(out, name)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return default

manifest = {
    "target": os.environ["TARGET"],
    "serial": os.environ["SERIAL"],
    "scheduler_label": os.environ["SCHEDULER_LABEL"],
    "frames": int(os.environ["FRAMES"]),
    "hz": int(os.environ["HZ"]),
    "stages": int(os.environ["STAGES"]),
    "stage_us": int(os.environ["STAGE_US"]),
    "bg_workers": int(os.environ["BG_WORKERS"]),
    "kernel": read("kernel.txt"),
    "nproc": read("nproc.txt"),
    "scx_state": read("scx_state.txt"),
    "scx_ops": read("scx_ops.txt"),
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2)
    f.write("\n")
PY

echo "wrote $OUT/fcp_frame_bench.json"
cat "$OUT/fcp_frame_bench.json"
