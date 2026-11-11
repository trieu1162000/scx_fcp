# Reproduce scx_fcp

This is the short end-to-end sequence. Detailed prerequisites, APK builds, and
troubleshooting are in [`BUILD.md`](BUILD.md) and [`RUN.md`](RUN.md).

## 1. Build

```bash
cd /path/to/scx_fcp
export SCX_ROOT=${SCX_ROOT:-$HOME/workspace/scx}
./artifact/pipeline.sh build
```

Build/obtain the workload APKs and put them in
`artifact/workloads/`:

```text
JankBench-platform-signed.apk
UiBench-app-debug.apk          # fixed bitmap-upload build
UiBenchJankTests-debug.apk
```

Use the fixed UiBench app for `bitmap-upload`.

## 2. Boot Cuttlefish

Set `CF_HOME`, `KERNEL`, `INITRAMFS`, and `SERIAL`, then:

```bash
bash docs/lit/launch_a17_618_nomod.sh
adb connect "$SERIAL"
adb -s "$SERIAL" root
adb -s "$SERIAL" shell setenforce 0
adb -s "$SERIAL" shell 'service check activity'
adb -s "$SERIAL" shell 'pidof system_server'
adb -s "$SERIAL" shell 'cat /sys/kernel/sched_ext/state'
```

Require a ready activity service and `sched_ext state=disabled`; do not trust
`sys.boot_completed=1` alone.

## 3. Run the full matrix

```bash
cd artifact
export SERIAL=0.0.0.0:6520
export ADB=${ADB:-$HOME/bin/adb}
export OUT=$PWD/results/full14_5a2e_$(date +%Y%m%d-%H%M%S)
# Use "none" for a strict run with the fixed UiBench APK. An empty value is
# treated as unset by the current driver and restores the old allowance.
export UI_ALLOW_INVALID=none

RUNS=10 RUN_COUNT=4 WARMUP_ITERATIONS=1 \
  ./pipeline.sh full --out "$OUT"
```

The scheduler binary is:

```text
artifact/build/bin/scx_fcp_behavioral
```

## 4. Integrate and verify

From the artifact root:

```bash
cd /path/to/scx_fcp/artifact
./pipeline.sh analyze "$OUT" \
  --runs 10 --require-known-invalid \
  --out-dir "$OUT/integration"
```

Require:

```text
14 tests x 4 policies = 56/56 cells
10 valid units per cell
```

Invalid attempts are archived, not aggregated. A nonzero exit means the
campaign is not a paper result.

## 5. Stop

```bash
CF_HOME=${CF_HOME:?set the Cuttlefish host directory}
HOME="$CF_HOME" "$CF_HOME/bin/stop_cvd"
```
