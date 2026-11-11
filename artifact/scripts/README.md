# Artifact scripts

This docs branch contains only the Cuttlefish launcher. The evaluation
implementation is staged here at artifact freeze; it is intentionally not
duplicated from the working paper tree before the results are frozen.

Required final contents:

```text
build_all.sh                 # scheduler, baselines, bench, APK staging
run_full_matrix_14.sh        # smoke/full/resume/fail-fast orchestrator
run_stage3_jankbench.sh
run_jankbench_pilot.sh
run_jankbench_focused.sh
run_uibench_matrix.sh
run_uibench.sh
validate_jankbench.py
validate_uibench.py
jankbench_benchmarks.json
extract_uibench.py
integrate_full14.py
aggregate_full_matrix.py
launch_cuttlefish.sh         # present in this branch
```

## Staging rule

Do not copy stale working-tree scripts without checking:

```text
1. all paths are artifact-relative or environment-driven
2. no paper/scx_fcp_v2 path remains
3. no author-specific absolute path remains
4. script --help works
5. smoke run passes before full matrix
```

The docs branch currently has `launch_cuttlefish.sh` only. This README is the
mapping/checklist for the later staging commit.
