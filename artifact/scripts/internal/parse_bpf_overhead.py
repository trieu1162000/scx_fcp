#!/usr/bin/env python3
"""Parse before/after struct_ops runtime snapshots into one BPF-overhead record.

The snapshot producer is bpf_snapshot_fdinfo.py; it emits the minimal
program-id/runtime/count JSON shape consumed below.

TODO(1.4): switch to the native BPF stats helper once it ships in the image.
"""

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path


def load_progs(path: Path) -> dict[int, dict]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    data = json.loads(path.read_text())
    return {int(p["id"]): p for p in data if p.get("type") == "struct_ops"}


def wall_seconds(manifest: dict) -> float:
    try:
        start = datetime.fromisoformat(manifest["host_start"])
        end = datetime.fromisoformat(manifest["host_end"])
        return max(0.0, (end - start).total_seconds())
    except (KeyError, TypeError, ValueError):
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    args = ap.parse_args()
    run = args.run_dir

    before = load_progs(run / "bpf_before.json")
    after = load_progs(run / "bpf_after.json")
    if not before or not after:
        raise SystemExit(f"missing struct_ops snapshots in {run}")

    manifest = json.loads((run / "manifest.json").read_text())
    summary = json.loads((run / "summary.json").read_text())
    frames = int(summary.get("frames") or 0)
    wall_s = wall_seconds(manifest)

    rows = []
    total_ns = 0
    total_cnt = 0
    for pid in sorted(set(before) | set(after)):
        b = before.get(pid, {})
        a = after.get(pid, {})
        b_ns = int(b.get("run_time_ns", 0))
        a_ns = int(a.get("run_time_ns", 0))
        b_cnt = int(b.get("run_cnt", 0))
        a_cnt = int(a.get("run_cnt", 0))
        d_ns = a_ns - b_ns
        d_cnt = a_cnt - b_cnt
        if d_ns < 0 or d_cnt < 0:
            raise SystemExit(f"counter reset for prog {pid} in {run}")
        name = a.get("name") or b.get("name") or str(pid)
        if d_cnt == 0 and d_ns == 0:
            continue
        total_ns += d_ns
        total_cnt += d_cnt
        rows.append({
            "prog_id": pid,
            "callback": name,
            "run_time_ns": d_ns,
            "run_cnt": d_cnt,
            "ns_per_call": d_ns / d_cnt if d_cnt else None,
            "calls_per_frame": d_cnt / frames if frames else None,
            "us_per_frame": d_ns / 1e3 / frames if frames else None,
        })

    for r in rows:
        r["share"] = r["run_time_ns"] / total_ns if total_ns else None
    rows.sort(key=lambda r: r["run_time_ns"], reverse=True)

    out = {
        "policy": manifest.get("policy"),
        "run": manifest.get("run"),
        "frames": frames,
        "wall_s": wall_s,
        "bpf_stats_requested": manifest.get("bpf_stats_requested"),
        "bpf_stats_before": (run / "bpf_stats_enabled_before.txt").read_text().strip()
        if (run / "bpf_stats_enabled_before.txt").exists() else None,
        "bpf_stats_after": (run / "bpf_stats_enabled_after.txt").read_text().strip()
        if (run / "bpf_stats_enabled_after.txt").exists() else None,
        "scheduler_sha256": manifest.get("scheduler_sha256"),
        "total_run_time_ns": total_ns,
        "total_run_cnt": total_cnt,
        "bpf_cpu_s": total_ns / 1e9,
        "ns_per_call": total_ns / total_cnt if total_cnt else None,
        "calls_per_frame": total_cnt / frames if frames else None,
        "us_per_frame": total_ns / 1e3 / frames if frames else None,
        "pct_one_cpu": (total_ns / 1e9) / wall_s * 100 if wall_s else None,
        "pct_guest_capacity": ((total_ns / 1e9) / wall_s * 100 / manifest.get("nproc", 1))
        if wall_s and manifest.get("nproc") else None,
        "callbacks": rows,
    }
    (run / "bpf_overhead.json").write_text(json.dumps(out, indent=2) + "\n")

    with (run / "bpf_overhead_callbacks.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["prog_id", "callback", "run_time_ns", "run_cnt",
                                          "ns_per_call", "calls_per_frame", "us_per_frame", "share"])
        w.writeheader()
        w.writerows(rows)

    print(f"{out['policy']} run {out['run']}: "
          f"{out['ns_per_call']:.1f} ns/call, {out['bpf_cpu_s']:.4f} s, "
          f"{out['us_per_frame']:.4f} us/frame")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
