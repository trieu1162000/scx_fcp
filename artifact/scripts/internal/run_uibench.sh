#!/usr/bin/env bash
# Run AOSP UiBenchJankTests on a connected device and save raw output.
#
# The primary metric is the GfxMonitor instrumentation summary (gfx-avg-*),
# which covers every frame in the test window.  dumpsys gfxinfo framestats is
# kept as a diagnostic only; its ring buffer retains at most 120 frames.
#
# This script only installs the two APKs it is given, runs instrumentation,
# writes host-side files, and validates each run.  It does not attach a
# scheduler.
#
# Usage:
#   run_uibench.sh -p eevdf -r 5 -n 1 --tests inflating-list,bitmap-upload --shuffle
#   run_uibench.sh -s 0.0.0.0:6520 -p scx_fcp_behavioral -r 5 -n 1 \
#       --out results/uibench_stable/16vcpu --tests inflating-list,bitmap-upload,shadow-grid-list
#
# Output layout:
#   <out>/<policy>/uibench/run-<nn>/
#     manifest.json
#     <test>/rep-<nn>/{instrument.txt,env.json,gfxinfo_summary.txt,...}
#     metrics.json
#     metrics.csv
#     summary.txt

set -euo pipefail

SERIAL=${SERIAL:-0.0.0.0:6520}
ADB=${ADB:-adb}
APP_PKG=com.android.test.uibench
TEST_PKG=com.android.uibench.janktests
RUNNER=$TEST_PKG/androidx.test.runner.AndroidJUnitRunner
ITERATIONS=1
REPEATS=1
RUN=1
POLICY=unlabeled
TAG=uibench
SHUFFLE=0
INSTALL=1
OUTROOT=""
APKDIR=""

# short name | class#method
TESTS=(
  "bitmap-upload|com.android.uibench.janktests.UiBenchRenderingJankTests#testBitmapUploadJank"
  "shadow-grid-list|com.android.uibench.janktests.UiBenchRenderingJankTests#testShadowGridListFling"
  "inflating-list|com.android.uibench.janktests.UiBenchJankTests#testInflatingListViewFling"
  "slow-bind-recycler|com.android.uibench.janktests.UiBenchJankTests#testSlowBindRecyclerViewFling"
  "trivial-recycler|com.android.uibench.janktests.UiBenchJankTests#testTrivialRecyclerListViewFling"
  "layout-cache-low|com.android.uibench.janktests.UiBenchTextJankTests#testLayoutCacheLowHitrateFling"
)

usage() {
  sed -n '2,30p' "$0"
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    -s|--serial) SERIAL=$2; shift 2 ;;
    -p|--policy) POLICY=$2; shift 2 ;;
    -r|--repeats) REPEATS=$2; shift 2 ;;
    -n|--run) RUN=$2; shift 2 ;;
    -t|--tag) TAG=$2; shift 2 ;;
    -o|--out) OUTROOT=$2; shift 2 ;;
    -a|--apk-dir) APKDIR=$2; shift 2 ;;
    -i|--iterations) ITERATIONS=$2; shift 2 ;;
    --shuffle) SHUFFLE=1; shift ;;
    --no-install) INSTALL=0; shift ;;
    --tests) IFS=',' read -r -a WANTED <<< "$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown argument $1" >&2; usage ;;
  esac
done

HERE=$(cd "$(dirname "$0")" && pwd)
PAPER=$(cd "$HERE/../.." && pwd)
OUTROOT=${OUTROOT:-$PAPER/results/uibench_stable}
APKDIR=${APKDIR:-$PAPER/workloads}

APP_APK=${UI_APP_APK:-$APKDIR/UiBench-app-debug.apk}
TEST_APK=${UI_TEST_APK:-$APKDIR/UiBenchJankTests-debug.apk}
[ -f "$APP_APK" ] || { echo "missing $APP_APK" >&2; exit 1; }
[ -f "$TEST_APK" ] || { echo "missing $TEST_APK" >&2; exit 1; }

A=("$ADB" -s "$SERIAL")
"${A[@]}" get-state >/dev/null 2>&1 || { echo "device $SERIAL not reachable" >&2; exit 1; }

