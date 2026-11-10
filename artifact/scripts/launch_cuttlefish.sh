#!/usr/bin/env bash
# Launch the Android 17 / Linux 6.18 Cuttlefish image with a selected vCPU count.
#
# Required:
#   CF_HOME       Cuttlefish host directory (contains bin/launch_cvd)
#   KERNEL        path to bzImage
#   INITRAMFS     path to the matching initramfs
#
# Optional:
#   CPUS          4, 8, 16, or 32 (default 16)
#   MEMORY_MB     guest RAM in MiB (default: 4096/6144/6144/8192 by CPUS)
#   SERIAL        adb serial (default 0.0.0.0:6520)
#   STOP_EXISTING stop any existing cvd-1 instance (default 1)
set -euo pipefail

: "${CF_HOME:?set CF_HOME to the Cuttlefish host directory}"
: "${KERNEL:?set KERNEL to the Linux 6.18 bzImage}"
: "${INITRAMFS:?set INITRAMFS to the matching initramfs}"

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
STOP_EXISTING=${STOP_EXISTING:-1}
ADB=${ADB:-"$CF_HOME/bin/adb"}
LOG="$CF_HOME/launch_a17_${CPUS}vcpu.log"

if [ "$STOP_EXISTING" = 1 ]; then
  HOME="$CF_HOME" "$CF_HOME/bin/stop_cvd" >/dev/null 2>&1 || true
  pkill -f run_cvd 2>/dev/null || true
  pkill -f crosvm 2>/dev/null || true
  sleep 2
fi

cd "$CF_HOME"
HOME="$CF_HOME" nohup "$CF_HOME/bin/launch_cvd" \
  -system_image_dir="$CF_HOME" \
  -kernel_path="$KERNEL" \
  -initramfs_path="$INITRAMFS" \
  -extra_kernel_cmdline "sched_proxy_exec=0" \
  -enable_sandbox=false \
  -boot_slot=a \
  -enable_kernel_log=true \
  -cpus="$CPUS" \
  -memory_mb="$MEMORY_MB" \
  -daemon \
  -report_anonymous_usage_stats=n >"$LOG" 2>&1 &

echo "launched cpus=$CPUS memory_mb=$MEMORY_MB serial=$SERIAL log=$LOG"
for _ in $(seq 1 120); do
  if "$ADB" connect "$SERIAL" >/dev/null 2>&1 &&
     [ "$("$ADB" -s "$SERIAL" get-state 2>/dev/null | tr -d '\r')" = device ]; then
    echo "device ready: $SERIAL"
    "$ADB" -s "$SERIAL" shell 'uname -r; nproc; getprop sys.boot_completed; cat /sys/kernel/sched_ext/state 2>/dev/null; cat /sys/kernel/sched_ext/root/ops 2>/dev/null'
    exit 0
  fi
  sleep 2
done

echo "device did not become ready; see $LOG" >&2
exit 1
