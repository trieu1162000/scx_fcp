#!/usr/bin/env bash
# Controlled UiBench matrix driver.
#
# One policy block is: reboot, setenforce 0, wait for a stable UI, attach the
# policy, one ignored warm-up run, then the measured repeats.  Policy blocks
# are shuffled independently in each round, so host drift is not confounded
# with policy.  Every raw run stays on disk; validity is decided by
# extract_uibench.py and reported, never by deleting a run.
#
# Usage:
#   run_uibench_matrix.sh --out results/uibench_stable/16vcpu --rounds 2 --reps 5 \
#     --policies eevdf,scx_fcp_behavioral,scx_p2dq_pelt_off,scx_lavd \
#     --tests inflating-list,bitmap-upload,shadow-grid-list
#
# The default is the pre-registered 16-vCPU matrix.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
PAPER=$(cd "$HERE/../.." && pwd)
[ -f "$PAPER/config.env" ] && . "$PAPER/config.env"
SERIAL=${SERIAL:-127.0.0.1:6520}
ADB=${ADB:-adb}
A=("$ADB" -s "$SERIAL")
OUT=""
ROUNDS=2
REPS=5
POLICIES="eevdf,scx_fcp_behavioral,scx_p2dq_pelt_off,scx_lavd"
TESTS="inflating-list,bitmap-upload,shadow-grid-list"
WARMUP_TEST=inflating-list
REMOTE=/data/local/tmp/scx
BINDIR=${BINDIR:-${BIN_DIR:-$PAPER/build/bin}}
NO_REBOOT=0
VALIDATE=${VALIDATE:-1}
VALIDATE_ATTEMPTS=${VALIDATE_ATTEMPTS:-3}
BPF_STATS=${BPF_STATS:-0}
# Tests that are known to be unmeasurable with the frozen APK.  Their rows are
# still attempted and reported, but they do not make the whole block fail (a
# single permanently-invalid test must not discard valid data for the others).
ALLOW_INVALID_TESTS=${ALLOW_INVALID_TESTS:-}

# The frozen UiBench app shipped with an NPE in BitmapUploadActivity. A fixed
# debug build is available; use it when present and require bitmap-upload.
FIXED_UI_APK=${FIXED_UI_APK:-${WORKLOADS_DIR:-$PAPER/workloads}/UiBench-app-debug.apk}
UI_TEST_APK=${UI_TEST_APK:-${WORKLOADS_DIR:-$PAPER/workloads}/UiBenchJankTests-debug.apk}
export UI_TEST_APK
REQUIRE_FIXED_UI_APK=${REQUIRE_FIXED_UI_APK:-1}
if [ -f "$FIXED_UI_APK" ]; then
  UI_APP_APK=$FIXED_UI_APK
  FIXED_UI_APK_SHA=$(sha256sum "$FIXED_UI_APK" | awk '{print $1}')
  ALLOW_INVALID_TESTS=""
  export UI_APP_APK
elif [ "$REQUIRE_FIXED_UI_APK" = 1 ]; then
  echo "FATAL UiBench APK missing: $FIXED_UI_APK (see artifact/BUILD.md §4)" >&2
  exit 1
fi

while [ $# -gt 0 ]; do
  case "$1" in
    -s|--serial) SERIAL=$2; A=("$ADB" -s "$SERIAL"); shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    --rounds) ROUNDS=$2; shift 2 ;;
    --reps) REPS=$2; shift 2 ;;
    --policies) POLICIES=$2; shift 2 ;;
    --tests) TESTS=$2; shift 2 ;;
    --warmup-test) WARMUP_TEST=$2; shift 2 ;;
    --no-reboot) NO_REBOOT=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done

[ -n "$OUT" ] || { echo "set --out" >&2; exit 2; }
mkdir -p "$OUT"
LOG=$OUT/matrix.log
: > "$LOG"
log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG"; }
die() { log "FATAL $*"; exit 1; }

