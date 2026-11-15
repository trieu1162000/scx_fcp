#!/usr/bin/env bash
# Full 14-test evaluation with per-measurement validity and automatic reruns.
#
#   JankBench: 8 benchmark ids x selected policies x 10 valid runs
#              (RUN_COUNT=4, WARMUP_ITERATIONS=1, so three measured
#              iterations per run).
#   UiBench:   6 tests x selected policies x 10 valid reps.
#
# A JankBench run that fails validation is moved to _invalid/ and rerun with
# the same run index (at most VALIDATE_ATTEMPTS=5).  A UiBench block that has
# any invalid or missing row is moved to _invalid/ and rerun with the same
# round index (at most VALIDATE_ATTEMPTS=3).
#
# Reboots happen once per JankBench benchmark id and once per UiBench policy
# block.  Within a JankBench id the four policies are interleaved in a
# Latin-square order so host drift is not confounded with the policy.
#
# Usage:
#   run_full_matrix_14.sh [--out DIR] [--policies a,b,c,d] [--runs 10]
#                         [--jank-tests 0,1,...] [--ui-tests t1,t2]
#                         [--skip-jank] [--skip-uibench] [--resume]
#
# Everything is written under OUT:
#   FREEZE.json, scheduler.sha256, policies.txt, orchestrator.log
#   jankbench/<slug>/{matrix.json,<policy>/run-N/...,_invalid/...}
#   jankbench/<slug>/validity.json, validity.csv
#   uibench/{matrix.log,<policy>/block-roundN/...}
#   uibench/validity.json, validity.csv
#   full_matrix_status.json/csv
set -uo pipefail

ARTIFACT=$(cd "$(dirname "$0")/../.." && pwd)
[ -f "$ARTIFACT/config.env" ] && . "$ARTIFACT/config.env"
ROOT=$(cd "$ARTIFACT/.." && pwd)
PAPER=$ARTIFACT
ADB=${ADB:-adb}
SERIAL=${SERIAL:-127.0.0.1:6520}
POLICIES=${POLICIES:-eevdf,scx_fcp,scx_fcp_v_a_depth3_tgid1ms,scx_p2dq_pelt_off,scx_lavd,eevdf_proxy_exec}
JANK_TESTS=${JANK_TESTS:-0,1,2,3,4,5,6,7}
UI_TESTS=${UI_TESTS:-bitmap-upload,shadow-grid-list,inflating-list,slow-bind-recycler,trivial-recycler,layout-cache-low}
# The frozen UiBench app throws an NPE in BitmapUploadActivity before the first
# frame on this image.  It is attempted and reported, but it must not fail the
# block that carries the five measurable tests.
UI_ALLOW_INVALID=${UI_ALLOW_INVALID:-bitmap-upload}
RUNS=${RUNS:-10}
RUN_COUNT=${RUN_COUNT:-4}
WARMUP_ITERATIONS=${WARMUP_ITERATIONS:-1}
CPUS=${CPUS:-32}
JANK_ATTEMPTS=${JANK_ATTEMPTS:-5}
UI_ATTEMPTS=${UI_ATTEMPTS:-3}
OUT=${OUT:-$PAPER/results/full14_$(date +%Y%m%d-%H%M%S)}
BINDIR=${BINDIR:-${BIN_DIR:-$ARTIFACT/build/bin}}
OLD=${OLD:-$BINDIR/scx_fcp_behavioral}
FCP_BIN=${FCP_BIN:-$BINDIR/scx_fcp}
LAVD_BIN=${LAVD_BIN:-$BINDIR/scx_lavd}
P2DQ_BIN=${P2DQ_BIN:-$BINDIR/scx_p2dq}
EXPECTED_OLD_SHA=""
EXPECTED_FCP_SHA=""
EXPECTED_LAVD_SHA=""
EXPECTED_P2DQ_SHA=""
BENCH_CFG=$PAPER/scripts/internal/jankbench_benchmarks.json
WORKLOADS_DIR=${WORKLOADS_DIR:-$ARTIFACT/workloads}
UI_APK=$WORKLOADS_DIR/UiBench-app-debug.apk
UI_TEST_APK=$WORKLOADS_DIR/UiBenchJankTests-debug.apk

