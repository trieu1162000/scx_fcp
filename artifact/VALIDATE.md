# Validation and aggregation

## 1. Smoke test

```bash
artifact/pipeline.sh smoke
```

Smoke output is accepted only when:

```text
state before/after is recorded
scheduler hash matches the freeze
benchmark/test name matches
frames > 0
no fatal logcat event
```

## 2. JankBench checks

Every JankBench run is checked against J1-J12:

```text
J1   required files and consistency
J2   policy/run/benchmark id/name/run_count
J3   frame counts, iterations, DB/summary agreement
J4   kernel, 16 vCPU, SDK, build, SELinux
J5   scheduler/app hashes and state/ops
J6   SystemUI/system_server/benchmark PID stability
J7   logcat: injection, fatal, ANR, process death, sleep
J8   boot id stable
J9   host window and uptime
J10  host/guest load
J11  bpf_stats disabled for timing
J12  freeze manifest
```

Failed runs are stored under `_invalid/` and are excluded from aggregation.

## 3. UiBench checks

Every UiBench test/rep row is checked for:

```text
instrumentation ends OK (1 test)
GfxMonitor gfx-avg-* summary exists
>= 100 monitored frames
p50/90/95/99 and jank keys exist
wall time 10-90 s
SystemUI PID stable
foreground remains UiBench
SELinux Permissive
sched_ext state/ops match policy
device Awake
no ANR/fatal/LMK/SystemUI death in logcat
```

A test block is accepted only when all requested tests and repetitions are
present.

## 4. Aggregation

```bash
artifact/pipeline.sh analyze <campaign-root> \
  --runs 10 --require-known-invalid \
  --out-dir <campaign-root>/integration
```

Expected matrix:

```text
14 tests
4 policies
56 cells
10 accepted runs/repetitions per cell
```

The aggregator rejects stale validity files, hash/freeze mismatches,
duplicate repetitions, missing metrics, partial cells, and invalid runs.

## 5. Outputs

```text
full14_summary.json
full14_summary.md
full14_jankbench.tex
full14_uibench.tex
full14_website.html
validity.json / validity.csv
```
