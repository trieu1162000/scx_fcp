# schbench

Vendored upstream `schbench` scheduler wakeup-latency benchmark.

Source: https://github.com/masoncl/schbench (`schbench.c`)
License: GPL-2.0 (see the copyright header in `schbench.c`).

Build a static binary for the target:

```bash
make -C tools/schbench CC=cc CFLAGS="-O2 -g" LDFLAGS="-static"
```

The artifact build also installs `schbench` into the selected `BIN_DIR`.