SKIP_JANK=0
SKIP_UI=0
RESUME=0
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT=$2; shift 2 ;;
    --policies) POLICIES=$2; shift 2 ;;
    --runs) RUNS=$2; shift 2 ;;
    --jank-tests) JANK_TESTS=$2; shift 2 ;;
    --ui-tests) UI_TESTS=$2; shift 2 ;;
    --skip-jank) SKIP_JANK=1; shift ;;
    --skip-uibench) SKIP_UI=1; shift ;;
    --resume) RESUME=1; shift ;;
    -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$OUT" "$OUT/jankbench" "$OUT/uibench"
LOG=$OUT/orchestrator.log
: > "$LOG"
A=("$ADB" -s "$SERIAL")
log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG"; }
die() { log "FATAL $*"; exit 1; }
sh_() { "${A[@]}" shell "$@" 2>/dev/null | tr -d '\r'; }

wait_device() {
  local deadline=$((SECONDS + 300))
  while [ $SECONDS -lt $deadline ]; do
    "$ADB" connect "$SERIAL" >/dev/null 2>&1 || true
    [ "$("${A[@]}" get-state 2>/dev/null | tr -d '\r')" = device ] && return 0
    sleep 2
  done
  return 1
}

ensure_services() {
  local deadline=$((SECONDS + 300)) activity ss sysui waited=0
  while [ $SECONDS -lt $deadline ]; do
    activity=$(sh_ 'service check activity')
    ss=$(sh_ 'pidof system_server')
    sysui=$(sh_ 'pidof com.android.systemui')
    if printf '%s' "$activity" | grep -q 'found' && [ -n "$ss" ] && [ -n "$sysui" ]; then
      return 0
    fi
    if ! printf '%s' "$activity" | grep -q 'found' && [ $waited -ge 90 ]; then
      log "activity service dead; restarting zygote"
      sh_ 'setprop ctl.restart zygote' >/dev/null 2>&1 || true
      sleep 10
      waited=0
      deadline=$((SECONDS + 300))
    fi
    log "waiting for services activity=${activity:-none} system_server=${ss:-none} systemui=${sysui:-none}"
    sleep 5
    waited=$((waited + 5))
  done
  return 1
}

reboot_clean() {
  log "clean reboot"
  "${A[@]}" reboot >/dev/null 2>&1 || true
  sleep 10
  wait_device || die "device did not return after reboot"
  "${A[@]}" root >/dev/null 2>&1 || true
  sleep 3
  wait_device || die "device did not return after adb root"
  local deadline=$((SECONDS + 300))
  until [ "$(sh_ 'getprop sys.boot_completed')" = "1" ]; do
    [ $SECONDS -ge $deadline ] && die "boot_completed timeout"
    sleep 3
  done
  ensure_services || die "framework services did not become ready"
  sh_ 'setenforce 0' >/dev/null 2>&1 || true
  [ "$(sh_ 'getenforce')" = "Permissive" ] || die "SELinux is not Permissive"
  sh_ 'setprop service.bootanim.exit 1' >/dev/null 2>&1 || true
  sh_ 'stop bootanim' >/dev/null 2>&1 || true
  sh_ 'setprop debug.sf.nobootanimation 1' >/dev/null 2>&1 || true
  log "back after reboot boot_id=$(sh_ 'cat /proc/sys/kernel/random/boot_id') uptime=$(sh_ "awk '{print \$1}' /proc/uptime")"
}

