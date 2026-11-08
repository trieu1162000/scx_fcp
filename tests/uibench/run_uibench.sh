#!/usr/bin/env bash
# Run AOSP UiBenchJankTests on a connected Android device.
#
# The primary metric is the GfxMonitor instrumentation summary (gfx-avg-*).
# Raw gfxinfo framestats are kept as a diagnostic only.
#
# Usage:
#   tests/uibench/run_uibench.sh -p eevdf -n 1
#   tests/uibench/run_uibench.sh -p scx_fcp_behavioral -n 1 -r 3 \
#       --tests inflating-list,bitmap-upload
#
# Environment:
#   ADB               adb binary                  (default: adb)
#   SERIAL            device serial               (default: 127.0.0.1:6520)
#   SCX_ROOT          scx workspace               (default: $HOME/workspace/scx)
#   RESULTS_ROOT      result directory            (default: <repo>/results/uibench)
#   BIN_DIR           scheduler binary directory  (default: $SCX_ROOT/target/release)
#   UIBENCH_APK       UiBench app APK             (optional)
#   UIBENCH_TEST_APK  UiBenchJankTests APK        (optional)
#   SCHED_BIN         scheduler binary name       (optional, enables attach)
#   OPS               expected sched_ext ops name (default: derived from policy)
#
# The output layout is <results>/<policy>/uibench/run-NN/<test>/rep-NN/.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
ADB=${ADB:-adb}
SERIAL=${SERIAL:-127.0.0.1:6520}
SCX_ROOT=${SCX_ROOT:-$HOME/workspace/scx}
RESULTS_ROOT=${RESULTS_ROOT:-$REPO_ROOT/results/uibench}
BIN_DIR=${BIN_DIR:-$SCX_ROOT/target/release}
REMOTE=${REMOTE:-/data/local/tmp/scx}
POLICY=${POLICY:-unlabeled}
RUN=${RUN:-1}
REPEATS=${REPEATS:-1}
ITERATIONS=${ITERATIONS:-1}
TAG=${TAG:-uibench}
SHUFFLE=${SHUFFLE:-0}
INSTALL=${INSTALL:-0}
SCHED_BIN=${SCHED_BIN:-}
SCHED_ARGS=${SCHED_ARGS:-}
WANTED=""

while [ $# -gt 0 ]; do
    case "$1" in
        -s|--serial) SERIAL=${2:?missing serial}; shift 2 ;;
        -p|--policy) POLICY=${2:?missing policy}; shift 2 ;;
        -n|--run) RUN=${2:?missing run}; shift 2 ;;
        -r|--repeats) REPEATS=${2:?missing repeats}; shift 2 ;;
        -i|--iterations) ITERATIONS=${2:?missing iterations}; shift 2 ;;
        -t|--tag) TAG=${2:?missing tag}; shift 2 ;;
        --tests) WANTED=${2:?missing tests}; shift 2 ;;
        --shuffle) SHUFFLE=1; shift ;;
        --install) INSTALL=1; shift ;;
        --sched-bin) SCHED_BIN=${2:?missing binary}; shift 2 ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown argument $1" >&2; exit 2 ;;
    esac
done

case "$POLICY" in
    eevdf|base) EXPECTED_STATE=disabled; DEFAULT_OPS="" ;;
    scx_fcp) EXPECTED_STATE=enabled; DEFAULT_OPS=fcp ;;
    scx_fcp_named) EXPECTED_STATE=enabled; DEFAULT_OPS=fcp_nh ;;
    scx_fcp_behavioral|scx_fcp_zero_hint) EXPECTED_STATE=enabled; DEFAULT_OPS=fcp_zh ;;
    scx_lavd) EXPECTED_STATE=enabled; DEFAULT_OPS=lavd ;;
    scx_p2dq) EXPECTED_STATE=enabled; DEFAULT_OPS=p2dq ;;
    *) EXPECTED_STATE=""; DEFAULT_OPS="" ;;
esac
EXPECTED_OPS=${OPS:-$DEFAULT_OPS}

APP_PKG=com.android.test.uibench
TEST_PKG=com.android.uibench.janktests
RUNNER=$TEST_PKG/androidx.test.runner.AndroidJUnitRunner
APP_APK=${UIBENCH_APK:-}
TEST_APK=${UIBENCH_TEST_APK:-}

