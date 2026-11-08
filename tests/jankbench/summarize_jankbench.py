#!/usr/bin/env python3
"""Summarize one or more JankBench result directories.

Each run directory must contain BenchmarkResults.db and should contain
manifest.json. A typical layout is:

    <results-root>/<policy>/run-<n>/BenchmarkResults.db

Usage:
    summarize_jankbench.py [results-root ...] [--out-dir DIR]

The default root is $RESULTS_ROOT, then ./results/jankbench. The script
writes summary.csv, summary.md, and summary.json and prints a compact table.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

STAGES = ("command_issue", "swap_buffers", "unknown_delay")
FIELDS = [
    "policy", "run", "frames", "mean_ms", "p50_ms", "p95_ms", "p99_ms",
    "max_ms", "jank_frames", "jank_pct", "p95_command_issue_ms",
    "p95_swap_buffers_ms", "p95_unknown_delay_ms", "run_dir", "valid",
    "invalid_reason",
]


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q / 100.0
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def describe(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "frames": 0, "mean_ms": None, "p50_ms": None, "p95_ms": None,
            "p99_ms": None, "max_ms": None,
        }
    return {
        "frames": len(values),
        "mean_ms": sum(values) / len(values),
        "p50_ms": percentile(values, 50),
        "p95_ms": percentile(values, 95),
        "p99_ms": percentile(values, 99),
        "max_ms": max(values),
    }


def infer_policy(run_dir: Path) -> str:
    parts = run_dir.parts
    if "run-" in run_dir.name and len(parts) >= 2:
        return run_dir.parent.name
    return parts[-1] if parts else "unknown"


def read_manifest(run_dir: Path) -> dict:
    path = run_dir / "manifest.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def read_run(run_dir: Path) -> dict:
    manifest = read_manifest(run_dir)
    policy = manifest.get("policy") or infer_policy(run_dir)
    run = manifest.get("run")
    if run is None:
        suffix = run_dir.name.split("-")[-1]
        run = int(suffix) if suffix.isdigit() else run_dir.name

    row = {
        "policy": policy,
        "run": run,
        "frames": 0,
        "mean_ms": None,
        "p50_ms": None,
        "p95_ms": None,
        "p99_ms": None,
        "max_ms": None,
        "jank_frames": 0,
        "jank_pct": None,
        "p95_command_issue_ms": None,
        "p95_swap_buffers_ms": None,
        "p95_unknown_delay_ms": None,
        "run_dir": str(run_dir),
        "valid": True,
        "invalid_reason": "",
    }

    db = run_dir / "BenchmarkResults.db"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "select total_duration, jank_frame, command_issue, swap_buffers, "
            "unknown_delay from ui_results"
        ).fetchall()
        con.close()
    except sqlite3.Error as exc:
        row["valid"] = False
        row["invalid_reason"] = f"database error: {exc}"
        return row

    usable = [r for r in rows if r["total_duration"] is not None]
    durations = [float(r["total_duration"]) for r in usable]
    if not durations:
        row["valid"] = False
        row["invalid_reason"] = "no total_duration rows"
        return row

    stats = describe(durations)
    budget_ms = float(os.environ.get("JANK_BUDGET_MS", "16.6667"))
    jank = sum(1 for d in durations if d > budget_ms)
    row.update(stats)
    row["jank_frames"] = jank
    row["jank_pct"] = 100.0 * jank / len(usable)
    for stage in STAGES:
        values = [float(r[stage]) for r in usable if r[stage] is not None]
        row[f"p95_{stage}_ms"] = percentile(values, 95)
    return row


def fmt(value, digits: int = 3) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="*", type=Path)
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()

    roots = args.roots
    if not roots:
        env_root = os.environ.get("RESULTS_ROOT")
        roots = [Path(env_root) if env_root else Path("results/jankbench")]
    roots = [root.resolve() for root in roots]
    runs: list[dict] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for db in sorted(root.rglob("BenchmarkResults.db")):
            run_dir = db.parent.resolve()
            if run_dir in seen:
                continue
            seen.add(run_dir)
            runs.append(read_run(run_dir))

    out_dir = (args.out_dir or roots[0]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if not runs:
        print("no BenchmarkResults.db found under: " + ", ".join(str(r) for r in roots))
        return 1

    runs.sort(key=lambda r: (str(r["policy"]), str(r["run"])))
    with (out_dir / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for row in runs:
            writer.writerow({key: row.get(key) for key in FIELDS})

    groups: dict[str, list[dict]] = defaultdict(list)
    for row in runs:
        if row["valid"]:
            groups[row["policy"]].append(row)

    lines = ["# JankBench summary", ""]
    lines.append("| Policy | Runs | Frames | Mean ms | p50 ms | p95 ms | p99 ms | Max ms | Jank % |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for policy in sorted(groups):
        group = groups[policy]
        def med(key: str):
            values = [r[key] for r in group if r[key] is not None]
            return percentile(values, 50)
        frames = sum(int(r["frames"]) for r in group)
        lines.append(
            f"| {policy} | {len(group)} | {frames} | {fmt(med('mean_ms'))} | "
            f"{fmt(med('p50_ms'))} | {fmt(med('p95_ms'))} | "
            f"{fmt(med('p99_ms'))} | {fmt(med('max_ms'))} | "
            f"{fmt(med('jank_pct'), 2)} |"
        )
    lines.append("")
    lines.append("## Per run")
    lines.append("")
    lines.append("| Policy | Run | Frames | Mean ms | p95 ms | p99 ms | Jank % | Valid |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---|")
    for row in runs:
        lines.append(
            f"| {row['policy']} | {row['run']} | {row['frames']} | "
            f"{fmt(row['mean_ms'])} | {fmt(row['p95_ms'])} | "
            f"{fmt(row['p99_ms'])} | {fmt(row['jank_pct'], 2)} | "
            f"{'yes' if row['valid'] else row['invalid_reason']} |"
        )
    lines.append("")
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    summary = {
        "roots": [str(root) for root in roots],
        "runs": runs,
        "policy_medians": {
            policy: {
                "runs": len(group),
                "frames": sum(int(r["frames"]) for r in group),
                "mean_ms": percentile([r["mean_ms"] for r in group if r["mean_ms"] is not None], 50),
                "p50_ms": percentile([r["p50_ms"] for r in group if r["p50_ms"] is not None], 50),
                "p95_ms": percentile([r["p95_ms"] for r in group if r["p95_ms"] is not None], 50),
                "p99_ms": percentile([r["p99_ms"] for r in group if r["p99_ms"] is not None], 50),
                "max_ms": percentile([r["max_ms"] for r in group if r["max_ms"] is not None], 50),
                "jank_pct": percentile([r["jank_pct"] for r in group if r["jank_pct"] is not None], 50),
            }
            for policy, group in sorted(groups.items())
        },
    }
    with (out_dir / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")

    print("policy                     runs  frames   mean    p50    p95    p99   jank%")
    for policy in sorted(groups):
        group = groups[policy]
        med = lambda key: percentile([r[key] for r in group if r[key] is not None], 50)
        print(
            f"{policy:26s} {len(group):4d} {sum(int(r['frames']) for r in group):7d} "
            f"{fmt(med('mean_ms')):>7s} {fmt(med('p50_ms')):>6s} "
            f"{fmt(med('p95_ms')):>6s} {fmt(med('p99_ms')):>6s} "
            f"{fmt(med('jank_pct'), 2):>6s}"
        )
    print(f"wrote {out_dir / 'summary.csv'}")
    print(f"wrote {out_dir / 'summary.md'}")
    print(f"wrote {out_dir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
