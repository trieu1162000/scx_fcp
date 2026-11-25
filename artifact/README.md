# `scx_fcp` Artifact Reproduction Guide

Clean, deterministic reproduction pipeline for `scx_fcp`: multi-core scaling (`fcp_frame_bench` + `schbench`), AOSP `JankBench` (8 workloads), AOSP `UiBenchJankTests` (6 workloads), component ablation, and isolated `struct_ops` eBPF runtime overhead.

## Quick start

1. Review [`artifact/config.env`](config.env) (`SCX_ROOT`, `CF_HOME`, `ANDROID_ADB_SERVER_PORT=5038` for Cuttlefish or `5037` for physical ARM64 device).
2. Run from the repository root:

```bash
./artifact/pipeline.sh build
./artifact/pipeline.sh launch
./artifact/pipeline.sh smoke

# 1. Multi-core scaling (4 / 8 / 16 / 32 vCPUs, sym + dl, 6 policies, 10 runs)
./artifact/pipeline.sh micro --suite all --runs 10 --out artifact/results/cf_micro_10x
./artifact/pipeline.sh analyze artifact/results/cf_micro_10x

# 2. Full 14-test AOSP macro suite (8 JankBench + 6 UiBench) on 32-vCPU Asymmetric (dl) & Symmetric (sym)
./artifact/pipeline.sh macro --topo dl  --runs 10 --out artifact/results/cf32_macro_dl
./artifact/pipeline.sh macro --topo sym --runs 10 --out artifact/results/cf32_macro_sym
./artifact/pipeline.sh analyze artifact/results/cf32_macro_dl artifact/results/cf32_macro_sym

# 3. Component ablation (notoken, nofanout, behavioral, full) on 32-vCPU Asymmetric (dl)
./artifact/pipeline.sh ablation --topo dl --runs 5 --out artifact/results/cf32_ablation_dl
./artifact/pipeline.sh analyze artifact/results/cf32_ablation_dl

# 4. Isolated eBPF struct_ops runtime overhead (bpf_stats_enabled=1)
./artifact/pipeline.sh overhead --topo dl  --runs 3 --out artifact/results/cf32_overhead_dl
./artifact/pipeline.sh overhead --topo sym --runs 3 --out artifact/results/cf32_overhead_sym
./artifact/pipeline.sh analyze artifact/results/cf32_overhead_dl artifact/results/cf32_overhead_sym
```

## Physical Qualcomm SA8155P (`arm64`, port `5037`, zero-reboot safe)

When the physical SA8155P board is connected on ADB server port `5037`, the harness runs without rebooting the device (preserving USB ADB connectivity):

```bash
PLATFORM=arm64 ANDROID_ADB_SERVER_PORT=5037 SERIAL=1feeaa2f ./artifact/pipeline.sh build
PLATFORM=arm64 ANDROID_ADB_SERVER_PORT=5037 SERIAL=1feeaa2f ./artifact/pipeline.sh launch
PLATFORM=arm64 ANDROID_ADB_SERVER_PORT=5037 SERIAL=1feeaa2f ./artifact/pipeline.sh macro --runs 10 --out artifact/results/sa8155p_macro_10x
PLATFORM=arm64 ANDROID_ADB_SERVER_PORT=5037 SERIAL=1feeaa2f ./artifact/pipeline.sh overhead --runs 3 --out artifact/results/sa8155p_overhead
PLATFORM=arm64 ./artifact/pipeline.sh analyze artifact/results/sa8155p_macro_10x artifact/results/sa8155p_overhead
```

## Integrity & Reproducibility Guarantees

- **Persistent Host Topology Verification**: On Cuttlefish (`PLATFORM=x86_64`), `scripts/harness.py` verifies every `crosvm_vcpu` thread's Linux scheduler policy (`SCHED_DEADLINE` `892us/2000us` on LITTLE half `0..C/2-1` and `SCHED_OTHER nice -10` on big half `C/2..C-1` for `dl`; `SCHED_OTHER nice 0` on all vCPUs for `sym`) **before and after every run**.
- **One-Way `sched_proxy_exec` Ordering**: Kernel `sched_proxy_exec/enable` is a one-way static branch (`0 -> 1`). All non-proxy policies run round-robin interleaved first while `sched_proxy_exec == 0`, followed by `eevdf_proxy_exec` (`1`), with automatic guest reboot + host `SCHED_DEADLINE` restoration whenever resetting `1 -> 0`.
- **Zero Timing Perturbation**: All latency campaigns enforce `kernel.bpf_stats_enabled=0`; eBPF callback cost is measured exclusively in isolated `overhead` campaigns.
- **Forensic Audit**: `scripts/analyze.py` reads raw SQLite `ui_results` rows and `GfxMonitor` instrumentation outputs directly, verifying strict percentile monotonicity (`p50 <= p95 <= p99`) and exact frame counts.
