**Table 1**: 32-vCPU Symmetric SMP (Uniform capacity 1024 : 1024, 20 background workers) AOSP `JankBench` aggregate evaluation (top) and per-benchmark breakdown (bottom). `scx_fcp` outperforms all three baselines on **every** aggregate and per-benchmark metric.

| Scheduler | Frames (N) | Mean (ms) | p₅₀ (ms) | p₉₅ (ms) | p₉₉ (ms) | Max (ms) | AOSP Jank (%) | Frames ≥ 12 ms (%) | BPF (ns/call) | BPF CPU (s) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Baseline (EEVDF)** | 4,307 | 5.127 | 4.600 | 7.501 | 19.513 | **112.186** | 0.580% | 1.323% | N/A | N/A |
| **scx_p2dq** | 4,451 | 23.274 | 28.562 | 49.638 | 55.097 | 153.327 | 45.136% | 67.535% | 1,205.2 | 102.30 |
| **scx_lavd** | 4,265 | 6.598 | 5.861 | 10.011 | 24.624 | 137.040 | 0.539% | 3.494% | 517.2 | 58.50 |
| **scx_fcp (Ours)** | **4,582** | **3.310** | **2.893** | **5.029** | **8.759** | 113.350 | **0.349%** | **0.655%** | **110.6** | **8.43** |

<br>

| Benchmark (JankBench Symmetric) | Scheduler | Frames | Mean (ms) | p₅₀ (ms) | p₉₅ (ms) | p₉₉ (ms) | AOSP Jank (%) | Frames ≥ 12 ms (%) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **List View Fling** | Baseline (EEVDF) | 1,540 | 4.923 | 4.598 | 6.684 | 9.103 | **0.32%** | 0.78% |
| | scx_p2dq | 1,522 | 18.671 | 16.900 | 37.478 | 51.851 | 41.46% | 53.68% |
| | scx_lavd | 1,539 | 6.487 | 6.408 | 9.113 | 12.611 | **0.32%** | 1.17% |
| | **scx_fcp (Ours)** | **1,540** | **2.865** | **2.547** | **4.001** | **8.095** | **0.32%** | **0.65%** |
| **Image List View Fling** | Baseline (EEVDF) | 1,547 | 4.537 | 4.008 | 7.355 | 12.527 | 0.58% | 1.10% |
| | scx_p2dq | 1,459 | 22.201 | 18.331 | 49.551 | 57.309 | 40.37% | 70.32% |
| | scx_lavd | 1,152 | 6.311 | 5.643 | 9.694 | 22.208 | 0.60% | 2.05% |
| | **scx_fcp (Ours)** | **1,514** | **3.380** | **2.974** | **5.190** | **10.771** | **0.53%** | **0.99%** |
| **Shadow Grid Fling** | Baseline (EEVDF) | 1,220 | 6.131 | 5.380 | 8.727 | 23.409 | 0.90% | 2.30% |
| | scx_p2dq | 1,470 | 29.103 | 31.860 | 50.458 | 61.163 | 53.67% | 79.12% |
| | scx_lavd | 1,214 | 7.098 | 5.324 | 22.477 | 26.850 | 0.74% | 8.24% |
| | **scx_fcp (Ours)** | **1,528** | **3.688** | **3.504** | **5.240** | **8.149** | **0.20%** | **0.33%** |

---

**Table 2**: 32-vCPU Asymmetric big.LITTLE (446 : 1024 capacity, 20 background contention workers) AOSP `JankBench` aggregate evaluation (top) and per-benchmark breakdown (bottom).

| Scheduler | Frames (N) | Mean (ms) | p₅₀ (ms) | p₉₅ (ms) | p₉₉ (ms) | Max (ms) | AOSP Jank (%) | Frames ≥ 12 ms (%) | BPF (ns/call) | BPF CPU (s) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Baseline (EEVDF)** | 4,591 | 5.025 | 4.171 | 10.176 | **15.439** | 120.820 | 0.457% | 4.095% | N/A | N/A |
| **scx_p2dq** | 4,173 | 28.199 | 30.638 | 50.121 | 64.652 | 154.620 | 44.596% | 80.757% | 1,225.1 | 97.08 |
| **scx_lavd** | 4,295 | 4.969 | 4.969 | 9.307 | 17.034 | 140.154 | 0.396% | 1.863% | 487.6 | 59.70 |
| **scx_fcp (Ours)** | **4,303** | **3.698** | **3.228** | **5.098** | 17.107 | **110.699** | **0.395%** | **1.139%** | **113.4** | **8.13** |

<br>

| Benchmark (JankBench Asymmetric) | Scheduler | Frames | Mean (ms) | p₅₀ (ms) | p₉₅ (ms) | p₉₉ (ms) | AOSP Jank (%) | Frames ≥ 12 ms (%) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **List View Fling** | Baseline (EEVDF) | 1,541 | 3.793 | 3.281 | 5.324 | 9.766 | 0.45% | 0.71% |
| | scx_p2dq | 1,508 | 24.803 | 30.720 | 49.417 | 58.782 | 41.11% | 76.19% |
| | scx_lavd | 1,544 | 5.222 | 4.690 | 7.675 | 10.952 | 0.32% | 0.71% |
| | **scx_fcp (Ours)** | **1,539** | **3.814** | **3.516** | **4.946** | **8.237** | **0.32%** | **0.52%** |
| **Image List View Fling** | Baseline (EEVDF) | 1,522 | 4.466 | 3.994 | 7.135 | 15.291 | 0.72% | 1.25% |
| | scx_p2dq | 1,493 | 29.562 | 30.762 | 50.436 | 63.903 | 46.75% | 81.78% |
| | scx_lavd | 1,534 | 4.972 | 4.356 | 8.578 | 14.348 | 0.33% | 1.30% |
| | **scx_fcp (Ours)** | **1,547** | **3.106** | **2.858** | **4.302** | **8.848** | **0.32%** | **0.90%** |
| **Shadow Grid Fling** | Baseline (EEVDF) | 1,528 | 6.823 | 5.884 | 13.608 | **16.030** | **0.20%** | 10.34% |
| | scx_p2dq | 1,172 | 30.834 | 30.306 | 51.113 | 66.070 | 46.33% | 85.32% |
| | scx_lavd | 1,217 | 7.022 | 6.022 | 11.163 | 25.654 | 0.58% | 4.03% |
| | **scx_fcp (Ours)** | **1,217** | **4.302** | **3.688** | **6.436** | 20.052 | 0.58% | **2.22%** |

<br>
<img width="100%" height="211" alt="image" src="https://github.com/user-attachments/assets/adaedcf3-e04b-4c6a-a637-dd4ca8c6b854" />

**Figure 1**: AOSP `FrameMetrics` stage-latency summaries on 32-vCPU Cuttlefish. Panels (a) and (b) show the asymmetric and symmetric topologies, respectively. Markers show each stage's measured p₉₅ duration in milliseconds on a logarithmic scale; lines connect stage summaries and do not represent per-frame distributions.