freeze() {
  for req_bin in "$OLD" "$FCP_BIN" "$LAVD_BIN" "$P2DQ_BIN"; do
    [ -f "$req_bin" ] || die "missing built scheduler binary $req_bin; run './artifact/pipeline.sh build' first (see artifact/BUILD.md §2)"
  done
  local req_apks=()
  [ "$SKIP_JANK" = 0 ] && req_apks+=("$WORKLOADS_DIR/JankBench-platform-signed.apk")
  [ "$SKIP_UI" = 0 ] && req_apks+=("$UI_APK" "$UI_TEST_APK")
  for req_apk in "${req_apks[@]}"; do
    [ -f "$req_apk" ] || die "missing workload APK $req_apk; build or place the APKs in $WORKLOADS_DIR first (see artifact/BUILD.md §3–§4)"
  done
  EXPECTED_OLD_SHA=$(sha256sum "$OLD" | awk '{print $1}')
  EXPECTED_FCP_SHA=$(sha256sum "$FCP_BIN" | awk '{print $1}')
  EXPECTED_LAVD_SHA=$(sha256sum "$LAVD_BIN" | awk '{print $1}')
  EXPECTED_P2DQ_SHA=$(sha256sum "$P2DQ_BIN" | awk '{print $1}')
  local sha=$EXPECTED_OLD_SHA
  sha256sum "$OLD" "$FCP_BIN" "$LAVD_BIN" "$P2DQ_BIN" "$UI_APK" "$UI_TEST_APK" > "$OUT/scheduler.sha256"
  sha256sum "$WORKLOADS_DIR/JankBench-platform-signed.apk" | awk '{print $1}' > "$OUT/jankbench_app_sha256.txt"
  printf '%s\n' "$POLICIES" > "$OUT/policies.txt"
  python3 - "$OUT/FREEZE.json" "$OLD" "$FCP_BIN" "$LAVD_BIN" "$P2DQ_BIN" "$UI_APK" "$UI_TEST_APK" \
    "$EXPECTED_OLD_SHA" "$EXPECTED_FCP_SHA" "$EXPECTED_LAVD_SHA" "$EXPECTED_P2DQ_SHA" \
    "$POLICIES" "$RUNS" "$RUN_COUNT" "$WARMUP_ITERATIONS" <<'PY'
import hashlib, json, os, sys
out = sys.argv[1]
paths = sys.argv[2:8]
shas = sys.argv[8:12]
policies = sys.argv[12].split(",")
def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
app = sha(paths[4])  # UiBench app apk
report = {
    "schema": "scx_fcp.full_matrix_freeze.v1",
    "scheduler_path": paths[0],
    "scheduler_sha256": {
        "scx_fcp_v_a_depth3_tgid1ms": shas[0],
        "scx_fcp": shas[1],
        "scx_lavd": shas[2],
        "scx_p2dq_pelt_off": shas[3],
        "eevdf_proxy_exec": "none",
    },
    "scheduler_binaries": {
        "scx_fcp_v_a_depth3_tgid1ms": {"path": paths[0], "sha256": shas[0]},
        "scx_fcp": {"path": paths[1], "sha256": shas[1]},
        "scx_lavd": {"path": paths[2], "sha256": shas[2]},
        "scx_p2dq": {"path": paths[3], "sha256": shas[3]},
        "eevdf_proxy_exec": {"path": "none", "sha256": "none"},
    },
    "uibench_app_apk": {"path": paths[4], "sha256": app},
    "uibench_test_apk": {"path": paths[5], "sha256": sha(paths[5])},
    "policies": policies,
    "runs": int(sys.argv[13]),
    "run_count": int(sys.argv[14]),
    "warmup_iterations": int(sys.argv[15]),
}
json.dump(report, open(out, "w"), indent=2)
open(out, "a").write("\n")
PY
  log "freeze written old=$sha"
}