KERNEL=$("${A[@]}" shell uname -r | tr -d '\r')
VCPU=$("${A[@]}" shell nproc | tr -d '\r')
SDK=$("${A[@]}" shell getprop ro.build.version.sdk | tr -d '\r')
BUILD=$("${A[@]}" shell getprop ro.build.display.id | tr -d '\r')
GOVERNOR=$("${A[@]}" shell 'cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo unknown' | tr -d '\r')
REFRESH=$("${A[@]}" shell "dumpsys display 2>/dev/null | grep -oE 'mRefreshRate=[0-9.]+|renderFrameRate [0-9.]+' | head -1" | tr -d '\r' | sed -E 's/.*[= ]//')
[ -n "${REFRESH:-}" ] || REFRESH=$("${A[@]}" shell "dumpsys SurfaceFlinger 2>/dev/null | grep -oE '[0-9.]+ Hz' | head -1" | tr -d '\r' | sed 's/ *Hz//')
[ -n "${REFRESH:-}" ] || REFRESH=60
case "$REFRESH" in
  *[!0-9.]*) REFRESH=60 ;;
esac

# Expected scheduler state and ops for the validity sidecar.  EEVDF means no
# sched_ext scheduler is attached.  The names match the artifact binaries.
case "$POLICY" in
  eevdf|base) EXPECTED_STATE=disabled; EXPECTED_OPS="" ;;
  scx_fcp_behavioral|scx_fcp|fcp_zh) EXPECTED_STATE=enabled; EXPECTED_OPS=fcp_zh ;;
  scx_fcp_v_stable_root|scx_fcp_v_a_depth3_expiry75|scx_fcp_v_a_depth3_tgid1ms) EXPECTED_STATE=enabled; EXPECTED_OPS=fcp_zh ;;
  scx_fcp_named|fcp_nh) EXPECTED_STATE=enabled; EXPECTED_OPS=fcp_nh ;;
  scx_p2dq_pelt_off|scx_p2dq|p2dq) EXPECTED_STATE=enabled; EXPECTED_OPS=p2dq ;;
  scx_lavd|lavd) EXPECTED_STATE=enabled; EXPECTED_OPS=lavd ;;
  *) EXPECTED_STATE=""; EXPECTED_OPS="" ;;
esac

KMAJOR=$(echo "$KERNEL" | grep -oE '^[0-9]+\.[0-9]+' || echo unknown)
PLATFORM="cf-${KMAJOR}-${VCPU}vcpu"
DATE=$(date +%F)
RUNDIR=$OUTROOT/$DATE/$PLATFORM/$POLICY/uibench/run-$(printf '%02d' "$RUN")
mkdir -p "$RUNDIR"

echo "device   $SERIAL"
echo "kernel   $KERNEL  vcpu $VCPU  sdk $SDK  refresh $REFRESH Hz"
echo "policy   $POLICY  expected state=$EXPECTED_STATE ops=${EXPECTED_OPS:-none}"
echo "out      $RUNDIR"

sh_() { "${A[@]}" shell "$@" 2>/dev/null; }
top_activity() {
  sh_ 'dumpsys activity activities' | tr -d '\r' | grep -m1 'topResumedActivity=' || true
}
wakefulness() {
  sh_ 'dumpsys power' | tr -d '\r' | grep -m1 'mWakefulness=' | sed 's/.*mWakefulness=//' || true
}
systemui_pid() {
  sh_ 'pidof com.android.systemui' | tr -d '\r' | awk '{print $1}' || true
}
selinux_state() { sh_ 'getenforce' | tr -d '\r'; }
scx_state() { sh_ 'cat /sys/kernel/sched_ext/state 2>/dev/null' | tr -d '\r' || true; }
scx_ops() { sh_ 'cat /sys/kernel/sched_ext/root/ops 2>/dev/null' | tr -d '\r' || true; }
boot_id() { sh_ 'cat /proc/sys/kernel/random/boot_id' | tr -d '\r' || true; }
uptime_s() { sh_ 'cut -d. -f1 /proc/uptime' | tr -d '\r' || true; }

