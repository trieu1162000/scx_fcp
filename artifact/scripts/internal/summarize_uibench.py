#!/usr/bin/env python3
"""Aggregate run-level UiBench GfxMonitor metrics.

The run is the independent unit.  For each test and policy this prints the
median and interquartile range of the run-level GfxMonitor values, plus exact
jank counts summed over valid runs.  Percentiles are never pooled or averaged
across tests.

Usage:
  summarize_uibench.py [results-root ...]

The default root is $RESULTS_ROOT. Each root is searched recursively for
run-level metrics.json files produced by extract_uibench.py.
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

METRICS = [
    ("frames", "frames"),
    ("p50_ms", "p50 ms"),
    ("p90_ms", "p90 ms"),
    ("p95_ms", "p95 ms"),
    ("p99_ms", "p99 ms"),
    ("jank_pct", "jank %"),
    ("slow_draw_pct", "slow draw %"),
    ("high_input_latency_ms", "high input ms"),
    ("wall_time_s", "wall s"),
]

POLICY_ORDER = ["eevdf", "scx_fcp_behavioral", "scx_fcp", "scx_fcp_named",
                "scx_p2dq_pelt_off", "scx_p2dq", "scx_lavd"]
TEST_ORDER = ["inflating-list", "bitmap-upload", "shadow-grid-list", "slow-bind-recycler",
              "trivial-recycler", "layout-cache-low"]


def median(vals: list[float]) -> float:
    if not vals:
        return math.nan
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


def quantile(vals: list[float], q: float) -> float:
    if not vals:
        return math.nan
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def fmt(v: float, nd: int = 1) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    return f"{v:.{nd}f}"


def load_rows(roots: list[Path]) -> list[dict]:
    rows: list[dict] = []
    seen: set[Path] = set()
    for root in roots:
        if root.is_file() and root.name == "metrics.json":
            files = [root]
        else:
            files = sorted(root.rglob("metrics.json"))
        for path in files:
            rp = path.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            if "_warmup" in rp.parts or "warmup" in rp.parts:
                continue
            try:
                data = json.loads(rp.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            policy = data.get("policy") or "unknown"
            platform = data.get("platform") or ""
            vcpu = data.get("vcpu")
            for row in data.get("tests", []):
                row = dict(row)
                row["policy"] = policy
                row["platform"] = platform
                row["vcpu"] = vcpu
                row["metrics_path"] = str(rp)
                rows.append(row)
    return rows


def main() -> int:
    if len(sys.argv) < 2:
        env_root = os.environ.get("RESULTS_ROOT")
        if not env_root:
            print(__doc__)
            return 2
        roots = [Path(env_root)]
    else:
        roots = [Path(arg) for arg in sys.argv[1:]]
    rows = load_rows(roots)
    if not rows:
        print("no metrics.json found")
        return 1

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row["policy"], row["test"])].append(row)

    policies = [p for p in POLICY_ORDER if any(k[0] == p for k in groups)]
    policies += sorted({k[0] for k in groups} - set(policies))
    tests = [t for t in TEST_ORDER if any(k[1] == t for k in groups)]
    tests += sorted({k[1] for k in groups} - set(tests))

    print("# UiBench run-level summary")
    print()
    print("Primary metric: GfxMonitor instrumentation (`gfx-avg-*`), all frames in the test window.")
    print("Raw `FrameCompleted - IntendedVsync` is diagnostic only and is not summarized here.")
    print("Each cell is a median over valid runs. IQR is Q3-Q1 over the same runs.")
    print()

    for test in tests:
        print(f"## {test}")
        print()
        print("| Policy | valid/invalid | frames med | p50 med | p90 med | p95 med | p99 med | jank % med | jank frames | slow draw % med | high input ms med |")
        print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for policy in policies:
            grp = groups.get((policy, test))
            if not grp:
                continue
            valid = [r for r in grp if r.get("valid")]
            invalid = len(grp) - len(valid)
            if not valid:
                print(f"| {policy} | 0/{invalid} | - | - | - | - | - | - | - | - | - |")
                continue
            def med(key):
                return median([r[key] for r in valid if r.get(key) is not None])
            frames = int(sum(r.get("frames", 0) for r in valid))
            jankn = int(sum(r.get("jank_frames", 0) for r in valid))
            print(
                f"| {policy} | {len(valid)}/{invalid} | {med('frames'):.0f} | "
                f"{fmt(med('p50_ms'))} | {fmt(med('p90_ms'))} | {fmt(med('p95_ms'))} | "
                f"{fmt(med('p99_ms'))} | {fmt(med('jank_pct'), 2)} | {jankn} | "
                f"{fmt(med('slow_draw_pct'), 2)} | {fmt(med('high_input_latency_ms'))} |"
            )
        print()

        print("| Policy | p50 IQR | p90 IQR | p95 IQR | p99 IQR | jank % IQR | frames IQR |")
        print("|---|---:|---:|---:|---:|---:|---:|")
        for policy in policies:
            grp = groups.get((policy, test))
            if not grp:
                continue
            valid = [r for r in grp if r.get("valid")]
            if not valid:
                continue
            def iqr(key):
                vals = [r[key] for r in valid if r.get(key) is not None]
                return quantile(vals, 0.75) - quantile(vals, 0.25) if vals else math.nan
            print(
                f"| {policy} | {fmt(iqr('p50_ms'))} | {fmt(iqr('p90_ms'))} | "
                f"{fmt(iqr('p95_ms'))} | {fmt(iqr('p99_ms'))} | "
                f"{fmt(iqr('jank_pct'), 2)} | {fmt(iqr('frames'), 0)} |"
            )
        print()

    # Head-to-head EEVDF vs fcp, per test, medians only.
    print("## Head-to-head medians, EEVDF vs scx_fcp behavioral")
    print()
    print("| Test | metric | EEVDF | fcp | fcp - EEVDF | lower is better |")
    print("|---|---|---:|---:|---:|---|")
    if any(policy == "eevdf" for policy, _ in groups):
        for test in tests:
            e = [r for r in groups.get(("eevdf", test), []) if r.get("valid")]
            f = [r for r in groups.get(("scx_fcp_behavioral", test), []) if r.get("valid")]
            if not e or not f:
                continue
            for key, label in [("p50_ms", "p50"), ("p95_ms", "p95"), ("p99_ms", "p99"), ("jank_pct", "jank %")]:
                ev = median([r[key] for r in e if r.get(key) is not None])
                fv = median([r[key] for r in f if r.get(key) is not None])
                diff = fv - ev
                verdict = "yes" if diff < 0 else "no"
                print(f"| {test} | {label} | {fmt(ev)} | {fmt(fv)} | {diff:+.2f} | {verdict} |")
        print()

    # Invalid runs, explicitly listed.
    print("## Invalid runs")
    print()
    any_invalid = False
    for row in rows:
        if not row.get("valid"):
            any_invalid = True
            print(
                f"- {row['policy']} {row['test']} rep {row.get('rep')}: "
                f"{row.get('invalid_reason') or 'unspecified'}"
            )
    if not any_invalid:
        print("None.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
