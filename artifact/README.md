# scx_fcp artifact

Reproducibility package for the scx_fcp paper. This directory is the
**canonical entrypoint** for building and running the evaluation; the design
dossier remains under `scx_fcp/docs/`.

The package covers the scheduler, workload APKs, Cuttlefish launch, the
validated matrix, and the aggregation path to paper tables.

## Quick start

```bash
./artifact/pipeline.sh build
./artifact/pipeline.sh launch
./artifact/pipeline.sh smoke
./artifact/pipeline.sh full
./artifact/pipeline.sh analyze --out results/full14
```

## Start here

| Need | Document |
|---|---|
| Host, Cuttlefish image, kernel, initramfs | [`SETUP.md`](SETUP.md) |
| Build the scheduler, synthetic bench, and workload APKs | [`BUILD.md`](BUILD.md) |
| Quick start, Cuttlefish launch, run the matrix | [`RUN.md`](RUN.md) |
| Fail-fast validity, retries, aggregation | [`VALIDATE.md`](VALIDATE.md) |
| Paper-to-artifact map and known limitations | this file |
| Design/ADR/code map | See the source tree's `docs/` dossier (not included in this docs branch) |

## Evaluated configuration

The full matrix evaluates four policies on a 16-vCPU Android 17 Cuttlefish
guest:

```text
eevdf
scx_fcp                 (zero-hint build)
scx_p2dq --enable-pelt false
scx_lavd
```

The evaluated scheduler source is in `old_5a2e_src/`; the pipeline builds it
into `artifact/build/bin/scx_fcp_behavioral`.

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
SETUP.md              host, Cuttlefish image, kernel, initramfs
BUILD.md              scheduler build, APK build
RUN.md                quick start, Cuttlefish launch, full matrix, analysis
scripts/launch_cuttlefish.sh  parameterized 4/8/16/32-vCPU launcher
configs/              kernel/device/Cuttlefish/policy configs (added at freeze)
old_5a2e_src/         frozen scheduler source snapshot
expected/             planned expected-output schema and sample tables
```

The public pipeline is:

```text
artifact/pipeline.sh     build | launch | smoke | full | resume | analyze
```

Per-benchmark adapters stay under `artifact/scripts/internal/` and are hidden
behind the pipeline interface.

## Not included

- The Cuttlefish kernel, initramfs, and system image (obtain the matching
  AOSP 17 / Linux 6.18 image separately).
- The physical SA8155P board and its measurements.