TESTS=(
    "bitmap-upload|com.android.uibench.janktests.UiBenchRenderingJankTests#testBitmapUploadJank"
    "shadow-grid-list|com.android.uibench.janktests.UiBenchRenderingJankTests#testShadowGridListFling"
    "inflating-list|com.android.uibench.janktests.UiBenchJankTests#testInflatingListViewFling"
    "slow-bind-recycler|com.android.uibench.janktests.UiBenchJankTests#testSlowBindRecyclerViewFling"
    "trivial-recycler|com.android.uibench.janktests.UiBenchJankTests#testTrivialRecyclerViewFling"
    "layout-cache-low|com.android.uibench.janktests.UiBenchTextJankTests#testLayoutCacheLowHitrateFling"
)

A=("$ADB" -s "$SERIAL")
RUNDIR=$RESULTS_ROOT/$POLICY/uibench/run-$(printf '%02d' "$RUN")
mkdir -p "$RUNDIR"

sh_() { "${A[@]}" shell "$@" 2>/dev/null | tr -d '\r'; }
top_activity() { sh_ 'dumpsys activity activities' | grep -m1 topResumedActivity || true; }
systemui_pid() { sh_ 'pidof com.android.systemui' | awk '{print $1}' || true; }
selinux_state() { sh_ 'getenforce' || true; }
scx_state() { sh_ 'cat /sys/kernel/sched_ext/state 2>/dev/null' || true; }
scx_ops() { sh_ 'cat /sys/kernel/sched_ext/root/ops 2>/dev/null' || true; }
boot_id() { sh_ 'cat /proc/sys/kernel/random/boot_id' || true; }

refresh_rate() {
    local rate
    rate=$(sh_ "dumpsys display 2>/dev/null | grep -oE 'mRefreshRate=[0-9.]+' | head -1" | sed 's/.*=//' || true)
    [ -n "$rate" ] || rate=$(sh_ "dumpsys SurfaceFlinger 2>/dev/null | grep -oE '[0-9.]+ Hz' | head -1" | sed 's/ *Hz//' || true)
    case "$rate" in ''|*[!0-9.]*) rate=60 ;; esac
    printf '%s' "$rate"
}

wait_device() {
    local deadline=$((SECONDS + 120))
    while [ $SECONDS -lt $deadline ]; do
        "$ADB" connect "$SERIAL" >/dev/null 2>&1 || true
        if [ "$("${A[@]}" get-state 2>/dev/null | tr -d '\r')" = device ]; then
            return 0
        fi
        sleep 2
    done
    return 1
}

kill_stale() {
    local name pid
    for name in scx_fcp scx_fcp_named scx_fcp_behavioral scx_lavd scx_p2dq; do
        pid=$(sh_ "pidof $name" || true)
        [ -n "$pid" ] && sh_ "kill -TERM $pid" >/dev/null 2>&1 || true
    done
    local deadline=$((SECONDS + 20))
    while [ $SECONDS -lt $deadline ]; do
        [ "$(scx_state)" = disabled ] && return 0
        sleep 1
    done
    return 1
}

start_scheduler() {
    [ -n "$SCHED_BIN" ] || return 0
    local src=$BIN_DIR/$SCHED_BIN
    [ -f "$src" ] || { echo "missing scheduler binary $src" >&2; exit 1; }
    sh_ "mkdir -p $REMOTE/stats" >/dev/null 2>&1 || true
    "${A[@]}" push "$src" "$REMOTE/$SCHED_BIN" >/dev/null
    local remote_log=$REMOTE/${POLICY}_uibench_${RUN}.log
    timeout 20 "${A[@]}" shell \
        "cd $REMOTE && rm -f $remote_log && ulimit -l unlimited && SCX_STATS_BASE_PATH=$REMOTE/stats nohup ./$SCHED_BIN $SCHED_ARGS > $remote_log 2>&1 < /dev/null &" \
        >/dev/null 2>&1 || true
    local deadline=$((SECONDS + 120)) st o
    while [ $SECONDS -lt $deadline ]; do
        st=$(scx_state)
        o=$(scx_ops)
        if [ "$st" = enabled ] && printf '%s' "$o" | grep -q "$EXPECTED_OPS"; then
            return 0
        fi
        sleep 2
    done
    echo "scheduler did not reach enabled with ops $EXPECTED_OPS" >&2
    exit 1
}

stop_scheduler() {
    [ -n "$SCHED_BIN" ] || return 0
    local pid
    pid=$(sh_ "pidof $SCHED_BIN" || true)
    [ -n "$pid" ] && sh_ "kill -TERM $pid" >/dev/null 2>&1 || true
    local deadline=$((SECONDS + 20))
    while [ $SECONDS -lt $deadline ]; do
        [ "$(scx_state)" = disabled ] && return 0
        sleep 1
    done
    echo "warning: sched_ext state after stop is $(scx_state)" >&2
    return 0
}

