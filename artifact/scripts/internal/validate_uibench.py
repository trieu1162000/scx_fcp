#!/usr/bin/env python3
"""Aggregate UiBench block metrics into a 10/10 validity report.

The block runner (run_uibench_matrix.sh) already validates each block before
accepting it.  This script is the matrix-level view: it walks every accepted
block under a result root, joins the per-row validity flags from
extract_uibench.py, and writes validity.json / validity.csv plus one line per
(policy, test) cell.

Usage:
  validate_uibench.py ROOT [--policies a,b,c] [--tests t1,t2] [--reps 10]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import OrderedDict

CSV_FIELDS = [
    "policy", "test", "reps_expected", "reps_valid", "reps_invalid",
    "reps_missing", "status", "valid_runs", "invalid_reasons",
]


def walk_metrics(root):
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith("_")]
        if "metrics.json" in files:
            yield os.path.join(base, "metrics.json")


def policy_from_path(path, data):
    if data.get("policy"):
        return data["policy"]
    parts = os.path.normpath(path).split(os.sep)
    if "uibench" in parts:
        idx = parts.index("uibench")
        if idx >= 1:
            return parts[idx - 1]
    return "unknown"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root")
    ap.add_argument("--policies", default=None)
    ap.add_argument("--tests", default=None)
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    policies = [p for p in (args.policies or "").split(",") if p]
    tests = [t for t in (args.tests or "").split(",") if t]

    rows = []
    for path in sorted(walk_metrics(root)):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        policy = policy_from_path(path, data)
        for row in data.get("tests") or []:
            rows.append({
                "policy": policy,
                "test": row.get("test"),
                "rep": int(row.get("rep") or 0),
                "valid": bool(row.get("valid")),
                "status": row.get("status"),
                "invalid_reason": row.get("invalid_reason") or "",
                "wall_time_s": row.get("wall_time_s"),
                "frames": row.get("frames"),
                "p50_ms": row.get("p50_ms"),
                "p90_ms": row.get("p90_ms"),
                "p95_ms": row.get("p95_ms"),
                "jank_pct": row.get("jank_pct"),
                "metrics_path": os.path.relpath(path, root),
            })

    if not policies:
        policies = sorted({r["policy"] for r in rows})
    if not tests:
        tests = sorted({r["test"] for r in rows if r["test"]})

    by_cell = OrderedDict()
    for policy in policies:
        for test in tests:
            by_cell[(policy, test)] = []
    for row in rows:
        by_cell.setdefault((row["policy"], row["test"]), []).append(row)

    cells = OrderedDict()
    all_ok = True
    for (policy, test), items in by_cell.items():
        seen = {}
        for item in sorted(items, key=lambda r: r["rep"]):
            seen.setdefault(item["rep"], []).append(item)
        valid_rows = [i for i in items if i["valid"]]
        invalid = [i for i in items if not i["valid"]]
        missing = [rep for rep in range(1, args.reps + 1) if rep not in seen]
        duplicates = [rep for rep, group in seen.items() if len(group) > 1]
        reasons = []
        if missing:
            reasons.append("missing reps " + ",".join(str(r) for r in missing))
        if duplicates:
            reasons.append("duplicate reps " + ",".join(str(r) for r in duplicates))
        if invalid:
            reasons.append("invalid: " + " | ".join(
                f"rep{r['rep']}: {r['invalid_reason'] or r['status']}"
                for r in invalid))
        if len(valid_rows) < args.reps and not missing and not invalid:
            reasons.append(f"{len(valid_rows)} valid rows, expected {args.reps}")
        ok = (len(valid_rows) >= args.reps and not missing
              and not duplicates and not invalid)
        all_ok = all_ok and ok
        cells[f"{policy}|{test}"] = {
            "policy": policy,
            "test": test,
            "reps_expected": args.reps,
            "reps_valid": len(valid_rows),
            "reps_invalid": len(invalid),
            "reps_missing": len(missing),
            "status": "pass" if ok else "fail",
            "valid_reasons": reasons,
            "rows": sorted(items, key=lambda r: r["rep"]),
        }

    report = {
        "schema": "scx_fcp.uibench_validity.v1",
        "kind": "uibench-validity",
        "root": root,
        "policies": policies,
        "tests": tests,
        "reps_expected": args.reps,
        "summary": {
            "cells_expected": len(policies) * len(tests),
            "cells_pass": sum(1 for c in cells.values() if c["status"] == "pass"),
            "cells_fail": sum(1 for c in cells.values() if c["status"] != "pass"),
            "all_cells_10_of_10": all_ok,
        },
        "cells": cells,
    }

    out_dir = args.out_dir or root
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "validity.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    with open(os.path.join(out_dir, "validity.csv"), "w", newline="",
              encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for cell in cells.values():
            writer.writerow({
                "policy": cell["policy"],
                "test": cell["test"],
                "reps_expected": cell["reps_expected"],
                "reps_valid": cell["reps_valid"],
                "reps_invalid": cell["reps_invalid"],
                "reps_missing": cell["reps_missing"],
                "status": cell["status"],
                "valid_runs": cell["reps_valid"],
                "invalid_reasons": " | ".join(cell["valid_reasons"]),
            })

    for cell in cells.values():
        print(f"{cell['policy']:34s} {cell['test']:22s} "
              f"{cell['reps_valid']:2d}/{cell['reps_expected']} "
              f"{cell['status']}")
    print("UI_BENCH_ALL_VALID" if all_ok else "UI_BENCH_INCOMPLETE")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
