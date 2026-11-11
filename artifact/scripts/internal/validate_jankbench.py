#!/usr/bin/env python3
"""Validate a JankBench result matrix against plan/47 criteria J1-J12.

Usage:
  validate_jankbench.py [RESULT_ROOT] [--out-dir DIR] [--expected-runs N]
                        [--benchmark-id N] [--strict] [--quiet]

RESULT_ROOT is a matrix root such as
  artifact/results/jankbench
containing <policy>/run-<n>/ directories.

The validator uses only data already on disk.  It does not contact the
device, the host monitor, or the network.  A criterion that cannot be
evaluated from the recorded data is reported as ``unverifiable``; it is not
silently treated as a pass.  A run is ``valid`` when no evaluable criterion
fails.  A run is ``strict_valid`` only when no criterion fails *and* every
J1-J12 criterion is evaluable.

Outputs (default: RESULT_ROOT):
  validity.json   matrix report, per-run checks, M1-M5 controls
  validity.csv    one row per discovered or expected run

Exit status:
  0  all discovered runs valid under the evaluable checks
  1  at least one run has a failed check
  2  --strict was given and at least one J1-J12 criterion is unverifiable
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import math
import os
import re
import sqlite3
import statistics
import sys
from collections import Counter, OrderedDict, defaultdict

SCHEMA = "scx_fcp.jankbench_validity.v1"

# ---------------------------------------------------------------------------
# Frozen expectations.  These are copied from plan/47 and from the manifests
# of the 10valid matrix.  They are deliberately explicit so a future matrix
# with a different freeze fails loudly instead of being pooled silently.

EXPECTED = {
    "benchmark_id": 0,
    "run_count": 3,
    "finish_when_done": True,
    "kernel": "6.18.33-cf618+",
    "nproc": 16,
    "sdk": 37,
    "build": "aosp_cf_x86_64_only_phone-userdebug 17 CP2A.260605.016 16644040 test-keys",
    "selinux": "Permissive",
    "app_sha256": "e4e521a3a6fa5c6a754fac9d4dcd9a5238c3c915af1dd84f1f086b12887974af",
    "min_total_frames": 4000,
    "min_iteration_frames": 1200,
    "iterations": [0, 1, 2],
    "host_window_s": [90.0, 180.0],
    "host_load_start_max": 3.0,
    "guest_load_start_max": 2.0,
    "host_load_end_warn": 3.0,
    "guest_load_end_warn": 2.0,
    "windows_cpu_mean_max_pct": 25.0,
    "windows_cpu_max_max_pct": 60.0,
    "bpf_stats_timing": 0,
}

# The paper's five-run EEVDF reference used by M1.
M1_HISTORICAL = {"mean_ms": 5.113, "p95_ms": 14.444}
M1_TOLERANCE_PCT = 10.0

# Catalogue shared with run_jankbench_pilot.sh.  It maps the integer benchmark
# id used by EXTRA_ENABLED_BENCHMARK_IDS to the name the app writes to
# ui_results.name and to the per-benchmark completeness floors.  Keeping this
# in one JSON file prevents the harness and the validator from drifting.
BENCHMARKS_CONFIG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "jankbench_benchmarks.json")


def load_benchmarks(path=None):
    path = path or BENCHMARKS_CONFIG
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return {int(k): v for k, v in (data.get("benchmarks") or {}).items()}


BENCHMARKS = load_benchmarks()

# policy -> expected binary, ops prefix, scheduler hash, and whether sched_ext
# is expected to be attached during the benchmark.
POLICY_SPEC = OrderedDict([
    ("eevdf", {
        "binary": "none", "ops": "", "sha256": "none", "attach": False,
        "label": "EEVDF (no sched_ext)",
    }),
    ("scx_lavd", {
        "binary": "scx_lavd", "ops": "lavd", "sha256":
        "638f9d2f583fce47ad105fd64d0467dcacbf7d32aa5d24ad9d8ba98f9d7ffba9",
        "attach": True, "label": "scx_lavd",
    }),
    ("scx_p2dq_pelt_off", {
        "binary": "scx_p2dq", "ops": "p2dq", "sha256":
        "c35ba165ad05568fa610272e6e00b32ecb53863687c4483f192aaab4bc78bdf1",
        "attach": True, "label": "scx_p2dq PELT off",
    }),
    ("scx_fcp_zh", {
        "binary": "scx_fcp", "ops": "fcp_zh", "sha256":
        "184d63b671cc0a59ad5dcf3d57622bdbaf1097dad0bfdd1494a331d9ab905b68",
        "attach": True, "label": "scx_fcp base (fcp_zh)",
    }),
    ("scx_fcp_nh", {
        "binary": "scx_fcp_named", "ops": "fcp_nh", "sha256":
        "bbb3c8a3e705e7da7fbc35c370c56553dee7acd4b65484ca01901ee019553d2b",
        "attach": True, "label": "scx_fcp named (fcp_nh)",
    }),
    ("scx_fcp_v_a_depth3_tgid1ms", {
        "binary": "scx_fcp_v_a_depth3_tgid1ms", "ops": "fcp_zh", "sha256":
        "5a2e7a7e5bca9a8cd0f9d25426e9766dae4ff4aaecfe22579a6a4d0371aab4c4",
        "attach": True, "label": "scx_fcp tuned",
    }),
])
POLICY_ALIASES = {
    "scx_fcp_named": "scx_fcp_nh",
}

REQUIRED_FILES = ["manifest.json", "summary.json", "BenchmarkResults.db",
                  "frames.csv"]
SUPPORT_FILES = ["logcat.txt", "harness.log", "scheduler.log", "am_start.txt",
                 "db_probe.txt", "kernel.txt", "nproc.txt", "sdk.txt",
                 "build.txt", "selinux.txt", "app_sha256.txt",
                 "scheduler_sha256.txt", "state_before.txt", "state_after.txt",
                 "host_load_start.txt", "host_load_end.txt",
                 "guest_load_start.txt", "guest_load_end.txt"]

LOGCAT_PATTERNS = [
    ("targeted_input_injection", re.compile(r"Targeted input event injection")),
    ("fatal_exception", re.compile(r"FATAL EXCEPTION")),
    ("benchmark_anr", re.compile(r"ANR in com\.android\.benchmark")),
    ("systemui_anr", re.compile(r"ANR in com\.android\.systemui")),
    ("benchmark_died",
     re.compile(r"Process com\.android\.benchmark[^\n]*has died")),
    ("systemui_died",
     re.compile(r"Process com\.android\.systemui[^\n]*has died")),
    ("system_server_died",
     re.compile(r"Process (?:system_server|com\.android\.system_server)"
                r"[^\n]*has died")),
    ("system_server_anr", re.compile(r"ANR in system_server")),
    ("lowmemorykiller",
     re.compile(r"lowmemorykiller|lmkd[^\n]*(?:Kill|kill)")),
]


# ---------------------------------------------------------------------------
# Small helpers


def iso_now():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def read_int_file(path):
    try:
        return int(read_text(path).strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def first_float(value):
    if value is None:
        return None
    try:
        return float(str(value).strip().split()[0])
    except (ValueError, IndexError):
        return None


def parse_iso(value):
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None


def parse_uptime(value):
    return first_float(value)


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def percentile(values, q):
    """Linear-interpolated percentile, same rule as parse_jankbench.py."""
    values = [v for v in values if v is not None]
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


def q1_q3(values):
    return percentile(values, 25), percentile(values, 75)


def pct_delta(current, reference):
    if current is None or reference in (None, 0):
        return None
    return 100.0 * (current - reference) / reference


def short(value, limit=240):
    text = str(value).replace("\n", " ").strip()
    return text[:limit]


def status_check(checks, check_id, ok, detail):
    checks[check_id] = {
        "status": "pass" if ok else "fail",
        "detail": detail,
    }
    return ok


def status_gap(checks, check_id, detail):
    checks[check_id] = {
        "status": "unverifiable",
        "detail": detail,
    }


def status_skip(checks, check_id, detail):
    checks[check_id] = {
        "status": "unverifiable",
        "detail": detail,
    }


def check_ids_for_summary():
    return [
        "J1_files", "J1b_file_consistency", "J2_manifest", "J3_frames",
        "J4_platform",
        "J5a_hashes", "J5b_state_before", "J5c_state_after_benchmark",
        "J5d_state_after_stop", "J6a_systemui", "J6b_system_server",
        "J6c_benchmark_process", "J7_logcat", "J8_boot_id",
        "J9_host_window", "J10a_load_gate", "J10b_windows_cpu",
        "J11_bpf_stats_off", "J12_freeze_manifest",
        "W_warmup",
    ]


def norm_policy(name):
    return POLICY_ALIASES.get(name, name)


# ---------------------------------------------------------------------------
# Database and summary consistency


def inspect_database(db_path):
    """Return row/frame/iteration facts, or an error string."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "select iteration, total_duration from ui_results"
        ).fetchall()
        con.close()
    except (sqlite3.Error, OSError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    frames = [r for r in rows if r["total_duration"] is not None]
    iterations = sorted({int(r["iteration"]) for r in frames})
    per_iteration = Counter(int(r["iteration"]) for r in frames)
    rows_per_iteration = Counter(int(r["iteration"]) for r in rows)
    return {
        "rows": len(rows),
        "frames": len(frames),
        "iterations": iterations,
        "frames_per_iteration": {str(k): per_iteration[k]
                                 for k in sorted(per_iteration)},
        "rows_per_iteration": {str(k): rows_per_iteration[k]
                               for k in sorted(rows_per_iteration)},
    }


# ---------------------------------------------------------------------------
# Per-run evaluation


def evaluate_run(root, policy, run, expected, freeze_manifest=None):
    """Evaluate one run directory.  Returns a JSON-safe dict."""
    run_dir = os.path.join(root, policy, f"run-{run}")
    record = {
        "policy": policy,
        "run": run,
        "path": os.path.relpath(run_dir, root).replace(os.sep, "/"),
        "present": os.path.isdir(run_dir),
        "valid": False,
        "strict_valid": False,
        "checks": OrderedDict(),
        "reasons": [],
        "warnings": [],
        "unverifiable": [],
        "protocol_gaps": [],
        "metrics": {},
        "loads": {},
        "boot": {},
        "hashes": {},
    }
    checks = record["checks"]

    if not record["present"]:
        status_check(checks, "J1_files", False,
                     f"missing run directory {run_dir}")
        record["reasons"].append("J1_files: missing run directory")
        for cid in check_ids_for_summary()[1:]:
            status_skip(checks, cid, "run directory missing")
        return record

    missing = [name for name in REQUIRED_FILES
               if not os.path.isfile(os.path.join(run_dir, name))]
    missing_support = [name for name in SUPPORT_FILES
                       if not os.path.isfile(os.path.join(run_dir, name))]
    status_check(
        checks, "J1_files", not missing,
        "required files present" if not missing
        else "missing required: " + ", ".join(missing),
    )
    if missing:
        record["reasons"].append(
            "J1_files: missing required file(s): " + ", ".join(missing))
    if missing_support:
        record["warnings"].append(
            "missing supporting file(s): " + ", ".join(missing_support))

    manifest_path = os.path.join(run_dir, "manifest.json")
    summary_path = os.path.join(run_dir, "summary.json")
    manifest = None
    summary = None
    if os.path.isfile(manifest_path):
        try:
            manifest = read_json(manifest_path)
        except (OSError, json.JSONDecodeError) as exc:
            record["reasons"].append(f"J2_manifest: cannot read manifest: {exc}")
    if os.path.isfile(summary_path):
        try:
            summary = read_json(summary_path)
        except (OSError, json.JSONDecodeError) as exc:
            record["reasons"].append(f"J3_frames: cannot read summary: {exc}")

    if manifest is None or summary is None:
        for cid in check_ids_for_summary():
            if cid not in checks:
                status_check(checks, cid, False,
                             "manifest.json or summary.json missing/unreadable")
        record["valid"] = False
        return record

    # J1b: supporting text files must agree with the manifest.  These files
    # are what the driver captured at run time; the manifest is assembled
    # from them, so a disagreement indicates a corrupted or hand-edited run.
    consistency_problems = []

    def compare_text_file(name, expected_value, label):
        path = os.path.join(run_dir, name)
        if not os.path.isfile(path):
            return
        try:
            actual = read_text(path).strip()
        except OSError as exc:
            consistency_problems.append(f"{name}: unreadable ({exc})")
            return
        expected_text = "" if expected_value is None else str(expected_value).strip()
        if actual != expected_text:
            consistency_problems.append(
                f"{label}: file {actual!r} != manifest {expected_text!r}")

    compare_text_file("kernel.txt", manifest.get("kernel"), "kernel")
    compare_text_file("nproc.txt", manifest.get("nproc"), "nproc")
    compare_text_file("sdk.txt", manifest.get("sdk"), "sdk")
    compare_text_file("build.txt", manifest.get("build"), "build")
    compare_text_file("selinux.txt", manifest.get("selinux"), "selinux")
    compare_text_file("app_sha256.txt", manifest.get("app_sha256"),
                      "app_sha256")
    compare_text_file("scheduler_sha256.txt",
                      manifest.get("scheduler_sha256"), "scheduler_sha256")
    for name, state_key, ops_key in (
            ("state_before.txt", "scx_state_before", "scx_ops_before"),
            ("state_after.txt", "scx_state_after_stop",
             "scx_ops_after_stop")):
        path = os.path.join(run_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            lines = read_text(path).splitlines()
        except OSError as exc:
            consistency_problems.append(f"{name}: unreadable ({exc})")
            continue
        file_state = lines[0].strip() if len(lines) > 0 else ""
        file_ops = lines[1].strip() if len(lines) > 1 else ""
        manifest_state = (manifest.get(state_key) or "").strip()
        manifest_ops = (manifest.get(ops_key) or "").strip()
        if file_state != manifest_state:
            consistency_problems.append(
                f"{name} state {file_state!r} != manifest "
                f"{manifest_state!r}")
        if file_ops != manifest_ops:
            consistency_problems.append(
                f"{name} ops {file_ops!r} != manifest {manifest_ops!r}")
    status_check(checks, "J1b_file_consistency",
                 not consistency_problems,
                 "supporting files agree with the manifest"
                 if not consistency_problems else
                 "; ".join(consistency_problems))
    record["reasons"].extend(
        f"J1b_file_consistency: {p}" for p in consistency_problems)

    # ---- J2: manifest identity and benchmark invocation -------------------
    j2_problems = []
    if norm_policy(manifest.get("policy")) != norm_policy(policy):
        j2_problems.append(
            f"manifest policy {manifest.get('policy')!r} != directory {policy!r}")
    if int(manifest.get("run", -1)) != int(run):
        j2_problems.append(
            f"manifest run {manifest.get('run')!r} != directory run {run}")
    if manifest.get("benchmark_id") != expected["benchmark_id"]:
        j2_problems.append(
            f"benchmark_id={manifest.get('benchmark_id')!r} != "
            f"{expected['benchmark_id']}")
    expected_name = expected.get("benchmark_name")
    manifest_name = manifest.get("benchmark_name")
    if expected_name and manifest_name is not None and manifest_name != expected_name:
        j2_problems.append(
            f"benchmark_name={manifest_name!r} != {expected_name!r}")
    if manifest.get("run_count") != expected["run_count"]:
        j2_problems.append(
            f"run_count={manifest.get('run_count')!r} != "
            f"{expected['run_count']}")
    if manifest.get("finish_when_done") is not expected["finish_when_done"]:
        j2_problems.append(
            f"finish_when_done={manifest.get('finish_when_done')!r}")
    if manifest.get("benchmark_am_rc") not in (0, None):
        j2_problems.append(f"benchmark_am_rc={manifest.get('benchmark_am_rc')!r}")
    status_check(checks, "J2_manifest", not j2_problems,
                 "identity and invocation match" if not j2_problems
                 else "; ".join(j2_problems))
    record["reasons"].extend(f"J2_manifest: {p}" for p in j2_problems)

    # ---- J3: frame counts, iterations, and DB consistency -----------------
    db_path = os.path.join(run_dir, "BenchmarkResults.db")
    db = inspect_database(db_path) if os.path.isfile(db_path) else {
        "error": "database missing"}
    record["metrics"] = {
        "frames": summary.get("frames"),
        "rows": summary.get("rows"),
        "iterations": summary.get("iterations"),
        "warmup_iterations": summary.get("warmup_iterations"),
        "mean_ms": (summary.get("overall") or {}).get("mean"),
        "p50_ms": (summary.get("overall") or {}).get("p50"),
        "p95_ms": (summary.get("overall") or {}).get("p95"),
        "p99_ms": (summary.get("overall") or {}).get("p99"),
        "jank_pct": (summary.get("overall") or {}).get("jank_pct"),
    }

    j3_problems = []
    summary_warmup = summary.get("warmup_iterations")
    if summary_warmup is None:
        summary_warmup = 0
        record["protocol_gaps"].append(
            "summary has no warmup_iterations field; validator assumes 0")
    elif not isinstance(summary_warmup, int) or summary_warmup < 0:
        j3_problems.append(
            f"summary warmup_iterations invalid: {summary_warmup!r}")
        summary_warmup = 0
    run_count = manifest.get("run_count")
    if isinstance(run_count, int) and run_count > 0:
        expected_db_iters = list(range(run_count))
    else:
        expected_db_iters = list(expected["iterations"])
    expected_summary_iters = [i for i in expected_db_iters
                              if i >= summary_warmup]
    if summary.get("iterations") != expected_summary_iters:
        j3_problems.append(
            f"summary iterations {summary.get('iterations')!r} != "
            f"{expected_summary_iters!r}")
    db_frames = db.get("frames")
    if "error" in db:
        j3_problems.append(f"database unusable: {db['error']}")
    else:
        if db.get("iterations") != expected_db_iters:
            j3_problems.append(
                f"database iterations {db.get('iterations')!r} != "
                f"{expected_db_iters!r}")
        db_measured_frames = sum(
            count for iteration, count in
            (db.get("frames_per_iteration") or {}).items()
            if int(iteration) >= summary_warmup)
        db_measured_rows = sum(
            count for iteration, count in
            (db.get("rows_per_iteration") or {}).items()
            if int(iteration) >= summary_warmup)
        if summary.get("frames") != db_measured_frames:
            j3_problems.append(
                f"summary frames {summary.get('frames')!r} != "
                f"database measured frames {db_measured_frames!r} "
                f"(warmup {summary_warmup})")
        if summary.get("rows") != db_measured_rows:
            j3_problems.append(
                f"summary rows {summary.get('rows')!r} != "
                f"database measured rows {db_measured_rows!r} "
                f"(warmup {summary_warmup})")

    frames = summary.get("frames")
    if not isinstance(frames, int) or frames < expected["min_total_frames"]:
        j3_problems.append(
            f"total frames {frames!r} < {expected['min_total_frames']}")
    per_iter = {int(item.get("iteration")): item
                for item in summary.get("per_iteration", [])
                if item.get("iteration") is not None}
    for iteration in expected_summary_iters:
        item = per_iter.get(iteration)
        if item is None:
            j3_problems.append(f"missing per-iteration record {iteration}")
            continue
        count = item.get("frames")
        if not isinstance(count, int) or count < expected["min_iteration_frames"]:
            j3_problems.append(
                f"iteration {iteration} frames {count!r} < "
                f"{expected['min_iteration_frames']}")
    names = summary.get("names") or []
    expected_name = expected.get("benchmark_name")
    if expected_name and names != [expected_name]:
        j3_problems.append(
            f"unexpected benchmark names {names!r}, expected "
            f"[{expected_name!r}]")

    status_check(checks, "J3_frames", not j3_problems,
                 "frame counts, iterations, and DB agree" if not j3_problems
                 else "; ".join(j3_problems))
    record["reasons"].extend(f"J3_frames: {p}" for p in j3_problems)
    record["metrics"]["db_frames"] = db_frames
    record["metrics"]["db_iterations"] = db.get("iterations")

    # ---- J4: platform and image -------------------------------------------
    j4_problems = []
    if manifest.get("kernel") != expected["kernel"]:
        j4_problems.append(
            f"kernel {manifest.get('kernel')!r} != {expected['kernel']!r}")
    if manifest.get("nproc") != expected["nproc"]:
        j4_problems.append(
            f"nproc {manifest.get('nproc')!r} != {expected['nproc']}")
    if manifest.get("sdk") != expected["sdk"]:
        j4_problems.append(f"sdk {manifest.get('sdk')!r} != {expected['sdk']}")
    if manifest.get("build") != expected["build"]:
        j4_problems.append(
            f"build {manifest.get('build')!r} != frozen build")
    if manifest.get("selinux") != expected["selinux"]:
        j4_problems.append(
            f"selinux {manifest.get('selinux')!r} != "
            f"{expected['selinux']!r}")
    status_check(checks, "J4_platform", not j4_problems,
                 "kernel, vCPU, SDK, build, SELinux match" if not j4_problems
                 else "; ".join(j4_problems))
    record["reasons"].extend(f"J4_platform: {p}" for p in j4_problems)

    # ---- J5: provenance, hashes, scheduler state/ops ----------------------
    spec = POLICY_SPEC.get(norm_policy(policy))
    if spec is None:
        status_check(checks, "J5a_hashes", False,
                     f"no frozen expectations for policy {policy!r}")
        record["reasons"].append(
            f"J5a_hashes: unknown policy {policy!r}")
    else:
        j5a_problems = []
        app_sha = manifest.get("app_sha256")
        sched_sha = (manifest.get("scheduler_sha256") or "").strip()
        if app_sha != expected["app_sha256"]:
            j5a_problems.append(
                f"app_sha256 {app_sha!r} != frozen {expected['app_sha256']!r}")
        if sched_sha != spec["sha256"]:
            j5a_problems.append(
                f"scheduler_sha256 {sched_sha!r} != frozen {spec['sha256']!r}")
        if manifest.get("scheduler_binary") != spec["binary"]:
            j5a_problems.append(
                f"scheduler_binary {manifest.get('scheduler_binary')!r} != "
                f"{spec['binary']!r}")
        status_check(checks, "J5a_hashes", not j5a_problems,
                     "app and scheduler hashes match the freeze"
                     if not j5a_problems else "; ".join(j5a_problems))
        record["reasons"].extend(f"J5a_hashes: {p}" for p in j5a_problems)
        record["hashes"] = {
            "app_sha256": app_sha,
            "scheduler_sha256": sched_sha,
            "scheduler_binary": manifest.get("scheduler_binary"),
        }

        def state_ops_problems(prefix, state_key, ops_key):
            problems = []
            state_val = (manifest.get(state_key) or "").strip()
            ops_val = (manifest.get(ops_key) or "").strip()
            if spec["attach"]:
                if state_val != "enabled":
                    problems.append(f"{prefix} state={state_val!r} != 'enabled'")
                if spec["ops"] and not ops_val.startswith(spec["ops"]):
                    problems.append(
                        f"{prefix} ops={ops_val!r} does not start with "
                        f"{spec['ops']!r}")
            else:
                if state_val != "disabled":
                    problems.append(
                        f"{prefix} state={state_val!r} != 'disabled' for EEVDF")
                if ops_val:
                    problems.append(
                        f"{prefix} ops={ops_val!r} must be empty for EEVDF")
            return problems

        before_problems = state_ops_problems(
            "before", "scx_state_before", "scx_ops_before")
        status_check(checks, "J5b_state_before", not before_problems,
                     "scheduler state/ops match the intended policy"
                     if not before_problems else "; ".join(before_problems))
        record["reasons"].extend(
            f"J5b_state_before: {p}" for p in before_problems)

        bench_problems = state_ops_problems(
            "after benchmark", "scx_state_after_benchmark",
            "scx_ops_after_benchmark")
        status_check(checks, "J5c_state_after_benchmark", not bench_problems,
                     "scheduler still attached at benchmark end"
                     if not bench_problems else "; ".join(bench_problems))
        record["reasons"].extend(
            f"J5c_state_after_benchmark: {p}" for p in bench_problems)

        stop_problems = []
        after_state = (manifest.get("scx_state_after_stop") or "").strip()
        after_ops = (manifest.get("scx_ops_after_stop") or "").strip()
        if after_state != "disabled":
            stop_problems.append(
                f"after stop state={after_state!r} != 'disabled'")
        if after_ops:
            stop_problems.append(
                f"after stop ops={after_ops!r} must be empty")
        status_check(checks, "J5d_state_after_stop", not stop_problems,
                     "sched_ext disabled and detached after the run"
                     if not stop_problems else "; ".join(stop_problems))
        record["reasons"].extend(
            f"J5d_state_after_stop: {p}" for p in stop_problems)

    # ---- J6: SystemUI, system_server, benchmark process -------------------
    ui_before = (manifest.get("systemui_pid_before") or "").strip()
    ui_after = (manifest.get("systemui_pid_after") or "").strip()
    ui_ok = bool(ui_before) and bool(ui_after) and ui_before == ui_after
    status_check(checks, "J6a_systemui", ui_ok,
                 f"SystemUI pid {ui_before} -> {ui_after}"
                 if ui_ok else
                 f"SystemUI pid changed or missing: {ui_before!r} -> "
                 f"{ui_after!r}")
    if not ui_ok:
        record["reasons"].append(
            f"J6a_systemui: SystemUI pid {ui_before!r} -> {ui_after!r}")

    ss_before = (manifest.get("system_server_pid_before") or "").strip()
    ss_after = (manifest.get("system_server_pid_after") or "").strip()
    if not ss_before and not ss_after:
        status_gap(
            checks, "J6b_system_server",
            "manifest has no system_server_pid_before/after; restart cannot "
            "be excluded from the manifest (logcat is scanned by J7)")
    else:
        ss_ok = bool(ss_before) and ss_before == ss_after
        status_check(checks, "J6b_system_server", ss_ok,
                     f"system_server pid {ss_before} -> {ss_after}"
                     if ss_ok else
                     f"system_server pid {ss_before!r} -> {ss_after!r}")
        if not ss_ok:
            record["reasons"].append(
                f"J6b_system_server: pid {ss_before!r} -> {ss_after!r}")

    bm_before = (manifest.get("benchmark_pid_before") or "").strip()
    bm_after = (manifest.get("benchmark_pid_after") or "").strip()
    harness_path = os.path.join(run_dir, "harness.log")
    restart_lines = []
    if os.path.isfile(harness_path):
        for line in read_text(harness_path).splitlines():
            if ("benchmark process restarted" in line
                    or "SystemUI restarted during run" in line):
                restart_lines.append(short(line))
    if bm_before and bm_after:
        bm_ok = bm_before == bm_after
        status_check(checks, "J6c_benchmark_process", bm_ok,
                     f"benchmark pid {bm_before} -> {bm_after}"
                     if bm_ok else
                     f"benchmark pid {bm_before!r} -> {bm_after!r}")
        if not bm_ok:
            record["reasons"].append(
                f"J6c_benchmark_process: pid {bm_before!r} -> {bm_after!r}")
    elif restart_lines:
        status_check(checks, "J6c_benchmark_process", False,
                     "; ".join(restart_lines[:3]))
        record["reasons"].append(
            "J6c_benchmark_process: " + "; ".join(restart_lines[:3]))
    elif os.path.isfile(harness_path):
        status_check(
            checks, "J6c_benchmark_process", True,
            "harness.log has no benchmark/SystemUI restart line; PID fields "
            "are not recorded, so this is an indirect check")
    else:
        status_gap(checks, "J6c_benchmark_process",
                   "no benchmark PID fields and no harness.log")

    # ---- J7: crash, sleep, and logcat signatures ---------------------------
    j7_problems = []
    if manifest.get("crash_suspected") not in (None, ""):
        j7_problems.append(
            f"crash_suspected={manifest.get('crash_suspected')!r}")
    sleep_events = manifest.get("sleep_events_during_run")
    if sleep_events != 0:
        j7_problems.append(f"sleep_events_during_run={sleep_events!r}")
    logcat_path = os.path.join(run_dir, "logcat.txt")
    logcat_hits = {}
    if os.path.isfile(logcat_path):
        counts = Counter()
        examples = {}
        for line in read_text(logcat_path).splitlines():
            for name, pattern in LOGCAT_PATTERNS:
                if pattern.search(line):
                    counts[name] += 1
                    examples.setdefault(name, short(line))
        logcat_hits = {
            name: {"count": counts.get(name, 0),
                   "example": examples.get(name)}
            for name, _ in LOGCAT_PATTERNS
        }
        for name, _ in LOGCAT_PATTERNS:
            if counts.get(name, 0):
                j7_problems.append(
                    f"logcat {name} x{counts[name]}: "
                    f"{examples.get(name, '')}")
    else:
        record["protocol_gaps"].append(
            "J7_logcat: logcat.txt missing; log signatures not scanned")
    record["logcat"] = logcat_hits

    if os.path.isfile(logcat_path) or j7_problems:
        status_check(checks, "J7_logcat", not j7_problems,
                     "no crash, sleep, injection, ANR, death, or LMK "
                     "signature" if not j7_problems else "; ".join(j7_problems))
    else:
        status_gap(checks, "J7_logcat",
                   "logcat.txt missing and manifest has no crash/sleep event")
    record["reasons"].extend(f"J7_logcat: {p}" for p in j7_problems)

    # ---- J8: boot ID ------------------------------------------------------
    boot_start = (manifest.get("boot_id") or "").strip()
    boot_end = (manifest.get("boot_id_end") or "").strip()
    record["boot"] = {"start": boot_start, "end": boot_end or None}
    if not boot_start:
        status_check(checks, "J8_boot_id", False, "boot_id missing")
        record["reasons"].append("J8_boot_id: boot_id missing")
    elif boot_end:
        ok = boot_start == boot_end
        status_check(checks, "J8_boot_id", ok,
                     f"boot_id start {boot_start} == end {boot_end}"
                     if ok else
                     f"boot_id changed {boot_start} -> {boot_end}")
        if not ok:
            record["reasons"].append(
                f"J8_boot_id: changed {boot_start} -> {boot_end}")
    else:
        status_gap(
            checks, "J8_boot_id",
            f"manifest has no boot_id_end (start={boot_start}); a mid-run "
            "reboot is not directly provable")

    # ---- J9: host window and uptime monotonicity --------------------------
    host_start = parse_iso(manifest.get("host_start"))
    host_end = parse_iso(manifest.get("host_end"))
    uptime_start = parse_uptime(manifest.get("uptime_start"))
    uptime_end = parse_uptime(manifest.get("uptime_end"))
    window_s = None
    j9_problems = []
    if host_start is None or host_end is None:
        j9_problems.append("host_start/host_end missing or unparsable")
    else:
        window_s = (host_end - host_start).total_seconds()
        low, high = expected["host_window_s"]
        if not (low <= window_s <= high):
            j9_problems.append(
                f"host window {window_s:.3f}s outside [{low:g}, {high:g}]")
    if uptime_start is None or uptime_end is None:
        j9_problems.append("uptime_start/uptime_end missing or unparsable")
    elif uptime_end <= uptime_start:
        j9_problems.append(
            f"uptime_end {uptime_end!r} <= uptime_start {uptime_start!r}")
    status_check(checks, "J9_host_window", not j9_problems,
                 f"host window {window_s:.1f}s, uptime monotonic"
                 if not j9_problems else "; ".join(j9_problems))
    record["reasons"].extend(f"J9_host_window: {p}" for p in j9_problems)
    record["metrics"]["host_window_s"] = window_s
    record["metrics"]["uptime_start"] = uptime_start
    record["metrics"]["uptime_end"] = uptime_end

    # ---- J10a: host/guest load gate --------------------------------------
    host_start_load = first_float(manifest.get("host_load_start"))
    guest_start_load = first_float(manifest.get("guest_load_start"))
    host_end_load = first_float(manifest.get("host_load_end"))
    guest_end_load = first_float(manifest.get("guest_load_end"))
    record["loads"] = {
        "host_start": host_start_load,
        "host_end": host_end_load,
        "guest_start": guest_start_load,
        "guest_end": guest_end_load,
    }
    j10a_problems = []
    if host_start_load is None or guest_start_load is None:
        j10a_problems.append("host/guest start load missing")
    else:
        if host_start_load > expected["host_load_start_max"]:
            j10a_problems.append(
                f"host start load {host_start_load:g} > "
                f"{expected['host_load_start_max']:g}")
        if guest_start_load > expected["guest_load_start_max"]:
            j10a_problems.append(
                f"guest start load {guest_start_load:g} > "
                f"{expected['guest_load_start_max']:g}")
    if guest_end_load is not None and guest_end_load > expected["guest_load_end_warn"]:
        record["warnings"].append(
            f"guest end load {guest_end_load:g} > "
            f"{expected['guest_load_end_warn']:g} (diagnostic, not a reject)")
    if host_end_load is not None and host_end_load > expected["host_load_end_warn"]:
        record["warnings"].append(
            f"host end load {host_end_load:g} > "
            f"{expected['host_load_end_warn']:g} (diagnostic, not a reject)")
    status_check(checks, "J10a_load_gate", not j10a_problems,
                 "start load within the pre-registered gate"
                 if not j10a_problems else "; ".join(j10a_problems))
    record["reasons"].extend(f"J10a_load_gate: {p}" for p in j10a_problems)

    # ---- J10b: Windows CPU / crosvm monitor ------------------------------
    win_mean = manifest.get("windows_cpu_mean_pct")
    win_max = manifest.get("windows_cpu_max_pct")
    if win_mean is None or win_max is None:
        status_gap(
            checks, "J10b_windows_cpu",
            "no Windows CPU fields and no host monitor data joined to this "
            "run; WSL host_load is not the Windows host")
    else:
        j10b = []
        if float(win_mean) > expected["windows_cpu_mean_max_pct"]:
            j10b.append(
                f"Windows CPU mean {win_mean} > "
                f"{expected['windows_cpu_mean_max_pct']:g}%")
        if float(win_max) > expected["windows_cpu_max_max_pct"]:
            j10b.append(
                f"Windows CPU max {win_max} > "
                f"{expected['windows_cpu_max_max_pct']:g}%")
        status_check(checks, "J10b_windows_cpu", not j10b,
                     "Windows CPU within gate" if not j10b else "; ".join(j10b))
        record["reasons"].extend(f"J10b_windows_cpu: {p}" for p in j10b)

    # ---- J11: bpf_stats_enabled must be 0 for timing runs ----------------
    bpf_manifest = manifest.get("bpf_stats_requested")
    before_path = os.path.join(run_dir, "bpf_stats_enabled_before.txt")
    after_path = os.path.join(run_dir, "bpf_stats_enabled_after.txt")
    bpf_before = read_int_file(before_path) if os.path.isfile(before_path) else None
    bpf_after = read_int_file(after_path) if os.path.isfile(after_path) else None
    record["metrics"]["bpf_stats_before"] = bpf_before
    record["metrics"]["bpf_stats_after"] = bpf_after
    j11 = []
    evidence = []
    if bpf_manifest is not None:
        evidence.append(f"manifest bpf_stats_requested={bpf_manifest!r}")
        if bpf_manifest is not False:
            j11.append("manifest bpf_stats_requested is not false")
    if bpf_before is not None:
        evidence.append(f"before={bpf_before}")
        if bpf_before != expected["bpf_stats_timing"]:
            j11.append(f"bpf_stats_enabled before={bpf_before}")
    if bpf_after is not None:
        evidence.append(f"after={bpf_after}")
        if bpf_after != expected["bpf_stats_timing"]:
            j11.append(f"bpf_stats_enabled after={bpf_after}")
    if not evidence:
        status_gap(
            checks, "J11_bpf_stats_off",
            "manifest has no bpf_stats_requested and the run has no "
            "bpf_stats_enabled_before/after snapshot; stats-free operation "
            "is not proven")
    else:
        status_check(checks, "J11_bpf_stats_off", not j11,
                     "bpf_stats_enabled=0 (" + ", ".join(evidence) + ")"
                     if not j11 else "; ".join(j11))
        record["reasons"].extend(f"J11_bpf_stats_off: {p}" for p in j11)

    # ---- J12: external freeze manifest -----------------------------------
    if freeze_manifest is None:
        status_gap(
            checks, "J12_freeze_manifest",
            "no matrix.json freeze manifest at the result root; internal hash "
            "consistency is checked at matrix level")
    else:
        frozen_app = freeze_manifest.get("app_sha256")
        frozen_sched = (freeze_manifest.get("scheduler_sha256") or {}).get(
            norm_policy(policy))
        j12 = []
        if frozen_app is not None and manifest.get("app_sha256") != frozen_app:
            j12.append("app hash differs from matrix.json")
        if frozen_sched is not None and (
                (manifest.get("scheduler_sha256") or "").strip()
                != frozen_sched):
            j12.append("scheduler hash differs from matrix.json")
        status_check(checks, "J12_freeze_manifest", not j12,
                     "hashes match matrix.json" if not j12 else "; ".join(j12))
        record["reasons"].extend(f"J12_freeze_manifest: {p}" for p in j12)

    # ---- W: warm-up iteration handling (protocol, not J1-J12) ------------
    warmup_manifest = manifest.get("warmup_iterations")
    warmup_summary = summary.get("warmup_iterations")
    warmup_info = {
        "manifest": warmup_manifest,
        "summary": summary_warmup,
        "iteration_0_included": None,
        "iteration_0_outlier": None,
        "note": None,
    }
    if warmup_manifest is None:
        status_gap(
            checks, "W_warmup",
            "manifest has no warmup_iterations field; the parser used "
            f"WARMUP_ITERATIONS={summary_warmup!r} and the aggregate includes "
            "iteration 0")
        record["protocol_gaps"].append(
            "manifest has no warmup_iterations field; the parser used "
            f"WARMUP_ITERATIONS={summary_warmup!r} and the aggregate includes "
            "iteration 0")
        warmup_info["note"] = (
            "manifest field absent; cannot tell whether iteration 0 was "
            "intended as warm-up")
    elif warmup_summary is None:
        status_gap(
            checks, "W_warmup",
            f"manifest warmup_iterations={warmup_manifest!r} but summary has "
            "no warmup_iterations field")
        warmup_info["note"] = "summary field absent"
    elif int(warmup_manifest) != int(warmup_summary):
        status_check(
            checks, "W_warmup", False,
            f"manifest warmup_iterations={warmup_manifest!r} != "
            f"summary {warmup_summary!r}")
        record["reasons"].append(
            f"W_warmup: manifest warmup_iterations={warmup_manifest!r} != "
            f"summary {warmup_summary!r}")
        warmup_info["note"] = "manifest/summary mismatch"
    else:
        status_check(checks, "W_warmup", True,
                     f"warmup_iterations={summary_warmup}")
        warmup_info["note"] = "manifest and summary agree"

    if summary_warmup == 0:
        warmup_info["iteration_0_included"] = True
        i0 = per_iter.get(0)
        later = [per_iter[i] for i in expected_summary_iters if i != 0]
        if i0 and later:
            ref_p95 = median([item.get("p95") for item in later])
            ref_mean = median([item.get("mean") for item in later])
            ref_jank = median([item.get("jank_pct") for item in later])
            ratio_p95 = (i0.get("p95") / ref_p95
                         if i0.get("p95") and ref_p95 else None)
            ratio_mean = (i0.get("mean") / ref_mean
                          if i0.get("mean") and ref_mean else None)
            delta_jank = (i0.get("jank_pct") - ref_jank
                          if i0.get("jank_pct") is not None
                          and ref_jank is not None else None)
            is_outlier = bool(
                (ratio_p95 is not None and ratio_p95 >= 1.15
                 and i0.get("p95") - ref_p95 >= 1.0)
                or (ratio_mean is not None and ratio_mean >= 1.15
                    and i0.get("mean") - ref_mean >= 0.25)
                or (delta_jank is not None and delta_jank >= 3.0
                    and i0.get("jank_pct") >= 8.0))
            warmup_info["iteration_0_outlier"] = is_outlier
            warmup_info["iteration_0_p95_ratio"] = ratio_p95
            warmup_info["iteration_0_mean_ratio"] = ratio_mean
            warmup_info["iteration_0_jank_delta_pp"] = delta_jank
            if is_outlier:
                p95_txt = (f"{ratio_p95:.2f}" if ratio_p95 is not None
                           else "n/a")
                mean_txt = (f"{ratio_mean:.2f}" if ratio_mean is not None
                            else "n/a")
                record["warnings"].append(
                    "iteration 0 is a cold-start outlier "
                    f"(p95 ratio {p95_txt}, mean ratio {mean_txt}); "
                    "the summary aggregate includes it")
    else:
        warmup_info["iteration_0_included"] = False
    record["warmup"] = warmup_info

    # ---- Verdict ----------------------------------------------------------
    record["reasons"] = list(dict.fromkeys(record["reasons"]))
    record["warnings"] = list(dict.fromkeys(record["warnings"]))
    record["unverifiable"] = [
        f"{cid}: {checks[cid]['detail']}"
        for cid in check_ids_for_summary()
        if cid.startswith("J") and cid in checks
        and checks[cid]["status"] == "unverifiable"
    ]
    record["valid"] = not record["reasons"]
    record["strict_valid"] = record["valid"] and not record["unverifiable"]
    return record


# ---------------------------------------------------------------------------
# Matrix-level checks


def matrix_controls(root, records, expected_runs, freeze_manifest=None,
                    policies=None, expected=None):
    present = [r for r in records if r["present"]]
    expected = expected or EXPECTED
    by_policy = defaultdict(list)
    for rec in present:
        by_policy[rec["policy"]].append(rec)
    for policy in by_policy:
        by_policy[policy].sort(key=lambda r: r["run"])

    controls = OrderedDict()

    # Single boot / monotonic uptime across the matrix.
    boots = {r["boot"].get("start") for r in present if r["boot"].get("start")}
    boot_ok = len(boots) == 1
    by_time = sorted(
        present,
        key=lambda r: r["metrics"].get("_host_start") or "",
    )
    monotonic = True
    monotonic_pairs = []
    for previous, current in zip(by_time, by_time[1:]):
        pu = previous["metrics"].get("uptime_end")
        cu = current["metrics"].get("uptime_start")
        if pu is not None and cu is not None and not (pu <= cu):
            monotonic = False
            monotonic_pairs.append(
                f"{previous['policy']}/run-{previous['run']} "
                f"end={pu} > {current['policy']}/run-{current['run']} "
                f"start={cu}")
    controls["single_boot"] = {
        "status": "pass" if boot_ok and monotonic else "fail",
        "boot_ids": sorted(b for b in boots if b),
        "uptime_monotonic_between_runs": monotonic,
        "violations": monotonic_pairs,
        "detail": (
            "one boot ID and monotonic uptime across the matrix" if
            boot_ok and monotonic else
            f"{len(boots)} boot ID(s); monotonic={monotonic}"),
    }

    # M1: EEVDF reference, first and last run plus the cell median.
    reference = expected.get("reference")
    eevdf = by_policy.get("eevdf", [])
    if not reference:
        controls["M1_eevdf_reference"] = {
            "status": "not_applicable",
            "detail": "no frozen EEVDF reference exists for this benchmark; "
                      "M1 is the List View Fling control only",
        }
    elif len(eevdf) < 2:
        controls["M1_eevdf_reference"] = {
            "status": "unverifiable",
            "detail": "fewer than two EEVDF runs",
        }
    else:
        reference = reference
        def m1_point(rec):
            mean_ms = rec["metrics"].get("mean_ms")
            p95_ms = rec["metrics"].get("p95_ms")
            return {
                "policy": rec["policy"], "run": rec["run"],
                "mean_ms": mean_ms, "p95_ms": p95_ms,
                "mean_delta_pct": pct_delta(mean_ms, reference["mean_ms"]),
                "p95_delta_pct": pct_delta(p95_ms, reference["p95_ms"]),
                "within_10pct": (
                    mean_ms is not None and p95_ms is not None
                    and abs(pct_delta(mean_ms, reference["mean_ms"])) <= M1_TOLERANCE_PCT
                    and abs(pct_delta(p95_ms, reference["p95_ms"])) <= M1_TOLERANCE_PCT
                ),
            }
        first = m1_point(eevdf[0])
        last = m1_point(eevdf[-1])
        cell_mean = median([r["metrics"].get("mean_ms") for r in eevdf])
        cell_p95 = median([r["metrics"].get("p95_ms") for r in eevdf])
        cell = {
            "runs": len(eevdf),
            "mean_ms": cell_mean,
            "p95_ms": cell_p95,
            "mean_delta_pct": pct_delta(cell_mean, reference["mean_ms"]),
            "p95_delta_pct": pct_delta(cell_p95, reference["p95_ms"]),
            "within_10pct": (
                cell_mean is not None and cell_p95 is not None
                and abs(pct_delta(cell_mean, reference["mean_ms"])) <= M1_TOLERANCE_PCT
                and abs(pct_delta(cell_p95, reference["p95_ms"])) <= M1_TOLERANCE_PCT
            ),
        }
        controls["M1_eevdf_reference"] = {
            "status": "pass" if first["within_10pct"] and last["within_10pct"]
            else "fail",
            "historical": reference,
            "tolerance_pct": M1_TOLERANCE_PCT,
            "first_run": first,
            "last_run": last,
            "cell_median": cell,
            "detail": (
                "first and last EEVDF runs within 10% of "
                f"{reference['mean_ms']:.3f}/{reference['p95_ms']:.3f} ms"
                if first["within_10pct"] and last["within_10pct"] else
                "first and/or last EEVDF run outside the 10% reference band; "
                "M1 is not met (the signed deltas show the direction)"),
        }

    # M2: balanced order and ten runs per policy.
    complete = {p: len(v) for p, v in by_policy.items()}
    expected_policies = list(policies) if policies else list(by_policy)
    counts_ok = all(complete.get(p) == expected_runs for p in expected_policies)
    sorted_present = sorted(present, key=lambda r: r["metrics"].get("_host_start") or "")
    chunk = len(expected_policies)
    rounds = []
    balanced = True
    if chunk:
        for start in range(0, len(sorted_present), chunk):
            block = sorted_present[start:start + chunk]
            policies_in_block = [r["policy"] for r in block]
            complete_block = len(block) == chunk
            ok = complete_block and sorted(policies_in_block) == sorted(expected_policies)
            balanced = balanced and ok
            rounds.append({
                "round": start // chunk + 1,
                "runs": [f"{r['policy']}/run-{r['run']}" for r in block],
                "complete": complete_block,
                "balanced": ok,
            })
    controls["M2_balanced_order"] = {
        "status": "pass" if counts_ok and balanced else "fail",
        "runs_per_policy": complete,
        "expected_runs_per_policy": expected_runs,
        "rounds": rounds,
        "latin_square": False,
        "detail": (
            f"{len(rounds)} complete rounds, each with all "
            f"{len(expected_policies)} policies once"
            if counts_ok and balanced else
            "missing runs or an unbalanced round"),
    }

    # M3: failure rate.
    expected_total = len(expected_policies) * expected_runs
    invalid = sum(1 for r in present if not r["valid"])
    failed_dirs = []
    invalid_dirs = []
    for name in ("_failed", "_invalid"):
        path = os.path.join(root, name)
        if os.path.isdir(path):
            for dirpath, _dirnames, filenames in os.walk(path):
                if filenames:
                    failed_dirs.append(os.path.relpath(dirpath, root))
    failed_total = len(failed_dirs)
    missing = max(0, expected_total - len(present))
    attempts = len(present) + failed_total
    failure_rate = ((invalid + failed_total + missing) / expected_total
                    if expected_total else None)
    controls["M3_failure_rate"] = {
        "status": "pass" if failure_rate is not None and failure_rate <= 0.20
        else "fail",
        "expected_runs": expected_total,
        "discovered_runs": len(present),
        "invalid_runs": invalid,
        "missing_runs": missing,
        "archived_failed_or_invalid_dirs": failed_dirs[:20],
        "failure_rate": failure_rate,
        "retry_cap_evidence": "no _failed/_invalid directory at the root"
        if not failed_dirs else f"{failed_total} archived attempts",
        "detail": (
            f"{invalid + failed_total + missing}/{expected_total} invalid, "
            "missing, or archived attempts"),
    }

    # M4: monitor A/B.
    monitor_candidates = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            lower = name.lower()
            if ("host_cpu" in lower or "crosvm" in lower
                    or "monitor" in lower) and lower.endswith(
                        (".csv", ".json", ".log", ".txt")):
                monitor_candidates.append(
                    os.path.relpath(os.path.join(base, name), root))
    controls["M4_monitor_ab"] = {
        "status": "unverifiable",
        "monitor_files_found": monitor_candidates[:20],
        "detail": (
            "no monitor A/B block (3 EEVDF runs monitor off vs on) is "
            "recorded at this root"),
    }

    # M5: frozen hashes.
    app_hashes = sorted({r["hashes"].get("app_sha256")
                         for r in present if r.get("hashes")})
    per_policy_hashes = {}
    for policy in expected_policies:
        values = sorted({r["hashes"].get("scheduler_sha256")
                         for r in by_policy.get(policy, [])
                         if r.get("hashes")})
        per_policy_hashes[policy] = values
    hash_issues = []
    if len(app_hashes) != 1:
        hash_issues.append(f"app hashes vary: {app_hashes}")
    for policy, values in per_policy_hashes.items():
        if len(values) != 1:
            hash_issues.append(f"{policy} scheduler hashes vary: {values}")
    external_ok = freeze_manifest is not None
    controls["M5_frozen_hashes"] = {
        "status": "pass" if not hash_issues and external_ok else
        ("unverifiable" if not hash_issues else "fail"),
        "app_sha256": app_hashes,
        "scheduler_sha256": per_policy_hashes,
        "matrix_json_present": external_ok,
        "issues": hash_issues,
        "detail": (
            "hashes are internally constant; no matrix.json freeze manifest "
            "exists at the root" if not hash_issues and not external_ok else
            ("hashes are constant and match matrix.json"
             if not hash_issues else "; ".join(hash_issues))),
    }

    # Diagnostic: per-policy median end load.
    end_loads = {}
    for policy in expected_policies:
        values = [r["loads"].get("guest_end") for r in by_policy.get(policy, [])
                  if r["loads"].get("guest_end") is not None]
        end_loads[policy] = {
            "median_guest_end": median(values),
            "max_guest_end": max(values) if values else None,
            "above_start_threshold": (
                median(values) is not None
                and median(values) > EXPECTED["guest_load_start_max"]),
        }
    controls["end_load_diagnostic"] = end_loads
    return controls


# ---------------------------------------------------------------------------
# Report assembly


def discover(root, policies, expected_runs, expected=None):
    """Return (policies, runs) including expected-but-missing run dirs."""
    expected = expected or EXPECTED
    if policies is None:
        policies = list(POLICY_SPEC)
        if os.path.isdir(root):
            for name in sorted(os.listdir(root)):
                path = os.path.join(root, name)
                if (os.path.isdir(path) and not name.startswith(("_", "."))
                        and norm_policy(name) not in policies):
                    policies.append(name)
    records = []
    for policy in policies:
        policy_dir = os.path.join(root, policy)
        present_runs = set()
        if os.path.isdir(policy_dir):
            for name in os.listdir(policy_dir):
                match = re.fullmatch(r"run-(\d+)", name)
                if match and os.path.isdir(os.path.join(policy_dir, name)):
                    present_runs.add(int(match.group(1)))
        expected_numbers = set(range(1, expected_runs + 1))
        for run in sorted(expected_numbers | present_runs):
            rec = evaluate_run(root, policy, run, expected)
            rec["_dir"] = os.path.join(root, policy, f"run-{run}")
            records.append(rec)
    records.sort(key=lambda r: (list(POLICY_SPEC).index(r["policy"])
                                if r["policy"] in POLICY_SPEC else 99,
                                r["run"]))
    return policies, records


def build_report(root, expected_runs=10, benchmark_id=None, policies=None,
                 run_count=None, benchmarks=None):
    expected = dict(EXPECTED)
    if benchmark_id is not None:
        expected["benchmark_id"] = benchmark_id
    if run_count is not None:
        expected["run_count"] = run_count
        expected["iterations"] = list(range(run_count))
    catalogue = benchmarks if benchmarks is not None else BENCHMARKS
    bench = catalogue.get(int(expected["benchmark_id"]))
    if bench:
        expected["benchmark_name"] = bench.get("name")
        expected["benchmark_slug"] = bench.get("slug")
        expected["benchmark_activity"] = bench.get("activity")
        for key in ("min_total_frames", "min_iteration_frames",
                    "host_window_s", "reference"):
            if key in bench:
                expected[key] = bench[key]
    if policies is not None and isinstance(policies, str):
        policies = [p for p in policies.split(",") if p]
    root = os.path.abspath(root)
    freeze_path = os.path.join(root, "matrix.json")
    freeze_manifest = None
    if os.path.isfile(freeze_path):
        try:
            freeze_manifest = read_json(freeze_path)
        except (OSError, json.JSONDecodeError):
            freeze_manifest = None

    policies, records = discover(root, policies, expected_runs, expected)
    # Re-evaluate with the caller-provided benchmark id and freeze manifest.
    for rec in records:
        fresh = evaluate_run(root, rec["policy"], rec["run"], expected,
                             freeze_manifest=freeze_manifest)
        fresh["_dir"] = rec["_dir"]
        rec.clear()
        rec.update(fresh)

    # Attach host_start to metrics for order without exposing private key.
    for rec in records:
        manifest_path = os.path.join(rec["_dir"], "manifest.json")
        try:
            rec["metrics"]["_host_start"] = read_json(
                manifest_path).get("host_start")
        except (OSError, json.JSONDecodeError):
            rec["metrics"]["_host_start"] = None

    controls = matrix_controls(root, records, expected_runs, freeze_manifest,
                               policies=policies, expected=expected)

    present = [r for r in records if r["present"]]
    valid = [r for r in present if r["valid"]]
    strict = [r for r in present if r["strict_valid"]]
    by_policy = OrderedDict()
    for policy in policies:
        group = [r for r in records if r["policy"] == policy]
        p_present = [r for r in group if r["present"]]
        p_valid = [r for r in p_present if r["valid"]]
        p_strict = [r for r in p_present if r["strict_valid"]]
        by_policy[policy] = {
            "expected_runs": expected_runs,
            "discovered_runs": len(p_present),
            "valid_runs": len(p_valid),
            "invalid_runs": len(p_present) - len(p_valid),
            "strict_valid_runs": len(p_strict),
            "missing_runs": len(group) - len(p_present),
            "reasons": sorted({reason for r in p_present
                               for reason in r["reasons"]}),
        }

    criterion_counts = OrderedDict()
    for cid in check_ids_for_summary():
        statuses = Counter(
            r["checks"].get(cid, {}).get("status", "unverifiable")
            for r in present
        )
        criterion_counts[cid] = {
            "pass": statuses.get("pass", 0),
            "fail": statuses.get("fail", 0),
            "unverifiable": statuses.get("unverifiable", 0),
        }

    failure_reasons = Counter()
    warning_reasons = Counter()
    protocol_gaps = Counter()

    def warning_category(text):
        if text.startswith("iteration 0 is a cold-start outlier"):
            return "iteration 0 is a cold-start outlier"
        if text.startswith("guest end load"):
            return "guest end load above the start threshold"
        if text.startswith("host end load"):
            return "host end load above the start threshold"
        return text

    for rec in present:
        for reason in rec["reasons"]:
            failure_reasons[reason] += 1
        for warning in rec["warnings"]:
            warning_reasons[warning_category(warning)] += 1
        for gap in rec["protocol_gaps"]:
            protocol_gaps[gap] += 1

    summary = {
        "expected_runs": len(policies) * expected_runs,
        "discovered_runs": len(present),
        "valid_runs_available_checks": len(valid),
        "invalid_runs": len(present) - len(valid),
        "strict_valid_runs": len(strict),
        "runs_with_unverifiable_criteria": sum(
            1 for r in present if r["unverifiable"]),
        "missing_runs": len(records) - len(present),
        "validity_fraction_available": (
            len(valid) / len(present) if present else None),
        "validity_fraction_expected": (
            len(valid) / (len(policies) * expected_runs)
            if policies and expected_runs else None),
        "by_policy": by_policy,
        "criterion_status_counts": criterion_counts,
        "failure_reasons": dict(failure_reasons),
        "warning_reasons": dict(warning_reasons),
        "protocol_gaps": dict(protocol_gaps),
    }

    # Drop helper fields that are only needed while building the report.
    for rec in records:
        rec.pop("_dir", None)
        rec.get("metrics", {}).pop("_host_start", None)

    report = {
        "schema": SCHEMA,
        "kind": "jankbench-validity",
        "benchmark": {
            "id": expected["benchmark_id"],
            "name": expected.get("benchmark_name"),
            "slug": expected.get("benchmark_slug"),
        },
        "generated_at": iso_now(),
        "root": root,
        "expectations": {
            "kernel": expected["kernel"],
            "nproc": expected["nproc"],
            "sdk": expected["sdk"],
            "build": expected["build"],
            "selinux": expected["selinux"],
            "app_sha256": expected["app_sha256"],
            "benchmark_id": expected["benchmark_id"],
            "benchmark_name": expected.get("benchmark_name"),
            "benchmark_slug": expected.get("benchmark_slug"),
            "run_count": expected["run_count"],
            "min_total_frames": expected["min_total_frames"],
            "min_iteration_frames": expected["min_iteration_frames"],
            "host_window_s": expected["host_window_s"],
            "guest_load_start_max": expected["guest_load_start_max"],
            "host_load_start_max": expected["host_load_start_max"],
            "bpf_stats_timing": expected["bpf_stats_timing"],
            "policy_spec": {
                p: {
                    "binary": spec["binary"],
                    "ops": spec["ops"],
                    "sha256": spec["sha256"],
                } for p, spec in POLICY_SPEC.items()
            },
        },
        "summary": summary,
        "matrix": controls,
        "runs": records,
    }
    return report


def flatten_row(rec):
    metrics = rec.get("metrics", {})
    loads = rec.get("loads", {})
    checks = rec.get("checks", {})
    return {
        "policy": rec["policy"],
        "run": rec["run"],
        "present": rec["present"],
        "valid": rec["valid"],
        "strict_valid": rec["strict_valid"],
        "reasons": " | ".join(rec["reasons"]),
        "warnings": " | ".join(rec["warnings"]),
        "unverifiable": " | ".join(rec["unverifiable"]),
        "warmup_check": checks.get("W_warmup", {}).get("status"),
        "warmup_manifest": rec.get("warmup", {}).get("manifest"),
        "summary_warmup": rec.get("warmup", {}).get("summary"),
        "iteration_0_outlier": rec.get("warmup", {}).get("iteration_0_outlier"),
        "iteration_0_included": rec.get("warmup", {}).get(
            "iteration_0_included"),
        "failed_checks": " | ".join(
            cid for cid, value in checks.items()
            if value.get("status") == "fail"),
        "frames": metrics.get("frames"),
        "iterations": ",".join(str(i) for i in (metrics.get("iterations") or [])),
        "mean_ms": metrics.get("mean_ms"),
        "p50_ms": metrics.get("p50_ms"),
        "p95_ms": metrics.get("p95_ms"),
        "p99_ms": metrics.get("p99_ms"),
        "jank_pct": metrics.get("jank_pct"),
        "host_window_s": metrics.get("host_window_s"),
        "guest_load_start": loads.get("guest_start"),
        "guest_load_end": loads.get("guest_end"),
        "host_load_start": loads.get("host_start"),
        "host_load_end": loads.get("host_end"),
        "bpf_stats_before": metrics.get("bpf_stats_before"),
        "bpf_stats_after": metrics.get("bpf_stats_after"),
        "boot_id": rec.get("boot", {}).get("start"),
        "boot_id_end": rec.get("boot", {}).get("end"),
        "scheduler_sha256": rec.get("hashes", {}).get("scheduler_sha256"),
        "app_sha256": rec.get("hashes", {}).get("app_sha256"),
    }


CSV_FIELDS = [
    "policy", "run", "present", "valid", "strict_valid", "reasons",
    "warnings", "unverifiable", "warmup_check", "warmup_manifest",
    "summary_warmup", "iteration_0_outlier", "iteration_0_included",
    "failed_checks", "frames", "iterations",
    "mean_ms", "p50_ms", "p95_ms", "p99_ms", "jank_pct", "host_window_s",
    "guest_load_start", "guest_load_end", "host_load_start", "host_load_end",
    "bpf_stats_before", "bpf_stats_after", "boot_id", "boot_id_end",
    "scheduler_sha256", "app_sha256",
]


def default_out_dir(root):
    """Prefer the result root, but fall back when the matrix is root-owned."""
    if os.access(root, os.W_OK):
        return root
    fallback = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "plan", "51_validity_output")
    return fallback


def write_outputs(report, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "validity.json")
    csv_path = os.path.join(out_dir, "validity.csv")
    summary_path = os.path.join(out_dir, "validity_summary.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    summary_report = {key: value for key, value in report.items()
                      if key != "runs"}
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary_report, f, indent=2)
        f.write("\n")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for rec in report["runs"]:
            writer.writerow(flatten_row(rec))
    return json_path, csv_path