ensure_awake() {
    local deadline=$((SECONDS + 30))
    while [ $SECONDS -lt $deadline ]; do
        sh_ 'dumpsys power' | grep -q 'mWakefulness=Awake' && return 0
        sh_ 'input keyevent KEYCODE_WAKEUP' >/dev/null 2>&1 || true
        sh_ 'wm dismiss-keyguard' >/dev/null 2>&1 || true
        sleep 2
    done
    return 1
}

prepare_device() {
    sh_ 'svc power stayon true' >/dev/null 2>&1 || true
    sh_ 'settings put global stay_on_while_plugged_in 7' >/dev/null 2>&1 || true
    sh_ 'settings put system screen_off_timeout 2147483647' >/dev/null 2>&1 || true
    sh_ 'settings put global window_animation_scale 0' >/dev/null 2>&1 || true
    sh_ 'settings put global transition_animation_scale 0' >/dev/null 2>&1 || true
    sh_ 'settings put global animator_duration_scale 0' >/dev/null 2>&1 || true
    ensure_awake || true
    sh_ 'input keyevent KEYCODE_HOME' >/dev/null 2>&1 || true
}

select_tests() {
    SELECTED=()
    if [ -n "$WANTED" ]; then
        local want row
        IFS=',' read -r -a want <<< "$WANTED"
        for row in "${TESTS[@]}"; do
            for name in "${want[@]}"; do
                [ "${row%%|*}" = "$name" ] && SELECTED+=("$row")
            done
        done
    else
        SELECTED=("${TESTS[@]}")
    fi
    [ ${#SELECTED[@]} -gt 0 ] || { echo "no matching UiBench tests" >&2; exit 1; }
}

wait_device || { echo "device $SERIAL not reachable" >&2; exit 1; }
"${A[@]}" root >/dev/null 2>&1 || true
sleep 2
wait_device || { echo "device $SERIAL not reachable after adb root" >&2; exit 1; }
sh_ 'setenforce 0' >/dev/null 2>&1 || true

if [ "$INSTALL" = 1 ]; then
    [ -f "$APP_APK" ] || { echo "UIBENCH_APK is required with --install" >&2; exit 1; }
    [ -f "$TEST_APK" ] || { echo "UIBENCH_TEST_APK is required with --install" >&2; exit 1; }
    "${A[@]}" install -r -g "$APP_APK" > "$RUNDIR/install_app.txt" 2>&1
    "${A[@]}" install -r -g "$TEST_APK" > "$RUNDIR/install_test.txt" 2>&1
fi

kill_stale || { echo "could not return sched_ext to disabled" >&2; exit 1; }
prepare_device
start_scheduler

STATE_BEFORE=$(scx_state)
OPS_BEFORE=$(scx_ops)
SELINUX=$(selinux_state)
KERNEL=$(sh_ 'uname -r' || true)
VCPU=$(sh_ 'nproc' || true)
SDK=$(sh_ 'getprop ro.build.version.sdk' || true)
BUILD=$(sh_ 'getprop ro.build.display.id' || true)
REFRESH=$(refresh_rate)
BOOT=$(boot_id)
START=$(date -Iseconds)

select_tests
for rep in $(seq 1 "$REPEATS"); do
    ORDER=("${SELECTED[@]}")
    if [ "$SHUFFLE" = 1 ]; then
        mapfile -t ORDER < <(printf '%s\n' "${SELECTED[@]}" | shuf)
    fi
    for row in "${ORDER[@]}"; do
        short=${row%%|*}
        spec=${row#*|}
        class=${spec%%#*}
        method=${spec##*#}
        td=$RUNDIR/$short
        [ "$REPEATS" -gt 1 ] && td=$td/rep-$(printf '%02d' "$rep")
        mkdir -p "$td"

        echo "run $short $class#$method rep $rep"
        ensure_awake >/dev/null 2>&1 || true
        sh_ "am force-stop $APP_PKG" >/dev/null 2>&1 || true
        sh_ "am force-stop $TEST_PKG" >/dev/null 2>&1 || true
        sh_ "dumpsys gfxinfo $APP_PKG reset" > "$td/gfxinfo_reset.txt" 2>&1 || true
        sh_ 'logcat -c' >/dev/null 2>&1 || true

        sys_before=$(systemui_pid)
        t_start=$(date +%s)
        set +e
        "${A[@]}" shell "am instrument -w -r -e class '$class#$method' -e iterations $ITERATIONS '$RUNNER'" > "$td/instrument.txt" 2>&1
        rc=$?
        set -e
        echo "$rc" > "$td/exit_code.txt"

        sh_ "dumpsys gfxinfo $APP_PKG" > "$td/gfxinfo_summary.txt" 2>&1 || true
        sh_ "dumpsys gfxinfo $APP_PKG framestats" > "$td/gfxinfo_framestats.txt" 2>&1 || true
        sh_ 'logcat -d -v threadtime' > "$td/logcat.txt" 2>&1 || true
        sys_after=$(systemui_pid)
        top_after=$(top_activity)
        t_end=$(date +%s)

        export POLICY short rep t_start t_end sys_before sys_after top_after
        export STATE_BEFORE OPS_BEFORE EXPECTED_STATE EXPECTED_OPS SELINUX BOOT REFRESH
        python3 - "$td/env.json" <<'PY'
import json
import os
import sys

env = os.environ
data = {
    "test": env["short"],
    "rep": int(env["rep"]),
    "policy": env["POLICY"],
    "start_epoch": int(env["t_start"]),
    "end_epoch": int(env["t_end"]),
    "duration_s": int(env["t_end"]) - int(env["t_start"]),
    "systemui_pid_before": env["sys_before"],
    "systemui_pid_after": env["sys_after"],
    "top_activity_after": env["top_after"],
    "boot_id": env["BOOT"],
    "selinux": env["SELINUX"],
    "scx_state_before": env["STATE_BEFORE"],
    "scx_ops_before": env["OPS_BEFORE"],
    "scx_state_after": env["STATE_BEFORE"],
    "scx_ops_after": env["OPS_BEFORE"],
    "expected_state": env["EXPECTED_STATE"],
    "expected_ops": env["EXPECTED_OPS"],
    "refresh_hz": float(env["REFRESH"]),
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
    done
done

END=$(date -Iseconds)
STATE_AFTER=$(scx_state)
OPS_AFTER=$(scx_ops)
stop_scheduler

APP_SHA=none
TEST_SHA=none
[ -f "$APP_APK" ] && APP_SHA=$(sha256sum "$APP_APK" | awk '{print $1}')
[ -f "$TEST_APK" ] && TEST_SHA=$(sha256sum "$TEST_APK" | awk '{print $1}')

export SERIAL KERNEL VCPU SDK BUILD POLICY RUN REPEATS ITERATIONS TAG START END
export STATE_BEFORE OPS_BEFORE STATE_AFTER OPS_AFTER EXPECTED_STATE EXPECTED_OPS
export REFRESH SELINUX APP_SHA TEST_SHA APP_APK TEST_APK
python3 - "$RUNDIR/manifest.json" <<'PY'
import json
import os
import sys

env = os.environ
manifest = {
    "date": env["START"][:10],
    "platform": f"android-{env['VCPU']}vcpu",
    "serial": env["SERIAL"],
    "kernel": env["KERNEL"],
    "vcpu": int(env["VCPU"]) if env["VCPU"].isdigit() else env["VCPU"],
    "sdk": env["SDK"],
    "build": env["BUILD"],
    "refresh_hz": float(env["REFRESH"]),
    "policy": env["POLICY"],
    "expected_state": env["EXPECTED_STATE"],
    "expected_ops": env["EXPECTED_OPS"],
    "tag": env["TAG"],
    "workload": "uibench",
    "run": int(env["RUN"]),
    "repeats": int(env["REPEATS"]),
    "iterations": int(env["ITERATIONS"]),
    "app_package": "com.android.test.uibench",
    "test_package": "com.android.uibench.janktests",
    "app_apk": os.path.basename(env.get("APP_APK") or ""),
    "app_sha256": env["APP_SHA"],
    "test_apk": os.path.basename(env.get("TEST_APK") or ""),
    "test_sha256": env["TEST_SHA"],
    "selinux": env["SELINUX"],
    "scx_state_before": env["STATE_BEFORE"],
    "scx_ops_before": env["OPS_BEFORE"],
    "scx_state_after": env["STATE_AFTER"],
    "scx_ops_after": env["OPS_AFTER"],
    "require_env": True,
    "start": env["START"],
    "end": env["END"],
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2)
    f.write("\n")
PY

python3 "$(dirname "$0")/extract_uibench.py" "$RUNDIR" | tee "$RUNDIR/summary.txt"
