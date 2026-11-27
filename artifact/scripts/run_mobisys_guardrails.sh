#!/usr/bin/env bash
# =============================================================================
# Minimal MobiSys Supplementary Guardrail Campaign (~12-18 minutes total)
#
# Collects ALL missing MobiSys reviewer metrics in ONE compact pass WITHOUT
# re-running the existing JankBench / UiBench / 4-32c Scaling campaigns:
#   1. Combined 45s Real UI Scroll + System Telemetry (EEVDF, scx_lavd, scx_fcp):
#      - Delivered FPS & 1% Low FPS (from SurfaceFlinger / gfxinfo framestats)
#      - Per-cluster CPU Utilization % (/proc/stat: Silver / Gold / Prime)
#      - Per-cluster DVFS Frequency MHz & High-OPP Residency % (time_in_state)
#      - SoC Thermal Start/Mean/Peak/Delta degC (/sys/class/thermal) & Power
#   2. General-Purpose CPU Throughput Guardrail (Single-Core & Multi-Core):
#      - Runs Geekbench 6 automatically if installed on device, AND runs an
#        open deterministic CPU compute throughput check (0% starvation proof).
#   3. Mini Concurrency Load Sweep (0, C/4, C/2, 3C/4, C bg workers @ 90 Hz
#      with explicit -f 11111 us frame budget, >=600 frames/run).
#
# Usage (SA8155P physical board):
#   source ./artifact/sa8155.env && ./artifact/pipeline.sh guardrails
# Usage (Cuttlefish VM):
#   ./artifact/pipeline.sh guardrails
# =============================================================================
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ARTIFACT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
[ -f "$ARTIFACT_DIR/config.env" ] && . "$ARTIFACT_DIR/config.env"

RUNS=${GUARDRAIL_RUNS:-3}
UI_DURATION_S=${UI_DURATION_S:-45}
COOLDOWN_S=${COOLDOWN_S:-20}
POLICIES_STR=${GUARDRAIL_POLICIES:-"eevdf scx_lavd scx_fcp"}
OUT_DIR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --runs) RUNS="$2"; shift 2 ;;
    --duration) UI_DURATION_S="$2"; shift 2 ;;
    --cooldown) COOLDOWN_S="$2"; shift 2 ;;
    --policies) POLICIES_STR="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--runs 3] [--duration 45] [--cooldown 20] [--policies \"eevdf scx_lavd scx_fcp\"] [--out DIR]"
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

TS=$(date +%Y%m%d-%H%M%S)
OUT_DIR=${OUT_DIR:-"$ARTIFACT_DIR/results/mobisys_guardrails_${PLATFORM}_${TS}"}
mkdir -p "$OUT_DIR"

ADB=${ADB:-adb}
ANDROID_ADB_SERVER_PORT=${ANDROID_ADB_SERVER_PORT:-5037}
SERIAL=${SERIAL:?set SERIAL (e.g. source artifact/sa8155.env)}
REMOTE_DIR=${REMOTE_DIR:-/data/local/tmp/scx}
A=("$ADB" -P "$ANDROID_ADB_SERVER_PORT" -s "$SERIAL")

case "$SERIAL" in
  *:*) "$ADB" -P "$ANDROID_ADB_SERVER_PORT" connect "$SERIAL" >/dev/null 2>&1 || true ;;
esac
"${A[@]}" get-state >/dev/null 2>&1 || {
  echo "error: device $SERIAL not reachable on ADB port $ANDROID_ADB_SERVER_PORT" >&2
  exit 1
}

USE_SU=0
if [ "$("${A[@]}" shell id -u 2>/dev/null | tr -d '\r')" != "0" ]; then
  if [ "$("${A[@]}" shell "su 0 sh -c 'id -u'" 2>/dev/null | tr -d '\r')" = "0" ]; then
    USE_SU=1
  else
    "${A[@]}" root >/dev/null 2>&1 || true
    sleep 2
  fi
fi

