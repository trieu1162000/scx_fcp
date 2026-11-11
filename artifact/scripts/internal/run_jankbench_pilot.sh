#!/usr/bin/env bash
# Run one JankBench List View Fling pilot sample under one scheduling policy.
#
# Usage: [REBOOT=1] run_jankbench_pilot.sh <policy> <run-number>
# Policies: eevdf, scx_lavd, scx_p2dq_pelt_off, scx_fcp_zh, scx_bpfland
#
# REBOOT=1 (default) reboots before the sample. REBOOT=0 reuses the current
# boot and is only valid when sched_ext is already disabled and SELinux is
# Permissive.
set -uo pipefail

POLICY=${1:?usage: run_jankbench_pilot.sh <policy> <run-number>}
RUN=${2:?usage: run_jankbench_pilot.sh <policy> <run-number>}
REBOOT=${REBOOT:-1}
BPF_STATS=${BPF_STATS:-0}
RUN_COUNT=${RUN_COUNT:-3}
WARMUP_ITERATIONS=${WARMUP_ITERATIONS:-0}
SERIAL=${SERIAL:-0.0.0.0:6520}
ADB=${ADB:-/home/ngerr/bin/adb}
A=("$ADB" -s "$SERIAL")
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
RESULTS_ROOT=${RESULTS_ROOT:-$ROOT/results/jankbench}
OUT="$RESULTS_ROOT/$POLICY/run-$RUN"
# Android SQLiteOpenHelper creates the file without an extension.
DB=/data/data/com.android.benchmark/databases/BenchmarkResults
REMOTE=/data/local/tmp/scx
BM_PKG=com.android.benchmark
BM_ACT=$BM_PKG/.app.RunLocalBenchmarksActivity
BM_ID=${BM_ID:-${BENCH_ID:-0}}
BENCH_CFG=$ROOT/scripts/internal/jankbench_benchmarks.json

# Resolve the benchmark id against the shared catalogue so the harness and the
# validator cannot disagree about the name, activity, or frame floors.
if [ -f "$BENCH_CFG" ]; then
  BENCH_FIELDS=$(python3 - "$BENCH_CFG" "$BM_ID" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
spec = (cfg.get("benchmarks") or {}).get(str(sys.argv[2]))
if spec is None:
    raise SystemExit(f"benchmark id {sys.argv[2]} not in {sys.argv[1]}")
print(spec.get("name", ""), spec.get("slug", ""), spec.get("activity", ""), sep="|")
PY
) || { echo "unknown BENCH_ID=$BM_ID" >&2; exit 2; }
  IFS='|' read -r BENCH_NAME BENCH_SLUG BENCH_ACTIVITY <<< "$BENCH_FIELDS"
else
  BENCH_NAME=${BENCH_NAME:-}
  BENCH_SLUG=${BENCH_SLUG:-id$BM_ID}
  BENCH_ACTIVITY=${BENCH_ACTIVITY:-}
fi
BENCH_NAME=${BENCH_NAME:-unknown-$BM_ID}
BENCH_SLUG=${BENCH_SLUG:-id$BM_ID}

case "$POLICY" in
  eevdf)
    BIN="" ; ARGS="" ; OPS="" ;;
  scx_lavd)
    BIN=scx_lavd ; ARGS="" ; OPS=lavd ;;
  scx_p2dq_pelt_off)
    BIN=scx_p2dq ; ARGS="--enable-pelt false" ; OPS=p2dq ;;
  scx_fcp_zh)
    BIN=scx_fcp ; ARGS="" ; OPS=fcp_zh ;;
  scx_fcp_no_token)
    BIN=scx_fcp_notoken ; ARGS="" ; OPS=fcp_zh ;;
  scx_fcp_named)
    BIN=scx_fcp_named ; ARGS="" ; OPS=fcp_nh ;;
  scx_fcp_v_stable_root)
    BIN=scx_fcp_v_stable_root ; ARGS="" ; OPS=fcp_zh ;;
  scx_fcp_v_a_depth3_expiry75)
    BIN=scx_fcp_v_a_depth3_expiry75 ; ARGS="" ; OPS=fcp_zh ;;
  scx_fcp_v_a_depth3_tgid1ms)
    BIN=scx_fcp_v_a_depth3_tgid1ms ; ARGS="" ; OPS=fcp_zh ;;
  scx_fcp_v_bounded_fanout)
    BIN=scx_fcp_v_bounded_fanout ; ARGS="" ; OPS=fcp_zh ;;
  scx_fcp_v_tail_guard)
    BIN=scx_fcp_v_tail_guard ; ARGS="" ; OPS=fcp_zh ;;
  scx_fcp_v_jank_guard)
    BIN=scx_fcp_v_jank_guard ; ARGS="" ; OPS=fcp_zh ;;
  scx_fcp_nh)
    BIN=scx_fcp_named ; ARGS="" ; OPS=fcp_nh ;;
  scx_bpfland)
    BIN=scx_bpfland ; ARGS="" ; OPS=bpfland ;;
  *)
    echo "unknown policy: $POLICY" >&2
    exit 2 ;;
esac