sh_() { "${A[@]}" shell "$@" 2>/dev/null; }
top_activity() { sh_ 'dumpsys activity activities' | tr -d '\r' | grep -m1 'topResumedActivity=' || true; }
wakefulness() { sh_ 'dumpsys power' | tr -d '\r' | grep -m1 'mWakefulness=' | sed 's/.*mWakefulness=//' || true; }
systemui_pid() { sh_ 'pidof com.android.systemui' | tr -d '\r' | awk '{print $1}' || true; }
system_server_pid() { sh_ 'pidof system_server' | tr -d '\r' | awk '{print $1}' || true; }
scx_state() { sh_ 'cat /sys/kernel/sched_ext/state 2>/dev/null' | tr -d '\r' || true; }
scx_ops() { sh_ 'cat /sys/kernel/sched_ext/root/ops 2>/dev/null' | tr -d '\r' || true; }
boot_id() { sh_ 'cat /proc/sys/kernel/random/boot_id' | tr -d '\r' || true; }
bpf_stats_set() { sh_ "echo $1 > /proc/sys/kernel/bpf_stats_enabled" >/dev/null 2>&1 || true; }
bpf_stats_get() { sh_ 'cat /proc/sys/kernel/bpf_stats_enabled 2>/dev/null' | tr -d '\r' || true; }
bpf_snapshot() {
  "${A[@]}" shell 'bpftool prog show --json' > "$1" 2>/dev/null || true
}

services_ready() {
  local activity ss sysui
  activity=$(sh_ 'service check activity' | tr -d '\r')
  ss=$(system_server_pid)
  sysui=$(systemui_pid)
  printf '%s' "$activity" | grep -q 'found' && [ -n "$ss" ] && [ -n "$sysui" ]
}

ensure_services() {
  local deadline=$((SECONDS + 300)) activity ss sysui waited=0
  while [ $SECONDS -lt $deadline ]; do
    if services_ready; then
      log "services ready: system_server=$(system_server_pid) systemui=$(systemui_pid)"
      return 0
    fi
    activity=$(sh_ 'service check activity' | tr -d '\r')
    ss=$(system_server_pid)
    sysui=$(systemui_pid)
    log "waiting for services activity=${activity:-none} system_server=${ss:-none} systemui=${sysui:-none}"
    if ! printf '%s' "$activity" | grep -q 'found' && [ $waited -ge 90 ]; then
      log "activity service still unavailable; restarting zygote"
      sh_ 'setprop ctl.restart zygote' >/dev/null 2>&1 || true
      sleep 10
      waited=0
      deadline=$((SECONDS + 300))
    fi
    sleep 5
    waited=$((waited + 5))
  done
  return 1
}

host_snapshot() {
  local f=$1
  python3 - "$f" <<PY
import json, sys, os, subprocess
def read(path):
    try:
        return open(path).read().strip()
    except OSError:
        return ""
env = {
  "uname": os.uname().release,
  "nproc": os.cpu_count(),
  "loadavg": read("/proc/loadavg"),
  "mem_total_mb": int(open("/proc/meminfo").read().split("MemTotal:")[1].split()[0]) // 1024 if os.path.exists("/proc/meminfo") else None,
  "mem_available_mb": int(open("/proc/meminfo").read().split("MemAvailable:")[1].split()[0]) // 1024 if "MemAvailable:" in open("/proc/meminfo").read() else None,
}
json.dump(env, open(sys.argv[1], "w"), indent=2)
PY
}

wait_device() {
  local deadline=$((SECONDS + 300))
  while [ $SECONDS -lt $deadline ]; do
    "$ADB" connect "$SERIAL" >/dev/null 2>&1 || true
    if [ "$("${A[@]}" get-state 2>/dev/null | tr -d '\r')" = "device" ]; then return 0; fi
    sleep 2
  done
  return 1
}

