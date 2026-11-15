#!/usr/bin/env python3
"""Extract UiBench JankTests metrics from a raw run directory.

Measurement contract
---------------------
Primary metrics come from the JankTestHelper GfxMonitor instrumentation
summary (`gfx-avg-*`).  GfxMonitor summarizes every frame in the test window,
including frames that the gfxinfo ring buffer has already discarded, and its
percentiles are the values AOSP publishes for UiBenchJankTests.

`dumpsys gfxinfo ... framestats` is diagnostic only.  The gfxinfo in-memory
buffer retains at most 120 frames, so the raw block is a truncated window (the
last frames of the run) rather than a run-level sample.  The raw metric used
here, FrameCompleted - IntendedVsync, also includes the wait for the intended
vsync, which can be stale while the UiAutomator harness sleeps.  It is not the
same quantity as the GfxMonitor total-duration histogram.

The extractor never deletes a run.  Every row carries a `valid` flag and an
`invalid_reason` list.  Invalid runs stay in metrics.json and metrics.csv.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from pathlib import Path

HELPER_KEY_RE = re.compile(r"\bgfx-(avg|max|min)-([a-z0-9-]+)=([0-9]+(?:\.[0-9]+)?)")

TEST_CLASSES = {
    "bitmap-upload": "com.android.uibench.janktests.UiBenchRenderingJankTests#testBitmapUploadJank",
    "shadow-grid-list": "com.android.uibench.janktests.UiBenchRenderingJankTests#testShadowGridListFling",
    "inflating-list": "com.android.uibench.janktests.UiBenchJankTests#testInflatingListViewFling",
    "slow-bind-recycler": "com.android.uibench.janktests.UiBenchJankTests#testSlowBindRecyclerViewFling",
    "trivial-recycler": "com.android.uibench.janktests.UiBenchJankTests#testTrivialRecyclerListViewFling",
    "layout-cache-low": "com.android.uibench.janktests.UiBenchTextJankTests#testLayoutCacheLowHitrateFling",
}

PRIMARY_KEYS = (
    "total-frames",
    "frame-time-50",
    "frame-time-90",
    "frame-time-95",
    "frame-time-99",
    "jank",
    "slow-draw",
    "slow-ui-thread",
    "slow-bitmap-uploads",
    "missed-vsync",
    "num-frame-deadline-missed",
    "high-input-latency",
)

MIN_VALID_FRAMES = 100


def percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Requires a sorted non-empty list."""
    if not sorted_values:
        return math.nan
    n = len(sorted_values)
    rank = max(1, math.ceil(pct / 100.0 * n))
    return sorted_values[min(rank, n) - 1]


def parse_helper(path: Path) -> dict[str, dict[str, float]]:
    """Return {'avg': {...}, 'max': {...}, 'min': {...}} from instrument output."""
    values: dict[str, dict[str, float]] = {"avg": {}, "max": {}, "min": {}}
    if not path.exists():
        return values
    text = path.read_text(errors="replace")
    for group, key, raw in HELPER_KEY_RE.findall(text):
        try:
            values[group][key] = float(raw)
        except ValueError:
            continue
    return values


def read_framestats(path: Path) -> tuple[list[float], int, bool]:
    """Return raw frame durations, block length, and whether the block hit the cap.

    The gfxinfo buffer holds at most 120 frames.  A block of 120 rows is treated
    as truncated even if the test rendered exactly 120 frames; this errs on the
    side of labelling the diagnostic window correctly.
    """
    if not path.exists():
        return [], 0, False
    lines = path.read_text(errors="replace").splitlines()
    blocks: list[list[float]] = []
    i = 0
    while i < len(lines):
        if lines[i].strip() != "---PROFILEDATA---":
            i += 1
            continue
        i += 1
        if i >= len(lines):
            break
        header = [c.strip() for c in lines[i].split(",")]
        i += 1
        try:
            i_int = header.index("IntendedVsync")
            i_fin = header.index("FrameCompleted")
        except ValueError:
            continue
        durations: list[float] = []
        while i < len(lines):
            line = lines[i].strip()
            if not line or line.startswith("---"):
                break
            parts = [p.strip() for p in line.split(",")]
            i += 1
            if len(parts) <= max(i_int, i_fin):
                continue
            try:
                intended = int(parts[i_int])
                completed = int(parts[i_fin])
            except ValueError:
                continue
            if intended <= 0 or completed <= 0 or completed < intended:
                continue
            d = (completed - intended) / 1e6
            if 0.0 < d < 1000.0:
                durations.append(d)
        if durations:
            blocks.append(durations)
    if not blocks:
        return [], 0, False
    block = max(blocks, key=len)
    return block, len(block), len(block) >= 120


