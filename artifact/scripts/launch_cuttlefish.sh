#!/usr/bin/env bash
# Launch the Android Cuttlefish image with a selected vCPU count, wait for
# full Android framework readiness, configure guest rendering/animation
# properties, and stage built scheduler binaries and workload APKs.
#
# Required:
#   CF_HOME        Cuttlefish host directory (contains bin/launch_cvd)
#
# Optional:
#   KERNEL         optional override kernel path
#   INITRAMFS      optional override initramfs path
#   CPUS           4, 8, 16, or 32 (default 16)
#   MEMORY_MB      guest RAM in MiB (default: 4096/6144/6144/8192 by CPUS)
#   SERIAL         adb serial (default 0.0.0.0:6520)
#   PROXY_EXEC     kernel cmdline sched_proxy_exec=0|1 (default 0; set 1 for EEVDF w/ ProxyExec)
#   TOPO           host vCPU capacity shaping: asym (default) or sym
#   STOP_EXISTING  stop any existing cvd instance (default 1)
#   STAGE_DEVICE   push built binaries & install APKs if present (default 1)
#   BIN_DIR        host directory with built scheduler binaries
#   WORKLOADS_DIR  host directory with JankBench/UiBench APKs
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ARTIFACT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
REPO_ROOT=$(cd "$ARTIFACT_DIR/.." && pwd)
[ -f "$ARTIFACT_DIR/config.env" ] && . "$ARTIFACT_DIR/config.env"

: "${CF_HOME:?set CF_HOME in artifact/config.env or environment}"

CPUS=${CPUS:-16}
case "$CPUS" in
  4)  DEFAULT_MEMORY=4096 ;;
  8)  DEFAULT_MEMORY=6144 ;;
  16) DEFAULT_MEMORY=6144 ;;
  32) DEFAULT_MEMORY=8192 ;;
  *) echo "CPUS must be 4, 8, 16, or 32 (got $CPUS)" >&2; exit 2 ;;
esac
MEMORY_MB=${MEMORY_MB:-$DEFAULT_MEMORY}
SERIAL=${SERIAL:-0.0.0.0:6520}
PROXY_EXEC=${PROXY_EXEC:-0}
STOP_EXISTING=${STOP_EXISTING:-1}
STAGE_DEVICE=${STAGE_DEVICE:-1}
ADB=${ADB:-"$CF_HOME/bin/adb"}
if [ ! -x "$ADB" ]; then
  ADB=$(command -v adb)
fi
BIN_DIR=${BIN_DIR:-"$ARTIFACT_DIR/build/bin"}
if [ ! -d "$BIN_DIR" ] && [ -d "$REPO_ROOT/build/bin" ]; then
  BIN_DIR="$REPO_ROOT/build/bin"
fi
WORKLOADS_DIR=${WORKLOADS_DIR:-"$ARTIFACT_DIR/workloads"}
REMOTE_DIR=${REMOTE_DIR:-/data/local/tmp/scx}
LOG="$CF_HOME/launch_a17_${CPUS}vcpu.log"

KERNEL_ARGS=()
if [ -n "${KERNEL:-}" ] || [ -n "${INITRAMFS:-}" ]; then
  : "${KERNEL:?set KERNEL when INITRAMFS is set}"
  : "${INITRAMFS:?set INITRAMFS when KERNEL is set}"
  KERNEL_ARGS=(-kernel_path "$KERNEL" -initramfs_path "$INITRAMFS")
fi

sh_() {
  "$ADB" -s "$SERIAL" shell "$@" 2>/dev/null | tr -d '\r'
}

if [ "$STOP_EXISTING" = 1 ]; then
  HOME="$CF_HOME" "$CF_HOME/bin/stop_cvd" >/dev/null 2>&1 || true
  pkill -f run_cvd 2>/dev/null || true
  pkill -f crosvm 2>/dev/null || true
  sleep 2
fi

for dev in /dev/kvm /dev/vhost-vsock /dev/vhost-net /dev/net/tun; do
  if [ ! -e "$dev" ] || ! (exec 3<>"$dev") 2>/dev/null; then
    echo "error: host virtualization device $dev is missing or inaccessible (ensure KVM, vhost_vsock, and cuttlefish-base are active on the host)" >&2
    exit 1
  fi
done

cd "$CF_HOME"
HOME="$CF_HOME" \
ANDROID_HOST_OUT="$CF_HOME" \
ANDROID_PRODUCT_OUT="$CF_HOME" \
nohup "$CF_HOME/bin/launch_cvd" \
  -system_image_dir="$CF_HOME" \
  "${KERNEL_ARGS[@]}" \
  -extra_kernel_cmdline "sched_proxy_exec=$PROXY_EXEC" \
  -gpu_mode=guest_swiftshader \
  -guest_hwui_renderer=skiagl \
  -guest_enforce_security=false \
  -enable_sandbox=false \
  -boot_slot=a \
  -enable_kernel_log=true \
  -cpus="$CPUS" \
  -memory_mb="$MEMORY_MB" \
  -daemon \
  -report_anonymous_usage_stats=n >"$LOG" 2>&1 &

echo "launched cpus=$CPUS memory_mb=$MEMORY_MB serial=$SERIAL log=$LOG"

# 1. Wait for ADB transport
for _ in $(seq 1 120); do
  if "$ADB" connect "$SERIAL" >/dev/null 2>&1 &&
     [ "$("$ADB" -s "$SERIAL" get-state 2>/dev/null | tr -d '\r')" = device ]; then
    break
  fi
  sleep 2
