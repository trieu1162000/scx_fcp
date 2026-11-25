# Running & Resuming `scx_fcp` Reproduction Campaigns

All reproduction commands go through the single entrypoint `./artifact/pipeline.sh` backed by `artifact/scripts/harness.py` and `artifact/scripts/analyze.py`.

## Commands

| Command | Description |
| :--- | :--- |
| `./artifact/pipeline.sh build` | Build static `scx_fcp`, `scx_fcp_behavioral`, `scx_fcp_notoken`, `scx_fcp_nofanout`, `scx_lavd`, `scx_p2dq`, `fcp_frame_bench`, and `schbench` |
| `./artifact/pipeline.sh launch` | Launch Cuttlefish (`PLATFORM=x86_64`, `ANDROID_ADB_SERVER_PORT=5038`) or stage physical device (`PLATFORM=arm64`, `ANDROID_ADB_SERVER_PORT=5037`) |
| `./artifact/pipeline.sh smoke` | Fast 1-run validation across all policies on `JankBench` ID 0 |
| `./artifact/pipeline.sh micro` | Multi-core scaling (`4 / 8 / 16 / 32` vCPUs, `sym` + `dl`) on calibrated fixed-work `fcp_frame_bench` + `schbench` |
| `./artifact/pipeline.sh macro` | Full 14-test AOSP macro suite (`8` JankBench workloads + `6` UiBenchJankTests workloads) under `fcp_frame_bench --bg-only` CPU contention |
| `./artifact/pipeline.sh overhead` | Separate `bpf_stats_enabled=1` `struct_ops` `fdinfo` delta campaign (`ns/call`, `us/frame`, `bpf_cpu_s`) |
| `./artifact/pipeline.sh ablation` | 4-variant component ablation (`scx_fcp_notoken`, `scx_fcp_nofanout`, `scx_fcp_behavioral`, `scx_fcp`) |
| `./artifact/pipeline.sh analyze <DIR...>` | Parse raw SQLite DBs / logs, validate statistical invariants, and emit `campaign_summary.json` |

## Resuming Interrupted Campaigns

Every campaign writes atomic per-run directories (`run-01` .. `run-10`) and archives failed attempts to `_invalid/`. Pass `--resume` with the same `--out DIR` to continue from completed runs:

```bash
./artifact/pipeline.sh macro --topo dl --runs 10 --resume --out artifact/results/cf32_macro_dl
```