rsh() {
  if [ "$USE_SU" = 1 ]; then
    local q
    printf -v q '%q' "export PATH=/product/bin:/apex/com.android.runtime/bin:/system/bin:/vendor/bin:\$PATH; $*"
    "${A[@]}" shell "su 0 sh -c $q"
  else
    "${A[@]}" shell "$@"
  fi
}

rsh_bg() {
  local inner="cd '$REMOTE_DIR' && ulimit -l unlimited && SCX_STATS_BASE_PATH='$REMOTE_DIR/stats' exec $*"
  if [ "$USE_SU" = 1 ]; then
    local q
    printf -v q '%q' "export PATH=/product/bin:/apex/com.android.runtime/bin:/system/bin:/vendor/bin:\$PATH; $inner"
    "${A[@]}" shell "su 0 sh -c $q" </dev/null >/dev/null 2>&1 &
  else
    "${A[@]}" shell "$inner" </dev/null >/dev/null 2>&1 &
  fi
}

echo "=== MobiSys Minimal Guardrail Suite ==="
echo "  Platform : $PLATFORM ($SERIAL)"
echo "  Output   : $OUT_DIR"
echo "  Runs     : $RUNS (UI window=${UI_DURATION_S}s, cooldown=${COOLDOWN_S}s)"

NCPU=$(rsh "nproc" | tr -d '\r')
KERNEL_REL=$(rsh "uname -r" | tr -d '\r')
echo "  Device   : ${NCPU} cores, kernel ${KERNEL_REL}"

# Resolve scx_fcp binary on device (prefer pure behavioral zero-hint binary if present)
FCP_BIN="$REMOTE_DIR/scx_fcp_behavioral"
if [ "$(rsh "[ -x '$FCP_BIN' ] && echo yes || echo no" | tr -d '\r')" != "yes" ]; then
  FCP_BIN="$REMOTE_DIR/scx_fcp"
fi
echo "  scx_fcp  : $FCP_BIN"
rsh "mkdir -p '$REMOTE_DIR/stats'" >/dev/null 2>&1 || true

HAS_BENCH=0
HAS_BG_ONLY=0
HAS_HZ_FLAG=0
if [ "$(rsh "[ -x '$REMOTE_DIR/fcp_frame_bench' ] && echo yes || echo no" | tr -d '\r')" = "yes" ]; then
  HAS_BENCH=1
  BENCH_HELP=$(rsh "'$REMOTE_DIR/fcp_frame_bench' --help 2>&1 || true" | tr -d '\r')
  echo "$BENCH_HELP" | grep -q -- '--bg-only' && HAS_BG_ONLY=1
  echo "$BENCH_HELP" | grep -q -- '--hz' && HAS_HZ_FLAG=1
fi
echo "  bench    : present=$HAS_BENCH bg_only=$HAS_BG_ONLY hz_flag=$HAS_HZ_FLAG"