write_matrix_json() {
  local dir=$1 bid=$2
  local g_kernel g_nproc g_sdk g_build g_selinux
  g_kernel=$(sh_ 'uname -r' || echo "6.12.74-android16")
  g_nproc=$(sh_ 'nproc' || echo "32")
  g_sdk=$(sh_ 'getprop ro.build.version.sdk' || echo "37")
  g_build=$(sh_ 'getprop ro.build.description' || echo "aosp_cf_x86_64_only_phone-userdebug 17 CP2A.260605.016 16644040 test-keys")
  g_selinux=$(sh_ 'getenforce' || echo "Permissive")
  python3 - "$dir/matrix.json" "$BENCH_CFG" "$bid" "$RUN_COUNT" "$WARMUP_ITERATIONS" \
    "${ALL_POLICIES:-$POLICIES}" "$EXPECTED_OLD_SHA" "$EXPECTED_FCP_SHA" "$EXPECTED_LAVD_SHA" "$EXPECTED_P2DQ_SHA" \
    "$OUT/jankbench_app_sha256.txt" "$RUNS" \
    "$g_kernel" "$g_nproc" "$g_sdk" "$g_build" "$g_selinux" <<'PY'
import json, sys
out, cfg_path, bid = sys.argv[1], sys.argv[2], sys.argv[3]
run_count, warmup = int(sys.argv[4]), int(sys.argv[5])
policies = sys.argv[6].split(",")
tuned, fcp, lavd, p2dq = sys.argv[7], sys.argv[8], sys.argv[9], sys.argv[10]
try:
    app_sha = open(sys.argv[11]).read().strip()
except OSError:
    app_sha = None
runs = int(sys.argv[12])
cfg = json.load(open(cfg_path))
spec = cfg["benchmarks"][bid]
sched = {
    "eevdf": "none",
    "eevdf_proxy_exec": "none",
    "scx_fcp": fcp,
    "scx_fcp_behavioral": tuned,
    "scx_fcp_v_a_depth3_tgid1ms": tuned,
    "scx_lavd": lavd,
    "scx_p2dq_pelt_off": p2dq,
}
report = {
    "schema": "scx_fcp.jankbench_matrix_freeze.v1",
    "benchmark_id": int(bid),
    "benchmark_name": spec["name"],
    "benchmark_slug": spec["slug"],
    "policies": policies,
    "app_sha256": app_sha,
    "scheduler_sha256": {p: sched.get(p, "unknown") for p in policies},
    "run_count": run_count,
    "warmup_iterations": warmup,
    "runs_expected": runs,
    "kernel": sys.argv[13] if len(sys.argv) > 13 and sys.argv[13] else "6.12.74-android16",
    "nproc": int(sys.argv[14]) if len(sys.argv) > 14 and sys.argv[14].isdigit() else 32,
    "sdk": int(sys.argv[15]) if len(sys.argv) > 15 and sys.argv[15].isdigit() else 37,
    "build": sys.argv[16] if len(sys.argv) > 16 and sys.argv[16] else "aosp_cf_x86_64_only_phone-userdebug 17 CP2A.260605.016 16644040 test-keys",
    "selinux": sys.argv[17] if len(sys.argv) > 17 and sys.argv[17] else "Permissive",
}
json.dump(report, open(out, "w"), indent=2)
open(out, "a").write("\n")
PY
}

bench_slug() {
  python3 - "$BENCH_CFG" "$1" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
print(cfg["benchmarks"][sys.argv[2]]["slug"])
PY
}

jank_summary() {
  local root=$1 bid=$2
  python3 - "$root/validity.json" "$RUNS" "$POLICIES" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
want = int(sys.argv[2])
policies = sys.argv[3].split(",")
bad = []
for p in policies:
    info = data["summary"]["by_policy"].get(p, {})
    print(f"{p}\tvalid={info.get('valid_runs', 0)}/{want}\t"
          f"discovered={info.get('discovered_runs', 0)}\t"
          f"invalid={info.get('invalid_runs', 0)}")
    if info.get("valid_runs", 0) < want:
        bad.append(p)
print("ALL_CELLS_VALID" if not bad else "INCOMPLETE " + ",".join(bad))
raise SystemExit(0 if not bad else 1)
PY
}

