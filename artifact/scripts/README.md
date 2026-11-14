# Artifact pipeline

The public interface is one command:

```bash
./artifact/pipeline.sh build
./artifact/pipeline.sh launch
./artifact/pipeline.sh smoke
./artifact/pipeline.sh full
./artifact/pipeline.sh resume --out results/full14
./artifact/pipeline.sh analyze --out results/full14
```

Internal implementation lives under `scripts/internal/` and is not part of the
user-facing interface:

```text
scripts/launch_cuttlefish.sh
scripts/internal/build_static.sh
scripts/internal/run_full_matrix_14.sh
scripts/internal/run_stage3_jankbench.sh
scripts/internal/run_jankbench_pilot.sh
scripts/internal/run_uibench_matrix.sh
scripts/internal/run_uibench.sh
scripts/internal/validate_jankbench.py
scripts/internal/validate_uibench.py
scripts/internal/integrate_full14.py
scripts/internal/aggregate_full_matrix.py
scripts/internal/jankbench_benchmarks.json
```

The pipeline keeps one smoke/full/resume/analyze interface while the
per-benchmark adapters stay isolated for debugging.
