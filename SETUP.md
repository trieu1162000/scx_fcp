# scx_fcp Setup, Build, and Evaluation Guide

For the current paper artifact pipeline, use:

```text
artifact/SETUP.md
artifact/BUILD.md
artifact/RUN.md
```

This guide provides step-by-step instructions to build `scx_fcp`, configure Linux/Android GKI kernels (`6.18` LTS and `7.2` mainline), package self-contained Android bundles (`x86_64` and `aarch64`), and run benchmarks on Linux hosts, 32-vCPU Android Cuttlefish, Car Emulator, and physical ARM64 Android devices.

---

## 1. Host Prerequisites & Building `scx_fcp`

### 1.1. Toolchain Requirements
- **Rust toolchain**: `cargo` / `rustc` (1.82+)
- **Clang / LLVM**: `clang` 17+ (`clang` 22+ recommended for mainline kernels) with `llvm` and `bpftool`
- **Libraries**: `libelf-dev`, `zlib1g-dev`, `libzstd-dev`, `pkg-config`
- **Cross-compilation (for ARM64 Android devices)**: `gcc-aarch64-linux-gnu`, `libc6-dev-arm64-cross`

### 1.2. Build `scx_fcp` and `fcp_frame_bench` (x86_64 & aarch64)

```bash
cd /home/worker/workspace/scx
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"

# 1. Build x86_64 release binaries (for Linux host, Cuttlefish, and x86_64 Android Emulator)
cargo build --release -p scx_fcp -p scx_lavd -p scx_p2dq
make -C tools/scx_fcp_bench

# 2. Cross-compile aarch64 release binaries (for physical ARM64 Android devices)
CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
  cargo build --release --target aarch64-unknown-linux-gnu -p scx_fcp -p scx_lavd -p scx_p2dq
aarch64-linux-gnu-gcc -O2 -pthread tools/scx_fcp_bench/fcp_frame_bench.c \
  -o tools/scx_fcp_bench/fcp_frame_bench_aarch64 -lm
```

---

## 2. Kernel Configuration Requirements

To run `scx_fcp`, the target kernel must be built with `sched_ext` and BTF enabled (`pahole >= 1.22` in `PATH` during kernel build):

```text
CONFIG_BPF=y
CONFIG_BPF_SYSCALL=y
CONFIG_BPF_JIT=y
CONFIG_DEBUG_INFO_BTF=y
CONFIG_SCHED_CLASS_EXT=y
CONFIG_EXT_GROUP_SCHED=y
# On Android GKI kernels (android17-6.18 / android-mainline), disable Proxy Execution:
# CONFIG_SCHED_PROXY_EXEC is not set
```

- **Why `CONFIG_SCHED_PROXY_EXEC=n` is required on Android GKI**: Android's Proxy Execution splits `rq->curr` and `rq->donor` across mutex wait chains, which conflicts with `sched_ext` dispatch queues (and is marked `depends on !SCHED_CLASS_EXT` in `android-mainline` `init/Kconfig`).
- **`CONFIG_FUNCTION_TRACER` is optional**: `scx_fcp` attaches purely via `BPF_PROG_TYPE_STRUCT_OPS` (`sched_ext_ops`) and does not require tracefs `available_filter_functions`.

---

## 3. Packaging Self-Contained Bundles for Android Guests/Devices

Because Android uses Bionic (`libc.so`) while standard Rust builds link against `glibc`, `libelf`, and `libz`, package the shared libraries alongside `ld-linux` into `/data/local/tmp/scx`:

### 3.1. `x86_64` Android Bundle (Cuttlefish / Android Emulator)
```bash
mkdir -p /tmp/scx_x86_64_bundle/lib
cp -f target/release/scx_fcp target/release/scx_lavd target/release/scx_p2dq \
      tools/scx_fcp_bench/fcp_frame_bench /tmp/scx_x86_64_bundle/
cp -L /lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 \
      /lib/x86_64-linux-gnu/libc.so.6 \
      /lib/x86_64-linux-gnu/libm.so.6 \
      /lib/x86_64-linux-gnu/libgcc_s.so.1 \
      /lib/x86_64-linux-gnu/libelf.so.1 \
      /lib/x86_64-linux-gnu/libz.so.1 \
      /lib/x86_64-linux-gnu/libzstd.so.1 \
      /tmp/scx_x86_64_bundle/lib/
```

