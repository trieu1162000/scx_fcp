#!/usr/bin/env bash
# Prepare an already-booted physical ARM64 Android device for benchmark runs.
# It stages binaries/APKs and sets SELinux Permissive; it does not reboot.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ARTIFACT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
[ -f "$ARTIFACT_DIR/config.env" ] && . "$ARTIFACT_DIR/config.env"

[ "${PLATFORM:-}" = arm64 ] || { echo "launch_device.sh requires PLATFORM=arm64" >&2; exit 2; }
export ANDROID_ADB_SERVER_PORT=${ANDROID_ADB_SERVER_PORT:-5037}
SERIAL=${SERIAL:?set SERIAL to the physical adb serial}
BIN_DIR=${BIN_DIR:?set BIN_DIR to the ARM64 scheduler binary directory}
AOSP_OUT=${AOSP_OUT-}
ADB=${ADB:-adb}
command -v "$ADB" >/dev/null 2>&1 || { echo "adb not found: $ADB" >&2; exit 1; }
[ -d "$BIN_DIR" ] || { echo "missing BIN_DIR: $BIN_DIR" >&2; exit 1; }
command -v file >/dev/null 2>&1 || { echo "file command is required to verify ARM64 binaries" >&2; exit 1; }
[ -f "$BIN_DIR/scx_fcp" ] || { echo "missing scheduler binary: $BIN_DIR/scx_fcp" >&2; exit 1; }

WORKLOADS_DIR=${WORKLOADS_DIR:-"$ARTIFACT_DIR/workloads/arm64"}
REMOTE_DIR=${REMOTE_DIR:-/data/local/tmp/scx}
A=("$ADB" -P "$ANDROID_ADB_SERVER_PORT" -s "$SERIAL")

die() { echo "error: $*" >&2; exit 1; }

WORKLOAD_APKS=(
  "$WORKLOADS_DIR/JankBench-platform-signed.apk"
  "$WORKLOADS_DIR/UiBench-app-debug.apk"
  "$WORKLOADS_DIR/UiBenchJankTests-debug.apk"
)
AOSP_APKS=()
if [ -n "$AOSP_OUT" ]; then
  AOSP_APKS=(
    "$AOSP_OUT/testcases/JankBench/arm64/JankBench.apk"
    "$AOSP_OUT/testcases/UiBench/arm64/UiBench.apk"
    "$AOSP_OUT/testcases/UiBenchJankTests/arm64/UiBenchJankTests.apk"
  )
  for apk in "${AOSP_APKS[@]}"; do
    [ -f "$apk" ] || die "missing AOSP APK: $apk"
  done
fi

shopt -s nullglob
BINARIES=("$BIN_DIR"/scx_* "$BIN_DIR"/fcp_frame_bench "$BIN_DIR"/schbench)
[ -f "$BIN_DIR/scx_fcp" ] || die "missing scheduler binary: $BIN_DIR/scx_fcp"
for bin in "${BINARIES[@]}"; do
  [ -f "$bin" ] || continue
  description=$(file -b "$bin")
  case "$description" in
    *aarch64*) ;;
    *) die "not an ARM64 binary: $bin ($description)" ;;
  esac
done

maybe_connect() {
  case "$SERIAL" in
    *:*) "$ADB" -P "$ANDROID_ADB_SERVER_PORT" connect "$SERIAL" >/dev/null 2>&1 || true ;;
  esac
}

maybe_connect
"${A[@]}" get-state >/dev/null 2>&1 || die "device $SERIAL is not reachable on ADB port $ANDROID_ADB_SERVER_PORT"

USE_SU=0
RAW_UID=$("${A[@]}" shell id -u 2>/dev/null | tr -d '\r' || true)
if [ "$RAW_UID" != "0" ]; then
  SU_UID=$("${A[@]}" shell "su 0 sh -c 'id -u'" 2>/dev/null | tr -d '\r' || true)
  if [ "$SU_UID" = "0" ]; then
    USE_SU=1
  else
    "${A[@]}" root >/dev/null 2>&1 || true
    for _ in $(seq 1 60); do
      maybe_connect
      [ "$("${A[@]}" get-state 2>/dev/null | tr -d '\r')" = device ] && break
      sleep 1
    done
    [ "$("${A[@]}" get-state 2>/dev/null | tr -d '\r')" = device ] || die "device did not return after adb root"
  fi
fi

rsh() {
  if [ "$USE_SU" = 1 ]; then
    local q
    printf -v q '%q' "export PATH=/product/bin:/apex/com.android.runtime/bin:/apex/com.android.art/bin:/system_ext/bin:/system/bin:/system/xbin:/odm/bin:/vendor/bin:/vendor/xbin:\$PATH; $*"
    "${A[@]}" shell "su 0 sh -c $q"
  else
    "${A[@]}" shell "$@"
  fi
}

