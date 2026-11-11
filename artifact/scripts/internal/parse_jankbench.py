#!/usr/bin/env python3
"""Parse one JankBench result database into frames.csv and summary.json.

Usage: parse_jankbench.py <run-dir>

The run directory must contain BenchmarkResults.db (plus -wal/-shm if the
device produced them) and manifest.json.
"""
import csv
import json
import os
import sqlite3
import sys


def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (q / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def describe(values):
    if not values:
        return {
            "frames": 0,
            "mean": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
        }
    return {
        "frames": len(values),
        "mean": sum(values) / len(values),
        "p50": pct(values, 50),
        "p95": pct(values, 95),
        "p99": pct(values, 99),
        "max": max(values),
    }


def main():
    rundir = os.path.abspath(sys.argv[1])
    db_path = os.path.join(rundir, "BenchmarkResults.db")
    if not os.path.exists(db_path):
        raise SystemExit(f"missing database: {db_path}")

    with open(os.path.join(rundir, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    warmup = int(os.environ.get("WARMUP_ITERATIONS", "0"))

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = [
        "_id", "name", "run_id", "iteration", "timestamp", "unknown_delay",
        "input", "animation", "layout", "draw", "sync", "command_issue",
        "swap_buffers", "total_duration", "jank_frame", "device_charging",
    ]
    rows = con.execute(
        f"select {', '.join(cols)} from ui_results order by _id"
    ).fetchall()

    frames_csv = os.path.join(rundir, "frames.csv")
    with open(frames_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        for row in rows:
            writer.writerow([row[c] for c in cols])

    rows = [r for r in rows if int(r["iteration"]) >= warmup]
    valid = [r for r in rows if r["total_duration"] is not None]
    durations = [float(r["total_duration"]) for r in valid]
    budget_ms = float(os.environ.get("JANK_BUDGET_MS", "16.6667"))
    jank = sum(1 for r in valid if float(r["total_duration"]) > budget_ms)
    names = sorted({r["name"] for r in valid})
    run_ids = sorted({r["run_id"] for r in valid})
    iterations = sorted({r["iteration"] for r in valid})
    stamps = [r["timestamp"] for r in valid if r["timestamp"]]
    stages = ["command_issue", "swap_buffers", "unknown_delay"]

    def stage_p95(subset):
        return {
            s: pct([float(r[s]) for r in subset if r[s] is not None], 95)
            for s in stages
        }

    overall = describe(durations)
    overall["jank_frames"] = jank
    overall["jank_pct"] = (100.0 * jank / len(valid)) if valid else None
    overall["stage_p95_ms"] = stage_p95(valid)

    per_iteration = []
    for it in iterations:
        subset = [r for r in valid if r["iteration"] == it]
        d = describe([float(r["total_duration"]) for r in subset])
        j = sum(1 for r in subset if float(r["total_duration"]) > budget_ms)
        d["iteration"] = it
        d["jank_frames"] = j
        d["jank_pct"] = (100.0 * j / len(subset)) if subset else None
        d["stage_p95_ms"] = stage_p95(subset)
        per_iteration.append(d)

    summary = {
        "policy": manifest.get("policy"),
        "run": manifest.get("run"),
        "db": "BenchmarkResults.db",
        "rows": len(rows),
        "frames": len(valid),
        "names": names,
        "run_ids": run_ids,
        "iterations": iterations,
        "warmup_iterations": warmup,
        "timestamp_first": stamps[0] if stamps else None,
        "timestamp_last": stamps[-1] if stamps else None,
        "overall": overall,
        "per_iteration": per_iteration,
        "jank_budget_ms": budget_ms,
        "jank_definition": "share of total_duration above the refresh deadline",
    }
    with open(os.path.join(rundir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")

    print(
        f"{manifest.get('policy')} run {manifest.get('run')}: "
        f"frames={len(valid)} iterations={len(iterations)} "
        f"mean={overall['mean']:.3f} p95={overall['p95']:.3f} "
        f"jank={overall['jank_pct']:.3f}%"
    )


if __name__ == "__main__":
    main()