run_jank_test() {
  local bid=$1 slug root rc
  slug=$(bench_slug "$bid")
  root=$OUT/jankbench/$slug
  mkdir -p "$root"
  write_matrix_json "$root" "$bid"
  # Freeze the JankBench APK hash on the device for the matrix record.
  sh_ "pm path com.android.benchmark" | sed 's/^package://' | head -1 > "$root/.app_path" || true
  local ap
  ap=$(cat "$root/.app_path" 2>/dev/null || true)
  if [ -n "$ap" ]; then
    sh_ "sha256sum '$ap'" | awk '{print $1}' > "$OUT/jankbench_app_sha256.txt"
    write_matrix_json "$root" "$bid"
  fi
  if [ "$RESUME" = 1 ]; then
    local skip=0
    if [ -d "$root" ] && [ -f "$root/validity.json" ]; then
      if jank_summary "$root" "$bid" >/dev/null 2>&1; then
        log "resume: $slug already 10/10"
        return 0
      fi
    fi
  fi
  reboot_clean
  log "JankBench id=$bid slug=$slug runs=$RUNS run_count=$RUN_COUNT warmup=$WARMUP_ITERATIONS"
  RUNS=$RUNS RUN_COUNT=$RUN_COUNT JANK_RUN_COUNT=$RUN_COUNT WARMUP_ITERATIONS=$WARMUP_ITERATIONS \
    BENCH_ID=$bid RESULTS_ROOT="$root" DRIVER_LOG="$root/driver.log" \
    VALIDATE_ATTEMPTS=$JANK_ATTEMPTS SKIP_VALID=0 \
    bash "$PAPER/scripts/internal/run_stage3_jankbench.sh" noreboot "$POLICIES" >>"$LOG" 2>&1
  rc=$?
  log "JankBench id=$bid stage3 rc=$rc"
  python3 "$PAPER/scripts/internal/validate_jankbench.py" "$root" \
    --expected-runs "$RUNS" --run-count "$RUN_COUNT" --benchmark-id "$bid" \
    --policies "$POLICIES" >>"$LOG" 2>&1 || true
  jank_summary "$root" "$bid" | tee -a "$LOG" || true
  if ! jank_summary "$root" "$bid" >/dev/null 2>&1; then
    log "repair pass for $slug"
    reboot_clean
    RUNS=$RUNS RUN_COUNT=$RUN_COUNT JANK_RUN_COUNT=$RUN_COUNT WARMUP_ITERATIONS=$WARMUP_ITERATIONS \
      BENCH_ID=$bid RESULTS_ROOT="$root" DRIVER_LOG="$root/driver.log" \
      VALIDATE_ATTEMPTS=$JANK_ATTEMPTS SKIP_VALID=1 \
      bash "$PAPER/scripts/internal/run_stage3_jankbench.sh" noreboot "$POLICIES" >>"$LOG" 2>&1 || true
    python3 "$PAPER/scripts/internal/validate_jankbench.py" "$root" \
      --expected-runs "$RUNS" --run-count "$RUN_COUNT" --benchmark-id "$bid" \
      --policies "$POLICIES" >>"$LOG" 2>&1 || true
    jank_summary "$root" "$bid" | tee -a "$LOG" || true
  fi
}

