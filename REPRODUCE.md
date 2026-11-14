# Reproducing scx_fcp measurements

This procedure builds the tuned scheduler, prepares an Android device, and
runs the approved workloads.

## Tested state

- Scheduler source: this repository.
- Host workspace: an `scx` tree selected with `SCX_ROOT`.
- Target: Android Cuttlefish x86_64 and SA8155P ARM64.

## 1. Build

```bash
cd scx_fcp
export SCX_ROOT=/path/to/scx
export BIN_DIR=$PWD/build/bin
scripts/build_static.sh
```

The script builds `scx_fcp`, `scx_fcp_named`, and `scx_fcp_behavioral` plus
`fcp_frame_bench`, then verifies that each binary is static and writes
`SHA256SUMS`. Set `BUILD_BASELINES=1` to also build `scx_lavd`,
`scx_p2dq`, and `scx_bpfland`.

Check the mirrored sources against the canonical tree.

```bash
STRICT_SOURCE=1 scripts/build_static.sh
```

## 2. Prepare the device

```bash
export ADB=${ADB:-adb}
export SERIAL=${SERIAL:-127.0.0.1:6520}
$ADB connect "$SERIAL"
$ADB -s "$SERIAL" root
$ADB -s "$SERIAL" shell setenforce 0
$ADB -s "$SERIAL" shell 'svc power stayon true'
$ADB -s "$SERIAL" shell 'settings put global stay_on_while_plugged_in 7'
```

Confirm sched_ext is available and no scheduler is attached.

```bash
$ADB -s "$SERIAL" shell 'cat /sys/kernel/sched_ext/state'
$ADB -s "$SERIAL" shell 'cat /sys/kernel/sched_ext/root/ops'
```

The expected state before a run is `disabled`.

## 3. Synthetic pipeline

Run the shortest test first. It validates the binary, the device staging
directory, and the scheduler attach path.

```bash
TARGET=device SERIAL=$SERIAL BIN_DIR=$BIN_DIR \
SCHEDULER_LABEL=scx_fcp_behavioral \
  tests/synthetic/run_fcp_frame_bench.sh
```

The benchmark prints one JSON record with `mean_ms`, `p50_ms`, `p95_ms`,
`p99_ms`, and jank rates. A missing or empty JSON record means the binary did
not run, not that the result was zero.

## 4. JankBench

Run EEVDF and the tuned scheduler as separate policy blocks. Reboot before
the first sample of each block, then reuse the boot for the remaining samples.

```bash
REBOOT=1 BIN_DIR=$BIN_DIR \
  tests/jankbench/run_jankbench.sh -p eevdf -n 1

REBOOT=0 BIN_DIR=$BIN_DIR \
  tests/jankbench/run_jankbench.sh -p eevdf -n 2

REBOOT=1 BIN_DIR=$BIN_DIR \
  tests/jankbench/run_jankbench.sh -p scx_fcp_behavioral -n 1

REBOOT=0 BIN_DIR=$BIN_DIR \
  tests/jankbench/run_jankbench.sh -p scx_fcp_behavioral -n 2
```

A valid run has three measured iterations, a matching scheduler ops name, a
stable SystemUI process, and a pulled `BenchmarkResults.db`. The harness
rejects a sample when the benchmark process restarts, the device sleeps, or
the database has no completed iteration.

Aggregate the samples.

```bash
tests/jankbench/summarize_jankbench.py results/jankbench
```

## 5. UiBench

Prepare the two APKs and a policy block.

```bash
export UIBENCH_APK=/path/to/UiBench-app-debug.apk
export UIBENCH_TEST_APK=/path/to/UiBenchJankTests-debug.apk

BIN_DIR=$BIN_DIR SCHED_BIN=scx_fcp_behavioral \
  tests/uibench/run_uibench.sh -p scx_fcp_behavioral -n 1 -r 3 \
  --install --tests inflating-list,shadow-grid-list

BIN_DIR=$BIN_DIR \
  tests/uibench/run_uibench.sh -p eevdf -n 1 -r 3 \
  --tests inflating-list,shadow-grid-list
```

The runner extracts GfxMonitor metrics after each run. Inspect
`summary.txt` and reject any `INVALID` row before aggregating.

```bash
tests/uibench/summarize_uibench.py results/uibench
```

## 6. Sanity checks

Run these before accepting a result set.

```bash
grep -R 'scx_ops_before' results/jankbench/*/run-*/manifest.json
grep -R 'INVALID' results/uibench/*/uibench/run-*/summary.txt
grep -R 'valid' results/uibench/*/uibench/run-*/metrics.json | head
```

Verify that each scheduler hash matches the binary you intended to test.

```bash
sha256sum "$BIN_DIR"/scx_fcp*
cat results/jankbench/*/run-*/manifest.json | grep scheduler_sha256
```

## 7. Cleanup

Stop the scheduler and return the device to its normal state.

```bash
pid=$($ADB -s "$SERIAL" shell pidof scx_fcp_behavioral | tr -d '\r')
[ -n "$pid" ] && $ADB -s "$SERIAL" shell "kill -TERM $pid"
$ADB -s "$SERIAL" shell 'cat /sys/kernel/sched_ext/state'
```