def summarize(durations: list[float], budget_ms: float) -> dict[str, float | int]:
    if not durations:
        return {
            "frames": 0,
            "mean_ms": math.nan,
            "p50_ms": math.nan,
            "p90_ms": math.nan,
            "p95_ms": math.nan,
            "p99_ms": math.nan,
            "max_ms": math.nan,
            "jank_frames": 0,
            "jank_pct": math.nan,
        }
    vals = sorted(durations)
    jank = sum(1 for d in vals if d > budget_ms)
    return {
        "frames": len(vals),
        "mean_ms": round(sum(vals) / len(vals), 3),
        "p50_ms": round(percentile(vals, 50), 3),
        "p90_ms": round(percentile(vals, 90), 3),
        "p95_ms": round(percentile(vals, 95), 3),
        "p99_ms": round(percentile(vals, 99), 3),
        "max_ms": round(vals[-1], 3),
        "jank_frames": jank,
        "jank_pct": round(100.0 * jank / len(vals), 3),
    }


def test_status(path: Path) -> tuple[str, float | None]:
    if not path.exists():
        return "missing", None
    text = path.read_text(errors="replace")
    duration = None
    match = re.search(r"^Time:\s*([0-9.]+)", text, re.MULTILINE)
    if match:
        duration = float(match.group(1))
    if "INSTRUMENTATION_FAILED" in text or "shortMsg=" in text:
        return "error", duration
    if "FAILURES!!!" in text or "INSTRUMENTATION_STATUS_CODE: -2" in text:
        return "failed", duration
    if "OK (1 test)" in text:
        return "passed", duration
    if "INSTRUMENTATION_CODE: -1" in text:
        return "unknown", duration
    return "unknown", duration