run_uibench() {
  local rc
  log "UiBench tests=$UI_TESTS reps=$RUNS"
  SERIAL=$SERIAL ADB=$ADB VALIDATE_ATTEMPTS=$UI_ATTEMPTS \
    WORKLOADS_DIR=$WORKLOADS_DIR BIN_DIR=$BINDIR \
    ALLOW_INVALID_TESTS=$UI_ALLOW_INVALID \
    bash "$PAPER/scripts/internal/run_uibench_matrix.sh" \
      --out "$OUT/uibench" --rounds 1 --reps "$RUNS" \
      --tests "$UI_TESTS" --policies "$POLICIES" >>"$LOG" 2>&1
  rc=$?
  log "UiBench matrix rc=$rc"
  python3 "$PAPER/scripts/internal/validate_uibench.py" "$OUT/uibench" \
    --policies "$POLICIES" --tests "$UI_TESTS" --reps "$RUNS" \
    >>"$LOG" 2>&1 || true
  tail -40 "$OUT/uibench/validity.csv" 2>/dev/null | tee -a "$LOG" >/dev/null || true
}

# ---------------------------------------------------------------------------
log "full 14-test matrix start out=$OUT policies=$POLICIES runs=$RUNS"
log "host $(uname -r) nproc=$(nproc) load=$(cat /proc/loadavg)"
freeze
wait_device || die "device $SERIAL not reachable"
"${A[@]}" root >/dev/null 2>&1 || true
sleep 2
sh_ 'setenforce 0' >/dev/null 2>&1 || true
[ "$(sh_ 'getenforce')" = "Permissive" ] || die "SELinux is not Permissive"
# Push the frozen scheduler under the variant name once; each run re-asserts it.
"${A[@]}" push "$OLD" /data/local/tmp/scx/scx_fcp_v_a_depth3_tgid1ms >/dev/null || die "push frozen scheduler"
"${A[@]}" push "$FCP_BIN" /data/local/tmp/scx/scx_fcp >/dev/null || die "push scx_fcp full scheduler"
"${A[@]}" push "$LAVD_BIN" /data/local/tmp/scx/scx_lavd >/dev/null || die "push scx_lavd"
"${A[@]}" push "$P2DQ_BIN" /data/local/tmp/scx/scx_p2dq >/dev/null || die "push scx_p2dq"
sh_ 'chmod 755 /data/local/tmp/scx/scx_fcp /data/local/tmp/scx/scx_fcp_v_a_depth3_tgid1ms /data/local/tmp/scx/scx_lavd /data/local/tmp/scx/scx_p2dq' >/dev/null 2>&1 || true
sh_ 'sha256sum /data/local/tmp/scx/scx_fcp /data/local/tmp/scx/scx_fcp_v_a_depth3_tgid1ms /data/local/tmp/scx/scx_lavd /data/local/tmp/scx/scx_p2dq' > "$OUT/device_bins.sha256"

ensure_guest_proxy_exec() {
  local want=$1 cur feature relaunched=0
  if sh_ 'grep -q "sched_proxy_exec=0" /proc/cmdline'; then
    cur=0
  else
    cur=1
  fi
  feature=$(sh_ 'if [ -r /proc/config.gz ]; then zcat /proc/config.gz 2>/dev/null | grep -q "^CONFIG_SCHED_PROXY_EXEC=y" && echo y || echo n; elif [ -r /boot/config-$(uname -r) ]; then grep -q "^CONFIG_SCHED_PROXY_EXEC=y" "/boot/config-$(uname -r)" && echo y || echo n; else echo unknown; fi' || echo unknown)
  if [ "$want" = 1 ]; then
    case "$feature" in
      n) die "guest kernel does not enable CONFIG_SCHED_PROXY_EXEC; cannot run eevdf_proxy_exec" ;;
      unknown) log "WARNING proxy-exec feature not exposed in guest config; relying on cmdline" ;;
    esac
  fi
  if [ "$cur" != "$want" ]; then
    log "switching guest kernel cmdline to sched_proxy_exec=$want via launch_cuttlefish.sh"
    if [ -n "${CF_HOME:-}" ] && [ -x "$ARTIFACT/scripts/launch_cuttlefish.sh" ]; then
      PROXY_EXEC=$want SERIAL=$SERIAL ADB=$ADB "$ARTIFACT/scripts/launch_cuttlefish.sh" >>"$LOG" 2>&1 || \
        die "failed to relaunch Cuttlefish with PROXY_EXEC=$want"
      relaunched=1
    else
      die "guest has sched_proxy_exec=$cur but policy requires sched_proxy_exec=$want (set CF_HOME so run_full_matrix_14.sh can relaunch Cuttlefish automatically)"
    fi
  fi
  if ! sh_ "grep -q 'sched_proxy_exec=$want' /proc/cmdline"; then
    die "guest cmdline does not contain sched_proxy_exec=$want"
  fi
  if [ "$relaunched" = 1 ]; then
    local got_nproc
    got_nproc=$(sh_ 'nproc')
    if [ -n "$got_nproc" ] && [ "$got_nproc" != "$CPUS" ]; then
      die "guest nproc=$got_nproc after relaunch, expected CPUS=$CPUS"
    fi
  fi
}

