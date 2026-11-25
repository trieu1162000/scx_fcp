# Validation, Statistical Audit, and Paper Rendering

## 1. Per-Run Runtime Validation (`artifact/scripts/harness.py`)

Every benchmark run is validated before acceptance (failed attempts are preserved under `_invalid/`):

1. **Host vCPU Capacity Shaping (`x86_64` Cuttlefish)**:
   - `ensure_host_vcpu_topology` inspects `/proc/<crosvm_pid>/task/*/stat` (`ps -L`) before and after every run:
     - `TOPO=dl`: `crosvm_vcpu0 .. C/2-1` must hold `SCHED_DEADLINE` (`DLN`, `892us / 2000us`), and `crosvm_vcpu{C/2} .. C-1` must hold `SCHED_OTHER` (`TS`, `nice=-10`).
     - `TOPO=sym`: all `crosvm_vcpu0 .. C-1` must hold `SCHED_OTHER` (`TS`, `nice=0`).
2. **Kernel Scheduler & Static-Branch Invariants**:
   - `/sys/kernel/sched_ext/state` and `/sys/kernel/sched_ext/root/ops` must match the attached policy (`disabled/none` for `eevdf*`, `enabled` + `p2dq` / `lavd` / `fcp_zh` / `fcp` / `fcp_nt` / `fcp_nf` for `sched_ext` policies) both before and after the workload.
   - `/sys/kernel/sched_proxy_exec/enable` must equal `0` for all non-proxy policies and `1` for `eevdf_proxy_exec`.
   - `/proc/sys/kernel/bpf_stats_enabled` must equal `0` on all timing campaigns (`micro`, `macro`, `ablation`) and `1` on isolated `overhead` campaigns.
3. **Workload Integrity**:
   - `JankBench`: SQLite `BenchmarkResults.db` `ui_results` contains all requested benchmark names, meets per-test frame floors, and satisfies $0 < p_{50} \le p_{95} \le p_{99} < 5000\text{ ms}$.
   - `UiBenchJankTests`: `AndroidJUnitRunner` completes with `INSTRUMENTATION_CODE: -1`, zero failures, $\ge 100$ frames per test, and $0 < p_{50} \le p_{90} \le p_{95} \le p_{99}$.

## 2. Campaign Aggregation & Paper Rendering

```bash
./artifact/pipeline.sh analyze artifact/results/cf_micro_10x \
  artifact/results/cf32_macro_dl artifact/results/cf32_macro_sym \
  artifact/results/cf32_ablation_dl \
  artifact/results/cf32_overhead_dl artifact/results/cf32_overhead_sym

python3 ../scx_fcp_paper/scripts/render_paper_artifacts.py
```

Outputs:
- `<campaign>/campaign_summary.json`: aggregated `Mean`, `SD`, percentiles, jank rates, `FrameMetrics` stage `p95`, `schbench` wakeup `p99`, and raw `run_series`.
- `scx_fcp_paper/data/verified_paper_results.json`: unified verified dataset.
- `scx_fcp_paper/generated/{eval_macros,tab_main_scaling,tab_main_macro,tab_main_overhead,tab_appendix_all}.tex` and `scx_fcp_paper/figures/{eval_pipeline,fig_result,eval_framemetrics}.tex`.
