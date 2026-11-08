#!/usr/bin/env bash
# Run one JankBench List View Fling sample under one scheduling policy.
#
# Usage:
#   tests/jankbench/run_jankbench.sh -p scx_fcp_behavioral -n 1
#   REBOOT=1 tests/jankbench/run_jankbench.sh -p eevdf -n 1
#
# Environment:
#   ADB          adb binary                  (default: adb)
#   SERIAL       device serial               (default: 127.0.0.1:6520)
#   SCX_ROOT     scx workspace               (default: $HOME/workspace/scx)
#   RESULTS_ROOT result directory            (default: <repo>/results/jankbench)
#   BIN_DIR      scheduler binary directory  (default: $SCX_ROOT/target/release)
#
# Policies: eevdf, scx_fcp, scx_fcp_named, scx_fcp_behavioral,
#           scx_lavd, scx_p2dq. A policy may also name a binary in BIN_DIR
#           whose sched_ext ops name is given by OPS.
set -uo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
ADB=${ADB:-adb}
SERIAL=${SERIAL:-127.0.0.1:6520}
SCX_ROOT=${SCX_ROOT:-$HOME/workspace/scx}
RESULTS_ROOT=${RESULTS_ROOT:-$REPO_ROOT/results/jankbench}
BIN_DIR=${BIN_DIR:-$SCX_ROOT/target/release}
REMOTE=${REMOTE:-/data/local/tmp/scx}
REBOOT=${REBOOT:-0}
RUN_COUNT=${RUN_COUNT:-3}
NO_SQLITE_WAIT=${NO_SQLITE_WAIT:-120}
APK=${JANKBENCH_APK:-}

POLICY=""
RUN=""
while [ $# -gt 0 ]; do
    case "$1" in
        -p|--policy) POLICY=${2:?missing policy}; shift 2 ;;
        -n|--run) RUN=${2:?missing run number}; shift 2 ;;
        -s|--serial) SERIAL=${2:?missing serial}; shift 2 ;;
        --reboot) REBOOT=1; shift ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "unknown argument $1" >&2; exit 2 ;;
    esac
done
[ -n "$POLICY" ] || { echo "missing -p policy" >&2; exit 2; }
[ -n "$RUN" ] || RUN=1

case "$POLICY" in
    eevdf)              BIN="" ; ARGS="" ; OPS="" ;;
    scx_fcp)            BIN=scx_fcp ; ARGS="" ; OPS=fcp ;;
    scx_fcp_named)      BIN=scx_fcp_named ; ARGS="" ; OPS=fcp_nh ;;
    scx_fcp_behavioral) BIN=scx_fcp_behavioral ; ARGS="" ; OPS=fcp_zh ;;
    scx_fcp_zero_hint)  BIN=scx_fcp_behavioral ; ARGS="" ; OPS=fcp_zh ;;
    scx_lavd)           BIN=scx_lavd ; ARGS="" ; OPS=lavd ;;
    scx_p2dq)           BIN=scx_p2dq ; ARGS="--enable-pelt false" ; OPS=p2dq ;;
    *)
        BIN=$POLICY
        ARGS=${OPS_ARGS:-}
        OPS=${OPS:-$POLICY}
        ;;
esac

A=("$ADB" -s "$SERIAL")
OUT=$RESULTS_ROOT/$POLICY/run-$RUN
LOG=$OUT/harness.log
DB_DEVICE=/data/data/com.android.benchmark/databases/BenchmarkResults
DB_TMP=/data/local/tmp/jb_pull/BenchmarkResults.db
BM_PKG=com.android.benchmark
BM_ACT=$BM_PKG/.app.RunLocalBenchmarksActivity

log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG"; }
die() { log "FATAL $*"; exit 1; }
sh_() { "${A[@]}" shell "$@" 2>>"$LOG"; }
state() { sh_ 'cat /sys/kernel/sched_ext/state 2>/dev/null' | tr -d '\r' || true; }
ops() { sh_ 'cat /sys/kernel/sched_ext/root/ops 2>/dev/null' | tr -d '\r' || true; }