mkdir -p "$RESULTS_ROOT"
exec 9>"$RESULTS_ROOT/.lock"
flock -w 3600 9 || { echo "another run holds the lock" >&2; exit 1; }

rm -rf "$OUT"
mkdir -p "$OUT"
LOG="$OUT/harness.log"
: > "$LOG"

log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG"; }
die() { bpf_stats_set 0; log "FATAL $*"; exit 1; }

sh_() { "${A[@]}" shell "$@" 2>>"$LOG"; }

bpf_stats_set() {
  sh_ "echo $1 > /proc/sys/kernel/bpf_stats_enabled" >/dev/null 2>&1 || true
}

bpf_snapshot() {
  "${A[@]}" shell 'bpftool prog show --json' > "$1" 2>>"$LOG" || true
}

state() {
  sh_ 'cat /sys/kernel/sched_ext/state 2>/dev/null' | tr -d '\r' || true
}
ops() {
  sh_ 'cat /sys/kernel/sched_ext/root/ops 2>/dev/null' | tr -d '\r' || true
}

kill_stale_schedulers() {
  local p pid
  for p in scx_fcp scx_fcp_named scx_fcp_v_stable_root \
           scx_fcp_v_a_depth3_expiry75 scx_fcp_v_a_depth3_tgid1ms \
           scx_lavd scx_p2dq; do
    pid=$(sh_ "pidof $p" | tr -d '\r')
    if [ -n "$pid" ]; then      log "cleaning stale $p pid $pid"
      sh_ "kill -TERM $pid" >/dev/null 2>&1 || true
    fi
  done
  local deadline=$((SECONDS + 20))
  while [ $SECONDS -lt $deadline ]; do
    [ "$(state)" = "disabled" ] && return 0
    sleep 1
  done
  return 1
}

wait_device() {
  local deadline=$((SECONDS + 240))
  while [ $SECONDS -lt $deadline ]; do
    "$ADB" connect "$SERIAL" >/dev/null 2>&1 || true
    if [ "$("${A[@]}" get-state 2>/dev/null | tr -d '\r')" = "device" ]; then
      return 0
    fi
    sleep 2
  done
  return 1
}

# The image occasionally reaches sys.boot_completed=1 with a dead activity
# service: adb works and the shell answers, but every am/dumpsys call fails
# with "Can't find service: activity" and the benchmark cannot be launched.
# Require the three services the measurement depends on before proceeding and
# restart zygote once if the framework does not come back on its own.
services_ready() {
  local activity ss sysui
  activity=$(sh_ 'service check activity' | tr -d '\r')
  ss=$(sh_ 'pidof system_server' | tr -d '\r')
  sysui=$(sh_ 'pidof com.android.systemui' | tr -d '\r')
  printf '%s' "$activity" | grep -q 'found' && [ -n "$ss" ] && [ -n "$sysui" ]
}

restart_zygote() {
  log "activity service unavailable; restarting zygote"
  sh_ 'setprop ctl.restart zygote' >/dev/null 2>&1 || true
  sleep 10
}

ensure_services() {
  local deadline=$((SECONDS + ${SERVICES_TIMEOUT:-300})) activity ss sysui waited=0
  while [ $SECONDS -lt $deadline ]; do
    if services_ready; then
      log "services ready: activity found system_server=$(sh_ 'pidof system_server' | tr -d '\r') systemui=$(sh_ 'pidof com.android.systemui' | tr -d '\r')"
      return 0
    fi
    activity=$(sh_ 'service check activity' | tr -d '\r')
    ss=$(sh_ 'pidof system_server' | tr -d '\r')
    sysui=$(sh_ 'pidof com.android.systemui' | tr -d '\r')
    if ! printf '%s' "$activity" | grep -q 'found'; then
      if [ $waited -ge 90 ]; then
        restart_zygote
        waited=0
        deadline=$((SECONDS + ${SERVICES_TIMEOUT:-300}))
      fi
    fi
    log "waiting for services activity=${activity:-none} system_server=${ss:-none} systemui=${sysui:-none}"
    sleep 5
    waited=$((waited + 5))
  done
  return 1
}

reboot_and_prepare() {
  log "reboot"
  "${A[@]}" reboot >/dev/null 2>&1 || true
  sleep 10
  wait_device || die "device did not return after reboot"
  "${A[@]}" root >/dev/null 2>&1 || true
  sleep 3
  wait_device || die "device did not return after adb root"
  sh_ 'setenforce 0' >/dev/null 2>&1 || true

  local deadline=$((SECONDS + 300))
  until [ "$(sh_ 'getprop sys.boot_completed' | tr -d '\r')" = "1" ]; do
    [ $SECONDS -ge $deadline ] && die "boot_completed timeout"
    sleep 3
  done

  ensure_services || die "framework services did not become ready after reboot"

  sh_ 'setenforce 0' >/dev/null 2>&1 || true
  local selinux
  selinux=$(sh_ 'getenforce' | tr -d '\r')
  [ "$selinux" = "Permissive" ] || die "SELinux is $selinux, expected Permissive"

  sh_ 'svc power stayon true' >/dev/null 2>&1 || true
  sh_ 'settings put global stay_on_while_plugged_in 7' >/dev/null 2>&1 || true
  sh_ 'settings put system screen_off_timeout 2147483647' >/dev/null 2>&1 || true
  sh_ 'settings put secure lockscreen.disabled 1' >/dev/null 2>&1 || true
  sh_ 'locksettings set-disabled true' >/dev/null 2>&1 || true
  sh_ 'dumpsys deviceidle disable' >/dev/null 2>&1 || true
}