BOOT_COMPLETED=0
for _ in $(seq 1 60); do
  if [ "$(rsh getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ]; then
    BOOT_COMPLETED=1
    break
  fi
  sleep 1
done
[ "$BOOT_COMPLETED" = 1 ] || die "Android framework did not finish booting"
DEVICE_UID=$(rsh id -u 2>/dev/null | tr -d '\r')
[ "$DEVICE_UID" = 0 ] || die "root adb shell required to run sched_ext; enable adb root or su 0 and retry"
DEVICE_ARCH=$(rsh uname -m 2>/dev/null | tr -d '\r')
[ "$DEVICE_ARCH" = aarch64 ] || die "expected an ARM64 device, got $DEVICE_ARCH"
DEVICE_ABI=$(rsh getprop ro.product.cpu.abi 2>/dev/null | tr -d '\r')
case "$DEVICE_ABI" in
  arm64*) ;;
  *) die "expected an ARM64 Android ABI, got $DEVICE_ABI" ;;
esac
SCHED_EXT_STATE=$(rsh 'cat /sys/kernel/sched_ext/state 2>/dev/null' 2>/dev/null | tr -d '\r' || true)
[ -n "$SCHED_EXT_STATE" ] || die "kernel does not expose /sys/kernel/sched_ext/state"
VMLINUX_BTF=$(rsh '[ -r /sys/kernel/btf/vmlinux ] && echo yes' 2>/dev/null | tr -d '\r' || true)
[ "$VMLINUX_BTF" = yes ] || die "kernel BTF is unavailable at /sys/kernel/btf/vmlinux"
mkdir -p "$WORKLOADS_DIR"
if [ -n "$AOSP_OUT" ]; then
  for i in "${!AOSP_APKS[@]}"; do
    cp -f "${AOSP_APKS[$i]}" "${WORKLOAD_APKS[$i]}"
    echo "copied AOSP workload APK: ${WORKLOAD_APKS[$i]}"
  done
else
  for apk in "${WORKLOAD_APKS[@]}"; do
    [ -f "$apk" ] || die "missing workload APK $apk; set AOSP_OUT or copy APKs into $WORKLOADS_DIR"
  done
fi

rsh "mkdir -p '$REMOTE_DIR' '$REMOTE_DIR/stats' && chmod 777 '$REMOTE_DIR' '$REMOTE_DIR/stats'"
for bin in "${BINARIES[@]}"; do
  [ -f "$bin" ] || continue
  name=$(basename "$bin")
  l_sha=$(sha256sum "$bin" | awk '{print $1}')
  r_sha=$(rsh "[ -x '$REMOTE_DIR/$name' ] && sha256sum '$REMOTE_DIR/$name' | awk '{print \$1}' || true" 2>/dev/null | tr -d '\r')
  if [ "$l_sha" != "$r_sha" ]; then
    rsh "rm -f '$REMOTE_DIR/$name'"
    "${A[@]}" push "$bin" "$REMOTE_DIR/$name" >/dev/null
    rsh "chmod 755 '$REMOTE_DIR/$name'" >/dev/null
    echo "staged ARM64 binary: $REMOTE_DIR/$name"
  else
    echo "verified ARM64 binary up-to-date: $REMOTE_DIR/$name"
  fi
done

install_apk_if_needed() {
  local apk=$1 pkg=$2
  local cur_user
  cur_user=$(rsh am get-current-user 2>/dev/null | tr -d '\r' || echo 0)
  local existing
  existing=$("${A[@]}" shell pm path --user "$cur_user" "$pkg" 2>/dev/null | tr -d '\r' || true)
  if [ -n "$existing" ] && [ "${FORCE_INSTALL_APKS:-0}" != 1 ]; then
    echo "verified workload APK already installed ($pkg): $(basename "$apk")"
    return 0
  fi
  local -a flags=(-r -g -t --bypass-low-target-sdk-block)
  local output
  if output=$("${A[@]}" install "${flags[@]}" "$apk" 2>&1); then
    echo "installed workload APK: $(basename "$apk")"
  else
    printf '%s\n' "$output" >&2
    die "could not install $apk"
  fi
}
install_apk_if_needed "${WORKLOAD_APKS[0]}" com.android.benchmark
install_apk_if_needed "${WORKLOAD_APKS[1]}" com.android.test.uibench
install_apk_if_needed "${WORKLOAD_APKS[2]}" com.android.uibench.janktests

rsh setenforce 0 >/dev/null 2>&1 || die "failed to set SELinux Permissive"
SELINUX=$(rsh getenforce 2>/dev/null | tr -d '\r' || true)
[ "$SELINUX" = Permissive ] || die "expected SELinux Permissive, got ${SELINUX:-unknown}"
echo "device ready: $SERIAL"
echo "  abi:             $DEVICE_ABI"
DEVICE_KERNEL=$("${A[@]}" shell uname -r 2>/dev/null | tr -d '\r')
echo "  kernel:          $DEVICE_KERNEL"
echo "  sched_ext state: $SCHED_EXT_STATE"
echo "  SELinux:         ${SELINUX:-unknown}"
echo "  binaries:        $BIN_DIR"
echo "  workloads:       $WORKLOADS_DIR"