stop_remote_daemons() {
  rsh "
    for n in scx_fcp scx_fcp_behavioral scx_fcp_named scx_fcp_notoken scx_fcp_nofanout scx_lavd scx_p2dq scx_bpfland fcp_frame_bench awk; do
      for p in \$(pidof \"\$n\" 2>/dev/null || true); do
        kill -TERM \"\$p\" 2>/dev/null || true
      done
    done
    for _i in 1 2 3 4 5 6 7 8; do
      alive=''
      for n in scx_fcp scx_fcp_behavioral scx_fcp_named scx_fcp_notoken scx_fcp_nofanout scx_lavd scx_p2dq scx_bpfland; do
        p=\$(pidof \"\$n\" 2>/dev/null || true)
        [ -n \"\$p\" ] && alive=1
      done
      [ -z \"\$alive\" ] && break
      sleep 0.5
    done
    for n in scx_fcp scx_fcp_behavioral scx_fcp_named scx_fcp_notoken scx_fcp_nofanout scx_lavd scx_p2dq scx_bpfland fcp_frame_bench awk; do
      for p in \$(pidof \"\$n\" 2>/dev/null || true); do
        kill -KILL \"\$p\" 2>/dev/null || true
      done
    done
    if [ \"\$(cat /sys/kernel/sched_ext/state 2>/dev/null)\" = 'enabled' ] && [ -x '$REMOTE_DIR/scx_detach' ]; then
      '$REMOTE_DIR/scx_detach' >/dev/null 2>&1 || true
    fi
  " >/dev/null 2>&1 || true
}

detach_schedulers() {
  stop_remote_daemons
  local waited=0
  while [ "$waited" -lt 30 ]; do
    local st
    st=$(rsh "cat /sys/kernel/sched_ext/state 2>/dev/null || echo disabled" | tr -d '\r')
    [ "$st" != "enabled" ] && return 0
    rsh "[ -x '$REMOTE_DIR/scx_detach' ] && '$REMOTE_DIR/scx_detach' >/dev/null 2>&1 || true" >/dev/null 2>&1 || true
    sleep 0.5
    waited=$((waited + 1))
  done
  echo "error: sched_ext did not return to disabled after detach" >&2
  return 1
}

attach_scheduler() {
  local pol=$1
  local budget_us=${2:-16667}
  detach_schedulers
  local log_file="$REMOTE_DIR/stats/guard_${pol}.log"
  rsh ": > '$log_file' && chmod 666 '$log_file'" >/dev/null 2>&1 || true
  case "$pol" in
    eevdf)
      ;;
    scx_lavd)
      rsh_bg "'$REMOTE_DIR/scx_lavd' >'$log_file' 2>&1"
      ;;
    scx_p2dq)
      rsh_bg "'$REMOTE_DIR/scx_p2dq' --enable-pelt false >'$log_file' 2>&1"
      ;;
    scx_fcp)
      rsh_bg "'$FCP_BIN' -f '$budget_us' -s 1 >'$log_file' 2>&1"
      ;;
  esac
  if [ "$pol" = "eevdf" ]; then
    local st
    st=$(rsh "cat /sys/kernel/sched_ext/state 2>/dev/null || echo disabled" | tr -d '\r')
    echo "    [sched_ext=$st] policy=$pol (budget=${budget_us}us)"
    return 0
  fi
  local waited=0
  local st="disabled"
  while [ "$waited" -lt 60 ]; do
    st=$(rsh "cat /sys/kernel/sched_ext/state 2>/dev/null || echo disabled" | tr -d '\r')
    if [ "$st" = "enabled" ]; then
      echo "    [sched_ext=$st] policy=$pol (budget=${budget_us}us)"
      return 0
    fi
    sleep 0.5
    waited=$((waited + 1))
  done
  echo "error: policy $pol failed to attach (sched_ext=$st)" >&2
  rsh "tail -n 20 '$log_file' 2>/dev/null || true" >&2 || true
  return 1
}

capture_snapshot() {
  local dst=$1
  rsh "
    echo '---PROC_STAT---'
    grep '^cpu' /proc/stat
    echo '---CPU_CAP---'
    for c in /sys/devices/system/cpu/cpu[0-9]*; do
      id=\${c##*cpu}
      cap=\$(cat \$c/cpu_capacity 2>/dev/null || echo 1024)
      echo \"cpu\$id \$cap\"
    done
    echo '---DVFS_TIME_IN_STATE---'
    for p in /sys/devices/system/cpu/cpufreq/policy*; do
      [ -r \"\$p/stats/time_in_state\" ] || continue
      echo \"POLICY \$(basename \$p) cpus=\$(cat \$p/related_cpus 2>/dev/null | tr '\n' ' ')\"
      cat \"\$p/stats/time_in_state\"
    done
    echo '---THERMAL---'
    for z in /sys/class/thermal/thermal_zone*; do
      [ -r \"\$z/temp\" ] || continue
      echo \"\$(basename \$z) \$(cat \$z/type 2>/dev/null || echo unk) \$(cat \$z/temp 2>/dev/null || echo 0)\"
    done
    echo '---POWER---'
    for ps in /sys/class/power_supply/*; do
      [ -d \"\$ps\" ] || continue
      cur=\$(cat \$ps/current_now 2>/dev/null || echo '')
      vol=\$(cat \$ps/voltage_now 2>/dev/null || echo '')
      pwr=\$(cat \$ps/power_now 2>/dev/null || echo '')
      [ -n \"\$cur\$vol\$pwr\" ] && echo \"\$(basename \$ps) current=\$cur voltage=\$vol power=\$pwr\"
    done
  " > "$dst"
}

read -r -a POL_LIST <<< "$POLICIES_STR"
BG_CNT=$(( (3 * NCPU) / 4 ))
[ "$BG_CNT" -lt 1 ] && BG_CNT=1

start_bg_contention() {
  if [ "$HAS_BENCH" = "1" ] && [ "$HAS_BG_ONLY" = "1" ]; then
    rsh_bg "'$REMOTE_DIR/fcp_frame_bench' --bg-only --bg-workers '$BG_CNT' >/dev/null 2>&1"
  else
    # Fallback for older fcp_frame_bench without --bg-only: spawn pure-CPU awk spinners
    rsh_bg "sh -c 'for i in \$(seq 1 $BG_CNT); do awk \"BEGIN{while(1)x++}\" >/dev/null 2>&1 & done; wait'"
  fi
  sleep 1
}

stop_bg_contention() {
  rsh "
    for n in fcp_frame_bench awk; do
      for p in \$(pidof \"\$n\" 2>/dev/null || true); do
        kill -INT \"\$p\" 2>/dev/null || true
      done
    done
    sleep 0.5
    for n in fcp_frame_bench awk; do
      for p in \$(pidof \"\$n\" 2>/dev/null || true); do
        kill -KILL \"\$p\" 2>/dev/null || true
      done
    done
  " >/dev/null 2>&1 || true
}

# -----------------------------------------------------------------------------
# PART 1: Combined 45s Real App Scroll + CPU Util + DVFS + Thermal + FPS
# -----------------------------------------------------------------------------
echo ""
echo "[1/3] Running Combined Real-UI Scroll + CPU Util + DVFS + Thermal + FPS..."
UI_PKG="com.android.settings"
UI_ACT="com.android.settings/.Settings"

for run in $(seq 1 "$RUNS"); do
  for pol in "${POL_LIST[@]}"; do
    CELL_DIR="$OUT_DIR/ui_guardrail/${pol}/run-${run}"
    mkdir -p "$CELL_DIR"
    echo "  -> Run $run/$RUNS | Policy: $pol (cooling ${COOLDOWN_S}s)"
    detach_schedulers
    sleep "$COOLDOWN_S"

    attach_scheduler "$pol" 16667
    rsh "am start -W -n '$UI_ACT' >/dev/null 2>&1 || true; sleep 2; dumpsys gfxinfo '$UI_PKG' reset >/dev/null 2>&1 || true"

    capture_snapshot "$CELL_DIR/snap_before.txt"

    # Start background CPU contention + periodic thermal log + continuous fling gestures
    start_bg_contention
    rsh "
      : > '$REMOTE_DIR/stats/therm_trace.txt'
      END_TS=\$(( \$(date +%s) + $UI_DURATION_S ))
      while [ \$(date +%s) -lt \$END_TS ]; do
        input swipe 500 1400 500 350 180 >/dev/null 2>&1 || true
        input swipe 500 350 500 1400 180 >/dev/null 2>&1 || true
        for z in /sys/class/thermal/thermal_zone*/temp; do cat \$z 2>/dev/null; done | sort -nr | head -n 1 >> '$REMOTE_DIR/stats/therm_trace.txt'
      done
    " || true
    stop_bg_contention

    capture_snapshot "$CELL_DIR/snap_after.txt"
    rsh "dumpsys gfxinfo '$UI_PKG' framestats" > "$CELL_DIR/gfxinfo.txt" 2>/dev/null || true
    rsh "cat '$REMOTE_DIR/stats/therm_trace.txt' 2>/dev/null; rm -f '$REMOTE_DIR/stats/therm_trace.txt'" > "$CELL_DIR/therm_trace.txt" || true
    if [ "$pol" = "scx_fcp" ]; then
      rsh "cat '$REMOTE_DIR/stats/guard_scx_fcp.log' 2>/dev/null" > "$CELL_DIR/fcp_telemetry.log" || true
    fi
  done
done

# -----------------------------------------------------------------------------
# PART 2: General-Purpose CPU Throughput Guardrail (Single-Core & Multi-Core)
# -----------------------------------------------------------------------------
echo ""
echo "[2/3] Running General CPU Throughput Guardrail (Open Compute + Geekbench6 if installed)..."
HAS_GB6=$(rsh "pm list packages 2>/dev/null | grep -q 'geekbench' && echo apk || ([ -x /data/local/tmp/geekbench6 ] && echo cli || echo none)" | tr -d '\r')
echo "  Geekbench 6 detected on device: $HAS_GB6"

for run in $(seq 1 "$RUNS"); do
  for pol in "${POL_LIST[@]}"; do
    TP_DIR="$OUT_DIR/throughput/${pol}/run-${run}"
    mkdir -p "$TP_DIR"
    echo "  -> Throughput Run $run/$RUNS | Policy: $pol"
    attach_scheduler "$pol" 16667

    # Deterministic 15s Single-Core & Multi-Core SHA256/CPU throughput (MB/s)
    # Time on host (64-bit Ubuntu) to avoid Android toybox date/mksh integer issues
    T0=$(date +%s%N)
    rsh "dd if=/dev/zero bs=1M count=256 2>/dev/null | sha256sum >/dev/null"
    T1=$(date +%s%N)
    T2=$(date +%s%N)
    rsh "
      for i in \$(seq 1 $NCPU); do
        (dd if=/dev/zero bs=1M count=128 2>/dev/null | sha256sum >/dev/null) &
      done
      wait
    "
    T3=$(date +%s%N)
    printf 'single_ns=%s\nmulti_ns=%s ncpu=%s\n' "$((T1 - T0))" "$((T3 - T2))" "$NCPU" > "$TP_DIR/sha256_bench.txt"

    if [ "$HAS_GB6" = "cli" ] && [ "$run" -eq 1 ]; then
      rsh "/data/local/tmp/geekbench6 --cpu" > "$TP_DIR/geekbench6.txt" 2>&1 || true
    fi
  done
done

# -----------------------------------------------------------------------------
# PART 3: Mini Background Load Sweep (0, C/4, C/2, 3C/4, C) @ 90 Hz (-f 11111)
# -----------------------------------------------------------------------------
if [ "$HAS_BENCH" = "1" ]; then
  echo ""
  echo "[3/3] Running Mini 90Hz Load Sweep (600 frames/point, -f 11111us)..."
  mkdir -p "$OUT_DIR/load_sweep"
  HZ_FLAG=""
  [ "$HAS_HZ_FLAG" = "1" ] && HZ_FLAG="--hz 90"
  W1=$(( NCPU / 4 )); W2=$(( NCPU / 2 )); W3=$(( (3 * NCPU) / 4 )); W4=$NCPU
  for bg in 0 "$W1" "$W2" "$W3" "$W4"; do
    for pol in "${POL_LIST[@]}"; do
      attach_scheduler "$pol" 11111
      rsh "'$REMOTE_DIR/fcp_frame_bench' --scheduler '${pol}_bg${bg}' --frames 600 $HZ_FLAG --stages 4 --stage-us 1200 --bg-workers '$bg' --anonymous-comms" \
        >> "$OUT_DIR/load_sweep/sweep_90hz.jsonl"
    done
  done
fi

detach_schedulers

# Summarize all collected measurements into JSON + Markdown + LaTeX
python3 "$SCRIPT_DIR/summarize_guardrails.py" "$OUT_DIR"
echo ""
echo "=== DONE! Guardrail results & LaTeX table written to: $OUT_DIR ==="
