# Setup and reproduction pipeline

This document takes a clean Cuttlefish host from a booted Android 17 image to
the full 14-test paper matrix and paper tables.

The target driver path is:

```text
artifact/scripts/run_full_matrix_14.sh
```

This docs branch does not yet contain the implementation scripts. They are
staged from the evaluation tree at artifact freeze; see
[`scripts/README.md`](scripts/README.md). Do not claim the pipeline is runnable
from this branch until that staging step is complete.

When staged, the driver performs per-run validity checks and reruns invalid
measurements. Do not replace it with the old `artifact/run_matrix.sh`, which
implements the retired five-policy/three-ID protocol.

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

The lab launcher is:

```text
docs/lit/launch_a17_618_nomod.sh
```

It is currently parameterized by paths inside the lab. Export or edit these
before using it elsewhere:

```bash
export CF_HOME=${CF_HOME:-/root/lab/cf-image/cf}
export KERNEL=${KERNEL:-/root/lab/build618/arch/x86/boot/bzImage}
export INITRAMFS=${INITRAMFS:-$HOME/work/cf-run/initramfs-618-nomod.img}
export SERIAL=${SERIAL:-0.0.0.0:6520}
```

Launch:

```bash
bash docs/lit/launch_a17_618_nomod.sh
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
SELinux: Permissive (lab image workaround)
```

`boot_completed=1` alone is not sufficient: the current full-matrix driver
also waits for `activity`, `system_server`, and SystemUI, and restarts zygote
once if the activity service is dead.

## 1a. vCPU configurations (4/8/16/32)

Use the parameterized launcher rather than editing the old lab script:

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

The full-matrix driver performs the freeze check and pushes the scheduler
binaries. The expected files are documented in `BUILD.md`.

At minimum, before starting:

```bash
ls -l \
  artifact/workloads/JankBench-platform-signed.apk \
  artifact/workloads/UiBench-app-debug.apk \
  artifact/workloads/UiBenchJankTests-debug.apk
sha256sum artifact/workloads/*.apk
```

For the fixed UiBench app, replace the app APK with:

```text
work/aosp-tests/uibench-dl/UiBench-app-fixed-debug.apk
sha256 f8a6405f156861cfe4409b6001b97b37eb389293d0e5375fffae422fe34c5db2
```

and record the new hash in the campaign freeze. Do not use the old UiBench
app for a strict 14/14 run; it NPEs in `bitmap-upload`.

The frozen scheduler is:

```text
5a2e7a7e5bca9a8cd0f9d25426e9766dae4ff4aaecfe22579a6a4d0371aab4c4
```

If the scheduler or baseline hash differs, the driver aborts before the first
measurement.

## 3. Smoke test

Run the smallest useful cell first:

```bash
cd artifact
SERIAL=0.0.0.0:6520 ADB=$HOME/bin/adb \
OUT=$PWD/results/smoke \
  bash scripts/run_full_matrix_14.sh \
    --out "$OUT" --runs 1 --jank-tests 0 --skip-uibench
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
  bash scripts/run_full_matrix_14.sh --out "$OUT"
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
bash scripts/run_full_matrix_14.sh --out "$OUT" --resume
```

Resume skips cells that already have 10 valid units. Do not edit or delete
validity records to make an incomplete cell look complete.

### What invalid means

JankBench is rejected on any failed evaluable J1–J12 check: wrong state/ops,
hash mismatch, missing database, wrong iteration set, frame-count floor, host
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
python3 scripts/integrate_full14.py "$OUT" \
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

Approximate lab numbers:

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
CF_HOME=${CF_HOME:-/root/lab/cf-image/cf}
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