settle_system() {
  # The first SystemUI process on this image crashes once after boot even in
  # Permissive mode. Wait for a stable SystemUI pid and the launcher before
  # starting the workload, otherwise the benchmark cannot launch its activity.
  # A no-reboot run only needs a short confirmation; a fresh boot uses the full
  # post-boot settling window.
  local min_uptime=${SETTLE_MIN_UPTIME:-240}
  local stable_need=${SETTLE_STABLE_SAMPLES:-30}
  local deadline=$((SECONDS + ${SETTLE_TIMEOUT:-600}))
  local last_pid="" stable=0 up pid top kg
  while [ $SECONDS -lt $deadline ]; do
    up=$(sh_ "cut -d. -f1 /proc/uptime" | tr -d '\r')
    pid=$(sh_ 'pidof com.android.systemui' | tr -d '\r')
    top=$(top_activity)
    kg=$(keyguard_showing)
    if [ -n "$pid" ] && [ "$pid" = "$last_pid" ]; then
      stable=$((stable + 1))
    else
      stable=0
      last_pid=$pid
    fi
    if [ -n "${up:-}" ] && [ "$up" -ge "$min_uptime" ] 2>/dev/null && [ "$stable" -ge "$stable_need" ] &&
       printf '%s' "$top" | grep -q 'launcher3' &&
       printf '%s' "$kg" | grep -q 'KeyguardShowing=false' &&
       services_ready; then
      log "system settled uptime=${up}s systemui=$pid keyguard=false stable=$stable"
      return 0
    fi
    sleep 2
  done
  die "system did not settle (uptime=${up:-?} systemui=${last_pid:-none} top=${top:-?} kg=${kg:-?})"
}

ensure_awake() {
  local deadline=$((SECONDS + 30))
  while [ $SECONDS -lt $deadline ]; do
    if sh_ 'dumpsys power' | grep -q 'mWakefulness=Awake'; then
      return 0
    fi
    sh_ 'input keyevent KEYCODE_WAKEUP' >/dev/null 2>&1 || true
    sh_ 'wm dismiss-keyguard' >/dev/null 2>&1 || true
    sleep 2
  done
  return 1
}

wait_quiet() {
  # Measurement must start with a quiet host and guest. The image boot
  # transient can keep both busy for minutes after the UI first settles.
  local deadline=$((SECONDS + 900)) hl gl
  while [ $SECONDS -lt $deadline ]; do
    hl=$(awk '{print $1}' /proc/loadavg)
    gl=$(sh_ 'cut -d" " -f1 /proc/loadavg')
    if awk -v h="$hl" -v g="$gl" 'BEGIN{exit !(h<=3.0 && g<=2.0)}'; then
      log "load quiet host=$hl guest=$gl"
      return 0
    fi
    log "waiting for quiet host=$hl guest=$gl"
    sleep 15
  done
  log "WARNING load did not settle host=${hl:-?} guest=${gl:-?}"
  return 1
}

keyguard_showing() {
  sh_ 'dumpsys activity activities' | tr -d '\r' |
    grep -m1 'KeyguardShowing=' || true
}

# Dismiss the lock screen and wait until the launcher is the stable top
# activity. Waiting for keyguard=false alone is not enough: the unlock
# animation can time out several seconds later and launch home underneath a
# running benchmark, which makes JankBench drop its injected events.
ensure_unlocked() {
  local deadline=$((SECONDS + 180)) kg
  while [ $SECONDS -lt $deadline ]; do
    kg=$(keyguard_showing)
    if printf '%s' "$kg" | grep -q 'KeyguardShowing=false'; then
      break
    fi
    sh_ 'input keyevent KEYCODE_WAKEUP' >/dev/null 2>&1 || true
    sh_ 'wm dismiss-keyguard' >/dev/null 2>&1 || true
    sh_ 'input keyevent 82' >/dev/null 2>&1 || true
    sleep 2
  done
  kg=$(keyguard_showing)
  printf '%s' "$kg" | grep -q 'KeyguardShowing=false' ||
    die "keyguard still showing after dismiss attempts: ${kg:-none}"

  sh_ 'input keyevent KEYCODE_HOME' >/dev/null 2>&1 || true
  local stable=0 top
  deadline=$((SECONDS + 90))
  while [ $SECONDS -lt $deadline ]; do
    top=$(top_activity)
    if printf '%s' "$top" | grep -q 'launcher3'; then
      stable=$((stable + 1))
    else
      stable=0
    fi
    if [ "$stable" -ge 6 ]; then
      log "unlocked to launcher, keyguard=false"
      return 0
    fi
    sleep 2
  done
  die "launcher did not remain stable after unlock (top=${top:-none})"
}

