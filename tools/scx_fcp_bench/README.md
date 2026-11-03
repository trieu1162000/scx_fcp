# `fcp_frame_bench`

`fcp_frame_bench` is a scheduler-neutral Android frame-pipeline workload. It
models a synchronous `UI -> Render -> Binder -> GPU` chain with futex wakeups
and adds CPU-burning background workers. Every run emits one JSON record with
frame percentiles, 60/90 Hz jank rates, approximate CPU placement, and
critical-path CPU time.

The energy field is an estimate based on configurable little/big power
coefficients. It is not a hardware power measurement. For paper results,
replace it with INA231, Monsoon, or a Cuttlefish host power measurement.

Build and run locally:

```sh
make -C tools/scx_fcp_bench
tools/scx_fcp_bench/fcp_frame_bench \
  --scheduler baseline --frames 120 --hz 60 --stage-us 700 --bg-workers 2
```

The binary is a glibc x86-64 executable. On the Android Cuttlefish guest,
push it beside the scheduler binaries and invoke it through
`/data/local/tmp/scx/run_scx.sh`.
