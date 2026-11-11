#!/usr/bin/env bash
# Stage 3 JankBench final: 5 runs per policy.
# Usage: run_stage3_jankbench.sh <reboot|noreboot> <policy1,policy2,...>
# Reboot mode reboots once per policy block (run 1), then reuses the boot.
# No-reboot mode uses a balanced interleaved schedule across the five runs.
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SERIAL=${SERIAL:-0.0.0.0:6520}
ADB=${ADB:-/home/ngerr/bin/adb}
A=("$ADB" -s "$SERIAL")
MODE=${1:?usage: run_stage3_jankbench.sh <reboot|noreboot> <policies>}
IFS=',' read -r -a POLICY_ARR <<< "${2:?policies}"
RUNS=${RUNS:-5}
VALIDATE=${VALIDATE:-1}
VALIDATE_ATTEMPTS=${VALIDATE_ATTEMPTS:-5}
JANK_RUN_COUNT=${JANK_RUN_COUNT:-4}
BENCH_ID=${BENCH_ID:-0}
POLICIES_CSV=$(IFS=,; echo "${POLICY_ARR[*]}")
RESULTS_ROOT=${RESULTS_ROOT:-$ROOT/results/variant_final/jankbench}
LOG=${DRIVER_LOG:-$ROOT/results/variant_final/stage3_jankbench_driver.log}
mkdir -p "$RESULTS_ROOT/_driver"
: > "$LOG"

log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG"; }
sh_() { "${A[@]}" shell "$@" 2>/dev/null | tr -d '\r'; }

wait_quiet() {
  local deadline=$((SECONDS + 900)) hl gl
  local host_limit=${QUIET_HOST:-3.0}
  local guest_limit=${QUIET_GUEST:-2.0}
  while [ $SECONDS -lt $deadline ]; do
    hl=$(awk '{print $1}' /proc/loadavg)
    gl=$(sh_ 'cut -d" " -f1 /proc/loadavg')
    if awk -v h="$hl" -v g="$gl" -v hlim="$host_limit" -v glim="$guest_limit" 'BEGIN{exit !(h<=hlim && g<=glim)}'; then
      log "quiet host=$hl guest=$gl"
      return 0
    fi
    log "WAIT load high host=$hl guest=$gl"
    sleep 15
  done
  log "WARNING load did not settle host=${hl:-?} guest=${gl:-?}"
  return 1
}

archive_failed() {
  local policy=$1 run=$2 d=$RESULTS_ROOT/$policy/run-$run
  [ -d "$d" ] || return 0
  local dest=$RESULTS_ROOT/_failed/$policy/run-$run-$(date +%H%M%S)
  mkdir -p "$(dirname "$dest")"
  mv "$d" "$dest"
  log "archived failed $policy run $run to $dest"
}

validate_run() {
  local policy=$1 run=$2
  [ "$VALIDATE" = 1 ] || return 0
  python3 "$ROOT/scripts/validate_jankbench.py" "$RESULTS_ROOT" \
    --expected-runs "$RUNS" --run-count "$JANK_RUN_COUNT" \
    --benchmark-id "$BENCH_ID" --policies "$POLICIES_CSV" --quiet \
    >/dev/null 2>&1 || true
  python3 - "$RESULTS_ROOT/validity.json" "$policy" "$run" <<'PY'
import json, sys
path, policy, run = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    data = json.load(open(path))
except (OSError, json.JSONDecodeError) as exc:
    print(f"validity.json unreadable: {exc}")
    raise SystemExit(1)
rec = next((r for r in data.get("runs", [])
            if r.get("policy") == policy and r.get("run") == run), None)
if rec is None or not rec.get("present"):
    print(f"run not present in validity.json: {policy} run-{run}")
    raise SystemExit(1)
if not rec.get("valid"):
    print("INVALID: " + " | ".join(rec.get("reasons") or ["unknown"]))
    raise SystemExit(1)
print(f"VALID: {policy} run-{run} frames={rec.get('metrics', {}).get('frames')}")
PY
}

archive_invalid() {
  local policy=$1 run=$2 d=$RESULTS_ROOT/$policy/run-$run
  [ -d "$d" ] || return 0
  local dest=$RESULTS_ROOT/_invalid/$policy/run-$run-$(date +%H%M%S)
  mkdir -p "$(dirname "$dest")"
  mv "$d" "$dest"
  log "archived invalid $policy run $run to $dest"
}