ensure_awake() {
  local deadline=$((SECONDS + 30))
  while [ $SECONDS -lt $deadline ]; do
    [ "$(wakefulness)" = "Awake" ] && return 0
    sh_ 'input keyevent KEYCODE_WAKEUP' >/dev/null 2>&1 || true
    sh_ 'wm dismiss-keyguard' >/dev/null 2>&1 || true
    sleep 2
  done
  return 1
}

ensure_home() {
  sh_ 'input keyevent KEYCODE_HOME' >/dev/null 2>&1 || true
  local deadline=$((SECONDS + 30)) top
  while [ $SECONDS -lt $deadline ]; do
    top=$(top_activity)
    case "$top" in
      *launcher3*) return 0 ;;
    esac
    sleep 2
  done
  return 1
}

ensure_services() {
  local deadline=$((SECONDS + 240)) activity ss
  while [ $SECONDS -lt $deadline ]; do
    activity=$(sh_ 'service check activity' | tr -d '\r')
    ss=$(sh_ 'pidof system_server' | tr -d '\r')
    if printf '%s' "$activity" | grep -q 'found' && [ -n "$ss" ]; then
      return 0
    fi
    sleep 5
  done
  return 1
}

install_apks() {
  if ! "${A[@]}" install -r -g "$APP_APK" > "$RUNDIR/install_app.txt" 2>&1; then
    # A local rebuild can carry a different debug signature.  Replace the
    # package rather than failing the whole block.
    if grep -qi 'INSTALL_FAILED_UPDATE_INCOMPATIBLE\|signatures do not match' \
        "$RUNDIR/install_app.txt"; then
      "${A[@]}" uninstall "$APP_PKG" >/dev/null 2>&1 || true
      "${A[@]}" install -g "$APP_APK" >> "$RUNDIR/install_app.txt" 2>&1 || {
        echo "install failed for $APP_APK" >&2; cat "$RUNDIR/install_app.txt" >&2; exit 1; }
    else
      echo "install failed for $APP_APK" >&2; cat "$RUNDIR/install_app.txt" >&2; exit 1
    fi
  fi
  "${A[@]}" install -r -g "$TEST_APK" > "$RUNDIR/install_test.txt" 2>&1 || {
    echo "install failed for $TEST_APK" >&2; cat "$RUNDIR/install_test.txt" >&2; exit 1; }
}

wireless_off_animations() {
  sh_ 'svc power stayon true' >/dev/null 2>&1 || true
  sh_ 'setprop service.bootanim.exit 1' >/dev/null 2>&1 || true
  sh_ 'stop bootanim' >/dev/null 2>&1 || true
  sh_ 'setprop debug.sf.nobootanimation 1' >/dev/null 2>&1 || true
  sh_ 'input keyevent KEYCODE_WAKEUP' >/dev/null 2>&1 || true
  sh_ 'wm dismiss-keyguard' >/dev/null 2>&1 || true
  sh_ 'settings put global window_animation_scale 0' >/dev/null 2>&1 || true
  sh_ 'settings put global transition_animation_scale 0' >/dev/null 2>&1 || true
  sh_ 'settings put global animator_duration_scale 0' >/dev/null 2>&1 || true
  sh_ 'settings put system screen_off_timeout 2147483647' >/dev/null 2>&1 || true
  sh_ 'settings put global stay_on_while_plugged_in 7' >/dev/null 2>&1 || true
  sh_ 'settings put secure accessibility_disable_animations 0' >/dev/null 2>&1 || true
  sh_ 'setprop debug.hwui.filter_test_overhead true' >/dev/null 2>&1 || true
}

wait_ui_stable() {
  # The image can restart SystemUI once after boot.  The matrix driver performs
  # the long post-boot wait; this per-invocation check is short.
  local deadline=$((SECONDS + 60))
  local last_pid="" stable=0 pid top kg
  while [ $SECONDS -lt $deadline ]; do
    ensure_awake >/dev/null 2>&1 || true
    pid=$(systemui_pid)
    top=$(top_activity)
    kg=$(sh_ 'dumpsys activity activities' | tr -d '\r' | grep -m1 'KeyguardShowing=' || true)
    if [ -n "$pid" ] && [ "$pid" = "$last_pid" ]; then
      stable=$((stable + 1))
    else
      stable=0; last_pid=$pid
    fi
    if [ -n "$pid" ] && [ "$stable" -ge 6 ] &&
       printf '%s' "$top" | grep -q 'launcher3' &&
       printf '%s' "$kg" | grep -q 'KeyguardShowing=false'; then
      return 0
    fi
    ensure_home >/dev/null 2>&1 || true
    sleep 2
  done
  echo "WARNING UI did not settle (systemui=${last_pid:-none} top=${top:-?})" >&2
  return 1
}

