#!/usr/bin/env python3
"""Parse MobiSys minimal guardrail campaign into JSON, Markdown, and LaTeX."""
from __future__ import annotations

import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def parse_proc_stat(text: str) -> dict[int, tuple[int, int]]:
    out: dict[int, tuple[int, int]] = {}
    in_sec = False
    for ln in text.splitlines():
        if ln.startswith("---PROC_STAT---"):
            in_sec = True
            continue
        if ln.startswith("---"):
            in_sec = False
        if in_sec and re.match(r"^cpu\d+\s+", ln):
            parts = ln.split()
            cid = int(parts[0][3:])
            vals = [int(x) for x in parts[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            tot = sum(vals)
            out[cid] = (tot - idle, tot)
    return out


def parse_caps(text: str) -> dict[int, int]:
    caps: dict[int, int] = {}
    in_sec = False
    for ln in text.splitlines():
        if ln.startswith("---CPU_CAP---"):
            in_sec = True
            continue
        if ln.startswith("---"):
            in_sec = False
        if in_sec and ln.startswith("cpu"):
            p = ln.split()
            if len(p) >= 2:
                caps[int(p[0][3:])] = int(p[1])
    return caps


def parse_dvfs(text: str) -> dict[str, dict[int, int]]:
    pols: dict[str, dict[int, int]] = {}
    in_sec = False
    cur = None
    for ln in text.splitlines():
        if ln.startswith("---DVFS_TIME_IN_STATE---"):
            in_sec = True
            continue
        if ln.startswith("---"):
            in_sec = False
            cur = None
        if not in_sec:
            continue
        if ln.startswith("POLICY "):
            cur = ln.split()[1]
            pols[cur] = {}
        elif cur and ln.strip():
            p = ln.split()
            if len(p) == 2 and p[0].isdigit():
                pols[cur][int(p[0])] = int(p[1])
    return pols


def parse_max_temp_c(text: str) -> float | None:
    vals = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.isdigit():
            v = int(ln)
            vals.append(v / 1000.0 if v > 1000 else float(v))
            continue
        # Also parse ---THERMAL--- lines from snap_before/snap_after: "<zone> <type> <temp>"
        m = re.match(r"^thermal_zone\d+\s+\S+\s+(\d+)$", ln)
        if m:
            v = int(m.group(1))
            if v > 0:
                vals.append(v / 1000.0 if v > 1000 else float(v))
    return max(vals) if vals else None


def parse_gfxinfo_fps(text: str) -> dict[str, float]:
    total_frames = 0
    jank_frames = 0
    m_tot = re.search(r"Total frames rendered:\s*(\d+)", text)
    m_jk = re.search(r"Janky frames:\s*(\d+)", text)
    if m_tot:
        total_frames = int(m_tot.group(1))
    if m_jk:
        jank_frames = int(m_jk.group(1))

    # Extract PROFILEDATA frame durations using dynamic header columns
    # (Android 12+ inserts FrameTimelineVsyncId, moving IntendedVsync/FrameCompleted)
    durs_ms = []
    in_prof = False
    idx_intended = 1
    idx_completed = 13
    idx_interval = -1
    panel_hz = 60.0
    for ln in text.splitlines():
        if "---PROFILEDATA---" in ln:
            in_prof = not in_prof
            continue
        if not in_prof:
            continue
        cols = ln.strip().split(",")
        if "IntendedVsync" in cols and "FrameCompleted" in cols:
            idx_intended = cols.index("IntendedVsync")
            idx_completed = cols.index("FrameCompleted")
            if "FrameInterval" in cols:
                idx_interval = cols.index("FrameInterval")
            continue
        if cols and cols[0] == "0" and len(cols) > max(idx_intended, idx_completed):
            s_i, s_c = cols[idx_intended], cols[idx_completed]
            if s_i.isdigit() and s_c.isdigit():
                dt = (int(s_c) - int(s_i)) / 1e6
                if 0.5 < dt < 500.0:
                    durs_ms.append(dt)
            if idx_interval >= 0 and len(cols) > idx_interval and cols[idx_interval].isdigit():
                iv = int(cols[idx_interval])
                if 3_000_000 < iv < 50_000_000:
                    panel_hz = round(1e9 / iv, 1)

    durs_ms.sort()
    p99_ms = durs_ms[min(len(durs_ms) - 1, max(0, int(0.99 * len(durs_ms)) - 1))] if durs_ms else 0.0
    mean_ms = statistics.fmean(durs_ms) if durs_ms else 0.0

    # Fallback to dumpsys gfxinfo summary percentiles if PROFILEDATA section is absent
    if mean_ms <= 0.0:
        m_p50 = re.search(r"50th percentile:\s*(\d+)ms", text)
        m_p99 = re.search(r"99th percentile:\s*(\d+)ms", text)
        if m_p50:
            mean_ms = float(m_p50.group(1))
        if m_p99:
            p99_ms = float(m_p99.group(1))

    eff_fps = min(panel_hz, 1000.0 / mean_ms) if mean_ms > 0 else 0.0
    low1_fps = min(panel_hz, 1000.0 / p99_ms) if p99_ms > 0 else 0.0
    return {
        "panel_hz": panel_hz,
        "frames": total_frames or len(durs_ms),
        "jank_pct": round((100.0 * jank_frames / total_frames), 2) if total_frames else 0.0,
        "mean_ms": round(mean_ms, 2),
        "p99_ms": round(p99_ms, 2),
        "effective_fps": round(eff_fps, 2),
        "low1_fps": round(low1_fps, 2),
    }


def main() -> int:
    out_dir = Path(sys.argv[1]).resolve()
    summary: dict[str, object] = {"ui_guardrail": {}, "throughput": {}, "load_sweep": []}

    ui_root = out_dir / "ui_guardrail"
    if ui_root.is_dir():
        for pol_dir in sorted(ui_root.iterdir()):
            if not pol_dir.is_dir():
                continue
            pol = pol_dir.name
            runs_data = []
            for rdir in sorted(pol_dir.glob("run-*")):
                b_txt = (rdir / "snap_before.txt").read_text(errors="replace") if (rdir / "snap_before.txt").is_file() else ""
                a_txt = (rdir / "snap_after.txt").read_text(errors="replace") if (rdir / "snap_after.txt").is_file() else ""
                gfx_txt = (rdir / "gfxinfo.txt").read_text(errors="replace") if (rdir / "gfxinfo.txt").is_file() else ""
                th_txt = (rdir / "therm_trace.txt").read_text(errors="replace") if (rdir / "therm_trace.txt").is_file() else ""

                ps_b, ps_a = parse_proc_stat(b_txt), parse_proc_stat(a_txt)
                caps = parse_caps(b_txt)
                util_all, util_little, util_big = [], [], []
                min_cap = min(caps.values()) if caps else 1024
                max_cap = max(caps.values()) if caps else 1024
                thresh = (min_cap + max_cap) / 2.0

                for cid, (ba, ta) in ps_a.items():
                    bb, tb = ps_b.get(cid, (0, 0))
                    dt = max(1, ta - tb)
                    u = 100.0 * max(0, ba - bb) / dt
                    util_all.append(u)
                    if max_cap > min_cap and caps.get(cid, 1024) < thresh:
                        util_little.append(u)
                    else:
                        util_big.append(u)

                dv_b, dv_a = parse_dvfs(b_txt), parse_dvfs(a_txt)
                avg_mhz_by_pol = {}
                high_opp_pct_by_pol = {}
                for pname, st_a in dv_a.items():
                    st_b = dv_b.get(pname, {})
                    w_freq = 0.0
                    tot_t = 0
                    max_khz = max(st_a.keys()) if st_a else 0
                    high_t = 0
                    for khz, ta in st_a.items():
                        dt = max(0, ta - st_b.get(khz, 0))
                        w_freq += (khz / 1000.0) * dt
                        tot_t += dt
                        if max_khz > 0 and khz >= 0.8 * max_khz:
                            high_t += dt
                    if tot_t > 0:
                        avg_mhz_by_pol[pname] = round(w_freq / tot_t, 1)
                        high_opp_pct_by_pol[pname] = round(100.0 * high_t / tot_t, 1)

                gfx = parse_gfxinfo_fps(gfx_txt)
                peak_c = parse_max_temp_c("\n".join([th_txt, b_txt, a_txt]))
                runs_data.append({
                    "cpu_util_total_pct": round(statistics.fmean(util_all), 1) if util_all else 0.0,
                    "cpu_util_little_pct": round(statistics.fmean(util_little), 1) if util_little else 0.0,
                    "cpu_util_big_pct": round(statistics.fmean(util_big), 1) if util_big else 0.0,
                    "dvfs_avg_mhz": avg_mhz_by_pol,
                    "dvfs_high_opp_pct": high_opp_pct_by_pol,
                    "peak_temp_c": round(peak_c, 1) if peak_c else None,
                    **gfx,
                })
            if runs_data:
                summary["ui_guardrail"][pol] = {
                    "runs": len(runs_data),
                    "cpu_util_total_pct": round(statistics.fmean(r["cpu_util_total_pct"] for r in runs_data), 1),
                    "cpu_util_little_pct": round(statistics.fmean(r["cpu_util_little_pct"] for r in runs_data), 1),
                    "cpu_util_big_pct": round(statistics.fmean(r["cpu_util_big_pct"] for r in runs_data), 1),
                    "effective_fps": round(statistics.fmean(r["effective_fps"] for r in runs_data), 2),
                    "low1_fps": round(statistics.fmean(r["low1_fps"] for r in runs_data), 2),
                    "p99_ms": round(statistics.fmean(r["p99_ms"] for r in runs_data), 2),
                    "jank_pct": round(statistics.fmean(r["jank_pct"] for r in runs_data), 2),
                    "peak_temp_c": round(statistics.fmean(r["peak_temp_c"] for r in runs_data if r["peak_temp_c"] is not None), 1)
                    if any(r["peak_temp_c"] is not None for r in runs_data) else None,
                    "per_run": runs_data,
                }

    tp_root = out_dir / "throughput"
    if tp_root.is_dir():
        for pol_dir in sorted(tp_root.iterdir()):
            if not pol_dir.is_dir():
                continue
            pol = pol_dir.name
            sc_mbs, mc_mbs = [], []
            for rdir in sorted(pol_dir.glob("run-*")):
                f = rdir / "sha256_bench.txt"
                if not f.is_file():
                    continue
                txt = f.read_text(errors="replace")
                m1 = re.search(r"single_ns=(\d+)", txt)
                m2 = re.search(r"multi_ns=(\d+)\s+ncpu=(\d+)", txt)
                if m1 and int(m1.group(1)) > 0:
                    sc_mbs.append(256.0 / (int(m1.group(1)) / 1e9))
                if m2 and int(m2.group(1)) > 0:
                    mc_mbs.append((128.0 * int(m2.group(2))) / (int(m2.group(1)) / 1e9))
            if sc_mbs:
                summary["throughput"][pol] = {
                    "runs": len(sc_mbs),
                    "single_core_mb_s": round(statistics.fmean(sc_mbs), 1),
                    "multi_core_mb_s": round(statistics.fmean(mc_mbs), 1) if mc_mbs else 0.0,
                }

    sw_jsonl = out_dir / "load_sweep" / "sweep_90hz.jsonl"
    if sw_jsonl.is_file():
        for ln in sw_jsonl.read_text(errors="replace").splitlines():
            if ln.strip().startswith("{"):
                summary["load_sweep"].append(json.loads(ln))

    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    # Emit ready-to-paste LaTeX table
    tex = [
        r"\begin{table}[t]",
        r"\centering",
        r"\caption{System-level guardrails during contended UI scrolling and standalone CPU throughput ($N=" + str(len(next(iter(summary['ui_guardrail'].values()), {}).get('per_run', [])) or 3) + r"$).}",
        r"\label{tab:guardrails}",
        r"\scriptsize\setlength{\tabcolsep}{3.5pt}",
        r"\begin{tabular}{lrrrrrr}",
        r"\toprule",
        r"\textbf{Scheduler} & \textbf{FPS / 1\% Low} & \textbf{CPU Busy (\%)} & \textbf{Peak $T$ ($^\circ$C)} & \textbf{1T (MB/s)} & \textbf{MT (MB/s)} \\",
        r"\midrule",
    ]
    for pol in ("eevdf", "scx_lavd", "scx_fcp"):
        u = summary["ui_guardrail"].get(pol, {})
        t = summary["throughput"].get(pol, {})
        if not u and not t:
            continue
        lbl = {"eevdf": r"\texttt{EEVDF}", "scx_lavd": r"\scxl{}", "scx_fcp": r"\textbf{\scxfcp{} (Ours)}"}[pol]
        fps_s = f"{u.get('effective_fps', 0):.1f} / {u.get('low1_fps', 0):.1f}" if u else "--"
        busy_s = f"{u.get('cpu_util_total_pct', 0):.1f}\\%" if u else "--"
        tmp_s = f"{u['peak_temp_c']:.1f}" if u and u.get("peak_temp_c") else "--"
        sc_s = f"{t.get('single_core_mb_s', 0):.0f}" if t else "--"
        mc_s = f"{t.get('multi_core_mb_s', 0):.0f}" if t else "--"
        tex.append(f"{lbl} & {fps_s} & {busy_s} & {tmp_s} & {sc_s} & {mc_s} \\\\")
    tex.extend([r"\bottomrule", r"\end{tabular}", r"\end{table}"])
    (out_dir / "tab_guardrails.tex").write_text("\n".join(tex) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
