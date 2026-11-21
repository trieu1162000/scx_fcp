# Setup and reproduction pipeline

## Quick start

Configure [`artifact/config.env`](config.env) once (`SCX_ROOT`, `CF_HOME`, `SERIAL`, `CPUS`), place the workload APKs in `artifact/workloads/` (or `artifact/workloads/arm64/` for the physical device), and run:

```bash
./artifact/pipeline.sh build
./artifact/pipeline.sh launch
./artifact/pipeline.sh smoke
./artifact/pipeline.sh full --out artifact/results/full14
./artifact/pipeline.sh analyze artifact/results/full14 \
  --out-dir artifact/results/full14/integration
```

### Physical SA8155P / SCX 6.1

Use the full matrix for all 8 JankBench IDs and 6 UiBench tests, with 10 runs/reps per policy. The device policy set excludes ProxyExec.

```bash
source artifact/sa8155.env
./artifact/pipeline.sh build
OUT="$PWD/artifact/results/full14_sa8155_$(date +%Y%m%d-%H%M%S)"
./artifact/pipeline.sh full --out "$OUT"
```
On ARM64, each full-matrix reboot waits 60 seconds, then requires `sys.boot_completed=1` and framework services. It does not require a specific launcher package or wait for host/device load to settle; those load waits and the JankBench J10a load gate apply only to Cuttlefish.

Resume only if the full run is interrupted:

```bash
./artifact/pipeline.sh resume --out "$OUT"
```
The smoke summary reports each policy's sched_ext state, ops name, frame count, and scheduler log path.

Integrate the completed results:

```bash
./artifact/pipeline.sh analyze "$OUT"
```

`full` stages binaries and APKs, then reboots between test blocks. It uses `eevdf`, `scx_fcp`, `scx_fcp_v_a_depth3_tgid1ms`, `scx_p2dq_pelt_off`, and `scx_lavd`. The UiBench APK must be the fixed build; the device profile requires all six tests to pass.

Device launch sets SELinux to Permissive; restore it with `adb -s "$SERIAL" shell setenforce 1` or reboot.

## Overview

The Cuttlefish campaign covers **4, 8, 16, and 32 vCPUs** (`Symmetric` and `Asymmetric` `446:1024` topologies) and runs:
1. the 10-run multi-core scaling evaluation on `fcp_frame_bench` (Table 1);
2. the full 8-benchmark AOSP `JankBench` suite (`BM_ID=0,1,2,3,4,5,6,7`, Table 2);
3. the full 6-benchmark AOSP `UiBenchJankTests` suite (6 tests; `bitmap-upload`
   needs the fixed APK, otherwise it is reported as known-invalid, Table 3).

The SA8155P full matrix uses the device's fixed 8-core configuration; it does not run Cuttlefish vCPU scaling or ProxyExec.

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
Android Cuttlefish system image with sched_ext and BTF enabled
16 vCPUs, 8192 MB guest RAM
adb
```

The artifact launcher is:

```text
artifact/scripts/launch_cuttlefish.sh
```

It reads `CF_HOME`, `SERIAL`, `CPUS`, `MEMORY_MB`, `TOPO`, and `PROXY_EXEC` from [`artifact/config.env`](config.env) (any variable can be overridden inline):

```bash
CPUS=4  ./artifact/pipeline.sh launch
CPUS=8  ./artifact/pipeline.sh launch
CPUS=16 ./artifact/pipeline.sh launch
CPUS=32 ./artifact/pipeline.sh launch
```

`launch_cuttlefish.sh` passes `-cpus=N -memory_mb=M` to `launch_cvd`, connects
over `adb` as `root`, waits for `sys.boot_completed=1` and the `activity`,
`system_server`, and `com.android.systemui` services, sets `SELinux=Permissive`,
`debug.hwui.renderer=opengl`, and `animator_duration_scale=1`, stages all built
binaries from `artifact/build/bin/` into `/data/local/tmp/scx/`, and installs
any workload APKs present in `artifact/workloads/`:

```text
device ready: 127.0.0.1:6520
  kernel:          6.12.74-android16-... (32 vCPUs)
  activity:        Service activity: found
  system_server:   <pid>
  systemui:        <pid>
  boot_completed:  1
  sched_ext state: disabled
  SELinux:         Permissive
