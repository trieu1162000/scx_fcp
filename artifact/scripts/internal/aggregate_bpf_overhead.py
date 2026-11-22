#!/usr/bin/env python3
"""Aggregate per-run bpf_overhead.json files into paper-style JSON blocks.

Each input record is produced by parse_bpf_overhead.py. Records are grouped by
platform, suite, and topology, then by policy. ns_per_call is the median of the
per-run ratio; cpu_time_s is the sum of measured BPF runtime. Ratio-of-sums,
median CPU time, frames, calls/frame, and per-callback totals are retained for
auditing.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def infer_parts(root: Path, path: Path, platform: str | None,
                topology: str | None, suite: str | None) -> tuple[str, str, str]:
    rel = path.relative_to(root)
    parts = [p.lower() for p in rel.parts]
    joined = "/".join(parts)

    if suite is None:
        if "jankbench" in parts:
            suite = "jankbench"
        elif "uibench" in parts:
            suite = "uibench"
        else:
            suite = "jankbench"

    if topology is None:
        topology = "unknown"
        for token in ("sym", "dl", "asym"):
            if f"_{token}" in joined or f"-{token}" in joined:
                topology = token
                break

    if platform is None:
        if "sa8155" in joined:
            platform = "sa8155p"
        elif "cf" in joined or "cuttlefish" in joined:
            platform = "cuttlefish"
        else:
            platform = "unknown"

    return platform, suite, topology


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ns = [float(r["total_run_time_ns"]) for r in rows]
    cnt = [float(r["total_run_cnt"]) for r in rows]
    cpu = [float(r.get("bpf_cpu_s") or r["total_run_time_ns"] / 1e9) for r in rows]
    frames = [int(r.get("frames") or 0) for r in rows]
    ratios = [float(r["total_run_time_ns"]) / float(r["total_run_cnt"]) for r in rows]
    calls = [float(r["total_run_cnt"]) / float(r["frames"])
             for r in rows if r.get("frames")]

    callbacks: dict[str, dict[str, float]] = {}
    for row in rows:
        for cb in row.get("callbacks") or []:
            name = str(cb.get("callback") or cb.get("name") or cb.get("prog_id"))
            entry = callbacks.setdefault(name, {"run_time_ns": 0.0, "run_cnt": 0.0})
            entry["run_time_ns"] += float(cb.get("run_time_ns") or 0)
            entry["run_cnt"] += float(cb.get("run_cnt") or 0)
    for entry in callbacks.values():
        entry["ns_per_call"] = (
            entry["run_time_ns"] / entry["run_cnt"] if entry["run_cnt"] else None)
    total_cb_ns = sum(v["run_time_ns"] for v in callbacks.values())
    for entry in callbacks.values():
        entry["share"] = entry["run_time_ns"] / total_cb_ns if total_cb_ns else None

    return {
        "cpu_time_s": sum(ns) / 1e9,
        "ns_per_call": median(ratios),
        "ns_per_call_ratio": sum(ns) / sum(cnt),
        "cpu_time_s_median": median(cpu),
        "runs": len(rows),
        "frames": sum(frames),
        "calls_per_frame": median(calls),
        "callbacks": callbacks,
        "source_records": [r["_source"] for r in rows],
    }


def aggregate(root: Path, platform: str | None, topology: str | None,
              suite: str | None) -> dict[str, Any]:
    groups: dict[tuple[str, str, str], dict[str, list[dict[str, Any]]]] = {}
    records = sorted(root.rglob("bpf_overhead.json"))

    for path in records:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        run_ns = data.get("total_run_time_ns")
        run_cnt = data.get("total_run_cnt")
        if not isinstance(run_ns, (int, float)) or not isinstance(run_cnt, (int, float)):
            continue
        if run_ns <= 0 or run_cnt <= 0:
            continue
        policy = str(data.get("policy") or path.parent.parent.name)
        row = dict(data)
        row["_source"] = str(path.relative_to(root))
        p, s, t = infer_parts(root, path, platform, topology, suite)
        groups.setdefault((p, s, t), {}).setdefault(policy, []).append(row)

    if not groups:
        raise SystemExit(f"no usable bpf_overhead.json under {root}")

    ebpf: dict[str, Any] = {}
    dimensions: dict[str, Any] = {}
    for (p, s, t), policies in sorted(groups.items()):
        key = f"{p}_{s}_{t}"
        ebpf[key] = {
            policy: summarize_rows(rows)
            for policy, rows in sorted(policies.items())
        }
        dimensions[key] = {"platform": p, "suite": s, "topology": t}

    return {
        "schema": "scx_fcp.bpf_overhead.aggregate.v1",
        "description": "Aggregated eBPF overhead; ns_per_call is the median per-run ratio and cpu_time_s is the sum of measured BPF runtime.",
        "source_root": str(root),
        "dimensions": dimensions,
        "ebpf_overhead": ebpf,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--platform", choices=["cuttlefish", "sa8155p"])
    parser.add_argument("--topology", choices=["sym", "dl", "asym"])
    parser.add_argument("--suite", choices=["jankbench", "uibench"])
    args = parser.parse_args()
    result = aggregate(args.root, args.platform, args.topology, args.suite)
    text = json.dumps(result, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
        print(args.out)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
