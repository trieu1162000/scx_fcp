#!/usr/bin/env bash
# Run schbench and save its JSON record.
#
# Usage:
#   SCHEDULER_LABEL=scx_fcp tests/synthetic/run_schbench.sh
#   TARGET=device SERIAL=127.0.0.1:6520 SCHEDULER_LABEL=scx_fcp \
#     tests/synthetic/run_schbench.sh
#
# Environment:
#   ADB             adb binary                  (default: adb)
#   SERIAL          device serial               (default: 127.0.0.1:6520)
#   RESULTS_ROOT    result directory            (default: <repo>/results/schbench)
#   BIN_DIR         benchmark binary directory  (default: <repo>/artifact/build/bin)
#   TARGET          host or device              (default: host)
#   SCHEDULER_LABEL label written to the JSON   (default: unknown)
#   SCHBENCH_ARGS   extra schbench arguments    (default: -m 1 -r 30 -w 5 -j -)
#   SCHBENCH_THREADS  worker threads (-t)       (optional)
set -euo pipefail

REQUESTED_TARGET=${TARGET:-}
REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
[ -f "$REPO_ROOT/artifact/config.env" ] && . "$REPO_ROOT/artifact/config.env"
ADB=${ADB:-adb}
SERIAL=${SERIAL:-127.0.0.1:6520}
RESULTS_ROOT=${RESULTS_ROOT:-$REPO_ROOT/results/schbench}
BIN_DIR=${BIN_DIR:-$REPO_ROOT/artifact/build/bin}
TARGET=${REQUESTED_TARGET:-host}
REMOTE=${REMOTE:-/data/local/tmp/scx}
SCHEDULER_LABEL=${SCHEDULER_LABEL:-unknown}
SCHBENCH_THREADS=${SCHBENCH_THREADS:-}
SCHBENCH_ARGS=${SCHBENCH_ARGS:-"-m 1 -r 30 -w 5 -j -"}

BENCH=$BIN_DIR/schbench
[ -x "$BENCH" ] || {
    echo "missing schbench binary $BENCH" >&2
    echo "run ./artifact/pipeline.sh build or set BIN_DIR" >&2
    exit 1
}

STAMP=$(date +%Y%m%d-%H%M%S)-$$
OUT=$RESULTS_ROOT/${SCHEDULER_LABEL}/${TARGET}/run-$STAMP
mkdir -p "$OUT"
export TARGET SERIAL SCHEDULER_LABEL SCHBENCH_ARGS SCHBENCH_THREADS

if [ -n "$SCHBENCH_THREADS" ]; then
    # shellcheck disable=SC2206
    ARGS=($SCHBENCH_ARGS)
    ARGS=(-t "$SCHBENCH_THREADS" "${ARGS[@]}")
else
    # shellcheck disable=SC2206
    ARGS=($SCHBENCH_ARGS)
fi

if [ "$TARGET" = device ]; then
    A=("$ADB" -s "$SERIAL")
    "$ADB" connect "$SERIAL" >/dev/null 2>&1 || true
    [ "$("${A[@]}" get-state 2>/dev/null | tr -d '\r')" = device ] || {
        echo "device $SERIAL not reachable" >&2; exit 1; }
    "${A[@]}" shell "mkdir -p $REMOTE" >/dev/null
    "${A[@]}" push "$BENCH" "$REMOTE/schbench" >/dev/null
    "${A[@]}" shell "chmod 755 $REMOTE/schbench" >/dev/null
    "${A[@]}" shell "cat /sys/kernel/sched_ext/state 2>/dev/null" | tr -d '\r' > "$OUT/scx_state.txt" || true
    "${A[@]}" shell "cat /sys/kernel/sched_ext/root/ops 2>/dev/null" | tr -d '\r' > "$OUT/scx_ops.txt" || true
    "${A[@]}" shell "$REMOTE/schbench ${ARGS[*]}" > "$OUT/schbench.json" 2> "$OUT/schbench.log" || true
    "${A[@]}" shell 'uname -r' | tr -d '\r' > "$OUT/kernel.txt"
    "${A[@]}" shell 'nproc' | tr -d '\r' > "$OUT/nproc.txt"
else
    cat /sys/kernel/sched_ext/state 2>/dev/null > "$OUT/scx_state.txt" || true
    cat /sys/kernel/sched_ext/root/ops 2>/dev/null > "$OUT/scx_ops.txt" || true
    "$BENCH" "${ARGS[@]}" > "$OUT/schbench.json" 2> "$OUT/schbench.log" || true
    uname -r > "$OUT/kernel.txt"
    nproc > "$OUT/nproc.txt"
fi

python3 - "$OUT/schbench.json" "$OUT/manifest.json" <<'PY'
import json
import os
import sys

json_path, manifest_path = sys.argv[1], sys.argv[2]
out = os.path.dirname(os.path.abspath(manifest_path))

def read(name, default=""):
    try:
        return open(os.path.join(out, name), encoding="utf-8").read().strip()
    except OSError:
        return default

try:
    data = json.load(open(json_path, encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"schbench did not produce valid JSON: {exc}")
if not data.get("int"):
    raise SystemExit("schbench JSON has no latency block")

manifest = {
    "target": os.environ.get("TARGET", "host"),
    "serial": os.environ.get("SERIAL", ""),
    "scheduler_label": os.environ.get("SCHEDULER_LABEL", "unknown"),
    "schbench_args": os.environ.get("SCHBENCH_ARGS", ""),
    "schbench_threads": os.environ.get("SCHBENCH_THREADS", ""),
    "kernel": read("kernel.txt"),
    "nproc": read("nproc.txt"),
    "scx_state": read("scx_state.txt"),
    "scx_ops": read("scx_ops.txt"),
    "latency": data.get("int", {}),
}
json.dump(manifest, open(manifest_path, "w", encoding="utf-8"), indent=2)
open(manifest_path, "a", encoding="utf-8").write("\n")
PY

echo "wrote $OUT/schbench.json"