```

## 3. Smoke test

Run JankBench benchmark id 0 once for every configured policy, with one iteration
and no warm-up:

```bash
OUT=$PWD/artifact/results/smoke_$(date +%Y%m%d-%H%M%S)
./artifact/pipeline.sh smoke --out "$OUT"
```

Smoke makes one attempt per policy. Its smoke validation profile requires a
non-empty result for the single iteration and keeps the scheduler, state,
process, crash, boot, load, and BPF checks. It relaxes campaign frame-count and
minimum-duration gates, so smoke validity does not qualify a campaign run.
UiBench and full-matrix aggregation are skipped.

Inspect the validity record and run summary:

```bash
cat "$OUT/jankbench/list-view-fling/validity.json"
grep -E 'ALL_CELLS_VALID|INCOMPLETE|smoke passed|FATAL' "$OUT/orchestrator.log"
```

Run the full campaign only after smoke passes.

## 4. Multi-core scaling benchmark (`fcp_frame_bench`, 4 / 8 / 16 / 32 vCPUs, 10 Runs)

For each vCPU configuration (`C` in `{4, 8, 16, 32}`), run 10 iterations per scheduler using the following setup:

* **Background workers (`W_bg`):** `(3/4) * C` (corresponding to `3, 6, 12, 24` workers)
* **Big-core list (`C_big`):** `[C/2 .. C-1]`
* **Symmetric mode:** `renice 0` across all `crosvm_vcpu*` threads
* **Asymmetric mode:** `renice +10` on `crosvm_vcpu0` through `crosvm_vcpu(C/2 - 1)`, and `renice 0` on `crosvm_vcpu(C/2)` through `crosvm_vcpu(C - 1)`

```bash
# Attach scx_fcp (default) or scx_fcp_behavioral (zero-hint):
adb -s "$SERIAL" shell "cd /data/local/tmp/scx && ulimit -l unlimited && SCX_STATS_BASE_PATH=/data/local/tmp/scx/stats nohup ./scx_fcp --big-cpus $BIG_LIST -f 11111 -s 0 >/dev/null 2>&1 </dev/null &"

# Run 10 iterations of 90 Hz 4-stage fcp_frame_bench:
adb -s "$SERIAL" shell "
  for r in \$(seq 1 10); do
    /data/local/tmp/scx/fcp_frame_bench --scheduler scx_fcp_${CPUS}c_${TOPO} \
      --frames 60 --hz 90 --stages 4 --stage-us 1800 --bg-workers $BG_W --big-cpus $BIG_LIST
  done
"
```

## 5. Full 14-test AOSP matrix (8 JankBench + 6 UiBenchJankTests)

From the repository root:

```bash
OUT=$PWD/artifact/results/full14_$(date +%Y%m%d-%H%M%S)
./artifact/pipeline.sh full --out "$OUT"
```

Defaults:

```text
policies:  eevdf, eevdf w/ proxy_exec, scx_fcp, scx_fcp_v_a_depth3_tgid1ms (behavioral), scx_p2dq_pelt_off, scx_lavd
JankBench: all 8 benchmark IDs (0,1,2,3,4,5,6,7)
UiBench:   all 6 tests (bitmap-upload, shadow-grid-list, inflating-list,
           slow-bind-recycler, trivial-recycler, layout-cache-low)
Runs:      10 valid runs/reps per (test, policy)
```

`UI_ALLOW_INVALID=none` is important for the final run: it makes
`bitmap-upload` required instead of silently “known-invalid”. Use the fixed
UiBench APK first.

### Resume

The driver supports cell-level resume:

```bash
./artifact/pipeline.sh resume --out "$OUT"
```

Resume skips cells that already have 10 valid units. Do not edit or delete
validity records to make an incomplete cell look complete.

### What invalid means

JankBench is rejected on any failed evaluable J1–J11 check: wrong state/ops,
missing database, wrong iteration set, frame-count floor, host
or guest load gate, process changes, sleep events, logcat crash/ANR, or boot
change.
On Cuttlefish, the host load wait and JankBench J10a limit default to 25% of
host CPUs (16 when `nproc` is 64). The guest load limit remains 2.0.
JankBench manifests record the host CPU count and effective host limit.

UiBench is rejected when any required test/rep row is invalid: instrumentation
failure, missing GfxMonitor summary, too few frames, missing percentile/jank
keys, wrong foreground/SystemUI state, wrong sched_ext state, or a fatal
logcat event.

Invalid attempts are moved to `_invalid/` and rerun. They remain visible in
validity logs; they are never pooled.

## 6. Aggregate and verify

```bash
./artifact/pipeline.sh analyze "$OUT" \
  --runs 10 --require-known-invalid \
  --out-dir "$OUT/integration"
```

The integration step must report:

```text
14 tests
6 default policies
84/84 required cells
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
- The physical SA8155P path supports SCX 6.1 build, device preparation, a single-ID JankBench smoke test, and the 14-test matrix; the multi-core vCPU scaling campaign remains Cuttlefish-only.