ALL_POLICIES=$POLICIES
NON_PROXY_POLICIES=$(printf '%s' "$ALL_POLICIES" | tr ',' '\n' | grep -v '^eevdf_proxy_exec$' | paste -sd, - || true)
HAS_PROXY_POLICY=$(printf '%s' "$ALL_POLICIES" | tr ',' '\n' | grep -x 'eevdf_proxy_exec' || true)

if [ -n "$NON_PROXY_POLICIES" ]; then
  ensure_guest_proxy_exec 0
  POLICIES=$NON_PROXY_POLICIES
  if [ "$SKIP_JANK" = 0 ]; then
    IFS=',' read -r -a JANK_ARR <<< "$JANK_TESTS"
    for bid in "${JANK_ARR[@]}"; do
      run_jank_test "$bid"
    done
  fi
  if [ "$SKIP_UI" = 0 ]; then
    run_uibench
  fi
fi

if [ -n "$HAS_PROXY_POLICY" ]; then
  ensure_guest_proxy_exec 1
  POLICIES=eevdf_proxy_exec
  if [ "$SKIP_JANK" = 0 ]; then
    IFS=',' read -r -a JANK_ARR <<< "$JANK_TESTS"
    for bid in "${JANK_ARR[@]}"; do
      run_jank_test "$bid"
    done
  fi
  if [ "$SKIP_UI" = 0 ]; then
    run_uibench
  fi
fi
POLICIES=$ALL_POLICIES

# The proxy phase validates only its own policy. Revalidate both workloads
# against the full policy set so the earlier non-proxy records are preserved.
if [ "$SKIP_JANK" = 0 ]; then
  IFS=',' read -r -a JANK_ARR <<< "$JANK_TESTS"
  for bid in "${JANK_ARR[@]}"; do
    slug=$(bench_slug "$bid")
    python3 "$PAPER/scripts/internal/validate_jankbench.py" "$OUT/jankbench/$slug" \
      --expected-runs "$RUNS" --run-count "$RUN_COUNT" --benchmark-id "$bid" \
      --policies "$ALL_POLICIES" >>"$LOG" 2>&1 || \
      log "final JankBench validation failed for $slug"
  done
fi
if [ "$SKIP_UI" = 0 ]; then
  python3 "$PAPER/scripts/internal/validate_uibench.py" "$OUT/uibench" \
    --policies "$ALL_POLICIES" --tests "$UI_TESTS" --reps "$RUNS" \
    >>"$LOG" 2>&1 || log "final UiBench validation failed"
fi

python3 "$PAPER/scripts/internal/aggregate_full_matrix.py" "$OUT" \
  --policies "$POLICIES" --runs "$RUNS" | tee "$OUT/final_summary.txt"
agg_rc=${PIPESTATUS[0]}
if [ "$agg_rc" = 0 ]; then
  touch "$OUT/FULL_MATRIX_DONE"
  log "full matrix done out=$OUT"
else
  log "full matrix incomplete: aggregate rc=$agg_rc"
  exit 1
fi