kill_all_scx() {
  local b pid pids ps_out
  for b in scx_fcp scx_fcp_named scx_fcp_v_a_depth3_tgid1ms scx_lavd scx_p2dq; do
    pid=$(sh_ "pidof $b" | tr -d '\r')
    if [ -n "$pid" ]; then
      log "cleanup $b pid $pid"
      sh_ "kill -15 $pid" >/dev/null 2>&1 || true
    fi
  done
  ps_out=$(sh_ 'ps -A -o PID,ARGS' | tr -d '\r')
  pids=$(printf '%s\n' "$ps_out" | awk '/scx_fcp|scx_lavd|scx_p2dq/ {print $1}')
  for pid in $pids; do
    log "cleanup remote scx pid $pid"
    sh_ "kill -15 $pid" >/dev/null 2>&1 || true
  done
  sleep 2
  ps_out=$(sh_ 'ps -A -o PID,ARGS' | tr -d '\r')
  pids=$(printf '%s\n' "$ps_out" | awk '/scx_fcp|scx_lavd|scx_p2dq/ {print $1}')
  for pid in $pids; do
    log "force cleanup remote scx pid $pid"
    sh_ "kill -9 $pid" >/dev/null 2>&1 || true
  done
  sleep 2
}

launch_scheduler() {
  [ -n "$BIN" ] || return 0
  local st
  st=$(state)
  if [ "$st" != "disabled" ]; then
    kill_all_scx
    st=$(state)
  fi
  [ "$st" = "disabled" ] || die "sched_ext state is $st before launch"
  local logf="$REMOTE/${POLICY}_run${RUN}.log"
  timeout 15 "${A[@]}" shell "rm -rf $REMOTE/stats; mkdir -p $REMOTE/stats; cd $REMOTE && rm -f $logf && ulimit -l unlimited && SCX_STATS_BASE_PATH=$REMOTE/stats nohup ./$BIN $ARGS > $logf 2>&1 < /dev/null &" >>"$LOG" 2>&1 || true

  local deadline=$((SECONDS + 180))
  local i=0
  while [ $SECONDS -lt $deadline ]; do
    i=$((i + 1))
    st=$(state)
    local o
    o=$(ops)
    if [ "$st" = "enabled" ] && printf '%s' "$o" | grep -q "$OPS"; then
      log "scheduler enabled ops=$o"
      sh_ "cat $logf" > "$OUT/scheduler.log" 2>/dev/null || true
      return 0
    fi
    if [ $((i % 10)) -eq 0 ]; then
      log "waiting for scheduler state=$st ops=$o"
    fi
    sleep 2
  done
  sh_ "cat $logf" > "$OUT/scheduler.log" 2>/dev/null || true
  die "scheduler did not reach enabled with ops containing $OPS (state=$st ops=$(ops))"
}

stop_scheduler() {
  [ -n "$BIN" ] || return 0
  kill_all_scx
  local deadline=$((SECONDS + 20))
  while [ $SECONDS -lt $deadline ]; do
    [ "$(state)" = "disabled" ] && return 0
    sleep 1
  done
  log "WARNING sched_ext state after stop: $(state) ops=$(ops)"
}

check_inject_permission() {
  local granted
  granted=$(sh_ "dumpsys package $BM_PKG" | tr -d '\r' | grep 'android.permission.INJECT_EVENTS: granted=' | head -1)
  case "$granted" in
    *granted=true*) return 0 ;;
  esac
  log "INJECT_EVENTS missing ($granted), reinstalling platform APK"
  "${A[@]}" install -r -g "$ROOT/workloads/JankBench-platform-signed.apk" >>"$LOG" 2>&1 || true
  sh_ "pm grant $BM_PKG android.permission.INJECT_EVENTS" >/dev/null 2>&1 || true
  granted=$(sh_ "dumpsys package $BM_PKG" | tr -d '\r' | grep 'android.permission.INJECT_EVENTS: granted=' | head -1)
  case "$granted" in
    *granted=true*) return 0 ;;
  esac
  die "INJECT_EVENTS not granted: $granted"
}

prepare_app() {
  sh_ "am force-stop $BM_PKG" >/dev/null 2>&1 || true
  sh_ "pm clear $BM_PKG" >>"$LOG" 2>&1 || true
  check_inject_permission
  sh_ 'settings put global stay_on_while_plugged_in 7' >/dev/null 2>&1 || true
  sh_ 'settings put system screen_off_timeout 2147483647' >/dev/null 2>&1 || true
  # Do not inject events into a dead activity service: resolve the runner
  # activity first and fail loudly instead of timing out in wait_for_results.
  local resolved
  resolved=$(sh_ "cmd package resolve-activity --brief -n $BM_ACT" | tr -d '\r' | tail -n1)
  case "$resolved" in
    *RunLocalBenchmarksActivity*) log "benchmark activity resolves: $resolved" ;;
    *) die "benchmark activity does not resolve: ${resolved:-none}" ;;
  esac
}