wait_device() {
    local deadline=$((SECONDS + 240))
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
    for name in scx_fcp scx_fcp_named scx_fcp_behavioral scx_fcp_v_a_depth3_tgid1ms scx_lavd scx_p2dq; do
        pid=$(sh_ "pidof $name" | tr -d '\r')
        [ -n "$pid" ] && sh_ "kill -15 $pid" >/dev/null 2>&1 || true
    done
    pids=$(sh_ "ps -A -o PID,ARGS | awk '/data\\/local\\/tmp\\/scx\\/scx_/ {print \\$1}'" | tr -d '\r')
    for pid in $pids; do
        sh_ "kill -15 $pid" >/dev/null 2>&1 || true
    done
    local deadline=$((SECONDS + 20))
    while [ $SECONDS -lt $deadline ]; do
        [ "$(state)" = disabled ] && return 0
        sleep 1
    done
    return 1
}

wait_boot() {
    local deadline=$((SECONDS + 300))
    until [ "$(sh_ 'getprop sys.boot_completed' | tr -d '\r')" = 1 ]; do
        [ $SECONDS -ge $deadline ] && return 1
        sleep 3
    done
    return 0
}

prepare_device() {
    if [ "$REBOOT" = 1 ]; then
        log "rebooting $SERIAL"
        "${A[@]}" reboot >/dev/null 2>&1 || true
        sleep 10
        wait_device || die "device did not return after reboot"
    fi
    "${A[@]}" root >/dev/null 2>&1 || true
    sleep 2
    wait_device || die "device not reachable"
    wait_boot || die "boot_completed timeout"
    if ! sh_ 'setenforce 0' >/dev/null 2>&1; then
        log "warning: setenforce 0 failed"
    fi
    local selinux
    selinux=$(sh_ 'getenforce' | tr -d '\r')
    [ "$selinux" = Permissive ] || log "warning: SELinux is $selinux"
    sh_ 'svc power stayon true' >/dev/null 2>&1 || true
    sh_ 'settings put global stay_on_while_plugged_in 7' >/dev/null 2>&1 || true
    sh_ 'settings put system screen_off_timeout 2147483647' >/dev/null 2>&1 || true
    sh_ 'settings put secure lockscreen.disabled 1' >/dev/null 2>&1 || true
    sh_ 'input keyevent KEYCODE_WAKEUP' >/dev/null 2>&1 || true
    sh_ 'wm dismiss-keyguard' >/dev/null 2>&1 || true
}

push_binary() {
    [ -n "$BIN" ] || return 0
    local src=$BIN_DIR/$BIN
    [ -f "$src" ] || die "missing scheduler binary $src"
    sh_ "mkdir -p $REMOTE/stats" >/dev/null 2>&1 || true
    "${A[@]}" push "$src" "$REMOTE/$BIN" >>"$LOG" 2>&1 || die "push failed for $src"
    sh_ "chmod 755 $REMOTE/$BIN" >/dev/null 2>&1 || true
}

start_scheduler() {
    [ -n "$BIN" ] || return 0
    local st
    st=$(state)
    if [ "$st" != disabled ]; then
        kill_stale || true
        st=$(state)
    fi
    [ "$st" = disabled ] || die "sched_ext state is $st before launch"
    local remote_log=$REMOTE/${POLICY}_run${RUN}.log
    timeout 20 "${A[@]}" shell \
        "cd $REMOTE && rm -f $remote_log && ulimit -l unlimited && SCX_STATS_BASE_PATH=$REMOTE/stats nohup ./$BIN $ARGS > $remote_log 2>&1 < /dev/null &" \
        >>"$LOG" 2>&1 || true
    local deadline=$((SECONDS + 180)) i=0
    while [ $SECONDS -lt $deadline ]; do
        i=$((i + 1))
        st=$(state)
        local o
        o=$(ops)
        if [ "$st" = enabled ] && printf '%s' "$o" | grep -q "$OPS"; then
            sh_ "cat $remote_log" > "$OUT/scheduler.log" 2>/dev/null || true
            log "scheduler enabled ops=$o"
            return 0
        fi
        [ $((i % 10)) -eq 0 ] && log "waiting for scheduler state=$st ops=$o"
        sleep 2
    done
    sh_ "cat $remote_log" > "$OUT/scheduler.log" 2>/dev/null || true
    die "scheduler did not reach enabled with ops $OPS"
}