snapshot_env() {
  # $1 test dir, $2 rep, $3 start epoch, $4 systemui pid before
  local td=$1 rep=$2 start=$3 sysui_before=$4
  local end_epoch sysui_after top_after state_ops state_now uptime loadavg memavail
  end_epoch=$(date +%s)
  sysui_after=$(systemui_pid)
  top_after=$(top_activity)
  state_now=$(scx_state)
  state_ops=$(scx_ops)
  uptime=$(uptime_s)
  loadavg=$(cat /proc/loadavg 2>/dev/null | awk '{print $1" "$2" "$3}' || true)
  memavail=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || true)
  python3 - "$td/env.json" <<PY
import json, sys
env = {
  "test": "$(basename "$(dirname "$td")")" if "$(basename "$td")".startswith("rep-") else "$(basename "$td")",
  "rep": $rep,
  "policy": "$POLICY",
  "start_epoch": $start,
  "end_epoch": $end_epoch,
  "duration_s": $((end_epoch - start)),
  "systemui_pid_before": "$sysui_before",
  "systemui_pid_after": "$sysui_after",
  "top_activity_after": """$top_after""",
  "boot_id": "$(boot_id)",
  "selinux": "$(selinux_state)",
  "scx_state_before": "$STATE_BEFORE",
  "scx_ops_before": "$OPS_BEFORE",
  "scx_state_after": "$state_now",
  "scx_ops_after": "$state_ops",
  "expected_state": "$EXPECTED_STATE",
  "expected_ops": "$EXPECTED_OPS",
  "wakefulness": "$(wakefulness)",
  "device_uptime_s": "$uptime",
  "host_loadavg": "$loadavg",
  "host_mem_available_mb": "$memavail",
  "host_nproc": "$(nproc 2>/dev/null || echo unknown)",
  "refresh_hz": $REFRESH
}
json.dump(env, open(sys.argv[1], "w"), indent=2)
PY
}

# ---------------------------------------------------------------------------

if [ "$INSTALL" = 1 ]; then
  install_apks
fi
ensure_services || { echo "framework services unavailable" >&2; exit 1; }
wireless_off_animations
wait_ui_stable || true

# Resolve the test list. Default is all six.
SELECTED=()
if [ "${WANTED+x}" = x ]; then
  for w in "${WANTED[@]}"; do
    for row in "${TESTS[@]}"; do
      [ "${row%%|*}" = "$w" ] && SELECTED+=("$row")
    done
  done
else
  SELECTED=("${TESTS[@]}")