wait_ui_stable() {
  local deadline=$((SECONDS + 360))
  local last_pid="" stable=0 last_sys="" stable_sys=0 pid top kg up sys boot
  while [ $SECONDS -lt $deadline ]; do
    if [ "$(wakefulness)" != "Awake" ]; then
      sh_ 'input keyevent KEYCODE_WAKEUP' >/dev/null 2>&1 || true
      sh_ 'wm dismiss-keyguard' >/dev/null 2>&1 || true
    fi
    pid=$(systemui_pid)
    sys=$(system_server_pid)
    top=$(top_activity)
    kg=$(sh_ 'dumpsys activity activities' | tr -d '\r' | grep -m1 'KeyguardShowing=' || true)
    up=$(sh_ 'cut -d. -f1 /proc/uptime' | tr -d '\r' || true)
    if [ -n "$pid" ] && [ "$pid" = "$last_pid" ]; then stable=$((stable + 1)); else stable=0; last_pid=$pid; fi
    if [ -n "$sys" ] && [ "$sys" = "$last_sys" ]; then stable_sys=$((stable_sys + 1)); else stable_sys=0; last_sys=$sys; fi
    if [ "${PLATFORM:-}" = arm64 ]; then
      boot=$(sh_ 'getprop sys.boot_completed' | tr -d '\r')
      if [ "$boot" = "1" ] && [ -n "$pid" ] && [ -n "$sys" ] &&
         [ "$stable" -ge 6 ] && [ "$stable_sys" -ge 6 ] &&
         printf '%s' "$kg" | grep -q 'KeyguardShowing=false'; then
        return 0
      fi
    elif [ -n "$pid" ] && [ -n "$sys" ] && [ "$stable" -ge 6 ] && [ "$stable_sys" -ge 6 ] &&
       [ "${up:-0}" -ge 120 ] 2>/dev/null &&
       printf '%s' "$top" | grep -q 'launcher3' && printf '%s' "$kg" | grep -q 'KeyguardShowing=false'; then
      return 0
    fi
    if [ "${PLATFORM:-}" != arm64 ]; then
      sh_ 'input keyevent KEYCODE_HOME' >/dev/null 2>&1 || true
    fi
    sleep 2
  done
  return 1
}

wait_quiet() {
  if [ "${PLATFORM:-}" = arm64 ]; then
    log "skip host/device load wait on ARM64"
    return 0
  fi
  # Host and guest must be past the post-boot crash storm.  The image's media
  # codec can keep system_server busy for several minutes.  Waiting here keeps
  # a crash storm from being mistaken for a scheduler effect.
  local deadline=$((SECONDS + 420)) hl gl host_nproc
  local host_limit=${QUIET_HOST:-}
  host_nproc=$(nproc)
  if [ -z "$host_limit" ]; then
    host_limit=$(awk -v n="$host_nproc" 'BEGIN { printf "%.2f", n * 0.25 }')
  fi
  while [ $SECONDS -lt $deadline ]; do
    hl=$(cut -d' ' -f1 /proc/loadavg)
    gl=$(sh_ 'cut -d" " -f1 /proc/loadavg' | tr -d '\r')
    if awk -v h="$hl" -v g="$gl" -v hlim="$host_limit" 'BEGIN { exit !(h <= hlim && g <= 2.0) }'; then
      log "load quiet host=$hl host_limit=$host_limit guest=$gl guest_limit=2.0"
      return 0
    fi
    sleep 15
  done
  log "WARNING load did not settle host=${hl:-?} host_limit=$host_limit guest=${gl:-?} guest_limit=2.0; continuing"
  return 1
}

prepare_no_reboot() {
  log "no-reboot prepare (image crash-loop workaround)"
  sh_ 'setenforce 0' >/dev/null 2>&1 || true
  sh_ 'setprop service.bootanim.exit 1' >/dev/null 2>&1 || true
  sh_ 'stop bootanim' >/dev/null 2>&1 || true
  sh_ 'setprop debug.sf.nobootanimation 1' >/dev/null 2>&1 || true
  local se
  se=$(sh_ 'getenforce' | tr -d '\r')
  [ "$se" = "Permissive" ] || die "SELinux $se, expected Permissive"
  ensure_services || die "framework services unavailable before no-reboot block"
  wait_ui_stable || die "UI did not settle before no-reboot block"
  wait_quiet || true
  log "ready kernel=$(sh_ 'uname -r' | tr -d '\r') nproc=$(sh_ 'nproc' | tr -d '\r') selinux=$se boot_id=$(boot_id) top=$(top_activity)"
}

