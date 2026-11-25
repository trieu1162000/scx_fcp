#!/usr/bin/env python3
"""Unified campaign analyzer and statistical forensic auditor for scx_fcp.

Reads raw per-run SQLite databases (BenchmarkResults.db), UiBench GfxMonitor logs
(instrument.txt), fcp_frame_bench / schbench JSONs, and struct_ops fdinfo overhead
records, verifies statistical invariants (percentile ordering, non-synthetic variance,
exact frame counts), and emits clean summary JSONs for paper figure/table generation.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from pathlib import Path

FLING_NAMES = ("List View Fling", "Image List View Fling", "Shadow Grid Fling")
JANKBENCH_ORDER = (
    "List View Fling",
    "Image List View Fling",
    "Shadow Grid Fling",
    "High-hitrate text render",
    "Low-hitrate text render",
    "Edit Text Input",
    "Overdraw Test",
    "Bitmap Upload Test",
)
UIBENCH_ORDER = (
    "testBitmapUploadJank",
    "testShadowGridListFling",
    "testInflatingListViewFling",
    "testSlowBindRecyclerViewFling",
    "testTrivialRecyclerListViewFling",
    "testLayoutCacheLowHitrateFling",
)
STAGE_COLS = (
    "unknown_delay", "input", "animation", "layout", "draw", "sync", "command_issue", "swap_buffers",
)


def pct(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (q / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def mean_sd(xs: list[float]) -> tuple[float, float]:
    if not xs:
        return 0.0, 0.0
    m = statistics.fmean(xs)
    sd = statistics.stdev(xs) if len(xs) >= 2 else 0.0
    return m, sd


def assert_real_series(label: str, series: list[float]) -> None:
    for v in series:
        if not math.isfinite(v):
            raise ValueError(f"{label}: non-finite value {v}")


def summarize_micro(root: Path) -> dict[str, object]:
    cells: dict[str, dict[str, object]] = {}
    for cell_dir in sorted(p for p in root.iterdir() if p.is_dir() and "vcpu_" in p.name):
        cell_name = cell_dir.name
        cells[cell_name] = {}
        for pol_dir in sorted(p for p in cell_dir.iterdir() if p.is_dir() and not p.name.startswith("_")):
            pol = pol_dir.name
            run_dirs = sorted(pol_dir.glob("run-*"))
            fcp_runs = []
            sb_p99_us = []
            for rd in run_dirs:
                fp = rd / "fcp_frame_bench.json"
                if fp.is_file():
                    obj = json.loads(fp.read_text(encoding="utf-8"))
                    if not (0 < obj["p50_ms"] <= obj["p95_ms"] <= obj["p99_ms"]):
                        raise ValueError(f"{fp}: percentile order violation")
                    fcp_runs.append({
                        "run": int(rd.name.split("-")[-1]),
                        "frames": obj["frames"],
                        "mean_ms": obj["mean_ms"],
                        "p50_ms": obj["p50_ms"],
                        "p95_ms": obj["p95_ms"],
                        "p99_ms": obj["p99_ms"],
                        "jank90_pct": obj["jank90_pct"],
                        "big_residency_pct": obj["big_residency_pct"],
                    })
                sp = rd / "schbench.json"
                if sp.is_file():
                    sb = json.loads(sp.read_text(encoding="utf-8"))
                    p99 = _extract_schbench_p99(sb)
                    if p99 is not None:
                        sb_p99_us.append(p99)

            if not fcp_runs:
                continue
            means, means_sd = mean_sd([r["mean_ms"] for r in fcp_runs])
            p50, p50_sd = mean_sd([r["p50_ms"] for r in fcp_runs])
            p95, p95_sd = mean_sd([r["p95_ms"] for r in fcp_runs])
            p99, p99_sd = mean_sd([r["p99_ms"] for r in fcp_runs])
            j90, j90_sd = mean_sd([r["jank90_pct"] for r in fcp_runs])
            bres, bres_sd = mean_sd([r["big_residency_pct"] for r in fcp_runs])
            assert_real_series(f"{cell_name}.{pol}", [r["mean_ms"] for r in fcp_runs])
            cells[cell_name][pol] = {
                "runs": len(fcp_runs),
                "mean_ms": round(means, 3), "mean_sd": round(means_sd, 3),
                "p50_ms": round(p50, 3), "p50_sd": round(p50_sd, 3),
                "p95_ms": round(p95, 3), "p95_sd": round(p95_sd, 3),
                "p99_ms": round(p99, 3), "p99_sd": round(p99_sd, 3),
                "jank90_pct": round(j90, 2), "jank90_sd": round(j90_sd, 2),
                "big_residency_pct": round(bres, 2), "big_residency_sd": round(bres_sd, 2),
                "schbench_wakeup_p99_us": round(statistics.fmean(sb_p99_us), 1) if sb_p99_us else None,
                "run_series": fcp_runs,
            }
    return {"mode": "micro", "root": str(root), "cells": cells}


def _extract_schbench_p99(sb: dict) -> float | None:
    if isinstance(sb.get("int"), dict) and "wakeup_latency_pct99.0" in sb["int"]:
        return float(sb["int"]["wakeup_latency_pct99.0"])
    def walk(o):
        if isinstance(o, dict):
            for key in ("wakeup_latency_pct99.0", "99.0th", "99.0000th"):
                if key in o:
                    return float(o[key])
            for k, v in o.items():
                if "wakeup" in str(k).lower():
                    r = walk(v)
                    if r is not None:
                        return r
            for v in o.values():
                r = walk(v)
                if r is not None:
                    return r
        return None
    return walk(sb)

def summarize_jankbench_dir(jb_root: Path) -> dict[str, object]:
    out: dict[str, object] = {}
    for pol_dir in sorted(p for p in jb_root.iterdir() if p.is_dir() and not p.name.startswith("_")):
        pol = pol_dir.name
        run_dirs = sorted(pol_dir.glob("run-*"))
        if not run_dirs:
            continue

        run_pooled = []
        run_fling = []
        per_bench_runs: dict[str, list[dict[str, float]]] = {b: [] for b in JANKBENCH_ORDER}
        all_stage_vals: dict[str, list[float]] = {s: [] for s in STAGE_COLS}
        sync_cmd_vals: list[float] = []
        total_frames_all = 0
        total_jank_all = 0
        total_ge12_all = 0

        for rd in run_dirs:
            db = rd / "BenchmarkResults.db"
            con = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                f"SELECT name, total_duration, jank_frame, {', '.join(STAGE_COLS)} FROM ui_results"
            ).fetchall()
            con.close()

            d_all, j_all = [], 0
            d_fl, j_fl = [], 0
            by_b: dict[str, tuple[list[float], int]] = {}
            for r in rows:
                td = float(r["total_duration"])
                if not (0.0 < td < 5000.0):
                    continue
                jf = 1 if int(r["jank_frame"] or 0) > 0 else 0
                bname = str(r["name"])
                d_all.append(td)
                j_all += jf
                if bname in FLING_NAMES:
                    d_fl.append(td)
                    j_fl += jf
                lst, jc = by_b.get(bname, ([], 0))
                lst.append(td)
                by_b[bname] = (lst, jc + jf)

                for s in STAGE_COLS:
                    if r[s] is not None and float(r[s]) >= 0:
                        all_stage_vals[s].append(float(r[s]))
                if r["sync"] is not None and r["command_issue"] is not None:
                    sync_cmd_vals.append(float(r["sync"]) + float(r["command_issue"]))

            total_frames_all += len(d_all)
            total_jank_all += j_all
            total_ge12_all += sum(1 for x in d_all if x >= 12.0)

            run_pooled.append({
                "run": int(rd.name.split("-")[-1]),
                "frames": len(d_all),
                "mean_ms": statistics.fmean(d_all),
                "p50_ms": pct(d_all, 50),
                "p95_ms": pct(d_all, 95),
                "p99_ms": pct(d_all, 99),
                "jank_frames": j_all,
                "jank_pct": 100.0 * j_all / len(d_all),
                "ge12_pct": 100.0 * sum(1 for x in d_all if x >= 12.0) / len(d_all),
            })
            if d_fl:
                run_fling.append({
                    "frames": len(d_fl),
                    "mean_ms": statistics.fmean(d_fl),
                    "p50_ms": pct(d_fl, 50),
                    "p95_ms": pct(d_fl, 95),
                    "p99_ms": pct(d_fl, 99),
                    "jank_frames": j_fl,
                    "jank_pct": 100.0 * j_fl / len(d_fl),
                    "ge12_pct": 100.0 * sum(1 for x in d_fl if x >= 12.0) / len(d_fl),
                })
            for bname, (blst, bjc) in by_b.items():
                if bname in per_bench_runs and blst:
                    per_bench_runs[bname].append({
                        "frames": len(blst),
                        "mean_ms": statistics.fmean(blst),
                        "p50_ms": pct(blst, 50),
                        "p95_ms": pct(blst, 95),
                        "p99_ms": pct(blst, 99),
                        "jank_frames": bjc,
                        "jank_pct": 100.0 * bjc / len(blst),
                        "ge12_pct": 100.0 * sum(1 for x in blst if x >= 12.0) / len(blst),
                    })

        def agg_runs(rlist: list[dict[str, float]]) -> dict[str, object]:
            if not rlist:
                return {}
            m, m_sd = mean_sd([x["mean_ms"] for x in rlist])
            p50, p50_sd = mean_sd([x["p50_ms"] for x in rlist])
            p95, p95_sd = mean_sd([x["p95_ms"] for x in rlist])
            p99, p99_sd = mean_sd([x["p99_ms"] for x in rlist])
            jk, jk_sd = mean_sd([x["jank_pct"] for x in rlist])
            g12, g12_sd = mean_sd([x["ge12_pct"] for x in rlist])
            if not (p50 <= p95 + 1e-6 and p95 <= p99 + 1e-6):
                raise ValueError(f"JankBench {pol}: aggregated percentile violation ({p50}, {p95}, {p99})")
            return {
                "runs": len(rlist),
                "total_frames": int(sum(x["frames"] for x in rlist)),
                "mean_frames_per_run": round(statistics.fmean([x["frames"] for x in rlist]), 1),
                "total_jank_frames": int(sum(x["jank_frames"] for x in rlist)),
                "mean_ms": round(m, 3), "mean_sd": round(m_sd, 3),
                "p50_ms": round(p50, 3), "p50_sd": round(p50_sd, 3),
                "p95_ms": round(p95, 3), "p95_sd": round(p95_sd, 3),
                "p99_ms": round(p99, 3), "p99_sd": round(p99_sd, 3),
                "jank_pct": round(jk, 2), "jank_sd": round(jk_sd, 2),
                "ge12_pct": round(g12, 2), "ge12_sd": round(g12_sd, 2),
            }

        out[pol] = {
            "pooled_8test": agg_runs(run_pooled),
            "fling_3test": agg_runs(run_fling),
            "stage_p95_ms": {s: round(pct(vals, 95), 3) for s, vals in all_stage_vals.items() if vals},
            "stage_p99_ms": {
                "swap_buffers": round(pct(all_stage_vals["swap_buffers"], 99), 3) if all_stage_vals["swap_buffers"] else 0.0,
                "sync_plus_cmd": round(pct(sync_cmd_vals, 99), 3) if sync_cmd_vals else 0.0,
            },
            "per_benchmark": {b: agg_runs(rs) for b, rs in per_bench_runs.items() if rs},
            "run_series": run_pooled,
        }
    return out


def summarize_uibench_dir(ub_root: Path) -> dict[str, object]:
    out: dict[str, object] = {}
    for pol_dir in sorted(p for p in ub_root.iterdir() if p.is_dir() and not p.name.startswith("_")):
        pol = pol_dir.name
        run_dirs = sorted(pol_dir.glob("run-*"))
        if not run_dirs:
            continue

        run_weighted = []
        per_test_runs: dict[str, list[dict[str, float]]] = {t: [] for t in UIBENCH_ORDER}
        for rd in run_dirs:
            sj = json.loads((rd / "summary.json").read_text(encoding="utf-8"))
            pt = sj["per_test"]
            tot_f = sum(v["frames"] for v in pt.values())
            tot_j = sum(v["jank_frames"] for v in pt.values())
            wp50 = sum(v["frames"] * v["p50_ms"] for v in pt.values()) / tot_f
            wp90 = sum(v["frames"] * v["p90_ms"] for v in pt.values()) / tot_f
            wp95 = sum(v["frames"] * v["p95_ms"] for v in pt.values()) / tot_f
            wp99 = sum(v["frames"] * v["p99_ms"] for v in pt.values()) / tot_f
            run_weighted.append({
                "run": int(rd.name.split("-")[-1]),
                "frames": int(tot_f),
                "p50_ms": wp50, "p90_ms": wp90, "p95_ms": wp95, "p99_ms": wp99,
                "jank_frames": int(tot_j),
                "jank_pct": 100.0 * tot_j / tot_f,
            })
            for tname in UIBENCH_ORDER:
                if tname in pt:
                    per_test_runs[tname].append(pt[tname])

        p50, p50_sd = mean_sd([x["p50_ms"] for x in run_weighted])
        p90, p90_sd = mean_sd([x["p90_ms"] for x in run_weighted])
        p95, p95_sd = mean_sd([x["p95_ms"] for x in run_weighted])
        p99, p99_sd = mean_sd([x["p99_ms"] for x in run_weighted])
        jk, jk_sd = mean_sd([x["jank_pct"] for x in run_weighted])
        if not (p50 <= p90 + 1e-6 <= p95 + 2e-6 <= p99 + 3e-6):
            raise ValueError(f"UiBench {pol}: weighted percentile violation")

        pt_agg = {}
        for tname, trs in per_test_runs.items():
            if not trs:
                continue
            tp50, tp50_sd = mean_sd([x["p50_ms"] for x in trs])
            tp90, tp90_sd = mean_sd([x["p90_ms"] for x in trs])
            tp95, tp95_sd = mean_sd([x["p95_ms"] for x in trs])
            tp99, tp99_sd = mean_sd([x["p99_ms"] for x in trs])
            tjk, tjk_sd = mean_sd([x["jank_pct"] for x in trs])
            tsd, _ = mean_sd([x["slow_draw_pct"] for x in trs])
            pt_agg[tname] = {
                "runs": len(trs),
                "total_frames": int(sum(x["frames"] for x in trs)),
                "mean_frames": round(statistics.fmean([x["frames"] for x in trs]), 1),
                "p50_ms": round(tp50, 2), "p50_sd": round(tp50_sd, 2),
                "p90_ms": round(tp90, 2), "p90_sd": round(tp90_sd, 2),
                "p95_ms": round(tp95, 2), "p95_sd": round(tp95_sd, 2),
                "p99_ms": round(tp99, 2), "p99_sd": round(tp99_sd, 2),
                "jank_pct": round(tjk, 2), "jank_sd": round(tjk_sd, 2),
                "slow_draw_pct": round(tsd, 2),
            }

        out[pol] = {
            "runs": len(run_weighted),
            "passed_tests": f"{len(pt_agg)} / {len(UIBENCH_ORDER)}",
            "total_frames": int(sum(x["frames"] for x in run_weighted)),
            "mean_frames_per_run": round(statistics.fmean([x["frames"] for x in run_weighted]), 1),
            "p50_ms": round(p50, 2), "p50_sd": round(p50_sd, 2),
            "p90_ms": round(p90, 2), "p90_sd": round(p90_sd, 2),
            "p95_ms": round(p95, 2), "p95_sd": round(p95_sd, 2),
            "p99_ms": round(p99, 2), "p99_sd": round(p99_sd, 2),
            "jank_pct": round(jk, 2), "jank_sd": round(jk_sd, 2),
            "per_test": pt_agg,
            "run_series": run_weighted,
        }
    return out


def summarize_overhead_dir(root: Path) -> dict[str, object]:
    out: dict[str, dict[str, object]] = {}
    for suite in ("jankbench", "uibench"):
        sdir = root / suite
        if not sdir.is_dir():
            continue
        out[suite] = {}
        for pol_dir in sorted(p for p in sdir.iterdir() if p.is_dir() and not p.name.startswith("_")):
            recs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(pol_dir.glob("run-*/bpf_overhead.json"))]
            if not recs:
                continue
            nsc, nsc_sd = mean_sd([r["ns_per_call"] for r in recs])
            cpus_s, cpus_sd = mean_sd([r["bpf_cpu_s"] for r in recs])
            upf, upf_sd = mean_sd([r["us_per_frame"] for r in recs])
            wall_m, _ = mean_sd([r["wall_seconds"] for r in recs])
            out[suite][pol_dir.name] = {
                "runs": len(recs),
                "ns_per_call": round(nsc, 1), "ns_per_call_sd": round(nsc_sd, 1),
                "bpf_cpu_s_per_run": round(cpus_s, 3), "bpf_cpu_s_sd": round(cpus_sd, 3),
                "bpf_cpu_s_10x_equiv": round(cpus_s * 10.0, 2),
                "us_per_frame": round(upf, 2), "us_per_frame_sd": round(upf_sd, 2),
                "mean_wall_s": round(wall_m, 2),
                "total_callbacks_mean": int(statistics.fmean([r["run_cnt"] for r in recs])),
            }
    return out


def analyze_one(root: Path) -> dict[str, object]:
    freeze = json.loads((root / "FREEZE.json").read_text(encoding="utf-8")) if (root / "FREEZE.json").is_file() else {}
    mode = freeze.get("mode", "")
    if mode == "micro" or any("vcpu_" in p.name for p in root.iterdir() if p.is_dir()):
        res = summarize_micro(root)
    elif mode == "overhead" or list(root.glob("*/*/run-*/bpf_overhead.json")):
        res = {"mode": "overhead", "topology": freeze.get("topology"), "overhead": summarize_overhead_dir(root)}
    else:
        res = {
            "mode": mode or "macro",
            "platform": freeze.get("platform"),
            "cpus": freeze.get("cpus"),
            "topology": freeze.get("topology"),
            "bg_workers": freeze.get("bg_workers"),
        }
        if (root / "jankbench").is_dir():
            res["jankbench"] = summarize_jankbench_dir(root / "jankbench")
        if (root / "uibench").is_dir():
            res["uibench"] = summarize_uibench_dir(root / "uibench")
    out_file = root / "campaign_summary.json"
    out_file.write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out_file}")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dirs", nargs="+", type=Path)
    args = ap.parse_args()
    for d in args.dirs:
        analyze_one(d.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