fi
[ ${#SELECTED[@]} -gt 0 ] || { echo "no matching tests" >&2; exit 1; }

STATE_BEFORE=$(scx_state)
OPS_BEFORE=$(scx_ops)
printf '%s\n%s\n' "$STATE_BEFORE" "$OPS_BEFORE" > "$RUNDIR/state_before.txt"

START=$(date -Iseconds)
for rep in $(seq 1 "$REPEATS"); do
  ORDER=("${SELECTED[@]}")
  if [ "$SHUFFLE" = 1 ]; then
    mapfile -t ORDER < <(printf '%s\n' "${SELECTED[@]}" | shuf)
  fi
  ORDER_FILE=$RUNDIR/test_order_rep-$(printf '%02d' "$rep").txt
  : > "$ORDER_FILE"
  for row in "${ORDER[@]}"; do echo "${row%%|*}" >> "$ORDER_FILE"; done

  for row in "${ORDER[@]}"; do
    short=${row%%|*}
    spec=${row#*|}
    class=${spec%%#*}
    method=${spec##*#}
    td=$RUNDIR/$short
    if [ "$REPEATS" -gt 1 ]; then td=$RUNDIR/$short/rep-$(printf '%02d' "$rep"); fi
    mkdir -p "$td"

    echo "run $short $class#$method rep $rep"
    ensure_awake >/dev/null 2>&1 || true
    "${A[@]}" shell am force-stop "$APP_PKG" >/dev/null 2>&1 || true
    "${A[@]}" shell am force-stop "$TEST_PKG" >/dev/null 2>&1 || true
    sleep 1
    "${A[@]}" shell "dumpsys gfxinfo $APP_PKG reset" > "$td/gfxinfo_reset.txt" 2>&1 || true
    "${A[@]}" logcat -c >/dev/null 2>&1 || true

    SYS_BEFORE=$(systemui_pid)
    T_START=$(date +%s)
    set +e
    "${A[@]}" shell "am instrument -w -r -e class '$class#$method' -e iterations $ITERATIONS '$RUNNER'" > "$td/instrument.txt" 2>&1
    rc=$?
    set -e
    echo "$rc" > "$td/exit_code.txt"

    "${A[@]}" shell "dumpsys gfxinfo $APP_PKG" > "$td/gfxinfo_summary.txt" 2>&1 || true
    "${A[@]}" shell "dumpsys gfxinfo $APP_PKG framestats" > "$td/gfxinfo_framestats.txt" 2>&1 || true
    "${A[@]}" logcat -d -v threadtime > "$td/logcat.txt" 2>&1 || true
    snapshot_env "$td" "$rep" "$T_START" "$SYS_BEFORE"
  done
done
END=$(date -Iseconds)

"${A[@]}" shell "cat /sys/kernel/sched_ext/state 2>/dev/null" | tr -d '\r' > "$RUNDIR/state_after.txt" 2>/dev/null || true
"${A[@]}" shell "cat /sys/kernel/sched_ext/root/ops 2>/dev/null" | tr -d '\r' >> "$RUNDIR/state_after.txt" 2>/dev/null || true

APP_SHA=$(sha256sum "$APP_APK" | awk '{print $1}')
TEST_SHA=$(sha256sum "$TEST_APK" | awk '{print $1}')
HOST_KERNEL=$(uname -r)
HOST_NPROC=$(nproc)
HOST_MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
python3 - "$RUNDIR/manifest.json" <<PY
import json, sys
manifest = {
  "date": "$DATE",
  "platform": "$PLATFORM",
  "serial": "$SERIAL",
  "kernel": "$KERNEL",
  "vcpu": int("$VCPU") if "$VCPU".isdigit() else "$VCPU",
  "sdk": "$SDK",
  "build": "$BUILD",
  "governor": "$GOVERNOR",
  "refresh_hz": float("$REFRESH") if "$REFRESH" else 60.0,
  "policy": "$POLICY",
  "expected_state": "$EXPECTED_STATE",
  "expected_ops": "$EXPECTED_OPS",
  "tag": "$TAG",
  "workload": "uibench",
  "run": int("$RUN"),
  "repeats": int("$REPEATS"),
  "iterations": int("$ITERATIONS"),
  "runner": "$RUNNER",
  "app_package": "$APP_PKG",
  "test_package": "$TEST_PKG",
  "app_apk": "$APP_APK",
  "app_sha256": "$APP_SHA",
  "test_apk": "$TEST_APK",
  "test_sha256": "$TEST_SHA",
  "host_kernel": "$HOST_KERNEL",
  "host_nproc": $HOST_NPROC,
  "host_mem_total_mb": $HOST_MEM_MB,
  "selinux": "$(selinux_state)",
  "setenforce0_recorded": True,
  "require_env": True,
  "start": "$START",
  "end": "$END"
}
tests = []
for rep in range(1, int("$REPEATS") + 1):
    path = f"$RUNDIR/test_order_rep-{rep:02d}.txt"
    try:
        tests.extend([line.strip() for line in open(path) if line.strip()])
    except OSError:
        pass
manifest["test_order"] = tests
manifest["tests"] = sorted(set(tests))
json.dump(manifest, open(sys.argv[1], "w"), indent=2)
PY

python3 "$HERE/extract_uibench.py" "$RUNDIR" | tee "$RUNDIR/summary.txt"