reboot_device() {
  log "reboot"
  "${A[@]}" reboot >/dev/null 2>&1 || true
  sleep 10
  wait_device || die "device did not return after reboot"
  "${A[@]}" root >/dev/null 2>&1 || true
  sleep 3
  wait_device || die "device did not return after adb root"
  if [ "${PLATFORM:-}" = arm64 ]; then
    sleep 30
    local deadline=$((SECONDS + 300))
    until [ "$(sh_ 'getprop sys.boot_completed' | tr -d '\r')" = "1" ]; do
      [ $SECONDS -ge $deadline ] && die "boot_completed timeout"
      sleep 3
    done
  else
    local deadline=$((SECONDS + 300))
    until [ "$(sh_ 'getprop sys.boot_completed' | tr -d '\r')" = "1" ]; do
      [ $SECONDS -ge $deadline ] && die "boot_completed timeout"
      sleep 3
    done
  fi
  ensure_services || die "framework services did not become ready after reboot"
  sh_ 'setenforce 0' >/dev/null 2>&1 || true
  local se
  se=$(sh_ 'getenforce' | tr -d '\r')
  [ "$se" = "Permissive" ] || die "SELinux $se after boot, expected Permissive"
  sh_ 'setprop service.bootanim.exit 1' >/dev/null 2>&1 || true
  sh_ 'stop bootanim' >/dev/null 2>&1 || true
  sh_ 'setprop debug.sf.nobootanimation 1' >/dev/null 2>&1 || true
  log "boot ready kernel=$(sh_ 'uname -r' | tr -d '\r') nproc=$(sh_ 'nproc' | tr -d '\r') selinux=$se boot_id=$(boot_id)"
  wait_ui_stable || die "UI did not settle after reboot (top=$(top_activity))"
  sh_ 'svc power stayon true' >/dev/null 2>&1 || true
  sh_ 'settings put global stay_on_while_plugged_in 7' >/dev/null 2>&1 || true
  sh_ 'settings put system screen_off_timeout 2147483647' >/dev/null 2>&1 || true
  sh_ 'settings put global window_animation_scale 0' >/dev/null 2>&1 || true
  sh_ 'settings put global transition_animation_scale 0' >/dev/null 2>&1 || true
  sh_ 'setprop debug.hwui.renderer opengl' >/dev/null 2>&1 || true
  sh_ 'settings put global animator_duration_scale 1' >/dev/null 2>&1 || true
  sh_ 'input keyevent KEYCODE_WAKEUP' >/dev/null 2>&1 || true
  sh_ 'wm dismiss-keyguard' >/dev/null 2>&1 || true
  sh_ 'input keyevent KEYCODE_HOME' >/dev/null 2>&1 || true
  log "selinux after prep $(sh_ 'getenforce' | tr -d '\r'), awake=$(wakefulness), top=$(top_activity)"
}

push_bins() {
  "${A[@]}" shell "mkdir -p $REMOTE/stats" >/dev/null 2>&1 || true
  local base=${BINDIR:-$PAPER/build/bin}
  local tuned_bin=${TUNED_BIN:-$base/scx_fcp_behavioral}
  for path in "$tuned_bin" "$base/scx_lavd" "$base/scx_p2dq"; do
    [ -f "$path" ] || die "binary missing: $path (run ./artifact/pipeline.sh build first)"
  done

  "${A[@]}" push "$tuned_bin" "$REMOTE/scx_fcp_v_a_depth3_tgid1ms" >/dev/null 2>&1 || die "push tuned failed"
  "${A[@]}" push "$tuned_bin" "$REMOTE/scx_fcp_behavioral" >/dev/null 2>&1 || true
  [ -f "$base/scx_fcp" ] && "${A[@]}" push "$base/scx_fcp" "$REMOTE/scx_fcp" >/dev/null 2>&1 || true
  "${A[@]}" push "$base/scx_lavd" "$REMOTE/scx_lavd" >/dev/null 2>&1 || die "push lavd failed"
  "${A[@]}" push "$base/scx_p2dq" "$REMOTE/scx_p2dq" >/dev/null 2>&1 || die "push p2dq failed"
  for b in scx_fcp scx_fcp_behavioral scx_fcp_v_a_depth3_tgid1ms scx_lavd scx_p2dq; do
    "${A[@]}" shell "chmod 755 $REMOTE/$b" >/dev/null 2>&1 || true
  done
}

kill_all_scx() {
  local p pid
  for p in scx_fcp scx_fcp_named scx_fcp_v_stable_root \
           scx_fcp_v_a_depth3_expiry75 scx_fcp_v_a_depth3_tgid1ms \
           scx_lavd scx_p2dq; do
    pid=$(sh_ "pidof $p" || true)
    [ -n "$pid" ] && sh_ "kill -15 $pid" >/dev/null 2>&1 || true
  done
  local pids ps_out
  ps_out=$(sh_ 'ps -A -o PID,ARGS')
  pids=$(printf '%s\n' "$ps_out" | awk '/data\/local\/tmp\/scx\/scx_/ {print $1}')
  for pid in $pids; do
    sh_ "kill -15 $pid" >/dev/null 2>&1 || true
  done
  sleep 2
}