done
[ "$("$ADB" -s "$SERIAL" get-state 2>/dev/null | tr -d '\r')" = device ] || {
  echo "device did not connect over adb; see $LOG" >&2
  exit 1
}

# 2. Switch adbd to root and wait for sys.boot_completed=1 + framework services
"$ADB" -s "$SERIAL" root >/dev/null 2>&1 || true
sleep 2
"$ADB" connect "$SERIAL" >/dev/null 2>&1 || true

READY=0
for _ in $(seq 1 150); do
  if [ "$(sh_ 'getprop sys.boot_completed')" = "1" ] &&
     sh_ 'service check activity' | grep -q 'found' &&
     [ -n "$(sh_ 'pidof system_server')" ] &&
     [ -n "$(sh_ 'pidof com.android.systemui')" ]; then
    READY=1
    break
  fi
  sleep 2
done
[ "$READY" = 1 ] || {
  echo "guest framework services did not become ready; see $LOG" >&2
  exit 1
}

# 3. Configure SELinux Permissive, OpenGL ES HWUI, animator_duration_scale=1, and stay-awake
sh_ 'setenforce 0' >/dev/null 2>&1 || true
sh_ 'setprop debug.hwui.renderer opengl' >/dev/null 2>&1 || true
sh_ 'settings put global window_animation_scale 0' >/dev/null 2>&1 || true
sh_ 'settings put global transition_animation_scale 0' >/dev/null 2>&1 || true
sh_ 'settings put global animator_duration_scale 1' >/dev/null 2>&1 || true
sh_ 'svc power stayon true' >/dev/null 2>&1 || true
sh_ 'settings put global stay_on_while_plugged_in 7' >/dev/null 2>&1 || true
sh_ 'wm dismiss-keyguard' >/dev/null 2>&1 || true

# 4. Stage built scheduler binaries and workload APKs if present
if [ "$STAGE_DEVICE" = 1 ]; then
  sh_ "mkdir -p '$REMOTE_DIR' '$REMOTE_DIR/stats'" >/dev/null 2>&1 || true
  if [ -d "$BIN_DIR" ]; then
    for bin in "$BIN_DIR"/scx_* "$BIN_DIR"/fcp_frame_bench; do
      [ -f "$bin" ] || continue
      "$ADB" -s "$SERIAL" push "$bin" "$REMOTE_DIR/$(basename "$bin")" >/dev/null
      sh_ "chmod 755 '$REMOTE_DIR/$(basename "$bin")'" >/dev/null 2>&1 || true
      echo "staged binary: $REMOTE_DIR/$(basename "$bin")"
    done
    # Also create the legacy alias expected by run_full_matrix_14.sh when scx_fcp_behavioral is present
    if [ -f "$BIN_DIR/scx_fcp_behavioral" ]; then
      sh_ "cp -f '$REMOTE_DIR/scx_fcp_behavioral' '$REMOTE_DIR/scx_fcp_v_a_depth3_tgid1ms' && chmod 755 '$REMOTE_DIR/scx_fcp_v_a_depth3_tgid1ms'" >/dev/null 2>&1 || true
    fi
  fi
  if [ -d "$WORKLOADS_DIR" ]; then
    for apk in "$WORKLOADS_DIR"/*.apk; do
      [ -f "$apk" ] || continue
      "$ADB" -s "$SERIAL" install -r -g "$apk" >/dev/null 2>&1 && \
        echo "installed workload APK: $(basename "$apk")" || \
        echo "warning: could not install $(basename "$apk")" >&2
    done
  fi
fi

# 5. Apply optional host vCPU capacity shaping (TOPO=asym sets nice +10 on LITTLE half 0..CPUS/2-1)
TOPO=${TOPO:-asym}
HALF=$((CPUS / 2))
for tid in $(/bin/ps -L -o tid=,comm= -C crosvm 2>/dev/null | awk '/crosvm_vcpu/ {print $1}'); do
  renice -n 0 -p "$tid" >/dev/null 2>&1 || sudo -n renice -n 0 -p "$tid" >/dev/null 2>&1 || true
done
if [ "$TOPO" = "asym" ]; then
  for v in $(seq 0 $((HALF - 1))); do
    for tid in $(/bin/ps -L -o tid=,comm= -C crosvm 2>/dev/null | awk -v c="crosvm_vcpu$v" '$2 == c {print $1}'); do
      renice -n 10 -p "$tid" >/dev/null 2>&1 || sudo -n renice -n 10 -p "$tid" >/dev/null 2>&1 || true
    done
  done
fi

echo "device ready: $SERIAL"
echo "  kernel:          $(sh_ 'uname -r') ($(sh_ 'nproc') vCPUs)"
echo "  activity:        $(sh_ 'service check activity')"
echo "  system_server:   $(sh_ 'pidof system_server')"
echo "  systemui:        $(sh_ 'pidof com.android.systemui')"
echo "  boot_completed:  $(sh_ 'getprop sys.boot_completed')"
echo "  sched_ext state: $(sh_ 'cat /sys/kernel/sched_ext/state 2>/dev/null')"
echo "  SELinux:         $(sh_ 'getenforce')"
echo "  proxy_exec:      sched_proxy_exec=$PROXY_EXEC (topology=$TOPO)"
