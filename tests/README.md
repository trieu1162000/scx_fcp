# Reproduction tests

The scripts in this directory run the tuned `scx_fcp` build against three
workloads. They do not modify scheduler state without an explicit policy and
they never reboot a device unless `REBOOT=1` is set for a JankBench run.

## Environment

All runners read these variables and have defaults.

| Variable | Default | Purpose |
|---|---|---|
| `ADB` | `adb` | adb binary |
| `SERIAL` | `127.0.0.1:6520` | device serial |
| `SCX_ROOT` | `$HOME/workspace/scx` | canonical scx workspace |
| `RESULTS_ROOT` | `<repo>/results/<workload>` | host result directory |
| `BIN_DIR` | `$SCX_ROOT/target/release` | scheduler and benchmark binaries |
| `REMOTE` | `/data/local/tmp/scx` | device staging directory |

Build static binaries first with `scripts/build_static.sh`, then point
`BIN_DIR` at `build/bin` for device runs.

## JankBench

`jankbench/run_jankbench.sh` runs one AOSP JankBench List View Fling sample
under one policy. It records the scheduling state, device identity, kernel,
scheduler hash, and a consistent database snapshot.

```bash
BIN_DIR=build/bin REBOOT=1 \
  tests/jankbench/run_jankbench.sh -p scx_fcp_behavioral -n 1

BIN_DIR=build/bin tests/jankbench/run_jankbench.sh -p eevdf -n 1
```

Policies are `eevdf`, `scx_fcp`, `scx_fcp_named`, `scx_fcp_behavioral`,
`scx_lavd`, and `scx_p2dq`. Any other policy name is treated as a binary in
`BIN_DIR`, with `OPS` and `OPS_ARGS` as optional overrides.

Result layout.

```text
$RESULTS_ROOT/<policy>/run-<n>/
  BenchmarkResults.db
  manifest.json
  harness.log
  state_before.txt
  state_after.txt
  scheduler.log
```

Aggregate one or more result roots.

```bash
tests/jankbench/summarize_jankbench.py results/jankbench
tests/jankbench/summarize_jankbench.py results/jankbench --out-dir results/jankbench
```

The summary reports per-run and median frame latency, jank rate, and p95
stage times for command issue, buffer swap, and unknown delay.

## UiBench

`uibench/run_uibench.sh` runs one or more UiBenchJankTests. It records the
GfxMonitor `gfx-avg-*` summary as the primary metric and keeps the gfxinfo
ring buffer only as a diagnostic. Set `SCHED_BIN` to let the runner push and
attach a scheduler. Leave it empty when another harness owns the scheduler.

```bash
BIN_DIR=build/bin SCHED_BIN=scx_fcp_behavioral \
  tests/uibench/run_uibench.sh -p scx_fcp_behavioral -n 1 -r 3 \
  --tests inflating-list,shadow-grid-list

UIBENCH_APK=UiBench-app-debug.apk \
UIBENCH_TEST_APK=UiBenchJankTests-debug.apk \
  tests/uibench/run_uibench.sh -p eevdf -n 1 --install
```

Result layout.

```text
$RESULTS_ROOT/<policy>/uibench/run-<nn>/
  manifest.json
  <test>/rep-<nn>/instrument.txt
  <test>/rep-<nn>/gfxinfo_framestats.txt
  <test>/rep-<nn>/logcat.txt
  <test>/rep-<nn>/env.json
  metrics.json
  metrics.csv
  summary.txt
```

Each row carries a `valid` flag and a reason when invalid. A run is invalid
when instrumentation fails, fewer than 100 GfxMonitor frames are present,
SystemUI restarts, the workload leaves the foreground, SELinux is not
permissive, or the observed sched_ext state does not match the policy.

Summarize extracted metrics.

```bash
tests/uibench/summarize_uibench.py results/uibench
RESULTS_ROOT=results/uibench tests/uibench/summarize_uibench.py
```

The summary reports median and IQR values across valid runs, plus a head to
head table when both `eevdf` and `scx_fcp_behavioral` are present.

## Synthetic frame pipeline

`synthetic/run_fcp_frame_bench.sh` runs the four-stage synchronous pipeline
benchmark. The default target is the host. Use `TARGET=device` to push and run
the binary on the Android device.

```bash
SCHEDULER_LABEL=scx_fcp tests/synthetic/run_fcp_frame_bench.sh

TARGET=device SERIAL=127.0.0.1:6520 SCHEDULER_LABEL=scx_fcp_behavioral \
  FRAMES=300 HZ=90 STAGES=4 STAGE_US=1800 BG_WORKERS=24 \
  tests/synthetic/run_fcp_frame_bench.sh
```

Each run writes `fcp_frame_bench.json`, `manifest.json`, and the captured
sched_ext state under `$RESULTS_ROOT/<label>/<target>/run-<timestamp>/`.

## Validity rules

- Keep one scheduler attached at a time.
- Use `setenforce 0` only on a test image.
- Reboot before the first sample of a policy block and reuse the boot for the
  remaining samples unless a run is invalid.
- Reject any run where SystemUI restarts, the benchmark process restarts, or
  the device sleeps.
- Compare medians across runs, not pooled frame percentiles.