attach_policy() {
  local policy=$1
  local bin="" args="" ops="" state="enabled" fcp_args="" guest_nproc=""
  case "$policy" in
    scx_fcp*)
      guest_nproc=$(sh_ 'nproc' | tr -d '\r')
      case "$guest_nproc" in
        ''|*[!0-9]*) die "invalid guest nproc for $policy: ${guest_nproc:-empty}" ;;
      esac
      fcp_args=$(fcp_capacity_args "${TOPO:-}" "$guest_nproc" "${PLATFORM:-x86_64}") || \
        die "could not derive capacity labels for topology=${TOPO:-unset} nproc=$guest_nproc"
      ;;
  esac
  case "$policy" in
    eevdf|eevdf_proxy_exec) state=disabled; ops="" ;;
    scx_fcp) bin=scx_fcp; args="$fcp_args"; ops=fcp ;;
    scx_fcp_behavioral|scx_fcp_zh) bin=scx_fcp_behavioral; args="$fcp_args"; ops=fcp_zh ;;
    scx_fcp_v_stable_root) bin=scx_fcp_v_stable_root; args="$fcp_args"; ops=fcp_zh ;;
    scx_fcp_v_bounded_fanout) bin=scx_fcp_v_bounded_fanout; args="$fcp_args"; ops=fcp_zh ;;
    scx_fcp_v_tail_guard) bin=scx_fcp_v_tail_guard; args="$fcp_args"; ops=fcp_zh ;;
    scx_fcp_v_jank_guard) bin=scx_fcp_v_jank_guard; args="$fcp_args"; ops=fcp_zh ;;
    scx_fcp_v_a_depth3) bin=scx_fcp_v_a_depth3; args="$fcp_args"; ops=fcp_zh ;;
    scx_fcp_v_a_expiry75) bin=scx_fcp_v_a_expiry75; args="$fcp_args"; ops=fcp_zh ;;
    scx_fcp_v_a_depth3_expiry75) bin=scx_fcp_v_a_depth3_expiry75; args="$fcp_args"; ops=fcp_zh ;;
    scx_fcp_v_a_depth3_tgid1ms) bin=scx_fcp_v_a_depth3_tgid1ms; args="$fcp_args"; ops=fcp_zh ;;
    scx_fcp_v_a_depth4) bin=scx_fcp_v_a_depth4; args="$fcp_args"; ops=fcp_zh ;;
    scx_p2dq_pelt_off) bin=scx_p2dq; args="--enable-pelt false"; ops=p2dq ;;
    scx_lavd) bin=scx_lavd; ops=lavd ;;
    *) die "unknown policy $policy" ;;
  esac

  kill_all_scx
  sleep 2
  if [ "$policy" = "eevdf_proxy_exec" ]; then
    sh_ 'cat /proc/cmdline' | tr -d '\r' | grep -q 'sched_proxy_exec=1' || \
      die "eevdf_proxy_exec requested but guest cmdline lacks sched_proxy_exec=1"
  fi
  if [ "$state" = "disabled" ]; then
    [ "$(scx_state)" = "disabled" ] || die "EEVDF requested but sched_ext state=$(scx_state) ops=$(scx_ops)"
    log "policy eevdf ready, sched_ext disabled"
    return 0
  fi

  local logf=$REMOTE/${policy}_block.log
  timeout 20 "${A[@]}" shell "cd $REMOTE && rm -f $logf && ulimit -l unlimited && SCX_STATS_BASE_PATH=$REMOTE/stats nohup ./$bin $args > $logf 2>&1 < /dev/null &" >/dev/null 2>&1 || true
  local deadline=$((SECONDS + 120)) st o
  while [ $SECONDS -lt $deadline ]; do
    st=$(scx_state); o=$(scx_ops)
    if [ "$st" = "enabled" ] && printf '%s' "$o" | grep -q "$ops"; then
      log "policy $policy enabled ops=$o capacity_args=${fcp_args:-none}"
      sh_ "cat $logf" > "$OUT/${policy}_scheduler.log" 2>/dev/null || true
      sh_ 'dmesg' | grep -iE 'sched_ext|scx|lavd|p2dq|fcp' | tail -20 > "$OUT/${policy}_dmesg.txt" 2>/dev/null || true
      return 0
    fi
    sleep 2
  done
  sh_ "cat $logf" > "$OUT/${policy}_scheduler.log" 2>/dev/null || true
  die "policy $policy did not enable (state=$st ops=$o)"
}

