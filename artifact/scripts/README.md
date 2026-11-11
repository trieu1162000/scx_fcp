# Artifact scripts

```text
launch_cuttlefish.sh      4/8/16/32-vCPU launcher
run_full_matrix_14.sh     smoke/full/resume/fail-fast orchestrator
run_stage3_jankbench.sh
run_jankbench_pilot.sh
run_jankbench_focused.sh
run_uibench_matrix.sh
run_uibench.sh
validate_jankbench.py
validate_uibench.py
extract_uibench.py
integrate_full14.py
aggregate_full_matrix.py
jankbench_benchmarks.json
parse_jankbench.py
summarize_uibench.py
```

Paths are artifact-relative; no evaluation-tree path is required. Before a
release, run:

```bash
bash -n run_full_matrix_14.sh
python3 -m py_compile *.py
./run_full_matrix_14.sh --help
```

Do not use stale scripts from the evaluation tree without this check.
