# Evaluation status

Last updated: 2026-11-11

## Current status

```text
artifact/docs: ready for review
fast synthetic A/B: complete
mean-win gate: FAIL
AOSP 14-test matrix: not run
paper: draft/placeholders only
```

## What is verified

The synthetic workload is four serial fixed stages:

```text
90 Hz: 4 x 1800 us = 7.200 ms floor
60 Hz: 4 x  700 us = 2.800 ms floor
```

Measured EEVDF p50 is close to that floor. The mean −0.5 ms gate is therefore
not reachable on this workload for any queue-order scheduler.

Observed FCP effects:

```text
p95 tail: improved in several cells
mean:     tie or small regression
high-load FCP: frame drops; not a valid win
```

Patch 1 (critical task skips an idle little previous CPU) improves p95 but not
mean. The 2500 us same-TGID window fixes token depth propagation but not mean.

## What is not claimed

```text
- no mean win over EEVDF
- no AOSP result
- no full 14-test result
- no use of the cancelled full14 campaign
- no use of the old 32-vCPU table
```

## Next decision

```text
A. tail/jank claim with mean parity (recommended)
B. a new, pre-registered workload with scheduler-induced stalls
C. a new mechanism with evidence beyond the fixed-work bound
```

Any route must pass `VALIDATE.md`; invalid runs are archived and rerun.