stop_policy() {
  kill_all_scx
  local deadline=$((SECONDS + 20))
  while [ $SECONDS -lt $deadline ]; do
    [ "$(scx_state)" = "disabled" ] && return 0
    sleep 1
  done
  log "WARNING sched_ext state after stop $(scx_state) ops=$(scx_ops)"
}

validate_ub_block() {
  local blockdir=$1
  [ "$VALIDATE" = 1 ] || return 0
  local raw=$blockdir/raw
  local metrics
  metrics=$(find "$raw" -name metrics.json 2>/dev/null | sort)
  if [ -z "$metrics" ]; then
    log "INVALID no metrics.json under $blockdir"
    return 1
  fi
  python3 - "$metrics" "$TESTS" "$REPS" "${ALLOW_INVALID_TESTS:-}" <<'PY'
import json, sys
paths = [p for p in sys.argv[1].splitlines() if p.strip()]
tests = [t for t in sys.argv[2].split(",") if t]
reps = int(sys.argv[3])
allow = {t for t in sys.argv[4].split(",") if t}
rows = []
for path in paths:
    data = json.load(open(path))
    rows.extend(data.get("tests") or [])
required = [t for t in tests if t not in allow]
expected_keys = {(test, rep) for test in tests for rep in range(1, reps + 1)}
seen = {}
problems = []
for row in rows:
    key = (row.get("test"), int(row.get("rep") or 0))
    seen.setdefault(key, []).append(row)
for key in sorted(expected_keys):
    if key not in seen:
        problems.append(f"missing {key[0]} rep {key[1]}")
    elif len(seen[key]) > 1:
        problems.append(f"duplicate {key[0]} rep {key[1]}")
extra = sorted(set(seen) - expected_keys)
if extra:
    problems.append("unexpected rows: " + ", ".join(f"{t}/rep{r}" for t, r in extra))
invalid = [r for r in rows if not r.get("valid") and r.get("test") in required]
if invalid:
    problems.append("invalid: " + " | ".join(
        f"{r.get('test')}/rep{r.get('rep')}: {r.get('invalid_reason')}"
        for r in invalid))
known = [r for r in rows if not r.get("valid") and r.get("test") in allow]
known_detail = ""
if known:
    known_detail = " | known-invalid (allowed): " + " | ".join(
        f"{r.get('test')}/rep{r.get('rep')}: {(r.get('invalid_reason') or '')[:90]}"
        for r in known)
if problems:
    print("INVALID " + "; ".join(problems))
    raise SystemExit(1)
print(f"VALID: {len(rows)} UiBench measurements, "
      f"{len(required)} required tests x {reps} reps"
      + (f"; {len(allow)} known-invalid test(s) attempted" if allow else "")
      + known_detail)
PY
}

archive_invalid_ub() {
  local blockdir=$1
  [ -d "$blockdir" ] || return 0
  local dest=$OUT/_invalid/$(basename "$(dirname "$blockdir")")/$(basename "$blockdir")-$(date +%H%M%S)
  mkdir -p "$(dirname "$dest")"
  mv "$blockdir" "$dest"
  log "archived invalid UiBench block to $dest"
}