def print_summary(report):
    summary = report["summary"]
    print(f"root: {report['root']}")
    print(
        f"runs: expected={summary['expected_runs']} "
        f"discovered={summary['discovered_runs']} "
        f"valid(available)={summary['valid_runs_available_checks']} "
        f"invalid={summary['invalid_runs']} "
        f"strict_valid={summary['strict_valid_runs']} "
        f"missing={summary['missing_runs']}")
    print("by policy:")
    for policy, data in summary["by_policy"].items():
        print(
            f"  {policy:34s} valid {data['valid_runs']}/{data['discovered_runs']} "
            f"(expected {data['expected_runs']}), strict "
            f"{data['strict_valid_runs']}/{data['discovered_runs']}, "
            f"missing {data['missing_runs']}")
    if summary["failure_reasons"]:
        print("failure reasons:")
        for reason, count in summary["failure_reasons"].items():
            print(f"  x{count}: {reason}")
    print("unverifiable criteria:")
    for cid, counts in summary["criterion_status_counts"].items():
        if counts["unverifiable"]:
            print(f"  {cid:28s} {counts['unverifiable']}")
    print("matrix controls:")
    for name, value in report["matrix"].items():
        if name == "end_load_diagnostic":
            continue
        print(f"  {name:28s} {value.get('status', 'n/a'):13s} "
              f"{short(value.get('detail', ''), 100)}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root", nargs="?",
        default=os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "results", "variant_10valid",
            "jankbench"),
        help="matrix root containing <policy>/run-<n>/ directories")
    parser.add_argument("--out-dir", default=None,
                       help="directory for validity.json/validity.csv "
                            "(default: root)")
    parser.add_argument("--expected-runs", type=int, default=10)
    parser.add_argument("--benchmark-id", type=int, default=None)
    parser.add_argument("--run-count", type=int, default=None,
                       help="override expected JankBench run_count")
    parser.add_argument("--policies", default=None,
                       help="comma-separated policy list to treat as the "
                            "expected matrix (default: discover)")
    parser.add_argument("--benchmarks-config", default=None,
                       help="benchmark catalogue JSON (default: next to this "
                            "script)")
    parser.add_argument("--strict", action="store_true",
                       help="exit 2 if any J1-J12 criterion is unverifiable")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    report = build_report(
        args.root, expected_runs=args.expected_runs,
        benchmark_id=args.benchmark_id, run_count=args.run_count,
        policies=args.policies,
        benchmarks=load_benchmarks(args.benchmarks_config))
    out_dir = args.out_dir or default_out_dir(report["root"])
    if not args.quiet and out_dir != report["root"]:
        print(f"note: result root is not writable; writing to {out_dir}")
    json_path, csv_path = write_outputs(report, out_dir)
    if not args.quiet:
        print_summary(report)
        print(f"wrote {json_path}")
        print(f"wrote {csv_path}")

    if report["summary"]["invalid_runs"] or report["summary"]["missing_runs"]:
        return 1
    if args.strict and report["summary"]["strict_valid_runs"] < \
            report["summary"]["discovered_runs"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