### 3.2. `aarch64` Android Bundle (Physical ARM64 Devices)
```bash
mkdir -p /tmp/scx_aarch64_bundle/lib
cp -f target/aarch64-unknown-linux-gnu/release/scx_fcp \
      target/aarch64-unknown-linux-gnu/release/scx_lavd \
      target/aarch64-unknown-linux-gnu/release/scx_p2dq \
      /tmp/scx_aarch64_bundle/
cp -f tools/scx_fcp_bench/fcp_frame_bench_aarch64 /tmp/scx_aarch64_bundle/fcp_frame_bench
cp -L /usr/aarch64-linux-gnu/lib/ld-linux-aarch64.so.1 \
      /usr/aarch64-linux-gnu/lib/libc.so.6 \
      /usr/aarch64-linux-gnu/lib/libm.so.6 \
      /usr/aarch64-linux-gnu/lib/libgcc_s.so.1 \
      /usr/aarch64-linux-gnu/lib/libelf.so.1 \
      /usr/aarch64-linux-gnu/lib/libz.so.1 \
      /tmp/scx_aarch64_bundle/lib/
```

---

## 4. Running `scx_fcp` and Benchmarks

### 4.1. On a Standard Linux Host
```bash
# Attach scx_fcp with 60Hz (16667us) frame budget and 2s telemetry interval
sudo ./target/release/scx_fcp -f 16667 -c 2000 -i 3000 -b 6000 -t 850 -s 2

# In another terminal, verify sched_ext state and run fcp_frame_bench
cat /sys/kernel/sched_ext/state
cat /sys/kernel/sched_ext/root/ops
./tools/scx_fcp_bench/fcp_frame_bench --scheduler scx_fcp --frames 300 --hz 60 --stages 4 --stage-us 1800 --bg-workers 16
```

### 4.2. On 32-vCPU Android Cuttlefish (`x86_64`)
```bash
adb -s 127.0.0.1:6520 push /tmp/scx_x86_64_bundle /data/local/tmp/scx

# Start scx_fcp in background
adb -s 127.0.0.1:6520 shell 'su 0 sh -c "
  chmod 755 /data/local/tmp/scx/scx_* /data/local/tmp/scx/fcp_frame_bench /data/local/tmp/scx/lib/*
  setenforce 0
  ulimit -l unlimited
  nohup /data/local/tmp/scx/lib/ld-linux-x86-64.so.2 --library-path /data/local/tmp/scx/lib \
    /data/local/tmp/scx/scx_fcp -f 16667 -s 2 > /data/local/tmp/scx_fcp.log 2>&1 < /dev/null &
"'

# Verify and run 90Hz 4-stage pipeline benchmark
adb -s 127.0.0.1:6520 shell 'su 0 sh -c "
  cat /sys/kernel/sched_ext/state
  cat /sys/kernel/sched_ext/root/ops
  /data/local/tmp/scx/lib/ld-linux-x86-64.so.2 --library-path /data/local/tmp/scx/lib \
    /data/local/tmp/scx/fcp_frame_bench --scheduler scx_fcp --frames 300 --hz 90 --stages 4 --stage-us 1800 --bg-workers 24
  tail -n 5 /data/local/tmp/scx_fcp.log
"'
```

### 4.3. On Physical ARM64 SA8155P Android Device (`aarch64`)

This generic ARM64 packaging section does not replace the Android 6.1
backport procedure. For Android 14 device, first follow
`GUIDE_4_BACKPORT_ANDROID_6.1_SCX.md`, including the
`SCX_FCP_ANDROID_61=1` build flag and device-side attach verification.

```bash
adb push /tmp/scx_aarch64_bundle /data/local/tmp/scx

adb shell 'su 0 sh -c "
  chmod 755 /data/local/tmp/scx/scx_* /data/local/tmp/scx/fcp_frame_bench /data/local/tmp/scx/lib/*
  setenforce 0
  ulimit -l unlimited
  nohup /data/local/tmp/scx/lib/ld-linux-aarch64.so.1 --library-path /data/local/tmp/scx/lib \
    /data/local/tmp/scx/scx_fcp -f 16667 -s 2 > /data/local/tmp/scx_fcp.log 2>&1 < /dev/null &
"'

adb shell 'su 0 sh -c "
  cat /sys/kernel/sched_ext/state
  cat /sys/kernel/sched_ext/root/ops
  tail -n 10 /data/local/tmp/scx_fcp.log
"'
```

---

## 5. Collecting Kernel eBPF Execution Overhead (`bpf_stats_enabled`)

To measure exact per-callback eBPF execution latency (`ns/call`) and total CPU time (`s`) for any `sched_ext` scheduler:

```bash
# Enable kernel BPF runtime statistics before running a benchmark
adb shell 'su 0 sysctl -w kernel.bpf_stats_enabled=1'

# Inspect run_time_ns and run_cnt across all attached struct_ops programs of the scheduler PID
adb shell 'su 0 sh -c "
  PID=\$(pgrep -f scx_fcp | head -n 1)
  grep -H -E \"prog_id|run_time_ns|run_cnt\" /proc/\$PID/fdinfo/* 2>/dev/null
"'
```