run_block_once() {
  local policy=$1 round=$2 reps=$3
  local blockdir=$OUT/$policy/block-round$round
  mkdir -p "$blockdir"
  host_snapshot "$blockdir/host_before.json"
  log "block start policy=$policy round=$round reps=$reps"
  sh_ 'cat /proc/cmdline' | tr -d '\r' > "$blockdir/cmdline.txt" 2>/dev/null || true
  if [ "$NO_REBOOT" = 1 ]; then
    prepare_no_reboot
  else
    reboot_device
    wait_quiet || true
  fi
  push_bins
  attach_policy "$policy"
  host_snapshot "$blockdir/host_after_attach.json"

  # One ignored warm-up run.  It is kept under _warmup/ and never enters metrics.
  # Install the APKs at the start of every block: package state must not be
  # assumed to survive a reboot, and a missing instrumentation package turns
  # every row into an INSTRUMENTATION_FAILED error.
  log "warm-up policy=$policy test=$WARMUP_TEST"
  "$HERE/run_uibench.sh" -s "$SERIAL" -p "$policy" -r 1 -n 0 \
    --out "$OUT/_warmup/$policy/round$round" --tests "$WARMUP_TEST" > "$blockdir/warmup.log" 2>&1 || \
    log "WARNING warm-up failed"

  local bpf_before="" bpf_after="" start_iso="" end_iso=""
  if [ "$BPF_STATS" = 1 ]; then
    bpf_stats_set 1
    bpf_before=$(bpf_stats_get)
    printf '%s\n' "$bpf_before" > "$blockdir/bpf_stats_enabled_before.txt"
    bpf_snapshot "$blockdir/bpf_before.json"
    start_iso=$(date -Iseconds)
  fi

  log "measured policy=$policy round=$round reps=$reps"
  local ui_extra=()
  [ "$BPF_STATS" = 1 ] && ui_extra+=(--no-install)
  "$HERE/run_uibench.sh" -s "$SERIAL" -p "$policy" -r "$reps" -n "$round" \
    --out "$blockdir/raw" --tests "$TESTS" --shuffle "${ui_extra[@]}" \
    > "$blockdir/run.log" 2>&1 || \
    log "WARNING measured block returned non-zero"

  if [ "$BPF_STATS" = 1 ]; then
    end_iso=$(date -Iseconds)
    bpf_snapshot "$blockdir/bpf_after.json"
    bpf_after=$(bpf_stats_get)
    printf '%s\n' "$bpf_after" > "$blockdir/bpf_stats_enabled_after.txt"
    bpf_stats_set 0
    local guest_nproc
    guest_nproc=$(sh_ 'nproc' | tr -d '\r')
    python3 - "$blockdir" "$policy" "$round" "$start_iso" "$end_iso" \
      "$bpf_before" "$bpf_after" "$guest_nproc" <<'PY'
import glob
import json
import os
import sys
from pathlib import Path

block = Path(sys.argv[1])
policy, run = sys.argv[2], int(sys.argv[3])
start, end = sys.argv[4], sys.argv[5]
frames = 0
for path in glob.glob(str(block / "raw" / "**" / "metrics.json"), recursive=True):
    try:
        data = json.load(open(path))
    except (OSError, json.JSONDecodeError):
        continue
    for row in data.get("tests") or []:
        frames += int(row.get("frames") or 0)
manifest = {
    "policy": policy,
    "run": run,
    "host_start": start,
    "host_end": end,
    "nproc": int(sys.argv[8] or 0),
    "bpf_stats_enabled_before": sys.argv[6],
    "bpf_stats_enabled_after": sys.argv[7],
}
summary = {"frames": frames, "iterations": [0]}
(block / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
(block / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
PY
    python3 "$HERE/parse_bpf_overhead.py" "$blockdir" >> "$LOG" 2>&1 || \
      log "WARNING UiBench overhead parser failed for $blockdir"
  fi

  stop_policy
  host_snapshot "$blockdir/host_after.json"
  sh_ 'cat /proc/sys/kernel/random/boot_id' | tr -d '\r' > "$blockdir/boot_id.txt" 2>/dev/null || true
  sh_ 'getenforce' | tr -d '\r' > "$blockdir/selinux.txt" 2>/dev/null || true
  log "block done policy=$policy round=$round"
}

run_block() {
  local policy=$1 round=$2 reps=$3 attempt
  local blockdir=$OUT/$policy/block-round$round
  for attempt in $(seq 1 "$VALIDATE_ATTEMPTS"); do
    if run_block_once "$policy" "$round" "$reps"; then
      if validate_ub_block "$blockdir" 2>&1 | tee "$blockdir/validation.txt" | tee -a "$LOG"; then
        return 0
      fi
      archive_invalid_ub "$blockdir"
      log "INVALID UiBench block policy=$policy round=$round attempt=$attempt; rerun"
    else
      log "FAILED UiBench block policy=$policy round=$round attempt=$attempt"
      archive_invalid_ub "$blockdir"
    fi
    sleep 5
  done
  log "GIVEUP UiBench block policy=$policy round=$round"
  return 1
}

# ---------------------------------------------------------------------------

ui_fixed_smoke() {
  [ "${UI_FIXED_SMOKE:-1}" = 1 ] || return 0
  [ -n "${UI_APP_APK:-}" ] || return 0
  log "fixed-APK smoke: bitmap-upload 1 rep with $UI_APP_APK"
  # A previous run may have left _smoke/2026-.../metrics.json behind. The
  # validator below globs recursively and would then see more than one row,
  # falsely failing the whole block. Each smoke run owns a fresh directory.
  rm -rf "$OUT/_smoke"
  # The image can restart SystemUI once several minutes after boot.  Running
  # the smoke while that restart lands invalidates a perfectly good workload
  # and made the whole proxy block disappear.  Settle the UI and the load
  # first; this is also what the measured blocks do after their reboot.
  wait_ui_stable || die "UI did not settle before fixed-APK smoke"
  wait_quiet || true
  kill_all_scx
  sleep 2
  "$HERE/run_uibench.sh" -s "$SERIAL" -p eevdf -r 1 -n 99 \
    --out "$OUT/_smoke" --tests bitmap-upload > "$OUT/_smoke.log" 2>&1 || \
    log "WARNING smoke run returned non-zero"
  if ! python3 - "$OUT/_smoke" "$FIXED_UI_APK_SHA" <<'PY'
import glob, json, os, sys
root = sys.argv[1]
paths = glob.glob(os.path.join(root, "**", "metrics.json"), recursive=True)
if not paths:
    print("SMOKE FAILED: no metrics.json")
    raise SystemExit(1)
rows = []
for p in paths:
    data = json.load(open(p))
    rows.extend(data.get("tests") or [])
bitmap = [r for r in rows if r.get("test") == "bitmap-upload"]
if len(bitmap) != 1:
    print(f"SMOKE FAILED: {len(bitmap)} bitmap-upload rows")
    raise SystemExit(1)
row = bitmap[0]
if not row.get("valid") or row.get("status") != "passed":
    print("SMOKE FAILED: " + str(row.get("invalid_reason") or row.get("status")))
    raise SystemExit(1)
if int(row.get("frames") or 0) < 100:
    print(f"SMOKE FAILED: frames={row.get('frames')}")
    raise SystemExit(1)
print(f"SMOKE PASSED: bitmap-upload frames={row.get('frames')} "
      f"p50={row.get('p50_ms')} p95={row.get('p95_ms')}")
PY
  then
    log "fixed-APK smoke validation failed; refusing to run blocks"
    return 1
  fi
  log "fixed-APK smoke passed"
  return 0
}
log "UiBench app=${UI_APP_APK:-none}"

# ---------------------------------------------------------------------------
wait_device || die "device $SERIAL not reachable"
"${A[@]}" root >/dev/null 2>&1 || true
sleep 2
sh_ 'setenforce 0' >/dev/null 2>&1 || true
[ "$(sh_ 'getenforce' | tr -d '\r')" = "Permissive" ] || die "SELinux is not Permissive"
log "host $(uname -r) nproc=$(nproc) mem=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)MB load=$(cat /proc/loadavg)"
log "device kernel=$(sh_ 'uname -r' | tr -d '\r') nproc=$(sh_ 'nproc' | tr -d '\r') serial=$SERIAL"
log "policies=$POLICIES tests=$TESTS rounds=$ROUNDS reps=$REPS no_reboot=$NO_REBOOT out=$OUT"
ui_fixed_smoke

IFS=',' read -r -a POLICY_ARR <<< "$POLICIES"
for round in $(seq 1 "$ROUNDS"); do
  mapfile -t ORDER < <(printf '%s\n' "${POLICY_ARR[@]}" | shuf)
  reps=$(( REPS / ROUNDS ))
  [ "$round" -le $(( REPS % ROUNDS )) ] && reps=$((reps + 1))
  log "round $round reps=$reps policy order ${ORDER[*]}"
  for policy in "${ORDER[@]}"; do
    run_block "$policy" "$round" "$reps" || \
      log "GIVEUP UiBench block policy=$policy round=$round; continuing"
  done
done

# Aggregate every metrics.json under the output tree.
python3 "$HERE/summarize_uibench.py" "$OUT" | tee "$OUT/summary_all.txt"
log "matrix done"