stop_scheduler() {
    [ -n "$BIN" ] || return 0
    kill_stale || true
    [ "$(state)" = disabled ] && return 0
    log "warning: sched_ext state after stop is $(state)"
}

wait_results() {
    if [ "$HAVE_SQLITE" != yes ]; then
        log "device sqlite3 unavailable, waiting ${NO_SQLITE_WAIT}s for the benchmark"
        sleep "$NO_SQLITE_WAIT"
        echo "unknown|unknown" > "$OUT/db_probe.txt"
        return 0
    fi
    local deadline=$((SECONDS + 420)) last=-1 stable=0 probe maxit count
    while [ $SECONDS -lt $deadline ]; do
        probe=$(sh_ "[ -f $DB_DEVICE ] && sqlite3 $DB_DEVICE 'select max(iteration), count(*) from ui_results;' 2>/dev/null" | tr -d '\r' | tail -n1)
        maxit=${probe%%|*}
        count=${probe##*|}
        if [ -n "${maxit:-}" ] && [ "$maxit" -ge $((RUN_COUNT - 1)) ] 2>/dev/null; then
            if [ "$count" = "$last" ]; then
                stable=$((stable + 1))
            else
                stable=0
                last=$count
            fi
            if [ "$stable" -ge 2 ]; then
                echo "$maxit|$count" > "$OUT/db_probe.txt"
                return 0
            fi
        fi
        sleep 3
    done
    echo "${maxit:-none}|${count:-none}" > "$OUT/db_probe.txt"
    return 1
}

pull_db() {
    sh_ "rm -rf /data/local/tmp/jb_pull; mkdir -p /data/local/tmp/jb_pull" >/dev/null 2>&1 || true
    sh_ "sqlite3 $DB_DEVICE \".backup '$DB_TMP'\"" >>"$LOG" 2>&1 || true
    if ! sh_ "[ -s $DB_TMP ]" >/dev/null 2>&1; then
        log "sqlite backup unavailable, copying database files"
        sh_ "cp $DB_DEVICE $DB_TMP; [ -f $DB_DEVICE-wal ] && cp $DB_DEVICE-wal $DB_TMP-wal; [ -f $DB_DEVICE-shm ] && cp $DB_DEVICE-shm $DB_TMP-shm" >>"$LOG" 2>&1 || true
    fi
    rm -rf "$OUT/db"
    "${A[@]}" pull /data/local/tmp/jb_pull "$OUT/db" >>"$LOG" 2>&1 || true
    if [ -f "$OUT/db/BenchmarkResults.db" ]; then
        mv "$OUT/db/BenchmarkResults.db" "$OUT/BenchmarkResults.db"
        for extra in BenchmarkResults.db-wal BenchmarkResults.db-shm; do
            [ -f "$OUT/db/$extra" ] && mv "$OUT/db/$extra" "$OUT/$extra"
        done
        rmdir "$OUT/db" 2>/dev/null || true
    fi
    [ -s "$OUT/BenchmarkResults.db" ] || die "database pull failed"
}

mkdir -p "$OUT"
: > "$LOG"
log "start policy=$POLICY run=$RUN reboot=$REBOOT"
wait_device || die "device $SERIAL not reachable"
HAVE_SQLITE=$(sh_ 'command -v sqlite3 >/dev/null 2>&1 && echo yes || echo no')

if [ -n "$APK" ]; then
    [ -f "$APK" ] || die "missing APK $APK"
    "${A[@]}" install -r -g "$APK" >>"$LOG" 2>&1 || die "APK install failed"
fi

prepare_device
kill_stale || die "could not return sched_ext to disabled"
push_binary
start_scheduler

STATE_BEFORE=$(state)
OPS_BEFORE=$(ops)
printf '%s\n%s\n' "$STATE_BEFORE" "$OPS_BEFORE" > "$OUT/state_before.txt"

sh_ "am force-stop $BM_PKG" >/dev/null 2>&1 || true
sh_ "pm clear $BM_PKG" >>"$LOG" 2>&1 || true
sh_ 'logcat -c' >/dev/null 2>&1 || true
sh_ 'input keyevent KEYCODE_WAKEUP' >/dev/null 2>&1 || true
sh_ 'wm dismiss-keyguard' >/dev/null 2>&1 || true

BM_ID=${BM_ID:-0}
BM_CMD="am start -W -n $BM_ACT --eia com.android.benchmark.EXTRA_ENABLED_BENCHMARK_IDS $BM_ID --ei com.android.benchmark.EXTRA_RUN_COUNT $RUN_COUNT --ez com.android.benchmark.FINISH_WHEN_DONE true"
START=$(date -Iseconds)
log "benchmark: $BM_CMD"
"${A[@]}" shell "$BM_CMD" > "$OUT/am_start.txt" 2>&1

if ! wait_results; then
    sh_ 'logcat -d -v threadtime' > "$OUT/logcat.txt" 2>&1 || true
    stop_scheduler
    die "no complete JankBench result database"
fi
END=$(date -Iseconds)

sh_ 'logcat -d -v threadtime' > "$OUT/logcat.txt" 2>&1 || true
pull_db
stop_scheduler

sh_ 'getenforce' | tr -d '\r' > "$OUT/selinux.txt"
sh_ 'uname -r' | tr -d '\r' > "$OUT/kernel.txt"
sh_ 'nproc' | tr -d '\r' > "$OUT/nproc.txt"
sh_ 'getprop ro.build.display.id' | tr -d '\r' > "$OUT/build.txt"
sh_ 'getprop ro.build.version.sdk' | tr -d '\r' > "$OUT/sdk.txt"
printf '%s\n' "$(state)" "$(ops)" > "$OUT/state_after.txt"

SCHED_SHA=none
if [ -n "$BIN" ]; then
    SCHED_SHA=$(sh_ "sha256sum $REMOTE/$BIN" | tr -d '\r' | awk '{print $1}')
fi

export POLICY RUN SERIAL START END BM_CMD SCHED_SHA BIN ARGS REBOOT RUN_COUNT
python3 - "$OUT/manifest.json" <<'PY'
import json
import os
import sys

env = os.environ
out = os.path.dirname(os.path.abspath(sys.argv[1]))

def read(name):
    with open(os.path.join(out, name), encoding="utf-8") as f:
        return f.read().strip()

manifest = {
    "date": env["START"][:10],
    "policy": env["POLICY"],
    "run": int(env["RUN"]),
    "serial": env["SERIAL"],
    "kernel": read("kernel.txt"),
    "nproc": int(read("nproc.txt")),
    "build": read("build.txt"),
    "sdk": int(read("sdk.txt")),
    "selinux": read("selinux.txt"),
    "rebooted_before_run": env.get("REBOOT") == "1",
    "scheduler_binary": env.get("BIN") or "none",
    "scheduler_args": env.get("ARGS") or "",
    "scheduler_sha256": env.get("SCHED_SHA") or "none",
    "scx_state_before": read("state_before.txt").splitlines()[0],
    "scx_ops_before": read("state_before.txt").splitlines()[1],
    "workload": "AOSP JankBench 2.2 List View Fling (benchmark id 0)",
    "benchmark_command": env["BM_CMD"],
    "run_count": int(env["RUN_COUNT"]),
    "app_package": "com.android.benchmark",
    "db_name": "BenchmarkResults.db",
    "start": env["START"],
    "end": env["END"],
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2)
    f.write("\n")
PY

log "done policy=$POLICY run=$RUN out=$OUT"