wait_for_results() {
  local deadline=$((SECONDS + 420))
  local last_count=-1 stable=0 got_last=0 outside=0
  BM_PID_FIRST=""
  while [ $SECONDS -lt $deadline ]; do
    local cur_sysui cur_bm
    cur_sysui=$(sh_ 'pidof com.android.systemui' | tr -d '\r')
    if [ -n "$cur_sysui" ] && [ -n "${SYSTEMUI_BEFORE:-}" ] &&
       [ "$cur_sysui" != "$SYSTEMUI_BEFORE" ]; then
      log "SystemUI restarted during run ($SYSTEMUI_BEFORE -> $cur_sysui)"
      break
    fi
    cur_bm=$(sh_ 'pidof com.android.benchmark' | tr -d '\r')
    if [ -z "$BM_PID_FIRST" ] && [ -n "$cur_bm" ]; then
      BM_PID_FIRST=$cur_bm
    elif [ -n "$cur_bm" ] && [ -n "$BM_PID_FIRST" ] && [ "$cur_bm" != "$BM_PID_FIRST" ]; then
      log "benchmark process restarted during run ($BM_PID_FIRST -> $cur_bm)"
      break
    fi
    if ! sh_ 'dumpsys power' | grep -q 'mWakefulness=Awake'; then
      SLEEP_EVENTS=$((SLEEP_EVENTS + 1))
      log "WARNING device asleep during run (event $SLEEP_EVENTS), waking"
      sh_ 'input keyevent KEYCODE_WAKEUP' >/dev/null 2>&1 || true
      sh_ 'wm dismiss-keyguard' >/dev/null 2>&1 || true
    fi
    if [ "$got_last" = 0 ]; then
      local cur_top
      cur_top=$(top_activity)
      if printf '%s' "$cur_top" | grep -q 'com.android.benchmark'; then
        outside=0
      else
        outside=$((outside + 1))
        if [ "$outside" -ge 5 ]; then
          log "benchmark left foreground before completing: ${cur_top:-none}"
          break
        fi
      fi
    fi
    local probe maxit count
    probe=$(sh_ "[ -f $DB ] && sqlite3 $DB 'select max(iteration), count(*) from ui_results;' 2>/dev/null" | tr -d '\r' | tail -n1)
    maxit=""; count=""
    if [ -n "$probe" ]; then
      maxit=${probe%%|*}
      count=${probe##*|}
    fi
    if [ -n "${maxit:-}" ] && [ "$maxit" -ge $((RUN_COUNT - 1)) ] 2>/dev/null; then
      if [ "$got_last" = 0 ]; then
        log "last iteration present, rows=$count"
        got_last=1
      fi
      if [ "$count" = "$last_count" ]; then
        stable=$((stable + 1))
      else
        stable=0
        last_count=$count
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

top_activity() {
  sh_ 'dumpsys activity activities | grep -m1 topResumedActivity' | tr -d '\r'
}

# ---------------------------------------------------------------------------

log "start policy=$POLICY run=$RUN reboot=$REBOOT bench_id=$BM_ID name=$BENCH_NAME out=$OUT"
if [ "$REBOOT" = "1" ]; then
  reboot_and_prepare
else
  # A reused boot is already past the crash-storm window; only require a short
  # stable confirmation, but still refuse to measure with a dead framework.
  SETTLE_MIN_UPTIME=${SETTLE_MIN_UPTIME:-0}
  SETTLE_STABLE_SAMPLES=${SETTLE_STABLE_SAMPLES:-5}
  [ "$(sh_ 'getenforce' | tr -d '\r')" = "Permissive" ] || sh_ 'setenforce 0' >/dev/null 2>&1 || true
  sh_ 'svc power stayon true' >/dev/null 2>&1 || true
  sh_ 'settings put global stay_on_while_plugged_in 7' >/dev/null 2>&1 || true
  sh_ 'settings put system screen_off_timeout 2147483647' >/dev/null 2>&1 || true
  sh_ 'settings put secure lockscreen.disabled 1' >/dev/null 2>&1 || true
  sh_ 'locksettings set-disabled true' >/dev/null 2>&1 || true
  ensure_services || die "framework services unavailable on the reused boot"
fi
ensure_awake || die "device did not stay awake before unlock"
ensure_services || die "framework services unavailable before unlock"
ensure_unlocked
settle_system
ensure_awake || die "device did not stay awake before scheduler start"
wait_quiet || true
kill_stale_schedulers || die "could not return sched_ext to disabled"
log "boot ready kernel=$(sh_ 'uname -r' | tr -d '\r') nproc=$(sh_ 'nproc' | tr -d '\r') selinux=$(sh_ 'getenforce' | tr -d '\r') uptime=$(sh_ 'cat /proc/uptime' | tr -d '\r')"

STATE_BEFORE=$(state)
OPS_BEFORE=$(ops)
if [ "$POLICY" = eevdf ]; then
  [ "$STATE_BEFORE" = "disabled" ] || die "EEVDF requested but sched_ext state=$STATE_BEFORE ops=$OPS_BEFORE"
else
  launch_scheduler
fi
STATE_BEFORE=$(state)
OPS_BEFORE=$(ops)
printf '%s\n' "$STATE_BEFORE" > "$OUT/state_before.txt"
printf '%s\n' "$OPS_BEFORE" >> "$OUT/state_before.txt"

prepare_app
ensure_awake || die "device did not stay awake before benchmark"
sh_ 'logcat -c' >/dev/null 2>&1 || true
SLEEP_EVENTS=0
UPTIME_START=$(sh_ 'cat /proc/uptime' | tr -d '\r')
BOOT_ID=$(sh_ 'cat /proc/sys/kernel/random/boot_id' | tr -d '\r')
SYSTEM_SERVER_BEFORE=$(sh_ 'pidof system_server' | tr -d '\r')
HOST_START=$(date -Iseconds)
HOST_LOAD_START=$(cat /proc/loadavg)
GUEST_LOAD_START=$(sh_ 'cat /proc/loadavg' | tr -d '\r')
printf '%s\n' "$HOST_LOAD_START" > "$OUT/host_load_start.txt"
printf '%s\n' "$GUEST_LOAD_START" > "$OUT/guest_load_start.txt"

BM_ID=${BM_ID:-0}
BM_CMD="am start -W -n $BM_ACT --eia com.android.benchmark.EXTRA_ENABLED_BENCHMARK_IDS $BM_ID --ei com.android.benchmark.EXTRA_RUN_COUNT $RUN_COUNT --ez com.android.benchmark.FINISH_WHEN_DONE true"
log "benchmark id=$BM_ID name=$BENCH_NAME slug=$BENCH_SLUG"
if [ "$BPF_STATS" = 1 ]; then
  bpf_stats_set 1
  printf '1\n' > "$OUT/bpf_stats_enabled_before.txt"
  bpf_snapshot "$OUT/bpf_before.json"
  log "bpf_stats enabled before benchmark"
else
  bpf_stats_set 0
  printf '0\n' > "$OUT/bpf_stats_enabled_before.txt"
fi
ensure_awake || die "device did not stay awake at benchmark start"
SYSTEMUI_BEFORE=$(sh_ 'pidof com.android.systemui' | tr -d '\r')
log "benchmark: $BM_CMD"
set +e
"${A[@]}" shell "$BM_CMD" > "$OUT/am_start.txt" 2>&1
AM_RC=$?
set +e
log "am start rc=$AM_RC: $(tr '\n' ' ' < "$OUT/am_start.txt" | tail -c 300)"

if ! wait_for_results; then
  sh_ 'logcat -d -v threadtime' > "$OUT/logcat.txt" 2>&1 || true
  top_activity >> "$OUT/logcat.txt"
  stop_scheduler
  printf '%s\n%s\n' "$(state)" "$(ops)" > "$OUT/state_after.txt"
  die "no complete result database (expected $RUN_COUNT iterations)"
fi
HOST_END=$(date -Iseconds)
UPTIME_END=$(sh_ 'cat /proc/uptime' | tr -d '\r')
HOST_LOAD_END=$(cat /proc/loadavg)
GUEST_LOAD_END=$(sh_ 'cat /proc/loadavg' | tr -d '\r')
printf '%s\n' "$HOST_LOAD_END" > "$OUT/host_load_end.txt"
printf '%s\n' "$GUEST_LOAD_END" > "$OUT/guest_load_end.txt"
TOP_AT_END=$(top_activity)
log "results ready: $(cat "$OUT/db_probe.txt") top=$TOP_AT_END"

BM_PID_BEFORE=$BM_PID_FIRST
BM_PID_AFTER=$(sh_ 'pidof com.android.benchmark' | tr -d '\r')
REFRESH_HZ=$(sh_ "dumpsys display 2>/dev/null | grep -oE 'mRefreshRate=[0-9.]+|renderFrameRate [0-9.]+' | head -1" | tr -d '\r' | sed -E 's/.*[= ]//')
case "${REFRESH_HZ:-}" in ''|*[!0-9.]*) REFRESH_HZ=60 ;; esac
log "benchmark pid $BM_PID_BEFORE -> $BM_PID_AFTER refresh=${REFRESH_HZ}Hz"

sh_ 'logcat -d -v threadtime' > "$OUT/logcat.txt" 2>&1 || true
SYSTEMUI_AFTER=$(sh_ 'pidof com.android.systemui' | tr -d '\r')
sh_ 'dmesg' 2>/dev/null | grep -iE 'sched_ext|scx|lavd|p2dq|fcp' > "$OUT/dmesg_scx.txt" || true
if [ -n "$BIN" ]; then
  sh_ "cat $REMOTE/${POLICY}_run${RUN}.log" > "$OUT/scheduler.log" 2>/dev/null || true
else
  echo "EEVDF, no sched_ext scheduler attached" > "$OUT/scheduler.log"
fi

CRASH=""
if grep -q 'Targeted input event injection' "$OUT/logcat.txt"; then
  CRASH="input injection targeted a window outside com.android.benchmark"
elif grep -A8 'FATAL EXCEPTION' "$OUT/logcat.txt" | grep -q "$BM_PKG"; then
  CRASH="benchmark fatal exception in logcat"
fi
if [ -n "$CRASH" ] || [ "$SYSTEMUI_BEFORE" != "$SYSTEMUI_AFTER" ]; then
  [ -n "$CRASH" ] || CRASH="SystemUI restarted during run ($SYSTEMUI_BEFORE -> $SYSTEMUI_AFTER)"
  log "WARNING invalid run: $CRASH"
  stop_scheduler
  printf '%s\n%s\n' "$(state)" "$(ops)" > "$OUT/state_after.txt"
  die "run invalid: $CRASH"
fi

# Consistent single-file database snapshot.
sh_ "rm -rf /data/local/tmp/jb_pull; mkdir -p /data/local/tmp/jb_pull; sqlite3 $DB \".backup '/data/local/tmp/jb_pull/BenchmarkResults.db'\"" >>"$LOG" 2>&1 || true
if ! sh_ '[ -s /data/local/tmp/jb_pull/BenchmarkResults.db ]' >/dev/null 2>&1; then
  log "sqlite backup failed, falling back to file copy"
  sh_ "rm -rf /data/local/tmp/jb_pull; mkdir -p /data/local/tmp/jb_pull; cp $DB /data/local/tmp/jb_pull/BenchmarkResults.db; [ -f $DB-journal ] && cp $DB-journal /data/local/tmp/jb_pull/BenchmarkResults.db-journal; [ -f $DB-wal ] && cp $DB-wal /data/local/tmp/jb_pull/BenchmarkResults.db-wal; [ -f $DB-shm ] && cp $DB-shm /data/local/tmp/jb_pull/BenchmarkResults.db-shm" >>"$LOG" 2>&1 || true
fi
rm -rf "$OUT/db"
"${A[@]}" pull /data/local/tmp/jb_pull "$OUT/db" >>"$LOG" 2>&1 || true
if [ -f "$OUT/db/BenchmarkResults.db" ]; then
  mv "$OUT/db/BenchmarkResults.db" "$OUT/BenchmarkResults.db"
  for extra in BenchmarkResults.db-journal BenchmarkResults.db-wal BenchmarkResults.db-shm; do
    [ -f "$OUT/db/$extra" ] && mv "$OUT/db/$extra" "$OUT/$extra"
  done
  rmdir "$OUT/db" 2>/dev/null || true
fi
[ -s "$OUT/BenchmarkResults.db" ] || die "database pull failed"

sh_ 'getenforce' | tr -d '\r' > "$OUT/selinux.txt"
sh_ 'uname -r' | tr -d '\r' > "$OUT/kernel.txt"
sh_ 'nproc' | tr -d '\r' > "$OUT/nproc.txt"
sh_ 'getprop ro.build.display.id' | tr -d '\r' > "$OUT/build.txt"
sh_ 'getprop ro.build.version.sdk' | tr -d '\r' > "$OUT/sdk.txt"
APP_PATH=$(sh_ "pm path $BM_PKG" | tr -d '\r' | sed 's/^package://' | head -1)
APP_SHA=$(sh_ "sha256sum '$APP_PATH'" | tr -d '\r' | awk '{print $1}')
echo "$APP_SHA" > "$OUT/app_sha256.txt"
if [ -n "$BIN" ]; then
  sh_ "sha256sum $REMOTE/$BIN" | tr -d '\r' | awk '{print $1}' > "$OUT/scheduler_sha256.txt"
else
  echo none > "$OUT/scheduler_sha256.txt"
fi

STATE_AFTER_BENCH=$(state)
OPS_AFTER_BENCH=$(ops)
BOOT_ID_END=$(sh_ 'cat /proc/sys/kernel/random/boot_id' | tr -d '\r')
SYSTEM_SERVER_AFTER=$(sh_ 'pidof system_server' | tr -d '\r')
if [ "$BPF_STATS" = 1 ]; then
  bpf_snapshot "$OUT/bpf_after.json"
  printf '1\n' > "$OUT/bpf_stats_enabled_after.txt"
  log "bpf_stats snapshot after benchmark"
else
  printf '0\n' > "$OUT/bpf_stats_enabled_after.txt"
fi
bpf_stats_set 0
stop_scheduler
STATE_AFTER=$(state)
OPS_AFTER=$(ops)
printf '%s\n%s\n' "$STATE_AFTER" "$OPS_AFTER" > "$OUT/state_after.txt"

# Write the manifest before parsing so the parser can read it.
export POLICY RUN SERIAL HOST_START HOST_END UPTIME_START UPTIME_END \
  BOOT_ID STATE_BEFORE OPS_BEFORE STATE_AFTER_BENCH OPS_AFTER_BENCH \
  STATE_AFTER OPS_AFTER BM_CMD BM_ID AM_RC APP_SHA BIN ARGS OUT CRASH SLEEP_EVENTS \
  REBOOT TOP_AT_END SYSTEMUI_BEFORE SYSTEMUI_AFTER \
  HOST_LOAD_START HOST_LOAD_END GUEST_LOAD_START GUEST_LOAD_END BPF_STATS \
  RUN_COUNT WARMUP_ITERATIONS SYSTEM_SERVER_BEFORE SYSTEM_SERVER_AFTER \
  BOOT_ID_END BM_PID_BEFORE BM_PID_AFTER REFRESH_HZ BENCH_NAME BENCH_SLUG \
  BENCH_ACTIVITY
python3 - "$OUT/manifest.json" <<'PY'
import json
import os
import sys

env = os.environ
bin_name = env.get("BIN") or "none"
args = env.get("ARGS") or ""
if bin_name == "none":
    sched_cmd = "none (EEVDF)"
else:
    sched_cmd = f"SCX_STATS_BASE_PATH=/data/local/tmp/scx/stats ./{bin_name} {args}".strip()

m = {
    "date": env["HOST_START"][:10],
    "policy": env["POLICY"],
    "run": int(env["RUN"]),
    "serial": env["SERIAL"],
    "kernel": open(os.path.join(env["OUT"], "kernel.txt")).read().strip(),
    "nproc": int(open(os.path.join(env["OUT"], "nproc.txt")).read().strip()),
    "build": open(os.path.join(env["OUT"], "build.txt")).read().strip(),
    "sdk": int(open(os.path.join(env["OUT"], "sdk.txt")).read().strip()),
    "selinux": open(os.path.join(env["OUT"], "selinux.txt")).read().strip(),
    "boot_id": env["BOOT_ID"],
    "boot_id_end": env.get("BOOT_ID_END"),
    "system_server_pid_before": env.get("SYSTEM_SERVER_BEFORE"),
    "system_server_pid_after": env.get("SYSTEM_SERVER_AFTER"),
    "bpf_stats_enabled_before": open(os.path.join(env["OUT"], "bpf_stats_enabled_before.txt")).read().strip()
    if os.path.exists(os.path.join(env["OUT"], "bpf_stats_enabled_before.txt")) else None,
    "bpf_stats_enabled_after": open(os.path.join(env["OUT"], "bpf_stats_enabled_after.txt")).read().strip()
    if os.path.exists(os.path.join(env["OUT"], "bpf_stats_enabled_after.txt")) else None,
    "uptime_start": env["UPTIME_START"],
    "uptime_end": env["UPTIME_END"],
    "host_start": env["HOST_START"],
    "host_end": env["HOST_END"],
    "host_load_start": env.get("HOST_LOAD_START"),
    "host_load_end": env.get("HOST_LOAD_END"),
    "guest_load_start": env.get("GUEST_LOAD_START"),
    "guest_load_end": env.get("GUEST_LOAD_END"),
    "rebooted_before_run": env.get("REBOOT") == "1",
    "top_activity_at_end": env.get("TOP_AT_END"),
    "systemui_pid_before": env.get("SYSTEMUI_BEFORE"),
    "systemui_pid_after": env.get("SYSTEMUI_AFTER"),
    "benchmark_pid_before": env.get("BM_PID_BEFORE"),
    "benchmark_pid_after": env.get("BM_PID_AFTER"),
    "refresh_hz": float(env["REFRESH_HZ"]) if env.get("REFRESH_HZ") else None,
    "scheduler_binary": bin_name,
    "scheduler_args": args,
    "scheduler_command": sched_cmd,
    "scheduler_sha256": open(os.path.join(env["OUT"], "scheduler_sha256.txt")).read().strip(),
    "scx_state_before": env["STATE_BEFORE"],
    "scx_ops_before": env["OPS_BEFORE"],
    "scx_state_after_benchmark": env["STATE_AFTER_BENCH"],
    "scx_ops_after_benchmark": env["OPS_AFTER_BENCH"],
    "scx_state_after_stop": env["STATE_AFTER"],
    "scx_ops_after_stop": env["OPS_AFTER"],
    "workload": f"AOSP JankBench 2.2 benchmark id {env['BM_ID']}",
    "benchmark_id": int(env["BM_ID"]),
    "benchmark_name": env.get("BENCH_NAME"),
    "benchmark_slug": env.get("BENCH_SLUG"),
    "benchmark_activity": env.get("BENCH_ACTIVITY"),
    "benchmark_command": env["BM_CMD"],
    "benchmark_am_rc": int(env["AM_RC"]),
    "run_count": int(env.get("RUN_COUNT") or 3),
    "warmup_iterations": int(env.get("WARMUP_ITERATIONS") or 0),
    "finish_when_done": True,
    "db_name": "BenchmarkResults.db",
    "db_name_on_device": "BenchmarkResults",
    "app_package": "com.android.benchmark",
    "app_version": "2.2",
    "app_sha256": env["APP_SHA"],
    "crash_suspected": env.get("CRASH") or None,
    "sleep_events_during_run": int(env.get("SLEEP_EVENTS") or 0),
    "bpf_stats_requested": env.get("BPF_STATS") == "1",
    "notes": [
        "SELinux Permissive, image workaround for the gralloc/systemui crash",
        "JankBench APK is a locally rebuilt platform-signed artifact",
    ],
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(m, f, indent=2)
    f.write("\n")
PY

python3 "$ROOT/scripts/internal/parse_jankbench.py" "$OUT" | tee -a "$LOG" || die "parser failed"
log "done policy=$POLICY run=$RUN"
