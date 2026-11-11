# scx_fcp artifact

Reproducibility package for the scx_fcp paper. This directory is the
**canonical entrypoint** for building and running the evaluation; the design
dossier remains under `scx_fcp/docs/`.

Status: development. The full 14-test campaign is in progress; results are not
final until `integrate_full14.py` reports `56/56` required cells.

## Start here

| Need | Document |
|---|---|
| Build the scheduler, synthetic bench, and workload APKs | [`BUILD.md`](BUILD.md) |
| Set up Cuttlefish, run the matrix | [`RUN.md`](RUN.md) |
| Fail-fast validity, retries, aggregation | [`VALIDATE.md`](VALIDATE.md) |
| Current evaluation status and claim boundary | [`STATUS.md`](STATUS.md) |
| End-to-end command sequence | [`REPRODUCE.md`](REPRODUCE.md) |
| Paper-to-artifact map and known limitations | this file |
| Design/ADR/code map | [`../docs/README.md`](../docs/README.md) |

## Evaluated configuration

The full matrix evaluates four policies on a 16-vCPU Android 17 Cuttlefish
guest:

```text
eevdf
scx_fcp                 (frozen old 5a2e build)
scx_p2dq --enable-pelt false
scx_lavd
```

Frozen scheduler provenance:

```text
binary SHA-256: 5a2e7a7e5bca9a8cd0f9d25426e9766dae4ff4aaecfe22579a6a4d0371aab4c4
source commit:  ab428c5c4dce30159e74837079522e8a2a566ff2
source snapshot: old_5a2e_src/
```

The current repository `HEAD` is not the evaluated binary. Do not substitute a
newer scheduler build for the frozen binary and expect the paper numbers to
reproduce.

## Evaluation scope

```text
JankBench: 8 benchmark IDs (0..7)
UiBench:   6 test methods
Policies:  4
Runs:      10 valid runs/reps per (test, policy)
Cells:     14 tests x 4 policies = 56 required cells
```

Validity is checked **per run/rep**. Invalid JankBench runs and invalid UiBench
blocks are moved aside and rerun; they are never pooled into a result. See
`RUN.md` for the exact criteria.

## What is packaged here

```text
BUILD.md              host prerequisites, scheduler build, APK build
RUN.md                Cuttlefish setup, workload launch, full matrix, analysis
REPRODUCE.md          copy-paste end-to-end sequence
scripts/launch_cuttlefish.sh  parameterized 4/8/16/32-vCPU launcher
configs/              kernel/device/Cuttlefish/policy configs (added at freeze)
old_5a2e_src/         frozen scheduler source snapshot
expected/             planned expected-output schema and sample tables
```

The current lab scripts used by the campaign live under:

```text
paper/scx_fcp_v2/scripts/
  run_full_matrix_14.sh
  run_jankbench_pilot.sh
  run_stage3_jankbench.sh
  run_uibench_matrix.sh
  validate_jankbench.py
  integrate_full14.py
```

They are referenced here until the artifact directory is consolidated after
the evaluation freeze.

## Not included

- The Cuttlefish kernel, initramfs, and system image (large; obtain the
  matching AOSP 17 / Linux 6.18 image separately).
- The physical SA8155P board and its measurements.
- Fabrics or fabricated results. Every number in the paper must come from raw
  runs in a completed campaign.

## Reporting rule

Do not report a policy/cell until it has 10 valid units and the integration
step reports the cell as complete. Do not report old 4-vCPU/pilot/32-vCPU
numbers as current results.
