#!/usr/bin/env python3
"""Snapshot struct_ops BPF runtime counters without bpftool.

The guest exposes per-program counters through /proc/<pid>/fdinfo/<fd> when
kernel.bpf_stats_enabled is set.  Keep this collector in the artifact until the
device image ships a usable bpftool or libbpf helper.

TODO(1.4): replace this fdinfo fallback with a dedicated bpf_prog_info helper
that also reports program names and can be shipped as part of the artifact.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

# uapi/linux/bpf.h: BPF_PROG_TYPE_STRUCT_OPS.  The enum value is stable.
STRUCT_OPS_PROG_TYPE = 27


def snapshot(adb: str, serial: str, binary: str) -> list[dict[str, object]]:
    if not binary:
        return []

    remote = f"""
set -eu
bin={shlex.quote(Path(binary).name)}
pid=$(pidof "$bin" | awk '{{print $1}}')
if [ -z "$pid" ]; then
    # pidof may compare a truncated /proc comm on some Android builds.
    pid=$(ps -A -o PID,ARGS | awk -v b="$bin" '{{n=$2; sub(/^.*\\//, "", n); if (n == b) {{print $1; exit}}}}')
fi
[ -n "$pid" ] || {{ echo "scheduler process not found: $bin" >&2; exit 2; }}
for f in /proc/$pid/fdinfo/*; do
    [ -r "$f" ] || continue
    type=$(awk '/^prog_type:/ {{print $2; exit}}' "$f")
    [ "$type" = "{STRUCT_OPS_PROG_TYPE}" ] || continue
    id=$(awk '/^prog_id:/ {{print $2; exit}}' "$f")
    [ -n "$id" ] || continue
    ns=$(awk '/^run_time_ns:/ {{print $2; exit}}' "$f")
    cnt=$(awk '/^run_cnt:/ {{print $2; exit}}' "$f")
    printf '%s %s %s\\n' "$id" "$ns" "$cnt"
done
"""
    proc = subprocess.run(
        [adb, "-s", serial, "shell", remote],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"adb rc={proc.returncode}"
        raise RuntimeError(detail)

    rows: dict[int, dict[str, object]] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        prog_id, run_time_ns, run_cnt = (int(part) for part in parts)
        rows.setdefault(prog_id, {
            "id": prog_id,
            "type": "struct_ops",
            "name": f"prog_{prog_id}",
            "run_time_ns": run_time_ns,
            "run_cnt": run_cnt,
        })
    if not rows:
        raise RuntimeError(f"no struct_ops programs found under {binary}")
    return [rows[prog_id] for prog_id in sorted(rows)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--serial", required=True)
    parser.add_argument("--bin", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    try:
        rows = snapshot(args.adb, args.serial, args.bin)
    except RuntimeError as exc:
        print(f"bpf_snapshot_fdinfo: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(rows, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
