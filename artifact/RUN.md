# Setup and reproduction pipeline

## Quick start

```bash
cd /path/to/scx_fcp
export SCX_ROOT=/path/to/scx

./artifact/pipeline.sh build

# Put the workload APKs in artifact/workloads/.
export CF_HOME=/path/to/cuttlefish
export SERIAL=127.0.0.1:6520 # or 0.0.0.0:6520

./artifact/pipeline.sh launch
./artifact/pipeline.sh smoke
./artifact/pipeline.sh full --out results/full14
./artifact/pipeline.sh analyze results/full14 \
  --out-dir results/full14/integration
```

## Overview

This document covers launching Android 17 Cuttlefish across **4, 8, 16, and 32 vCPUs** (`Symmetric` and `Asymmetric` `446:1024` topologies) and running:
1. the 10-run multi-core scaling evaluation on `fcp_frame_bench` (Table 1);
2. the full 8-benchmark AOSP `JankBench` suite (`BM_ID=0,1,2,3,4,5,6,7`, Table 2);
3. the full 6-benchmark AOSP `UiBenchJankTests` suite (`6/6` passed, Table 3).

The public entrypoint is `artifact/pipeline.sh`:

```text
build | launch | smoke | full | resume | analyze
```

## 1. Environment

See [`SETUP.md`](SETUP.md) for Cuttlefish image, kernel, initramfs, and host
requirements. The run steps below start after those inputs exist.

## 2. Cuttlefish launch (4 / 8 / 16 / 32 vCPUs)

Required:

```text
Linux host with KVM
Android 17 Cuttlefish system image with sched_ext and BTF enabled
16 vCPUs, 8192 MB guest RAM
adb
```

The artifact launcher is:

```text
artifact/scripts/launch_cuttlefish.sh
```

It requires the Cuttlefish host, kernel, and initramfs paths:

```bash
export CF_HOME=/path/to/cuttlefish
export SERIAL=127.0.0.1:6520 # or 0.0.0.1:6520

# 4 vCPU
CPUS=4 MEMORY_MB=6144 bash artifact/scripts/launch_cuttlefish.sh

# 8 vCPU
CPUS=8 MEMORY_MB=6144 bash artifact/scripts/launch_cuttlefish.sh

# 16 vCPU
CPUS=16 MEMORY_MB=6144 bash artifact/scripts/launch_cuttlefish.sh

# 32 vCPU
CPUS=32 MEMORY_MB=8192 bash artifact/scripts/launch_cuttlefish.sh
```

TODO: This should be added to launch_cuttlefish.sh, shouldn't it?
The launcher passes `-cpus=N -memory_mb=M` to `launch_cvd` and waits for adb.
Then verify the guest framework and configure OpenGL ES + `animator_duration_scale 1`:

```bash
adb connect "$SERIAL"
adb -s "$SERIAL" root
adb -s "$SERIAL" shell setenforce 0

TODO: check these or not?
# adb -s "$SERIAL" shell setprop debug.hwui.renderer opengl
# adb -s "$SERIAL" shell settings put global animator_duration_scale 1

adb -s "$SERIAL" shell 'service check activity'
adb -s "$SERIAL" shell 'pidof system_server'
adb -s "$SERIAL" shell 'pidof com.android.systemui'
adb -s "$SERIAL" shell 'getprop sys.boot_completed'
adb -s "$SERIAL" shell 'cat /sys/kernel/sched_ext/state'
```

Expected:

```text
activity service: found
system_server and SystemUI: non-empty
boot_completed: 1
sched_ext state: disabled
SELinux: Permissive
```


## 3. Push the frozen binaries and APKs

TODO: This shoud be added to launch_cuttlefish.sh, shouldn't it?
The full-matrix driver pushes the scheduler binaries. Required files are in
`BUILD.md`.

Before starting:

```bash
ls -l \
  artifact/workloads/JankBench.apk \
  artifact/workloads/UiBench.apk \
  artifact/workloads/UiBenchJankTests.apk
```

Use the fixed UiBench app:

```text
artifact/workloads/UiBench.apk
```

The scheduler binaries are:

```text
artifact/build/bin/scx_*
```

## 4. Smoke test

Run the smallest useful cell first:

```bash
cd artifact
SERIAL=0.0.0.0:6520 ADB=$HOME/bin/adb \
  ./pipeline.sh smoke
```

Check:

```bash
cat "$OUT/full_matrix_status.json"
cat "$OUT/jankbench/list-view-fling/validity.json"
```

Do not start the full campaign until this cell is valid.

## 5. Multi-core scaling benchmark (`fcp_frame_bench`, 4 / 8 / 16 / 32 vCPUs, 10 Runs)

For each vCPU configuration (`C` in `{4, 8, 16, 32}`), run 10 iterations per scheduler using the following setup:

* **Background workers (`W_bg`):** `(3/4) * C` (corresponding to `3, 6, 12, 24` workers)
* **Big-core list (`C_big`):** `[C/2 .. C-1]`
* **Symmetric mode:** `renice 0` across all `crosvm_vcpu*` threads
* **Asymmetric mode:** `renice +10` on `crosvm_vcpu0` through `crosvm_vcpu(C/2 - 1)`, and `renice 0` on `crosvm_vcpu(C/2)` through `crosvm_vcpu(C - 1)`

```bash
# Attach scx_fcp (default) or scx_fcp_behavioral (zero-hint):
adb -s "$SERIAL" shell "cd /data/local/tmp/scx && nohup ./scx_fcp --big-cpus $BIG_LIST -f 11111 -s 0 >/dev/null 2>&1 &"

# Run 10 iterations of 90 Hz 4-stage fcp_frame_bench:
adb -s "$SERIAL" shell "
  for r in \$(seq 1 10); do
    /data/local/tmp/scx/fcp_frame_bench --scheduler scx_fcp_${CPUS}c_${TOPO} \
      --frames 60 --hz 90 --stages 4 --stage-us 1800 --bg-workers $BG_W
  done
"
```

## 6. Full 14-test AOSP matrix (8 JankBench + 6 UiBenchJankTests)

From the repository root:

```bash
cd artifact

export SERIAL=127.0.0.1:6520 # or 0.0.0.0:6520
export ADB=${ADB:-adb}
export OUT=$PWD/results/full14_$(date +%Y%m%d-%H%M%S)

# After the fixed UiBench APK is installed/recorded (use "none", not empty:
# the current driver treats an unset/empty value as the old bitmap allowance):
export UI_ALLOW_INVALID=none

RUNS=10 RUN_COUNT=4 WARMUP_ITERATIONS=1 \
  ./pipeline.sh full --out "$OUT"
```

Defaults:

```text
policies:  eevdf, eevdf w/ proxy_exec, scx_fcp, scx_fcp_behavioral, scx_p2dq_pelt_off, scx_lavd
JankBench: all 8 benchmark IDs (0,1,2,3,4,5,6,7)
UiBench:   all 6 tests (bitmap-upload, shadow-grid-list, inflating-list,
           slow-bind-recycler, trivial-recycler, layout-cache-low)
Runs:      10 valid runs/reps per (test, policy)
```

`UI_ALLOW_INVALID=none` is important for the final run: it makes
`bitmap-upload` required instead of silently “known-invalid”. Use the fixed
UiBench APK first. (Do not export an empty value: the current shell default
would restore `bitmap-upload`.)

### Resume

The driver supports cell-level resume:

```bash
./pipeline.sh resume --out "$OUT"
```

Resume skips cells that already have 10 valid units. Do not edit or delete
validity records to make an incomplete cell look complete.

### What invalid means

JankBench is rejected on any failed evaluable J1–J12 check: wrong state/ops,
missing database, wrong iteration set, frame-count floor, host
or guest load gate, process changes, sleep events, logcat crash/ANR, or boot
change.

UiBench is rejected when any required test/rep row is invalid: instrumentation
failure, missing GfxMonitor summary, too few frames, missing percentile/jank
keys, wrong foreground/SystemUI state, wrong sched_ext state, or a fatal
logcat event.

Invalid attempts are moved to `_invalid/` and rerun. They remain visible in
validity logs; they are never pooled.

## 7. Aggregate and verify

```bash
cd /path/to/scx_fcp/artifact
./pipeline.sh analyze "$OUT" \
  --runs 10 --require-known-invalid \
  --out-dir "$OUT/integration"
```

The integration step must report:

```text
14 tests
4 policies
56/56 required cells
10 valid units per cell
```

It writes:

```text
full14_summary.json
full14_summary.md
full14_jankbench.tex
full14_uibench.tex
full14_website.html
```

## 8. Cleanup

Stop schedulers and the device:

```bash
adb -s "$SERIAL" shell 'pidof scx_fcp_v_a_depth3_tgid1ms' \
  | xargs -r adb -s "$SERIAL" shell kill -TERM
adb -s "$SERIAL" shell 'cat /sys/kernel/sched_ext/state'
CF_HOME=${CF_HOME:?set the Cuttlefish host directory}
HOME="$CF_HOME" "$CF_HOME/bin/stop_cvd"
```

## 10. Known limitations

- UiBench `bitmap-upload` requires the fixed app APK; the frozen AOSP app
  crashes before the first frame.
- JankBench IDs 3 and 4 currently use the same 80% hit-rate adapter in the
  frozen tree; they are a repeatability pair until the JankBench source patch
  is applied.
- Windows-side CPU/crosvm monitoring is not joined to the run; `J10b` is
  reported as unverifiable, not silently passed.
- The physical SA8155P board is outside this artifact.
