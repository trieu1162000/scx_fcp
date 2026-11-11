# Setup and reproduction pipeline

This document takes a clean Cuttlefish host from a booted Android 17 image to
the full 14-test paper matrix and paper tables.

The public entrypoint is:

```text
artifact/pipeline.sh
```

It exposes one compact interface:

```text
build | launch | smoke | full | resume | analyze
```

The per-benchmark adapters stay under `artifact/scripts/internal/`; they are
not part of the user-facing interface.

## 1. Cuttlefish prerequisites

Required:

```text
Linux host with KVM
Android 17 Cuttlefish system image
Linux 6.18 kernel with CONFIG_SCHED_CLASS_EXT=y and CONFIG_DEBUG_INFO_BTF=y
matching initramfs
16 vCPUs, 6144 MB guest RAM
adb
```

The artifact launcher is:

```text
artifact/scripts/launch_cuttlefish.sh
```

It requires the Cuttlefish host, kernel, and initramfs paths:

```bash
export CF_HOME=${CF_HOME:?set the Cuttlefish host directory}
export KERNEL=${KERNEL:?set the Linux 6.18 bzImage}
export INITRAMFS=${INITRAMFS:?set the matching initramfs}
export SERIAL=${SERIAL:-0.0.0.0:6520}
```

Launch:

```bash
CPUS=16 MEMORY_MB=6144 artifact/scripts/launch_cuttlefish.sh
```

Then verify the framework, not only `sys.boot_completed`:

```bash
adb connect "$SERIAL"
adb -s "$SERIAL" root
adb -s "$SERIAL" shell setenforce 0
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

`boot_completed=1` alone is not sufficient: the current full-matrix driver
also waits for `activity`, `system_server`, and SystemUI, and restarts zygote
once if the activity service is dead.

## 1a. vCPU configurations (4/8/16/32)

Use the parameterized launcher:

```bash
export CF_HOME=/path/to/cuttlefish
export KERNEL=/path/to/bzImage
export INITRAMFS=/path/to/initramfs.img
export SERIAL=0.0.0.0:6520

# 4 vCPU
CPUS=4 MEMORY_MB=4096 \
  bash artifact/scripts/launch_cuttlefish.sh

# 8 vCPU
CPUS=8 MEMORY_MB=6144 \
  bash artifact/scripts/launch_cuttlefish.sh

# 16 vCPU (paper configuration)
CPUS=16 MEMORY_MB=6144 \
  bash artifact/scripts/launch_cuttlefish.sh

# 32 vCPU (functional/topology runs only on this host)
CPUS=32 MEMORY_MB=8192 \
  bash artifact/scripts/launch_cuttlefish.sh
```

The launcher passes `-cpus=N -memory_mb=M` to `launch_cvd` and waits for adb.

Host rules:

```text
4/8 vCPU   smoke and scaling experiments; host can stay valid
16 vCPU    paper configuration; requires at least 16 host threads
32 vCPU    requires >=32 host threads; on the current 8C/16T host it is
           oversubscribed and its performance numbers are not valid
```

The current strict full-matrix validator expects:

```text
nproc = 16
```

So only 16-vCPU runs are the paper matrix. A 4/8/32-vCPU launch is useful for
functional checks or scaling, but the validator/expected-output contract must
be updated before treating those numbers as paper results. Do not mix vCPU
counts in one comparison.

## 2. Push the frozen binaries and APKs

The full-matrix driver pushes the scheduler binaries. Required files are in
`BUILD.md`.

Before starting:

```bash
ls -l \
  artifact/workloads/JankBench-platform-signed.apk \
  artifact/workloads/UiBench-app-debug.apk \
  artifact/workloads/UiBenchJankTests-debug.apk
```

Use the fixed UiBench app:

```text
artifact/workloads/UiBench-app-debug.apk
```

The scheduler binary is:

```text
artifact/build/bin/scx_fcp_behavioral
```

## 3. Smoke test

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

## 4. Full matrix

From the repository root:

```bash
cd artifact

export SERIAL=0.0.0.0:6520
export ADB=${ADB:-$HOME/bin/adb}
export OUT=$PWD/results/full14_5a2e_$(date +%Y%m%d-%H%M%S)

# After the fixed UiBench APK is installed/recorded (use "none", not empty:
# the current driver treats an unset/empty value as the old bitmap allowance):
export UI_ALLOW_INVALID=none

RUNS=10 RUN_COUNT=4 WARMUP_ITERATIONS=1 \
  ./pipeline.sh full --out "$OUT"
```

Defaults:

```text
policies: eevdf, scx_fcp_v_a_depth3_tgid1ms, scx_p2dq_pelt_off, scx_lavd
JankBench: IDs 0..7
UiBench: 6 tests
10 valid runs/reps per (test, policy)
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

## 5. Aggregate and check expected results

From the artifact root:

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

If the strict command exits nonzero, the matrix is not a paper result.

## 6. Runtime and space

Approximate runtime:

```text
JankBench: ~3 min per run; 8 IDs x 4 policies x 10 runs
UiBench:   several minutes per block; 6 tests x 10 reps x 4 policies
Raw data:  tens of GB depending on retained logcat/gfxinfo databases
```

Plan for an overnight campaign and keep raw data. Do not delete invalid or
failed runs from the archive; they are part of the validity record.

## 7. Cleanup

Stop schedulers and the device:

```bash
adb -s "$SERIAL" shell 'pidof scx_fcp_v_a_depth3_tgid1ms' \
  | xargs -r adb -s "$SERIAL" shell kill -TERM
adb -s "$SERIAL" shell 'cat /sys/kernel/sched_ext/state'
CF_HOME=${CF_HOME:?set the Cuttlefish host directory}
HOME="$CF_HOME" "$CF_HOME/bin/stop_cvd"
```

## 8. Known limitations

- UiBench `bitmap-upload` requires the fixed app APK; the frozen AOSP app
  crashes before the first frame.
- JankBench IDs 3 and 4 currently use the same 80% hit-rate adapter in the
  frozen tree; they are a repeatability pair until the JankBench source patch
  is applied.
- Windows-side CPU/crosvm monitoring is not joined to the run; `J10b` is
  reported as unverifiable, not silently passed.
- The physical SA8155P board is outside this artifact.