def read_env(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _fatal_in_package(text: str, package: str) -> bool:
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if "FATAL EXCEPTION" in line:
            window = "\n".join(lines[idx : idx + 24])
            if package in window:
                return True
    return False


def logcat_reasons(text: str) -> list[str]:
    reasons: list[str] = []
    app = "com.android.test.uibench"
    if re.search(r"ANR in\s+" + re.escape(app), text):
        reasons.append("ANR in UiBench app")
    if _fatal_in_package(text, app):
        reasons.append("FATAL EXCEPTION in UiBench app")
    if re.search(r"Force finishing activity[^\n]*" + re.escape(app), text):
        reasons.append("UiBench activity force-finished")
    if re.search(r"lowmemorykiller[^\n]*" + re.escape(app), text):
        reasons.append("UiBench app killed by lowmemorykiller")
    if re.search(r"Process\s+" + re.escape("com.android.systemui") + r"\s+has died", text):
        reasons.append("SystemUI process died")
    if _fatal_in_package(text, "com.android.systemui"):
        reasons.append("FATAL EXCEPTION in SystemUI")
    return reasons


def expected_policy_ok(env: dict) -> tuple[bool, str | None]:
    expected_ops = env.get("expected_ops")
    expected_state = env.get("expected_state")
    state_before = env.get("scx_state_before")
    ops_before = env.get("scx_ops_before")
    state_after = env.get("scx_state_after")
    ops_after = env.get("scx_ops_after")

    if not expected_ops and not expected_state:
        return True, None
    if expected_state and state_before != expected_state:
        return False, f"sched_ext state before {state_before!r}, expected {expected_state!r}"
    if expected_state and state_after not in (None, expected_state):
        return False, f"sched_ext state after {state_after!r}, expected {expected_state!r}"
    if expected_ops:
        for where, ops in (("before", ops_before), ("after", ops_after)):
            if ops is None:
                continue
            if expected_ops not in ops:
                return False, f"scheduler ops {where} {ops!r} does not contain {expected_ops!r}"
    return True, None


def validate(
    status: str,
    frames: int,
    avg: dict[str, float],
    env: dict,
    logcat_text: str,
    duration: float | None,
) -> tuple[bool, list[str], list[str]]:
    reasons: list[str] = []
    warnings: list[str] = []

    if status != "passed":
        reasons.append(f"instrumentation status {status!r}, expected 'passed'")
    if not avg:
        reasons.append("GfxMonitor summary missing")
    if frames < MIN_VALID_FRAMES:
        reasons.append(f"GfxMonitor frames {frames} below completeness floor {MIN_VALID_FRAMES}")
    for key in ("frame-time-50", "frame-time-90", "frame-time-95", "frame-time-99", "jank"):
        if key not in avg:
            reasons.append(f"GfxMonitor key gfx-avg-{key} missing")

    if duration is not None and not (10.0 <= duration <= 900.0):
        reasons.append(f"wall time {duration:.1f}s outside bounds")

    if env:
        before = env.get("systemui_pid_before")
        after = env.get("systemui_pid_after")
        if before and after:
            if str(before) != str(after):
                reasons.append(f"SystemUI restarted during test ({before} -> {after})")
        else:
            warnings.append("SystemUI pid check unavailable")

        top = env.get("top_activity_after") or ""
        if top:
            if "com.android.test.uibench" not in top:
                reasons.append(f"UiBench left foreground at test end: {top}")
        else:
            warnings.append("top-activity check unavailable")

        selinux = env.get("selinux")
        if selinux and selinux != "Permissive":
            reasons.append(f"SELinux {selinux}, expected Permissive")
        elif not selinux:
            warnings.append("SELinux state unavailable")

        ok, why = expected_policy_ok(env)
        if not ok and why:
            reasons.append(why)

        if env.get("wakefulness") and env["wakefulness"] != "Awake":
            reasons.append(f"device wakefulness {env['wakefulness']}, expected Awake")
    else:
        warnings.append("environment sidecar missing (legacy run)")

    reasons.extend(logcat_reasons(logcat_text))
    return (not reasons), reasons, warnings


def iter_test_dirs(run_dir: Path):
    for test_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        name = test_dir.name
        if name not in TEST_CLASSES:
            continue
        rep_dirs = sorted(p for p in test_dir.iterdir() if p.is_dir() and p.name.startswith("rep-"))
        if rep_dirs:
            for rep_dir in rep_dirs:
                yield name, rep_dir, int(rep_dir.name.split("-")[1])
        else:
            yield name, test_dir, 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--budget-ms", type=float, default=None)
    args = ap.parse_args()

    run_dir = args.run_dir.resolve()
    manifest = {}
    mpath = run_dir / "manifest.json"
    if mpath.exists():
        manifest = json.loads(mpath.read_text())
    if not manifest.get("policy"):
        # A block killed before it writes its manifest still lives under
        # .../<policy>/uibench/run-NN.  Recover the label from the path.
        parts = run_dir.parts
        if "uibench" in parts:
            idx = parts.index("uibench")
            if idx >= 1:
                manifest["policy"] = parts[idx - 1]
    refresh = float(manifest.get("refresh_hz") or 60.0)
    budget = args.budget_ms if args.budget_ms is not None else 1000.0 / refresh
    expected_ops = manifest.get("expected_ops")
    expected_state = manifest.get("expected_state")
    require_env = bool(manifest.get("require_env", False))
    if expected_state is None:
        expected_state = "disabled" if manifest.get("policy") in ("eevdf", "base") else "enabled"

    rows = []
    for test, tdir, rep in iter_test_dirs(run_dir):
        helper = parse_helper(tdir / "instrument.txt")
        avg = helper["avg"]
        env = read_env(tdir / "env.json")
        if env:
            if not env.get("expected_ops"):
                env["expected_ops"] = expected_ops
            if not env.get("expected_state"):
                env["expected_state"] = expected_state
        status, duration = test_status(tdir / "instrument.txt")
        logcat_text = ""
        lpath = tdir / "logcat.txt"
        if lpath.exists():
            logcat_text = lpath.read_text(errors="replace")

        frames = int(avg.get("total-frames", 0))
        jank_frames = int(avg.get("num-frame-deadline-missed", -1))
        jank_pct = avg.get("jank")
        if jank_frames < 0:
            jank_frames = (
                int(round(frames * jank_pct / 100.0)) if frames and jank_pct is not None else -1
            )

        raw, raw_n, raw_truncated = read_framestats(tdir / "gfxinfo_framestats.txt")
        raw_s = summarize(raw, budget)

        valid, reasons, warnings = validate(status, frames, avg, env, logcat_text, duration)
        if require_env and not env:
            valid = False
            reasons.append("environment sidecar missing for a harness run that requires it")

        rows.append(
            {
                "test": test,
                "class_method": TEST_CLASSES[test],
                "rep": rep,
                "status": status,
                "valid": bool(valid),
                "invalid_reason": "; ".join(reasons),
                "warnings": "; ".join(warnings),
                "wall_time_s": duration,
                "frames": frames,
                "p50_ms": avg.get("frame-time-50"),
                "p90_ms": avg.get("frame-time-90"),
                "p95_ms": avg.get("frame-time-95"),
                "p99_ms": avg.get("frame-time-99"),
                "jank_pct": jank_pct,
                "jank_frames": jank_frames,
                "slow_draw_pct": avg.get("slow-draw"),
                "slow_ui_pct": avg.get("slow-ui-thread"),
                "slow_bitmap_pct": avg.get("slow-bitmap-uploads"),
                "missed_vsync": avg.get("missed-vsync"),
                "deadline_missed": avg.get("num-frame-deadline-missed"),
                "high_input_latency_ms": avg.get("high-input-latency"),
                "metric_source": "GfxMonitor instrumentation gfx-avg-*",
                "raw_frames": raw_s["frames"],
                "raw_p50_ms": raw_s["p50_ms"],
                "raw_p95_ms": raw_s["p95_ms"],
                "raw_p99_ms": raw_s["p99_ms"],
                "raw_max_ms": raw_s["max_ms"],
                "raw_jank_pct": raw_s["jank_pct"],
                "raw_window_truncated": raw_truncated,
                "raw_metric": "FrameCompleted - IntendedVsync (diagnostic)",
                "systemui_pid_before": env.get("systemui_pid_before"),
                "systemui_pid_after": env.get("systemui_pid_after"),
                "top_activity_after": env.get("top_activity_after"),
                "boot_id": env.get("boot_id"),
                "selinux": env.get("selinux"),
                "scx_state_before": env.get("scx_state_before"),
                "scx_ops_before": env.get("scx_ops_before"),
                "host_loadavg": env.get("host_loadavg"),
                "host_mem_available_mb": env.get("host_mem_available_mb"),
            }
        )

    out = {
        "run_dir": str(run_dir),
        "kernel": manifest.get("kernel"),
        "platform": manifest.get("platform"),
        "policy": manifest.get("policy"),
        "vcpu": manifest.get("vcpu"),
        "refresh_hz": refresh,
        "jank_budget_ms": round(budget, 4),
        "percentile_method": "GfxMonitor histogram, whole-millisecond buckets",
        "primary_metric": "GfxMonitor instrumentation gfx-avg-* over all frames in the test window",
        "diagnostic_metric": "FrameCompleted - IntendedVsync from the gfxinfo 120-frame ring buffer",
        "validity_rule": "instrumentation passed, GfxMonitor frames >= 100, stable SystemUI and foreground activity",
        "tests": rows,
    }
    (run_dir / "metrics.json").write_text(json.dumps(out, indent=2) + "\n")

    fields = list(rows[0].keys()) if rows else []
    with (run_dir / "metrics.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    header = (
        f"{'test':<22}{'status':<9}{'valid':<6}{'n':>6}{'p50':>7}"
        f"{'p95':>7}{'p99':>7}{'jank%':>8}{'jankN':>6}{'rawN':>6}{'rawP95':>8}"
    )
    print(header)
    for r in rows:
        print(
            f"{r['test']:<22}{r['status']:<9}{str(r['valid']):<6}{r['frames']:>6}"
            f"{(r['p50_ms'] if r['p50_ms'] is not None else float('nan')):>7.1f}"
            f"{(r['p95_ms'] if r['p95_ms'] is not None else float('nan')):>7.1f}"
            f"{(r['p99_ms'] if r['p99_ms'] is not None else float('nan')):>7.1f}"
            f"{(r['jank_pct'] if r['jank_pct'] is not None else float('nan')):>8.2f}"
            f"{r['jank_frames']:>6}{r['raw_frames']:>6}"
            f"{(r['raw_p95_ms'] if r['raw_p95_ms'] is not None else float('nan')):>8.2f}"
        )
        if not r["valid"]:
            print(f"  INVALID: {r['invalid_reason']}")
    print(f"budget {budget:.4f} ms at {refresh:.2f} Hz; raw FrameMetrics are diagnostic only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
