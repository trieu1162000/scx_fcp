#!/usr/bin/env python3
"""Join the 8 JankBench cells and 6 UiBench cells into one 14-test report.

Usage:
  aggregate_full_matrix.py OUT_ROOT [--policies a,b] [--runs 10]
                          [--benchmarks-config JSON]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

PAPER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(PAPER, "scripts", "jankbench_benchmarks.json")
UI_TESTS = [
    "inflating-list", "shadow-grid-list", "slow-bind-recycler",
    "trivial-recycler", "layout-cache-low", "bitmap-upload",
]
CSV_FIELDS = [
    "workload", "test", "policy", "valid", "expected", "status", "reasons",
    "path",
]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root")
    ap.add_argument("--policies", default="eevdf,scx_fcp_v_a_depth3_tgid1ms,"
                                         "scx_p2dq_pelt_off,scx_lavd")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--benchmarks-config", default=DEFAULT_CONFIG)
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    policies = [p for p in args.policies.split(",") if p]
    runs = args.runs
    cfg = json.load(open(args.benchmarks_config, encoding="utf-8"))
    benchmarks = cfg.get("benchmarks") or {}

    rows = []

    for bid, spec in sorted(benchmarks.items(), key=lambda kv: int(kv[0])):
        slug = spec.get("slug") or f"id{bid}"
        vpath = os.path.join(root, "jankbench", slug, "validity.json")
        data = None
        if os.path.isfile(vpath):
            try:
                data = json.load(open(vpath, encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
        by_policy = {}
        for policy in policies:
            info = ((data or {}).get("summary", {}).get("by_policy") or {}).get(
                policy, {})
            by_policy[policy] = info
        for policy in policies:
            info = by_policy[policy]
            valid = info.get("valid_runs", 0)
            reasons = info.get("reasons") or []
            if data is None:
                reasons = ["validity.json missing"]
            rows.append({
                "workload": "jankbench",
                "test": spec.get("name") or slug,
                "policy": policy,
                "valid": valid,
                "expected": runs,
                "status": "pass" if valid >= runs else "fail",
                "reasons": " | ".join(reasons)[:2000],
                "path": os.path.join("jankbench", slug),
            })

    upath = os.path.join(root, "uibench", "validity.json")
    udata = None
    if os.path.isfile(upath):
        try:
            udata = json.load(open(upath, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            udata = None
    for test in UI_TESTS:
        for policy in policies:
            cell = ((udata or {}).get("cells") or {}).get(f"{policy}|{test}", {})
            valid = cell.get("reps_valid", 0)
            reasons = cell.get("valid_reasons") or []
            if udata is None:
                reasons = ["uibench validity.json missing"]
            rows.append({
                "workload": "uibench",
                "test": test,
                "policy": policy,
                "valid": valid,
                "expected": runs,
                "status": "pass" if valid >= runs else "fail",
                "reasons": " | ".join(reasons)[:2000],
                "path": os.path.join("uibench"),
            })

    failing = [r for r in rows if r["status"] != "pass"]
    report = {
        "schema": "scx_fcp.full_matrix.v1",
        "kind": "full-14-test-matrix",
        "root": root,
        "policies": policies,
        "runs_expected": runs,
        "tests_expected": len(benchmarks) + len(UI_TESTS),
        "cells_expected": len(rows),
        "cells_pass": len(rows) - len(failing),
        "cells_fail": len(failing),
        "all_14_tests_10_of_10": not failing,
        "rows": rows,
    }
    with open(os.path.join(root, "full_matrix_status.json"), "w",
              encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    with open(os.path.join(root, "full_matrix_status.csv"), "w", newline="",
              encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    for row in rows:
        print(f"{row['workload']:9s} {row['test']:26s} {row['policy']:34s} "
              f"{row['valid']:2d}/{row['expected']:2d} {row['status']}")
    print(f"cells {report['cells_pass']}/{report['cells_expected']} pass")
    print("FULL_MATRIX_ALL_VALID" if not failing else "FULL_MATRIX_INCOMPLETE")
    if failing:
        print("failing cells:")
        for row in failing:
            print(f"  {row['test']} {row['policy']}: "
                  f"{row['reasons'][:300]}")
    return 0 if not failing else 1


if __name__ == "__main__":
    raise SystemExit(main())