run_one() {
  local policy=$1 run=$2 reboot=$3
  wait_quiet || true
  local hl gl
  hl=$(awk '{print $1}' /proc/loadavg)
  gl=$(sh_ 'cut -d" " -f1 /proc/loadavg')
  log "run policy=$policy run=$run reboot=$reboot host=$hl guest=$gl"
  local rc=0
  REBOOT=$reboot SERIAL=$SERIAL ADB=$ADB RESULTS_ROOT="$RESULTS_ROOT" \
    BENCH_ID=$BENCH_ID \
    "$ROOT/scripts/internal/run_jankbench_focused.sh" "$policy" "$run" >>"$LOG" 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    log "FAILED policy=$policy run=$run rc=$rc"
    archive_failed "$policy" "$run"
    return 1
  fi
  hl=$(awk '{print $1}' /proc/loadavg)
  gl=$(sh_ 'cut -d" " -f1 /proc/loadavg')
  log "done policy=$policy run=$run host=$hl guest=$gl state=$(sh_ 'cat /sys/kernel/sched_ext/state')"
  return 0
}

run_with_retry() {
  local policy=$1 run=$2 reboot=$3 attempt
  if [ "${SKIP_VALID:-0}" = 1 ] && [ -d "$RESULTS_ROOT/$policy/run-$run" ]; then
    if validate_run "$policy" "$run" >/dev/null 2>&1; then
      log "skip valid policy=$policy run=$run"
      return 0
    fi
    archive_invalid "$policy" "$run"
  fi
  for attempt in $(seq 1 "$VALIDATE_ATTEMPTS"); do
    if run_one "$policy" "$run" "$reboot"; then
      if validate_run "$policy" "$run"; then
        return 0
      fi
      archive_invalid "$policy" "$run"
      log "INVALID policy=$policy run=$run attempt=$attempt; rerun"
    else
      archive_failed "$policy" "$run"
      log "FAILED policy=$policy run=$run attempt=$attempt"
    fi
    reboot=0
    sleep 5
  done
  log "GIVEUP policy=$policy run=$run after $VALIDATE_ATTEMPTS attempts"
  return 1
}

log "stage3 jankbench mode=$MODE bench_id=$BENCH_ID policies=${POLICY_ARR[*]} runs=$RUNS root=$RESULTS_ROOT"
if [ "$MODE" = reboot ]; then
  mapfile -t ORDER < <(printf '%s\n' "${POLICY_ARR[@]}" | shuf)
  log "policy block order ${ORDER[*]}"
  for policy in "${ORDER[@]}"; do
    for run in $(seq 1 "$RUNS"); do
      reboot=0
      [ "$run" = 1 ] && reboot=1
      run_with_retry "$policy" "$run" "$reboot" || true
    done
  done
else
  # Balanced interleaving: five Latin-square passes over a shuffled base order.
  mapfile -t BASE < <(printf '%s\n' "${POLICY_ARR[@]}" | shuf)
  n=${#BASE[@]}
  log "interleave base order ${BASE[*]}"
  for run in $(seq 1 "$RUNS"); do
    for i in $(seq 0 $((n - 1))); do
      idx=$(( (i + run - 1) % n ))
      policy=${BASE[$idx]}
      run_with_retry "$policy" "$run" 0 || true
    done
  done
fi
log "stage3 jankbench complete"
if [ "$VALIDATE" = 1 ]; then
  python3 "$ROOT/scripts/validate_jankbench.py" "$RESULTS_ROOT" \
    --expected-runs "$RUNS" --run-count "$JANK_RUN_COUNT" \
    --benchmark-id "$BENCH_ID" --policies "$POLICIES_CSV" >>"$LOG" 2>&1 || true
  python3 - "$RESULTS_ROOT/validity.json" "$RUNS" >"$RESULTS_ROOT/driver_cell_summary.txt" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
want = int(sys.argv[2])
bad = []
for policy, info in data["summary"]["by_policy"].items():
    print(f"{policy}\tvalid={info['valid_runs']}/{want}\t"
          f"discovered={info['discovered_runs']}\tmissing={info['missing_runs']}\t"
          f"invalid={info['invalid_runs']}")
    if info["valid_runs"] < want:
        bad.append(policy)
print("ALL_CELLS_VALID" if not bad else "INCOMPLETE " + ",".join(bad))
raise SystemExit(0 if not bad else 1)
PY
  RC=$?
  cat "$RESULTS_ROOT/driver_cell_summary.txt" | tee -a "$LOG"
  [ "$RC" = 0 ] || log "WARNING cell incomplete for bench_id=$BENCH_ID"
fi
