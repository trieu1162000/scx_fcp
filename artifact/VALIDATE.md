# Validation and aggregation

This is the fail-fast contract for the matrix. A result is not a paper result
until every required cell is valid.

## 1. Smoke gate

Run before the full matrix:

```bash
# one JankBench benchmark id, one policy pair
run_full_matrix.sh --out results/smoke --jank-tests 0 --skip-uibench --runs 1

# one UiBench test, one policy pair
run_full_matrix.sh --out results/smoke-ui --skip-jank --ui-tests inflating-list --runs 1
```

Accept the smoke only if:

```text
state before/after is recorded
scheduler hash matches the freeze
expected benchmark/test name matches
frames > 0
no fatal logcat event
```

Stop immediately on a smoke failure. Do not run the full matrix hoping it
recovers.

## 2. JankBench validity

Every JankBench run must pass the evaluable J1-J12 checks:

```text
J1   required files + consistency
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

A failed run is moved to `_invalid/` and retried. It is never silently dropped
or averaged into the valid cell.

## 3. UiBench validity

Every UiBench test/rep row must pass:

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

A block is accepted only when all required tests have exactly the requested
repetitions and no invalid row. Invalid blocks are archived and rerun.

## 4. Aggregation gate

```bash
python3 scripts/integrate_full14.py <campaign-root> \
  --runs 10 --require-known-invalid --out-dir <campaign-root>/integration
```

Required:

```text
14 tests
4 policies
56/56 cells
10 valid units per cell
```

The aggregator must reject:

```text
stale validity.json
freeze/hash mismatch
duplicate repetitions
missing metrics
partial cells
invalid runs
```

## 5. Expected outputs

```text
full14_summary.json
full14_summary.md
full14_jankbench.tex
full14_uibench.tex
full14_website.html
validity.json / validity.csv
```

Any nonzero exit is a failed evaluation, not a paper result.

## 6. Stop/go rule

```text
smoke FAIL      -> stop
one invalid     -> archive + retry
cell incomplete -> stop/retry, never invent
56/56 valid     -> paper tables
otherwise       -> report incomplete, no claim
```
