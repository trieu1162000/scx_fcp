# `scx_fcp` Artifact Evaluation Guide

Reproducibility package for the `scx_fcp` paper. This directory provides the
step-by-step setup, build, execution, and validation pipeline to reproduce the
paper's synthetic scaling (`fcp_frame_bench`), AOSP `JankBench`, AOSP
`UiBenchJankTests`, and eBPF runtime overhead measurements.

## Quick start

1. Edit [`artifact/config.env`](config.env) once to set your host paths (`SCX_ROOT`, `CF_HOME`, `SERIAL`, `CPUS`), or override any variable inline.
2. Run the pipeline from the repository root:

```bash
./artifact/pipeline.sh build
./artifact/pipeline.sh launch
./artifact/pipeline.sh smoke
./artifact/pipeline.sh full --out artifact/results/full14
./artifact/pipeline.sh overhead --out artifact/results/bpf_overhead --suite all
./artifact/pipeline.sh overhead --quick          # one-run cross-check only
./artifact/pipeline.sh overhead-analyze artifact/results/bpf_overhead
./artifact/pipeline.sh analyze artifact/results/full14 --out-dir artifact/results/full14/integration
```

## Documentation map

| Step | Document | Contents |
| :---: | :--- | :--- |
| **1** | [`SETUP.md`](SETUP.md) | Host prerequisites, Cuttlefish prebuilt image (`6.12.74-android16` `boot.img` with `sched_proxy_exec=0`), optional custom mainline `7.2` kernel build, and `4/8/16/32`-vCPU launch |
| **2** | [`BUILD.md`](BUILD.md) | Building static `scx_fcp` profiles, baseline schedulers (`scx_lavd`, `scx_p2dq`), `fcp_frame_bench`, ARM64 cross-builds, and AOSP `JankBench` / `UiBench` APKs |
| **3** | [`RUN.md`](RUN.md) | Running the `4/8/16/32`-vCPU synthetic scaling suite, the full 14-test AOSP matrix (`8` JankBench + `6` UiBench), standalone per-suite runners, and eBPF overhead collection |
| **4** | [`VALIDATE.md`](VALIDATE.md) | Per-run validity gates (`J1–J12` for JankBench, `GfxMonitor` & process stability for UiBench) and LaTeX/JSON/HTML aggregation |

## Evaluated configurations

1. **Android Cuttlefish (`x86_64`, `4 / 8 / 16 / 32` vCPUs)**:
   - Topologies: Symmetric SMP (`1024 : 1024`) and Asymmetric `big.LITTLE` (`446 : 1024` capacity ratio via host `crosvm_vcpu` scheduling).
   - Policies: stock `EEVDF`, `EEVDF` w/ `CONFIG_SCHED_PROXY_EXEC=y`, `scx_p2dq`, `scx_lavd`, `scx_fcp` (`zero-hint` / `scx_fcp_behavioral`), and `scx_fcp` (`Full` / default).
2. **Physical Qualcomm Snapdragon SA8155P (`aarch64`, 8-Core `4+3+1` Kryo 485)**:
   - Evaluated using the companion Linux `6.1.145` backport repository: [`trieu1162000/scx-6.1`](https://github.com/trieu1162000/scx-6.1).

## Evaluation scope

```text
Synthetic VSYNC DAG: 4, 8, 16, 32 vCPUs (Symmetric & Asymmetric, 60/90/120 Hz)
AOSP JankBench:      8 benchmark IDs (BM_ID=0..7)
AOSP UiBench:        6 test methods (5 scrolling/rendering + BitmapUpload)
Repetitions:         10 valid runs/repetitions per (test, policy) cell
eBPF Overhead:       /proc/sys/kernel/bpf_stats_enabled (run_time_ns, run_cnt)
```

## Packaged entrypoints

- **Unified pipeline (`artifact/pipeline.sh`)**: `build | launch | smoke | full | overhead | resume | analyze`
- **Cuttlefish launcher (`artifact/scripts/launch_cuttlefish.sh`)**: parameterized `4/8/16/32`-vCPU guest launcher
- **Standalone suite harnesses (`../tests/`)**:
  - `tests/synthetic/run_fcp_frame_bench.sh`
  - `tests/jankbench/run_jankbench.sh` & `tests/jankbench/summarize_jankbench.py`
  - `tests/uibench/run_uibench.sh`, `tests/uibench/extract_uibench.py`, & `tests/uibench/summarize_uibench.py`
