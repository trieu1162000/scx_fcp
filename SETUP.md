# scx_fcp setup, build, and evaluation

This guide covers host build, Android bundle packaging, kernel requirements,
and benchmark setup. The canonical scheduler source is the `scx` workspace at
`$SCX_ROOT/scheds/rust/scx_fcp`. This repository mirrors that directory.
Set `SCX_ROOT` to the workspace path.

## 1. Host prerequisites

- Rust `cargo` and `rustc` 1.91 or newer.
- Clang and LLVM 17 or newer, `llvm`, `bpftool`, and `pahole`.
- `libelf-dev`, `zlib1g-dev`, `libzstd-dev`, `pkg-config`, `make`, and `cc`.
- ARM64 cross builds require `gcc-aarch64-linux-gnu` and
  `libc6-dev-arm64-cross`.
- Static builds require `libzstd.a` from `libzstd-dev`.

## 2. Build

### 2.1 Static build script

`scripts/build_static.sh` builds the default, named, and zero-hint profiles
with `-C target-feature=+crt-static`, then builds a static
`fcp_frame_bench`. It writes binaries, logs, and `SHA256SUMS` under
`build/`.

```bash
export SCX_ROOT=$HOME/workspace/scx
export BIN_DIR=$PWD/build/bin
scripts/build_static.sh
```

Useful variables are `PROFILES`, `TARGET`, `JOBS`, `ZSTD_A`, and
`BUILD_BASELINES=1`. Set `STRICT_SOURCE=1` to fail when this mirror differs
from `$SCX_ROOT/scheds/rust/scx_fcp`.

### 2.2 Direct cargo build

```bash
cd "$SCX_ROOT"
cargo build --release -p scx_fcp
make -C scheds/rust/scx_fcp/tools/scx_fcp_bench
```

Select a BPF variant with `SCX_FCP_BUILD`.

```bash
SCX_FCP_BUILD=zero-hint cargo build --release -p scx_fcp
SCX_FCP_BUILD=named cargo build --release -p scx_fcp
```

The default binary attaches as ops `fcp`. The zero-hint binary attaches as
`fcp_zh` and the named binary as `fcp_nh`. Android 6.1 backports are maintained
separately; this tree targets the mainline kernel ABI used by the paper.

### 2.3 ARM64 cross build

```bash
cd "$SCX_ROOT"
CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
  cargo build --release --target aarch64-unknown-linux-gnu -p scx_fcp
aarch64-linux-gnu-gcc -O2 -pthread -static \
  scheds/rust/scx_fcp/tools/scx_fcp_bench/fcp_frame_bench.c \
  -o scheds/rust/scx_fcp/tools/scx_fcp_bench/fcp_frame_bench_aarch64 -lm
```

## 3. Kernel requirements

```text
CONFIG_BPF=y
CONFIG_BPF_SYSCALL=y
CONFIG_BPF_JIT=y
CONFIG_DEBUG_INFO_BTF=y
CONFIG_SCHED_CLASS_EXT=y
CONFIG_EXT_GROUP_SCHED=y
# CONFIG_SCHED_PROXY_EXEC is not set
```

On Android GKI kernels without the sched_ext compatibility patches, Proxy
Execution must be disabled. Android mainline kernels enforce this through a
`depends on !CONFIG_SCHED_CLASS_EXT` guard. `CONFIG_FUNCTION_TRACER` is not
required.

Verify the running kernel before attaching.

```bash
cat /sys/kernel/sched_ext/state
zcat /proc/config.gz | grep -E 'SCHED_CLASS_EXT|DEBUG_INFO_BTF|PROXY_EXEC'
```

## 4. Android bundles

Android uses Bionic, so a host glibc build needs its loader and shared
libraries in the bundle. A static build from `build_static.sh` avoids this
step. For a dynamic x86_64 bundle, use the following layout.

```bash
BUNDLE=/tmp/scx_x86_64_bundle
mkdir -p "$BUNDLE/lib"
cp -f "$BIN_DIR"/scx_fcp* "$BIN_DIR/fcp_frame_bench" "$BUNDLE/"
cp -L /lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 \
      /lib/x86_64-linux-gnu/libc.so.6 \
      /lib/x86_64-linux-gnu/libm.so.6 \
      /lib/x86_64-linux-gnu/libgcc_s.so.1 \
      /lib/x86_64-linux-gnu/libelf.so.1 \
      /lib/x86_64-linux-gnu/libz.so.1 \
      /lib/x86_64-linux-gnu/libzstd.so.1 \
      "$BUNDLE/lib/"
adb -s "$SERIAL" push "$BUNDLE" /data/local/tmp/scx
```

For a dynamic aarch64 bundle, replace the loader with
`ld-linux-aarch64.so.1` and the libraries with their
`/usr/aarch64-linux-gnu/lib` counterparts.

## 5. Run on an Android device

```bash
ADB=${ADB:-adb}
SERIAL=${SERIAL:-127.0.0.1:6520}
REMOTE=/data/local/tmp/scx

$ADB -s "$SERIAL" root
$ADB -s "$SERIAL" shell "setenforce 0; mkdir -p $REMOTE/stats; chmod 755 $REMOTE/*"
$ADB -s "$SERIAL" shell "cd $REMOTE && ulimit -l unlimited && \
  SCX_STATS_BASE_PATH=$REMOTE/stats nohup ./scx_fcp_behavioral -f 16667 -s 2 \
  > $REMOTE/scx_fcp.log 2>&1 < /dev/null &"
```

Wait for the scheduler to attach.

```bash
$ADB -s "$SERIAL" shell 'cat /sys/kernel/sched_ext/state'
$ADB -s "$SERIAL" shell 'cat /sys/kernel/sched_ext/root/ops'
$ADB -s "$SERIAL" shell 'tail -n 5 /data/local/tmp/scx/scx_fcp.log'
```

Stop it with `kill -TERM` on the scheduler process, or run
`$ADB -s "$SERIAL" shell 'echo 0 > /sys/kernel/sched_ext/root/ops'` when the
kernel exposes that control.

## 6. Run on a Linux host

```bash
sudo "$BIN_DIR/scx_fcp" -f 16667 -c 2000 -i 3000 -b 6000 -s 2
SCHEDULER_LABEL=scx_fcp "$BIN_DIR/fcp_frame_bench" \
  --scheduler scx_fcp --frames 300 --hz 60 --stages 4 --stage-us 1800 --bg-workers 16
```

## 7. BPF runtime overhead

Enable BPF runtime statistics and read the scheduler program counters.

```bash
sysctl -w kernel.bpf_stats_enabled=1
PID=$(pgrep -f scx_fcp | head -n 1)
grep -H -E 'prog_id|run_time_ns|run_cnt' /proc/$PID/fdinfo/* 2>/dev/null
```

## 8. Reproduction tests

The reusable test scripts live under `tests/`.

- `tests/jankbench/run_jankbench.sh` runs one JankBench sample.
- `tests/uibench/run_uibench.sh` runs UiBenchJankTests.
- `tests/synthetic/run_fcp_frame_bench.sh` runs the synthetic pipeline.
- `tests/jankbench/summarize_jankbench.py` aggregates JankBench databases.
- `tests/uibench/extract_uibench.py` extracts GfxMonitor metrics.
- `tests/uibench/summarize_uibench.py` aggregates UiBench runs.

All scripts accept `ADB`, `SERIAL`, `SCX_ROOT`, `RESULTS_ROOT`, and `BIN_DIR`
with defaults. See `REPRODUCE.md` for the full sequence.
