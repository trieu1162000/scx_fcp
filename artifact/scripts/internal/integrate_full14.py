#!/usr/bin/env python3
"""Integrate the full 14-test, 4-policy campaign into paper/website fragments.

The script never modifies the campaign's measurement or validity inputs.
Outputs default to ``<root>/integration``; ``--dry-run`` redirects them
outside the campaign, and ``--refresh-validity`` is the only mode that
rewrites ``validity.json``.  It reads the per-benchmark JankBench
``validity.json`` files and the UiBench ``validity.json`` produced by
``validate_jankbench.py`` / ``validate_uibench.py``.  It never infers
validity from raw run directories.

A cell is one (test, policy) pair.  Only runs/reps that the validators marked
``valid`` are ever aggregated.  Invalid attempts are listed and excluded; an
incomplete cell is never silently promoted to a result.  The script exits
non-zero when the required matrix is incomplete, and exits 2 when the
validity data cannot be trusted (missing/stale/malformed reports).

Outputs (written to ``--out-dir``):

  full14_summary.json    machine-readable report, all 56 cells
  full14_summary.md      policy x test table with mean/p50/p95/p99/jank/n
  full14_jankbench.tex   LaTeX fragment for the 8 JankBench benchmarks
  full14_uibench.tex     LaTeX fragment for the 6 UiBench tests
  full14_website.html    compact HTML fragment for the project page

Usage:
  integrate_full14.py RESULTS_ROOT
  integrate_full14.py RESULTS_ROOT --dry-run --allow-incomplete
  integrate_full14.py RESULTS_ROOT --refresh-validity
  integrate_full14.py RESULTS_ROOT --require-known-invalid

Exit codes:
  0  all required cells complete (known-invalid bitmap-upload allowed)
  1  required cells incomplete or failing
  2  integrity error: missing/stale/malformed validity data, or a
     configuration mismatch.  ``--allow-incomplete`` does not suppress this.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict, defaultdict

PAPER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BENCHMARKS = os.path.join(PAPER, "scripts", "jankbench_benchmarks.json")
VALIDATE_JANKBENCH = os.path.join(PAPER, "scripts", "validate_jankbench.py")
VALIDATE_UIBENCH = os.path.join(PAPER, "scripts", "validate_uibench.py")

SCHEMA = "scx_fcp.full14_integration.v1"
POLICIES = [
    "eevdf",
    "scx_fcp_v_a_depth3_tgid1ms",
    "scx_p2dq_pelt_off",
    "scx_lavd",
]
POLICY_LABELS = {
    "eevdf": "EEVDF",
    "scx_fcp_v_a_depth3_tgid1ms": "scx_fcp (Variant A)",
    "scx_p2dq_pelt_off": "scx_p2dq (PELT off)",
    "scx_lavd": "scx_lavd",
}
LATEX_POLICY_LABELS = {
    "eevdf": "EEVDF",
    "scx_fcp_v_a_depth3_tgid1ms": r"\texttt{scx\_fcp} (Variant A)",
    "scx_p2dq_pelt_off": r"\texttt{scx\_p2dq} (PELT off)",
    "scx_lavd": r"\texttt{scx\_lavd}",
}
UI_TESTS = [
    ("inflating-list", "Inflating List"),
    ("shadow-grid-list", "Shadow Grid List"),
    ("slow-bind-recycler", "Slow-bind Recycler"),
    ("trivial-recycler", "Trivial Recycler"),
    ("layout-cache-low", "Layout Cache Low"),
    ("bitmap-upload", "Bitmap Upload"),
]
KNOWN_INVALID_TESTS = {"bitmap-upload"}
EXPECTED_RUNS = 10
EXPECTED_RUN_COUNT = 4
EXPECTED_WARMUP = 1
APP_SHA256 = "e4e521a3a6fa5c6a754fac9d4dcd9a5238c3c915af1dd84f1f086b12887974af"


class IntegrityError(Exception):
    """Validity data exists but cannot be trusted."""


# ---------------------------------------------------------------------------
# Small helpers


def eprint(*args):
    print(*args, file=sys.stderr)


def read_json(path):
    last = None
    for _attempt in range(3):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            last = exc
            time.sleep(0.2)
    raise last


def rel(root, path):
    return os.path.relpath(path, root).replace(os.sep, "/")


def has_jankbench_runs(slug_dir, policies):
    for policy in policies:
        policy_dir = os.path.join(slug_dir, policy)
        if not os.path.isdir(policy_dir):
            continue
        for name in os.listdir(policy_dir):
            if name.startswith("run-") and os.path.isdir(
                    os.path.join(policy_dir, name)):
                return True
    return False


def mean(values):
    values = [v for v in values if v is not None and not _isnan(v)]
    return sum(values) / len(values) if values else None


def median(values):
    values = [v for v in values if v is not None and not _isnan(v)]
    return statistics.median(values) if values else None


def iqr(values):
    values = [v for v in values if v is not None and not _isnan(v)]
    if len(values) < 2:
        return None
    try:
        q1, _q2, q3 = statistics.quantiles(values, n=4, method="inclusive")
    except statistics.StatisticsError:
        return None
    return q3 - q1


def stdev(values):
    values = [v for v in values if v is not None and not _isnan(v)]
    if len(values) < 2:
        return None
    return statistics.stdev(values)


def _isnan(value):
    return isinstance(value, float) and math.isnan(value)


def as_float(value):
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) or math.isinf(value) else value


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_iso(value):
    if not value:
        return None
    try:
        stamp = _dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return stamp


def file_mtime(path):
    try:
        return _dt.datetime.fromtimestamp(os.path.getmtime(path),
                                          tz=_dt.timezone.utc)
    except OSError:
        return None


def file_sig(path):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def write_text(path, text):
    """Write UTF-8 text atomically, creating parent directories."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".full14-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def fmt_ms(value, digits=3):
    return "\u2014" if value is None else f"{value:.{digits}f}"


def fmt_jank(value):
    return "\u2014" if value is None else f"{value:.2f}"


def fmt_latex_ms(value, digits=3):
    return "--" if value is None else f"{value:.{digits}f}"


def fmt_latex_jank(value):
    return "--" if value is None else f"{value:.2f}"


def md_escape(value):
    return str(value).replace("|", r"\|")


def latex_escape(value):
    text = str(value)
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in text)


def unique(items):
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def compact_numbers(numbers):
    values = sorted({int(n) for n in numbers if n is not None})
    if not values:
        return "?"
    ranges = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(str(start) if start == previous
                      else f"{start}-{previous}")
        start = previous = value
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


# ---------------------------------------------------------------------------
# Campaign configuration


def load_benchmarks(path):
    try:
        data = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read benchmark catalogue {path}: {exc}")
    raw = data.get("benchmarks") or {}
    benchmarks = {}
    for key, spec in raw.items():
        bid = as_int(key)
        if bid is None:
            raise SystemExit(f"benchmark catalogue has non-integer id {key!r}")
        if not spec.get("slug") or not spec.get("name"):
            raise SystemExit(f"benchmark {key} is missing name/slug")
        benchmarks[bid] = dict(spec)
    if sorted(benchmarks) != list(range(8)):
        raise SystemExit(
            "benchmark catalogue must contain ids 0..7, got "
            + ",".join(str(i) for i in sorted(benchmarks)))
    slugs = [spec["slug"] for spec in benchmarks.values()]
    if len(set(slugs)) != len(slugs):
        raise SystemExit("benchmark catalogue has duplicate slugs")
    return benchmarks


def check_freeze(root, policies, runs, run_count, warmup):
    """Read FREEZE.json/policies.txt and return (freeze, warnings, errors)."""
    warnings = []
    errors = []
    freeze_path = os.path.join(root, "FREEZE.json")
    freeze = None
    if os.path.isfile(freeze_path):
        try:
            freeze = read_json(freeze_path)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"FREEZE.json is unreadable: {exc}")
        else:
            declared = freeze.get("policies")
            if not isinstance(declared, list):
                errors.append(
                    "FREEZE.json policies is not a list: "
                    f"{type(declared).__name__}")
            elif set(declared) != set(policies):
                errors.append(
                    "FREEZE.json policies "
                    f"{sorted(declared)} != expected {sorted(policies)}")
            for key, expected in (("runs", runs), ("run_count", run_count),
                                  ("warmup_iterations", warmup)):
                value = freeze.get(key)
                if value is not None and as_int(value) != expected:
                    errors.append(
                        f"FREEZE.json {key}={value!r} != expected {expected}")
            frozen_hashes = freeze.get("scheduler_sha256") or {}
            for policy in policies:
                if policy == "eevdf":
                    continue
                if policy not in frozen_hashes:
                    errors.append(
                        f"FREEZE.json scheduler_sha256 is missing {policy}")
    else:
        warnings.append(
            "FREEZE.json is missing; per-benchmark matrix.json files are "
            "checked only against their own contents")

    policies_path = os.path.join(root, "policies.txt")
    if os.path.isfile(policies_path):
        try:
            text = open(policies_path, encoding="utf-8").read().strip()
        except OSError as exc:
            errors.append(f"policies.txt is unreadable: {exc}")
        else:
            declared = [p.strip() for p in text.split(",") if p.strip()]
            if set(declared) != set(policies):
                errors.append(
                    f"policies.txt {sorted(declared)} != expected "
                    f"{sorted(policies)}")
    else:
        warnings.append("policies.txt is missing")

    return freeze, warnings, errors


def check_matrix(root, spec, freeze, policies, runs, run_count, warmup):
    """Validate jankbench/<slug>/matrix.json against the campaign freeze."""
    errors = []
    slug_dir = os.path.join(root, "jankbench", spec["slug"])
    matrix_path = os.path.join(slug_dir, "matrix.json")
    if not os.path.isfile(matrix_path):
        return [f"{rel(root, slug_dir)}: matrix.json is missing"]
    try:
        matrix = read_json(matrix_path)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{rel(root, matrix_path)}: unreadable ({exc})"]

    if as_int(matrix.get("benchmark_id")) != spec["benchmark_id"]:
        errors.append(
            f"{rel(root, matrix_path)}: benchmark_id "
            f"{matrix.get('benchmark_id')!r} != {spec['benchmark_id']}")
    if matrix.get("benchmark_slug") != spec["slug"]:
        errors.append(
            f"{rel(root, matrix_path)}: benchmark_slug "
            f"{matrix.get('benchmark_slug')!r} != {spec['slug']!r}")
    if matrix.get("benchmark_name") not in (None, spec["name"]):
        errors.append(
            f"{rel(root, matrix_path)}: benchmark_name "
            f"{matrix.get('benchmark_name')!r} != {spec['name']!r}")
    if matrix.get("policies") is not None and \
            set(matrix["policies"]) != set(policies):
        errors.append(
            f"{rel(root, matrix_path)}: policies "
            f"{sorted(matrix['policies'])} != expected {sorted(policies)}")
    if matrix.get("runs_expected") is not None and \
            as_int(matrix["runs_expected"]) != runs:
        errors.append(
            f"{rel(root, matrix_path)}: runs_expected "
            f"{matrix['runs_expected']!r} != {runs}")
    for key, expected in (("run_count", run_count),
                          ("warmup_iterations", warmup)):
        if matrix.get(key) is not None and as_int(matrix[key]) != expected:
            errors.append(
                f"{rel(root, matrix_path)}: {key}={matrix[key]!r} != "
                f"{expected}")

    if matrix.get("app_sha256") not in (None, APP_SHA256):
        errors.append(
            f"{rel(root, matrix_path)}: app_sha256 "
            f"{matrix.get('app_sha256')!r} != frozen {APP_SHA256}")

    app_hash_path = os.path.join(root, "jankbench_app_sha256.txt")
    if os.path.isfile(app_hash_path):
        try:
            recorded = open(app_hash_path, encoding="utf-8").read().strip()
        except OSError:
            recorded = ""
        if recorded and matrix.get("app_sha256") not in (None, recorded):
            errors.append(
                f"{rel(root, app_hash_path)}: {recorded} != matrix "
                f"app_sha256 {matrix.get('app_sha256')}")

    if freeze:
        frozen_hashes = freeze.get("scheduler_sha256") or {}
        matrix_hashes = matrix.get("scheduler_sha256") or {}
        for policy, expected in frozen_hashes.items():
            actual = matrix_hashes.get(policy)
            if actual is not None and actual != expected:
                errors.append(
                    f"{rel(root, matrix_path)}: scheduler_sha256[{policy}] "
                    f"{actual!r} != FREEZE {expected!r}")
    return errors


# ---------------------------------------------------------------------------
# Cell model


def test_definitions(benchmarks, require_known_invalid):
    tests = []
    for bid in sorted(benchmarks):
        spec = benchmarks[bid]
        tests.append({
            "key": f"jankbench:{bid}",
            "workload": "jankbench",
            "test": spec["name"],
            "test_label": spec["name"],
            "slug": spec["slug"],
            "benchmark_id": bid,
            "known_invalid": False,
            "required": True,
        })
    for test, label in UI_TESTS:
        known_invalid = test in KNOWN_INVALID_TESTS
        tests.append({
            "key": f"uibench:{test}",
            "workload": "uibench",
            "test": test,
            "test_label": label,
            "slug": None,
            "benchmark_id": None,
            "known_invalid": known_invalid,
            "required": (not known_invalid) or require_known_invalid,
        })
    return tests


def new_cell(test_def, policy, expected):
    return {
        "key": f"{test_def['key']}:{policy}",
        "workload": test_def["workload"],
        "test": test_def["test"],
        "test_label": test_def["test_label"],
        "slug": test_def["slug"],
        "benchmark_id": test_def["benchmark_id"],
        "policy": policy,
        "policy_label": POLICY_LABELS[policy],
        "required": test_def["required"],
        "known_invalid": test_def["known_invalid"],
        "expected_n": expected,
        "valid_n": 0,
        "strict_valid_n": 0,
        "invalid_n": 0,
        "missing_n": expected,
        "status": "incomplete",
        "metrics": None,
        "partial_metrics": None,
        "valid_runs": [],
        "excluded_runs": [],
        "missing_runs": list(range(1, expected + 1)),
        "unvalidated_runs": [],
        "reasons": [],
        "source": None,
    }


def add_reason(cell, reason):
    if reason and reason not in cell["reasons"]:
        cell["reasons"].append(reason)


def aggregate_cell(cell):
    """Aggregate only the valid runs/reps already stored in the cell."""
    runs = cell["valid_runs"]
    if not runs:
        return None
    frames = [as_float(r.get("frames")) for r in runs]
    frames = [f for f in frames if f is not None and f > 0]
    frames_total = int(sum(frames)) if frames else 0

    jank_frames = []
    for run in runs:
        value = as_float(run.get("jank_frames"))
        if value is not None and value >= 0:
            jank_frames.append(value)
        else:
            jank_pct = as_float(run.get("jank_pct"))
            run_frames = as_float(run.get("frames"))
            if jank_pct is not None and run_frames is not None:
                jank_frames.append(run_frames * jank_pct / 100.0)
    jank_frames_total = int(round(sum(jank_frames))) if jank_frames else None
    if frames_total and jank_frames_total is not None:
        jank_pct = 100.0 * jank_frames_total / frames_total
    else:
        weighted = [(as_float(r.get("frames")) or 0.0,
                     as_float(r.get("jank_pct"))) for r in runs]
        weighted = [(f, j) for f, j in weighted if f > 0 and j is not None]
        jank_pct = (sum(f * j for f, j in weighted) /
                    sum(f for f, _ in weighted)) if weighted else None

    def series(key):
        return [as_float(r.get(key)) for r in runs]

    mean_values = series("mean_ms")
    p50_values = series("p50_ms")
    p90_values = series("p90_ms")
    p95_values = series("p95_ms")
    p99_values = series("p99_ms")

    mean_pairs = []
    for run in runs:
        run_frames = as_float(run.get("frames"))
        run_mean = as_float(run.get("mean_ms"))
        if run_frames and run_frames > 0 and run_mean is not None:
            mean_pairs.append((run_frames, run_mean))
    if cell["workload"] == "jankbench" and mean_pairs:
        mean_ms = (sum(f * m for f, m in mean_pairs) /
                   sum(f for f, _ in mean_pairs))
        mean_aggregation = "frame-weighted across valid runs"
    else:
        mean_ms = mean(mean_values)
        mean_aggregation = "unweighted mean of valid runs"

    result = {
        "valid_n": len(runs),
        "frames_total": frames_total,
        "mean_ms": mean_ms,
        "mean_ms_aggregation": mean_aggregation,
        "mean_ms_median": median(mean_values),
        "mean_ms_iqr": iqr(mean_values),
        "mean_ms_sd": stdev(mean_values),
        "p50_ms": mean(p50_values),
        "p50_ms_median": median(p50_values),
        "p50_ms_iqr": iqr(p50_values),
        "p90_ms": mean(p90_values),
        "p95_ms": mean(p95_values),
        "p95_ms_median": median(p95_values),
        "p95_ms_iqr": iqr(p95_values),
        "p95_ms_sd": stdev(p95_values),
        "p99_ms": mean(p99_values),
        "p99_ms_median": median(p99_values),
        "p99_ms_iqr": iqr(p99_values),
        "p99_ms_sd": stdev(p99_values),
        "jank_pct": jank_pct,
        "jank_frames_total": jank_frames_total,
        "aggregation": (
            "mean is frame-weighted across valid runs; p50/p95/p99 are "
            "means of per-run summary percentiles; jank is frame-weighted; "
            "percentiles are not pooled from raw frames"),
    }
    if cell["workload"] == "uibench":
        result["mean_ms"] = None
        result["mean_ms_aggregation"] = "not reported by GfxMonitor"
        result["mean_ms_median"] = None
        result["mean_ms_iqr"] = None
        result["mean_ms_sd"] = None
        result["mean_ms_available"] = False
    else:
        result["mean_ms_available"] = True
    return result


def finalize_cell(cell, include_partial):
    """Set status and metrics after all valid/excluded records are known."""
    if cell["status"] == "error":
        cell["metrics"] = None
        cell["partial_metrics"] = None
        return
    if cell["valid_n"] >= cell["expected_n"]:
        cell["status"] = "pass"
    elif (cell["known_invalid"] and not cell["required"]
          and (cell["invalid_n"] > 0 or cell["valid_n"] > 0)):
        cell["status"] = "known-invalid"
    else:
        cell["status"] = "incomplete"

    if cell["status"] == "pass":
        cell["metrics"] = aggregate_cell(cell)
        cell["partial_metrics"] = None
        if cell["metrics"] is None:
            add_reason(cell, "valid runs are present but have no metrics")
            cell["status"] = "error"
    elif include_partial and cell["valid_runs"]:
        partial = aggregate_cell(cell)
        if partial:
            partial["partial"] = True
            partial["note"] = (
                "computed from valid runs only; the cell is incomplete and "
                "these values are not reportable")
            cell["partial_metrics"] = partial


# ---------------------------------------------------------------------------
# JankBench loader


def load_jankbench_slug(root, spec, policies, expected, freeze, args,
                        test_def):
    slug_dir = os.path.join(root, "jankbench", spec["slug"])
    source_rel = rel(root, os.path.join(slug_dir, "validity.json"))
    cells = OrderedDict((p, new_cell(test_def, p, expected))
                        for p in policies)
    warnings = []
    errors = []

    if not os.path.isdir(slug_dir):
        for cell in cells.values():
            cell["source"] = source_rel
            add_reason(cell, "benchmark directory missing (not started)")
            cell["status"] = "incomplete"
        return cells, warnings, errors, None

    errors.extend(check_matrix(root, spec, freeze, policies, expected,
                               args.run_count, args.warmup))
    if errors:
        for cell in cells.values():
            cell["source"] = source_rel
            cell["status"] = "error"
            add_reason(cell, "campaign freeze/matrix check failed")
        return cells, warnings, errors, None

    validity_path = os.path.join(slug_dir, "validity.json")
    if not os.path.isfile(validity_path):
        started = has_jankbench_runs(slug_dir, policies)
        reason = "validity.json missing"
        if started:
            reason += "; run directories exist but are not validated"
            if not (args.allow_incomplete or args.allow_stale_validity):
                errors.append(
                    f"{source_rel}: {reason}; rerun validate_jankbench.py")
        else:
            reason += " (benchmark not started)"
        for cell in cells.values():
            cell["source"] = source_rel
            add_reason(cell, reason)
            cell["status"] = "error" if (started and errors) else "incomplete"
        return cells, warnings, errors, None

    try:
        data = read_json(validity_path)
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"{source_rel}: unreadable validity.json ({exc})"
        errors.append(msg)
        for cell in cells.values():
            cell["source"] = source_rel
            cell["status"] = "error"
            add_reason(cell, msg)
        return cells, warnings, errors, None

    generated_at = parse_iso(data.get("generated_at"))
    if generated_at is None:
        generated_at = file_mtime(validity_path)
    bench = data.get("benchmark") or {}
    expectations = data.get("expectations") or {}
    summary = data.get("summary") or {}
    records = data.get("runs") or []

    structural = []
    if data.get("schema") != "scx_fcp.jankbench_validity.v1":
        structural.append(f"unexpected schema {data.get('schema')!r}")
    if as_int(bench.get("id")) != spec["benchmark_id"]:
        structural.append(
            f"benchmark.id {bench.get('id')!r} != {spec['benchmark_id']}")
    if bench.get("slug") != spec["slug"]:
        structural.append(
            f"benchmark.slug {bench.get('slug')!r} != {spec['slug']!r}")
    if bench.get("name") not in (None, spec["name"]):
        structural.append(
            f"benchmark.name {bench.get('name')!r} != {spec['name']!r}")
    recorded_root = data.get("root")
    if recorded_root and os.path.realpath(recorded_root) != os.path.realpath(slug_dir):
        structural.append(
            f"validity root {recorded_root!r} != {slug_dir!r}")
    if as_int(expectations.get("run_count")) != args.run_count:
        structural.append(
            f"expectations.run_count {expectations.get('run_count')!r} != "
            f"{args.run_count}")
    if as_int(expectations.get("benchmark_id")) != spec["benchmark_id"]:
        structural.append(
            f"expectations.benchmark_id "
            f"{expectations.get('benchmark_id')!r} != {spec['benchmark_id']}")
    # validate_jankbench.py keeps the whole policy catalogue in
    # expectations.policy_spec (six entries), so only require the four
    # campaign policies and check their hashes.
    policy_spec = expectations.get("policy_spec") or {}
    missing_spec = sorted(set(policies) - set(policy_spec))
    if missing_spec:
        structural.append(
            "expectations.policy_spec is missing campaign policies: "
            + ", ".join(missing_spec))
    if freeze:
        frozen_hashes = freeze.get("scheduler_sha256") or {}
        for policy, expected_hash in frozen_hashes.items():
            actual = (policy_spec.get(policy) or {}).get("sha256")
            if actual is not None and actual != expected_hash:
                structural.append(
                    f"policy_spec[{policy}].sha256 {actual!r} != FREEZE "
                    f"{expected_hash!r}")

    if structural:
        errors.extend(f"{source_rel}: {msg}" for msg in structural)
        for cell in cells.values():
            cell["source"] = source_rel
            cell["status"] = "error"
            add_reason(cell, "validity.json is inconsistent with the campaign")
        return cells, warnings, errors, {
            "validity": source_rel,
            "generated_at": data.get("generated_at"),
            "summary": summary,
        }

    by_policy = summary.get("by_policy") or {}
    if set(by_policy) != set(policies):
        msg = (f"summary.by_policy {sorted(by_policy)} != expected "
               f"{sorted(policies)}")
        errors.append(f"{source_rel}: {msg}")
        for cell in cells.values():
            cell["source"] = source_rel
            cell["status"] = "error"
            add_reason(cell, msg)
        return cells, warnings, errors, None

    # Expected run numbers are 1..expected.  The validator writes a record for
    # every expected number, present or not.
    records_by_policy = defaultdict(list)
    seen = set()
    for rec in records:
        if not isinstance(rec, dict):
            errors.append(f"{source_rel}: run record is not an object")
            continue
        policy = rec.get("policy")
        run = as_int(rec.get("run"))
        if policy not in cells or run is None:
            errors.append(
                f"{source_rel}: run record has unknown policy/run "
                f"{policy!r}/{rec.get('run')!r}")
            continue
        if (policy, run) in seen:
            errors.append(
                f"{source_rel}: duplicate run record {policy}/run-{run}")
            continue
        seen.add((policy, run))
        records_by_policy[policy].append(rec)

    if len(seen) < len(policies) * expected:
        errors.append(
            f"{source_rel}: {len(seen)} run records, expected at least "
            f"{len(policies) * expected}")

    total_valid = total_present = total_missing = 0
    for policy in policies:
        cell = cells[policy]
        cell["source"] = source_rel
        recs = sorted(records_by_policy.get(policy, []),
                      key=lambda r: as_int(r.get("run")) or 0)
        record_numbers = {as_int(r.get("run")) for r in recs}
        missing_records = sorted(set(range(1, expected + 1)) - record_numbers)
        if missing_records:
            msg = (f"{source_rel}: no run record for "
                   + ", ".join(f"{policy}/run-{n}" for n in missing_records))
            errors.append(msg)
            cell["status"] = "error"
            add_reason(cell, "validity.json is missing expected run records")
        present = [r for r in recs if r.get("present")]
        valid = [r for r in present if r.get("valid")]
        invalid = [r for r in present if not r.get("valid")]
        valid_numbers = {as_int(r.get("run")) for r in valid}
        present_numbers = {as_int(r.get("run")) for r in present}
        missing_numbers = sorted(set(range(1, expected + 1)) - present_numbers)

        policy_summary = by_policy.get(policy) or {}
        checks = []
        if as_int(policy_summary.get("valid_runs")) != len(valid):
            checks.append(
                f"summary.by_policy[{policy}].valid_runs="
                f"{policy_summary.get('valid_runs')!r} but {len(valid)} "
                "valid records")
        if as_int(policy_summary.get("discovered_runs")) != len(present):
            checks.append(
                f"summary.by_policy[{policy}].discovered_runs="
                f"{policy_summary.get('discovered_runs')!r} but {len(present)} "
                "present records")
        if as_int(policy_summary.get("missing_runs")) != len(missing_numbers):
            checks.append(
                f"summary.by_policy[{policy}].missing_runs="
                f"{policy_summary.get('missing_runs')!r} but "
                f"{len(missing_numbers)} missing run numbers")
        if checks:
            errors.extend(f"{source_rel}: {msg}" for msg in checks)
            cell["status"] = "error"
            add_reason(cell, "validity summary does not match run records")

        # Actual directories, used to detect a stale validity snapshot.
        policy_dir = os.path.join(slug_dir, policy)
        actual_dirs = set()
        complete_dirs = set()
        if os.path.isdir(policy_dir):
            for name in os.listdir(policy_dir):
                if not name.startswith("run-"):
                    continue
                run = as_int(name.split("-", 1)[1])
                path = os.path.join(policy_dir, name)
                if run is None or not os.path.isdir(path):
                    continue
                actual_dirs.add(run)
                if (os.path.isfile(os.path.join(path, "manifest.json"))
                        and os.path.isfile(os.path.join(path, "summary.json"))):
                    complete_dirs.add(run)

        missing_dirs = sorted(present_numbers - actual_dirs)
        if missing_dirs:
            msg = (f"{source_rel}: validity records runs that no longer exist: "
                   + ", ".join(f"{policy}/run-{n}" for n in missing_dirs))
            errors.append(msg)
            cell["status"] = "error"
            add_reason(cell, "validity references missing run directories")

        unvalidated = sorted(complete_dirs - present_numbers)
        if unvalidated:
            cell["unvalidated_runs"] = unvalidated
            msg = (f"{source_rel}: stale validity snapshot; complete run(s) "
                   f"not represented: "
                   + ", ".join(f"{policy}/run-{n}" for n in unvalidated))
            if args.allow_stale_validity or args.allow_incomplete:
                warnings.append(msg + " (excluded from aggregation)")
            else:
                errors.append(msg + "; rerun validate_jankbench.py")
                cell["status"] = "error"
                add_reason(cell, "validity snapshot is stale")

        if generated_at:
            for rec in valid:
                run = as_int(rec.get("run"))
                summary_path = os.path.join(policy_dir, f"run-{run}",
                                            "summary.json")
                mtime = file_mtime(summary_path)
                if mtime and mtime > generated_at + _dt.timedelta(seconds=2):
                    msg = (f"{source_rel}: {policy}/run-{run}/summary.json is "
                           "newer than validity.json")
                    if args.allow_stale_validity or args.allow_incomplete:
                        warnings.append(msg + " (using validated snapshot)")
                    else:
                        errors.append(msg + "; rerun validate_jankbench.py")
                        cell["status"] = "error"
                        add_reason(cell, "validity snapshot is stale")
                    break

        if len(valid) > expected:
            msg = (f"{source_rel}: {policy} has {len(valid)} valid runs, "
                   f"expected {expected}; refusing to pool extra runs")
            errors.append(msg)
            cell["status"] = "error"
            add_reason(cell, msg)

        cell["valid_n"] = len(valid)
        cell["strict_valid_n"] = sum(1 for r in valid if r.get("strict_valid"))
        cell["invalid_n"] = len(invalid)
        cell["missing_n"] = len(missing_numbers)
        cell["missing_runs"] = missing_numbers

        for rec in valid:
            run = as_int(rec.get("run"))
            if rec.get("reasons"):
                errors.append(
                    f"{source_rel}: {policy}/run-{run} is marked valid but "
                    f"has reasons: {'; '.join(rec['reasons'])}")
                cell["status"] = "error"
                add_reason(cell, f"run-{run} is marked valid but has reasons")
            metrics = rec.get("metrics") or {}
            required_metrics = ("frames", "mean_ms", "p50_ms", "p95_ms",
                                "p99_ms", "jank_pct")
            missing_metrics = [key for key in required_metrics
                               if as_float(metrics.get(key)) is None
                               and key != "frames"]
            if as_int(metrics.get("frames")) is None:
                missing_metrics.append("frames")
            if missing_metrics:
                errors.append(
                    f"{source_rel}: {policy}/run-{run} is valid but lacks "
                    f"metrics: {', '.join(missing_metrics)}")
                cell["status"] = "error"
                add_reason(cell, f"run-{run} has no usable metrics")
                continue

            # Cross-check the validator's metrics against the run summary.
            summary_path = os.path.join(policy_dir, f"run-{run}",
                                        "summary.json")
            try:
                run_summary = read_json(summary_path)
                overall = run_summary.get("overall") or {}
            except (OSError, json.JSONDecodeError):
                errors.append(
                    f"{source_rel}: {policy}/run-{run}/summary.json is "
                    "missing or unreadable")
                cell["status"] = "error"
                add_reason(cell, f"run-{run} summary is missing")
                continue
            mismatches = []
            for metric_key, summary_key in (
                    ("frames", "frames"), ("mean_ms", "mean"),
                    ("p50_ms", "p50"), ("p95_ms", "p95"),
                    ("p99_ms", "p99"), ("jank_pct", "jank_pct")):
                actual = as_float(metrics.get(metric_key))
                expected_value = as_float(overall.get(summary_key))
                if actual is None or expected_value is None:
                    continue
                if abs(actual - expected_value) > 1e-6:
                    mismatches.append(
                        f"{metric_key}={actual} != summary {expected_value}")
            if mismatches:
                errors.append(
                    f"{source_rel}: {policy}/run-{run} metrics disagree with "
                    f"summary.json: {'; '.join(mismatches)}")
                cell["status"] = "error"
                add_reason(cell, f"run-{run} metrics disagree with summary")

            cell["valid_runs"].append({
                "run": run,
                "frames": as_int(metrics.get("frames")),
                "mean_ms": as_float(metrics.get("mean_ms")),
                "p50_ms": as_float(metrics.get("p50_ms")),
                "p95_ms": as_float(metrics.get("p95_ms")),
                "p99_ms": as_float(metrics.get("p99_ms")),
                "jank_pct": as_float(metrics.get("jank_pct")),
                "jank_frames": as_int(metrics.get("jank_frames")),
            })

        for rec in invalid:
            run = as_int(rec.get("run"))
            reasons = rec.get("reasons") or []
            cell["excluded_runs"].append({
                "run": run,
                "reasons": reasons,
            })
        if invalid:
            first_reasons = invalid[0].get("reasons") or []
            add_reason(
                cell,
                f"{len(invalid)} invalid run(s) excluded"
                + (f" (first: {first_reasons[0]})" if first_reasons else ""))

        if missing_numbers:
            add_reason(
                cell,
                "missing run(s): " + ", ".join(str(n) for n in missing_numbers))
        if unvalidated:
            add_reason(
                cell,
                "unvalidated complete run(s) excluded: "
                + ", ".join(str(n) for n in unvalidated))

        total_valid += len(valid)
        total_present += len(present)
        total_missing += len(missing_numbers)

    expected_total = len(policies) * expected
    summary_checks = [
        ("expected_runs", as_int(summary.get("expected_runs")), expected_total),
        ("discovered_runs", as_int(summary.get("discovered_runs")),
         total_present),
        ("valid_runs_available_checks",
         as_int(summary.get("valid_runs_available_checks")), total_valid),
        ("invalid_runs", as_int(summary.get("invalid_runs")),
         total_present - total_valid),
        ("missing_runs", as_int(summary.get("missing_runs")), total_missing),
    ]
    for key, actual, wanted in summary_checks:
        if actual != wanted:
            errors.append(
                f"{source_rel}: summary.{key}={actual!r} but run records "
                f"imply {wanted}")

    source_info = {
        "validity": source_rel,
        "generated_at": data.get("generated_at") or (
            generated_at.isoformat() if generated_at else None),
        "summary": summary,
        "matrix_controls": data.get("matrix"),
    }
    return cells, warnings, errors, source_info


# ---------------------------------------------------------------------------
# UiBench loader


def walk_metrics(root):
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("_invalid", "_warmup")]
        if "metrics.json" in files:
            yield os.path.join(base, "metrics.json")


def load_uibench(root, policies, tests, expected, args, test_defs):
    ui_root = os.path.join(root, "uibench")
    source_rel = rel(root, os.path.join(ui_root, "validity.json"))
    cells = OrderedDict()
    for test_def in test_defs:
        for policy in policies:
            cells[(policy, test_def["test"])] = new_cell(
                test_def, policy, expected)
    warnings = []
    errors = []

    if not os.path.isdir(ui_root):
        for cell in cells.values():
            cell["source"] = source_rel
            add_reason(cell, "uibench directory missing (not started)")
            cell["status"] = "incomplete"
        return cells, warnings, errors, None

    validity_path = os.path.join(ui_root, "validity.json")
    if not os.path.isfile(validity_path):
        # The runner creates uibench/matrix.log before the first block.  Any
        # content means the phase started, even if no metrics.json exists yet.
        started = bool(os.listdir(ui_root))
        reason = "uibench/validity.json missing"
        if started:
            reason += "; block data exists but is not validated"
            if not (args.allow_incomplete or args.allow_stale_validity):
                errors.append(
                    f"{source_rel}: {reason}; rerun validate_uibench.py")
        else:
            reason += " (UiBench phase not started)"
        for cell in cells.values():
            cell["source"] = source_rel
            add_reason(cell, reason)
            cell["status"] = "error" if (started and errors) else "incomplete"
        return cells, warnings, errors, None

    try:
        data = read_json(validity_path)
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"{source_rel}: unreadable validity.json ({exc})"
        errors.append(msg)
        for cell in cells.values():
            cell["source"] = source_rel
            cell["status"] = "error"
            add_reason(cell, msg)
        return cells, warnings, errors, None

    generated_at = parse_iso(data.get("generated_at"))
    if generated_at is None:
        generated_at = file_mtime(validity_path)
    structural = []
    if data.get("schema") != "scx_fcp.uibench_validity.v1":
        structural.append(f"unexpected schema {data.get('schema')!r}")
    recorded_root = data.get("root")
    if recorded_root and os.path.realpath(recorded_root) != os.path.realpath(ui_root):
        structural.append(f"validity root {recorded_root!r} != {ui_root!r}")
    declared_policies = data.get("policies") or []
    if set(declared_policies) != set(policies):
        structural.append(
            f"policies {sorted(declared_policies)} != expected "
            f"{sorted(policies)}")
    declared_tests = data.get("tests") or []
    expected_test_names = {test for test, _label in tests}
    if set(declared_tests) != expected_test_names:
        structural.append(
            f"tests {sorted(declared_tests)} != expected "
            f"{sorted(expected_test_names)}")
    if as_int(data.get("reps_expected")) != expected:
        structural.append(
            f"reps_expected {data.get('reps_expected')!r} != {expected}")

    if structural:
        errors.extend(f"{source_rel}: {msg}" for msg in structural)
        for cell in cells.values():
            cell["source"] = source_rel
            cell["status"] = "error"
            add_reason(cell, "validity.json is inconsistent with the campaign")
        return cells, warnings, errors, {
            "validity": source_rel,
            "generated_at": data.get("generated_at"),
            "summary": data.get("summary"),
        }

    validity_cells = data.get("cells") or {}
    expected_keys = {f"{policy}|{test}" for policy, test in cells}
    actual_keys = set(validity_cells)
    extra = sorted(actual_keys - expected_keys)
    missing = sorted(expected_keys - actual_keys)
    if extra:
        errors.append(f"{source_rel}: unexpected cells: {', '.join(extra)}")
    if missing:
        warnings.append(
            f"{source_rel}: missing cells: {', '.join(missing)} "
            "(treated as incomplete)")

    all_metrics = {rel(ui_root, path) for path in walk_metrics(ui_root)}
    referenced = set()
    total_valid = total_invalid = total_missing = 0

    checked_metrics_paths = set()
    missing_metrics_paths = set()
    stale_metrics_paths = set()
    for (policy, test), cell in cells.items():
        cell["source"] = source_rel
        key = f"{policy}|{test}"
        raw = validity_cells.get(key)
        if raw is None:
            add_reason(cell, "cell absent from uibench validity.json")
            cell["status"] = "incomplete" if not errors else cell["status"]
            continue

        rows = raw.get("rows") or []
        valid_rows = [r for r in rows if r.get("valid")]
        invalid_rows = [r for r in rows if not r.get("valid")]
        reps = [as_int(r.get("rep")) for r in rows]
        seen = defaultdict(int)
        for rep in reps:
            if rep is not None:
                seen[rep] += 1
        duplicates = sorted(rep for rep, count in seen.items() if count > 1)
        missing_reps = sorted(set(range(1, expected + 1)) - set(seen))

        checks = []
        if as_int(raw.get("reps_valid")) != len(valid_rows):
            checks.append(
                f"reps_valid={raw.get('reps_valid')!r} but "
                f"{len(valid_rows)} valid rows")
        if as_int(raw.get("reps_invalid")) != len(invalid_rows):
            checks.append(
                f"reps_invalid={raw.get('reps_invalid')!r} but "
                f"{len(invalid_rows)} invalid rows")
        if as_int(raw.get("reps_missing")) != len(missing_reps):
            checks.append(
                f"reps_missing={raw.get('reps_missing')!r} but "
                f"{len(missing_reps)} missing reps")
        if raw.get("status") == "pass" and len(valid_rows) < expected:
            checks.append(
                f"status=pass but only {len(valid_rows)}/{expected} valid rows")
        if (raw.get("status") != "pass" and len(valid_rows) >= expected
                and not invalid_rows and not missing_reps and not duplicates):
            checks.append(
                f"status={raw.get('status')!r} but rows are complete")
        if checks:
            errors.extend(f"{source_rel}: {key}: {msg}" for msg in checks)
            cell["status"] = "error"
            add_reason(cell, "validity cell counts do not match rows")

        if len(valid_rows) > expected:
            msg = (f"{source_rel}: {key}: {len(valid_rows)} valid reps, "
                   f"expected {expected}; refusing to pool extra reps")
            errors.append(msg)
            cell["status"] = "error"
            add_reason(cell, msg)

        for row in rows:
            if not isinstance(row, dict):
                errors.append(
                    f"{source_rel}: {key}: row is not an object")
                cell["status"] = "error"
                add_reason(cell, "a row is not an object")
                continue
            rep = as_int(row.get("rep"))
            if row.get("test") not in (None, test):
                errors.append(
                    f"{source_rel}: {key}: row test {row.get('test')!r} "
                    f"!= {test!r}")
                cell["status"] = "error"
                add_reason(cell, "a row belongs to a different test")
            if rep is None:
                errors.append(
                    f"{source_rel}: {key}: row has no usable rep number")
                cell["status"] = "error"
                add_reason(cell, "a row has no usable rep number")
            raw_path = row.get("metrics_path")
            if not raw_path:
                continue
            metrics_path = str(raw_path).replace(os.sep, "/")
            if os.path.isabs(metrics_path):
                metrics_path = rel(ui_root, metrics_path)
            referenced.add(metrics_path)
            path = os.path.join(ui_root, metrics_path)
            if not os.path.isfile(path):
                cell["status"] = "error"
                add_reason(cell, "a row references a missing metrics file")
                if metrics_path not in missing_metrics_paths:
                    errors.append(
                        f"{source_rel}: {key}: metrics_path {metrics_path} "
                        "does not exist")
                    missing_metrics_paths.add(metrics_path)
                continue
            if metrics_path in stale_metrics_paths:
                if not (args.allow_stale_validity or args.allow_incomplete):
                    cell["status"] = "error"
                    add_reason(cell, "validity snapshot is stale")
                continue
            if metrics_path in checked_metrics_paths:
                continue
            checked_metrics_paths.add(metrics_path)
            mtime = file_mtime(path)
            if generated_at and mtime and \
                    mtime > generated_at + _dt.timedelta(seconds=2):
                stale_metrics_paths.add(metrics_path)
                msg = (f"{source_rel}: {metrics_path} is newer than "
                       "validity.json")
                if args.allow_stale_validity or args.allow_incomplete:
                    warnings.append(msg + " (using validated snapshot)")
                else:
                    errors.append(msg + "; rerun validate_uibench.py")
                    cell["status"] = "error"
                    add_reason(cell, "validity snapshot is stale")

        for row in valid_rows:
            rep = as_int(row.get("rep"))
            required_metrics = ("frames", "p50_ms", "p95_ms", "p99_ms",
                                "jank_pct")
            missing_metrics = [name for name in required_metrics
                               if as_float(row.get(name)) is None
                               and name != "frames"]
            if as_int(row.get("frames")) is None:
                missing_metrics.append("frames")
            if missing_metrics:
                errors.append(
                    f"{source_rel}: {key}: rep {rep} is valid but lacks "
                    f"metrics: {', '.join(missing_metrics)}")
                cell["status"] = "error"
                add_reason(cell, f"rep {rep} has no usable metrics")
                continue
            cell["valid_runs"].append({
                "rep": rep,
                "frames": as_int(row.get("frames")),
                "p50_ms": as_float(row.get("p50_ms")),
                "p90_ms": as_float(row.get("p90_ms")),
                "p95_ms": as_float(row.get("p95_ms")),
                "p99_ms": as_float(row.get("p99_ms")),
                "jank_pct": as_float(row.get("jank_pct")),
                "jank_frames": as_int(row.get("jank_frames")),
            })

        for row in invalid_rows:
            rep = as_int(row.get("rep"))
            reason = row.get("invalid_reason") or row.get("status") or ""
            cell["excluded_runs"].append({
                "rep": rep,
                "reasons": [reason] if reason else [],
            })
        if invalid_rows:
            first_reason = invalid_rows[0].get("invalid_reason") or \
                invalid_rows[0].get("status") or ""
            add_reason(
                cell,
                f"{len(invalid_rows)} invalid rep(s) excluded"
                + (f" (first: {first_reason})" if first_reason else ""))

        cell["valid_n"] = len(valid_rows)
        cell["strict_valid_n"] = len(valid_rows)
        cell["invalid_n"] = len(invalid_rows)
        cell["missing_n"] = len(missing_reps)
        cell["missing_runs"] = missing_reps
        if missing_reps:
            add_reason(
                cell,
                "missing rep(s): " + ", ".join(str(n) for n in missing_reps))
        if duplicates:
            errors.append(
                f"{source_rel}: {key}: duplicate reps "
                + ", ".join(str(n) for n in duplicates))
            cell["status"] = "error"
            add_reason(cell, "duplicate reps")

        total_valid += len(valid_rows)
        total_invalid += len(invalid_rows)
        total_missing += len(missing_reps)

    extra_metrics = sorted(all_metrics - referenced)
    missing_metrics = sorted(referenced - all_metrics)
    if missing_metrics:
        errors.append(
            f"{source_rel}: referenced metrics files are missing: "
            + ", ".join(missing_metrics[:10])
            + (" ..." if len(missing_metrics) > 10 else ""))
    if extra_metrics:
        msg = (f"{source_rel}: metrics.json files are not represented in "
               f"validity.json: {', '.join(extra_metrics[:10])}"
               + (" ..." if len(extra_metrics) > 10 else ""))
        if args.allow_stale_validity or args.allow_incomplete:
            warnings.append(msg + " (excluded from aggregation)")
        else:
            errors.append(msg + "; rerun validate_uibench.py")
            for cell in cells.values():
                if cell["status"] != "error":
                    cell["status"] = "error"
                    add_reason(cell, "validity snapshot is stale")

    summary = data.get("summary") or {}
    summary_checks = [
        ("cells_expected", as_int(summary.get("cells_expected")),
         len(policies) * len(tests)),
        ("cells_pass", as_int(summary.get("cells_pass")),
         sum(1 for c in validity_cells.values()
             if c.get("status") == "pass")),
        ("cells_fail", as_int(summary.get("cells_fail")),
         sum(1 for c in validity_cells.values()
             if c.get("status") != "pass")),
    ]
    for key, actual, wanted in summary_checks:
        if actual != wanted:
            errors.append(
                f"{source_rel}: summary.{key}={actual!r} but cells imply "
                f"{wanted}")

    source_info = {
        "validity": source_rel,
        "generated_at": data.get("generated_at") or (
            generated_at.isoformat() if generated_at else None),
        "summary": summary,
    }
    return cells, warnings, errors, source_info


# ---------------------------------------------------------------------------
# Report assembly


def build_report(args, benchmarks, test_defs, cells, sources, freeze,
                 warnings, errors):
    ordered_cells = []
    by_key = {cell["key"]: cell for cell in cells}
    for test_def in test_defs:
        for policy in POLICIES:
            key = f"{test_def['key']}:{policy}"
            if key not in by_key:
                raise IntegrityError(f"internal error: cell {key} missing")
            ordered_cells.append(by_key[key])

    required = [c for c in ordered_cells if c["required"]]
    required_complete = [c for c in required if c["status"] == "pass"]
    pass_cells = [c for c in ordered_cells if c["status"] == "pass"]
    known_invalid = [c for c in ordered_cells
                     if c["status"] == "known-invalid"]
    incomplete = [c for c in ordered_cells if c["status"] == "incomplete"]
    cell_errors = [c for c in ordered_cells if c["status"] == "error"]
    excluded = []
    excluded_required = 0
    excluded_known = 0
    for cell in ordered_cells:
        for run in cell["excluded_runs"]:
            excluded.append({
                "workload": cell["workload"],
                "test": cell["test"],
                "policy": cell["policy"],
                "run": run.get("run", run.get("rep")),
                "reasons": run.get("reasons") or [],
            })
            if cell["required"]:
                excluded_required += 1
            else:
                excluded_known += 1

    unverifiable = defaultdict(int)
    matrix_controls = {}
    for slug, info in (sources.get("jankbench") or {}).items():
        if not info:
            continue
        summary = info.get("summary") or {}
        for criterion, counts in (summary.get("criterion_status_counts") or {}).items():
            count = as_int(counts.get("unverifiable")) or 0
            if count:
                unverifiable[criterion] += count
        controls = info.get("matrix_controls") or {}
        matrix_controls[slug] = {
            name: {
                "status": value.get("status"),
                "detail": value.get("detail"),
            }
            for name, value in controls.items()
            if isinstance(value, dict) and value.get("status")
        }

    non_strict = [c for c in ordered_cells
                  if c["valid_n"] > c["strict_valid_n"]]
    if non_strict:
        warnings.append(
            f"{len(non_strict)} cell(s) contain valid JankBench runs that are "
            "not strict_valid; J10b_windows_cpu/M4 are unverifiable. The "
            "integration gates on evaluable checks and reports the gap in "
            "unverifiable_criteria and strict_valid_n.")
    if cell_errors and not errors:
        errors.append(
            "cells in error state: "
            + ", ".join(c["key"] for c in cell_errors))
    excluded_cells = {(item["workload"], item["test"], item["policy"])
                      for item in excluded}

    status = "integrity-error" if errors else (
        "complete" if not [c for c in required if c["status"] != "pass"]
        else "incomplete")

    report = {
        "schema": SCHEMA,
        "generated_at": _dt.datetime.now().astimezone().isoformat(
            timespec="seconds"),
        "root": os.path.abspath(args.root),
        "campaign": os.path.basename(os.path.abspath(args.root).rstrip(os.sep)),
        "policies": list(POLICIES),
        "policy_labels": dict(POLICY_LABELS),
        "tests": test_defs,
        "runs_expected": args.runs,
        "run_count": args.run_count,
        "warmup_iterations": args.warmup,
        "cells_expected": len(ordered_cells),
        "cells_required": len(required),
        "cells_required_complete": len(required_complete),
        "cells_required_incomplete": len(required) - len(required_complete),
        "cells_pass": len(pass_cells),
        "cells_incomplete": len(incomplete),
        "cells_known_invalid": len(known_invalid),
        "cells_error": len(cell_errors),
        "all_required_cells_complete": not errors and
        len(required_complete) == len(required),
        "all_14_tests_10_of_10": not errors and
        len(pass_cells) == len(ordered_cells),
        "complete": status == "complete",
        "status": status,
        "allow_incomplete": bool(args.allow_incomplete),
        "allow_stale_validity": bool(args.allow_stale_validity),
        "include_partial": bool(args.include_partial),
        "require_known_invalid": bool(args.require_known_invalid),
        "dry_run": bool(args.dry_run),
        "outputs_written": not bool(args.no_write),
        "known_invalid_tests": [t["key"] for t in test_defs
                                if t["known_invalid"]],
        "aggregation": {
            "jankbench": (
                "mean is frame-weighted across valid runs; p50/p95/p99 are "
                "means of the per-run summary percentiles; jank is "
                "frame-weighted across valid runs. Percentiles are not pooled "
                "from raw frames."),
            "uibench": (
                "mean of per-rep GfxMonitor percentile values across valid "
                "reps; jank is frame-weighted across valid reps. GfxMonitor "
                "does not report a mean frame time."),
            "validity_gate": (
                "a run/rep is included only when the validator marked it "
                "valid; invalid attempts are listed and excluded. J10b and "
                "M4 unverifiable criteria are reported, not silently passed."),
        },
        "freeze": freeze,
        "warnings": unique(warnings),
        "integrity_errors": unique(errors),
        "unverifiable_criteria": dict(sorted(unverifiable.items())),
        "matrix_controls": matrix_controls,
        "excluded_invalid_runs": excluded,
        "excluded_invalid_units": len(excluded),
        "excluded_invalid_cells": len(excluded_cells),
        "excluded_invalid_units_required": excluded_required,
        "excluded_invalid_units_known_invalid": excluded_known,
        "sources": sources,
        "cells": ordered_cells,
    }
    return report


# ---------------------------------------------------------------------------
# Renderers


def cell_display_metrics(cell, include_partial):
    if cell["metrics"]:
        return cell["metrics"], ""
    if include_partial and cell["partial_metrics"]:
        return cell["partial_metrics"], "\u2020"
    return None, ""


def markdown_report(report, include_partial):
    lines = []
    lines.append("# Full 14-test integration")
    lines.append("")
    lines.append(f"- Campaign: `{report['root']}`")
    lines.append(f"- Generated: `{report['generated_at']}`")
    lines.append(f"- Status: **{report['status'].upper()}**")
    lines.append(
        f"- Required cells: {report['cells_required_complete']}/"
        f"{report['cells_required']} complete")
    lines.append(
        f"- All cells (including known-invalid tests): "
        f"{report['cells_pass']}/{report['cells_expected']} pass")
    lines.append(
        f"- All 14 tests at {report['runs_expected']}/{report['runs_expected']}: "
        f"**{report['all_14_tests_10_of_10']}**")
    lines.append(f"- Integrity errors: {len(report['integrity_errors'])}")
    lines.append(
        f"- Excluded invalid runs/reps: "
        f"{report['excluded_invalid_units']} in "
        f"{report['excluded_invalid_cells']} cell(s)")
    if report["known_invalid_tests"]:
        lines.append(
            "- Known-invalid tests: `"
            + "`, `".join(report["known_invalid_tests"])
            + "` (frozen APK limitation; reported, not required)")
    if include_partial:
        lines.append("")
        lines.append(
            "> **Debug view.** `\u2020` marks an incomplete cell whose values "
            "are computed from valid runs/reps only. These values are not "
            "reportable and must not be pasted into the paper or website.")
    lines.append("")
    lines.append("## Cells")
    lines.append("")
    lines.append(
        "| Workload | Test | Policy | Valid n | Mean (ms) | p50 (ms) | "
        "p95 (ms) | p99 (ms) | Jank (%) | Status |")
    lines.append(
        "|---|---|---|---:|---:|---:|---:|---:|---:|---|")
    for cell in report["cells"]:
        metrics, marker = cell_display_metrics(cell, include_partial)
        n = f"{cell['valid_n']}/{cell['expected_n']}{marker}"
        if metrics:
            mean_s = fmt_ms(metrics.get("mean_ms"))
            p50_s = fmt_ms(metrics.get("p50_ms"))
            p95_s = fmt_ms(metrics.get("p95_ms"))
            p99_s = fmt_ms(metrics.get("p99_ms"))
            jank_s = fmt_jank(metrics.get("jank_pct"))
        else:
            mean_s = p50_s = p95_s = p99_s = jank_s = "\u2014"
        lines.append(
            f"| {md_escape(cell['workload'])} | {md_escape(cell['test_label'])} "
            f"| `{md_escape(cell['policy'])}` | {n} | {mean_s} | {p50_s} | "
            f"{p95_s} | {p99_s} | {jank_s} | {cell['status']} |")
    lines.append("")
    lines.append(
        "UiBench GfxMonitor reports percentiles, not a mean frame time, so "
        "the Mean column is `\u2014` for UiBench cells. The JankBench mean is "
        "frame-weighted across valid runs; p50, p95, and p99 are means of the "
        "per-run summary values; jank is frame-weighted. Percentiles are not "
        "pooled from raw frames.")
    lines.append("")

    if report["integrity_errors"]:
        lines.append("## Integrity errors")
        lines.append("")
        for error in report["integrity_errors"]:
            lines.append(f"- {error}")
        lines.append("")

    non_pass = [c for c in report["cells"] if c["status"] != "pass"]
    if non_pass:
        lines.append("## Non-passing cells")
        lines.append("")
        for cell in non_pass:
            reasons = "; ".join(cell["reasons"]) or "no detail"
            lines.append(
                f"- `{cell['workload']}/{cell['test']}/{cell['policy']}`: "
                f"**{cell['status']}** ({cell['valid_n']}/"
                f"{cell['expected_n']}) \u2014 {reasons}")
        lines.append("")

    if report["excluded_invalid_runs"]:
        lines.append("## Excluded invalid runs/reps")
        lines.append("")
        groups = OrderedDict()
        for item in report["excluded_invalid_runs"]:
            key = (item["workload"], item["test"], item["policy"])
            groups.setdefault(key, []).append(item)
        for (workload, test, policy), items in groups.items():
            units = compact_numbers([item["run"] for item in items])
            reasons = unique(
                reason for item in items for reason in item["reasons"]
                if reason)
            reason_text = "; ".join(reasons[:2]) or "no reason recorded"
            if len(reason_text) > 300:
                reason_text = reason_text[:297] + "..."
            lines.append(
                f"- `{workload}/{test}/{policy}`: {len(items)} invalid "
                f"unit(s) excluded ({units}) \u2014 {reason_text}")
        lines.append("")

    if report["warnings"]:
        lines.append("## Warnings")
        lines.append("")
        for warning in report["warnings"]:
            lines.append(f"- {warning}")
        lines.append("")

    lines.append("## Provenance")
    lines.append("")
    for slug, info in (report["sources"].get("jankbench") or {}).items():
        if info:
            lines.append(
                f"- JankBench `{slug}`: `{info['validity']}` "
                f"(generated `{info.get('generated_at')}`)")
    ui = report["sources"].get("uibench")
    if ui:
        lines.append(
            f"- UiBench: `{ui['validity']}` "
            f"(generated `{ui.get('generated_at')}`)")
    lines.append("")
    return "\n".join(lines)


def latex_status_comment(report):
    if report["status"] == "complete":
        return "% STATUS: COMPLETE (all required cells have the expected valid n)."
    return (
        "% STATUS: "
        f"{report['status'].upper()} "
        f"({report['cells_required_complete']}/{report['cells_required']} "
        "required cells complete). Do not use this fragment as a final table.")


def latex_table_jankbench(report, include_partial):
    tests = [t for t in report["tests"] if t["workload"] == "jankbench"]
    by_test = defaultdict(dict)
    for cell in report["cells"]:
        if cell["workload"] == "jankbench":
            by_test[cell["test"]][cell["policy"]] = cell
    lines = [
        latex_status_comment(report),
        f"% Source: {report['root']}",
        f"% Generated: {report['generated_at']}",
        r"\begin{table*}[t]",
        r"\centering",
        r"\small",
        r"\caption{Full AOSP \texttt{JankBench} matrix on the 16-vCPU "
        r"Cuttlefish device: 8 benchmarks $\times$ 4 policies, "
        f"{report['runs_expected']} valid runs per cell. The mean is "
        r"frame-weighted across valid runs; $p_{50}$, $p_{95}$, and $p_{99}$ "
        r"are means of the per-run summary percentiles; jank is "
        r"frame-weighted. $n$ is valid runs / expected. \texttt{--} marks an "
        r"incomplete cell.}",
        r"\label{tab:full14_jankbench}",
        r"\begin{tabular}{llrrrrrr}",
        r"\toprule",
        r"\textbf{Test} & \textbf{Policy} & \textbf{$n$} & "
        r"\textbf{Mean (ms)} & \textbf{$p_{50}$ (ms)} & "
        r"\textbf{$p_{95}$ (ms)} & \textbf{$p_{99}$ (ms)} & "
        r"\textbf{Jank (\%)} \\",
        r"\midrule",
    ]
    for index, test in enumerate(tests):
        if index:
            lines.append(r"\midrule")
        for policy in POLICIES:
            cell = by_test[test["test"]][policy]
            metrics, marker = cell_display_metrics(cell, include_partial)
            n = f"{cell['valid_n']}/{cell['expected_n']}"
            if marker:
                n += r"$^\dagger$"
            if metrics:
                mean_s = fmt_latex_ms(metrics.get("mean_ms"))
                p50_s = fmt_latex_ms(metrics.get("p50_ms"))
                p95_s = fmt_latex_ms(metrics.get("p95_ms"))
                p99_s = fmt_latex_ms(metrics.get("p99_ms"))
                jank_s = fmt_latex_jank(metrics.get("jank_pct"))
            else:
                mean_s = p50_s = p95_s = p99_s = jank_s = "--"
            lines.append(
                f"{latex_escape(test['test_label'])} & "
                f"{LATEX_POLICY_LABELS[policy]} & {n} "
                f"& {mean_s} & {p50_s} & {p95_s} & {p99_s} & {jank_s} \\\\")
    lines.extend([
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table*}",
        "",
    ])
    if include_partial:
        lines.insert(2, r"% \dagger marks a partial cell; do not report.")
    return "\n".join(lines)


def latex_table_uibench(report, include_partial):
    tests = [t for t in report["tests"] if t["workload"] == "uibench"]
    by_test = defaultdict(dict)
    for cell in report["cells"]:
        if cell["workload"] == "uibench":
            by_test[cell["test"]][cell["policy"]] = cell
    lines = [
        latex_status_comment(report),
        f"% Source: {report['root']}",
        f"% Generated: {report['generated_at']}",
        r"\begin{table*}[t]",
        r"\centering",
        r"\small",
        r"\caption{Full AOSP \texttt{UiBenchJankTests} matrix on the 16-vCPU "
        r"Cuttlefish device: 6 tests $\times$ 4 policies, "
        f"{report['runs_expected']} valid reps per cell. GfxMonitor reports "
        r"whole-millisecond percentiles and no mean frame time; $p_{50}$ is "
        r"the central metric. Jank is frame-weighted across valid reps. $n$ "
        r"is valid reps / expected. \texttt{--} marks an incomplete cell. "
        r"The \texttt{bitmap-upload} row is known-invalid with the frozen APK "
        r"and is not a scheduler result.}",
        r"\label{tab:full14_uibench}",
        r"\begin{tabular}{llrrrrrr}",
        r"\toprule",
        r"\textbf{Test} & \textbf{Policy} & \textbf{$n$} & "
        r"\textbf{$p_{50}$ (ms)} & \textbf{$p_{90}$ (ms)} & "
        r"\textbf{$p_{95}$ (ms)} & \textbf{$p_{99}$ (ms)} & "
        r"\textbf{Jank (\%)} \\",
        r"\midrule",
    ]
    for index, test in enumerate(tests):
        if index:
            lines.append(r"\midrule")
        for policy in POLICIES:
            cell = by_test[test["test"]][policy]
            metrics, marker = cell_display_metrics(cell, include_partial)
            n = f"{cell['valid_n']}/{cell['expected_n']}"
            if marker:
                n += r"$^\dagger$"
            if metrics:
                p50_s = fmt_latex_ms(metrics.get("p50_ms"))
                p90_s = fmt_latex_ms(metrics.get("p90_ms"))
                p95_s = fmt_latex_ms(metrics.get("p95_ms"))
                p99_s = fmt_latex_ms(metrics.get("p99_ms"))
                jank_s = fmt_latex_jank(metrics.get("jank_pct"))
            else:
                p50_s = p90_s = p95_s = p99_s = jank_s = "--"
            lines.append(
                f"{latex_escape(test['test_label'])} & "
                f"{LATEX_POLICY_LABELS[policy]} & {n} "
                f"& {p50_s} & {p90_s} & {p95_s} & {p99_s} & {jank_s} \\\\")
    lines.extend([
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table*}",
        "",
    ])
    if include_partial:
        lines.insert(2, r"% \dagger marks a partial cell; do not report.")
    return "\n".join(lines)


def html_report(report, include_partial):
    esc = html.escape
    lines = [
        f"<!-- Generated by scripts/integrate_full14.py from {esc(report['root'])} -->",
        f"<!-- Generated: {esc(report['generated_at'])}; status: {esc(report['status'])} -->",
        '<section class="section" id="full14-results">',
        '  <div class="container is-max-desktop">',
        '    <h2 class="title is-3">Full 14-test AOSP evaluation</h2>',
    ]
    status = report["status"]
    if status == "complete":
        status_text = (
            f"<strong>Complete.</strong> {report['cells_required_complete']}/"
            f"{report['cells_required']} required cells have "
            f"{report['runs_expected']} valid runs/reps. "
            f"{report['cells_pass']}/{report['cells_expected']} cells pass; "
            "the known-invalid test is reported separately.")
    elif status == "integrity-error":
        status_text = (
            f"<strong>Integrity error.</strong> {len(report['integrity_errors'])} "
            "validity problem(s) prevent a result. No affected cell is "
            "aggregated.")
    else:
        status_text = (
            f"<strong>Incomplete.</strong> {report['cells_required_complete']}/"
            f"{report['cells_required']} required cells complete; "
            f"{report['cells_pass']}/{report['cells_expected']} cells pass.")
    lines.append(f'    <p class="full14-status">{status_text}</p>')
    if include_partial:
        lines.append(
            '    <div class="notification is-warning is-light">'
            "Debug view: cells marked with &dagger; are incomplete and show "
            "values computed from valid runs/reps only. Do not cite these "
            "values.</div>")
    lines.append('    <h3 class="title is-5">p95 frame latency (ms) and jank (%)</h3>')
    lines.append('    <div class="table-container">')
    lines.append('      <table class="table is-fullwidth is-striped is-narrow">')
    lines.append('        <thead>')
    lines.append('          <tr>')
    lines.append('            <th>Test</th>')
    lines.append('            <th>Workload</th>')
    for policy in POLICIES:
        lines.append(
            f'            <th class="has-text-right">{esc(POLICY_LABELS[policy])}</th>')
    lines.append('          </tr>')
    lines.append('        </thead>')
    lines.append('        <tbody>')

    by_test = defaultdict(dict)
    for cell in report["cells"]:
        by_test[cell["test"]][cell["policy"]] = cell
    for test_def in report["tests"]:
        test = test_def["test"]
        lines.append('          <tr>')
        test_label = esc(test_def["test_label"])
        if test_def["known_invalid"]:
            test_label += (
                ' <span class="tag is-warning is-light">known invalid</span>')
        lines.append(f'            <td>{test_label}</td>')
        lines.append(f'            <td>{esc(test_def["workload"])}</td>')
        for policy in POLICIES:
            cell = by_test[test][policy]
            metrics, marker = cell_display_metrics(cell, include_partial)
            if metrics:
                value = (f"{fmt_ms(metrics.get('p95_ms'), 2)} "
                         f"({fmt_jank(metrics.get('jank_pct'))}%){marker}")
                title = (f"{cell['valid_n']}/{cell['expected_n']} valid; "
                         f"{cell['status']}")
                lines.append(
                    f'            <td class="has-text-right" title="{esc(title)}">'
                    f"{value}</td>")
            else:
                title = "; ".join(cell["reasons"]) or cell["status"]
                lines.append(
                    '            <td class="has-text-right has-text-grey" '
                    f'title="{esc(title)}">&mdash;</td>')
        lines.append('          </tr>')
    lines.extend([
        '        </tbody>',
        '      </table>',
        '    </div>',
        '    <p class="figure-caption">',
        "      p95 frame latency in milliseconds, with frame-weighted jank "
        "in parentheses. Each cell is the mean of the per-run/per-rep p95 "
        "values across valid units; the JankBench mean column is "
        "frame-weighted. UiBench GfxMonitor reports percentiles, not a mean "
        "frame time. An em dash marks an incomplete or known-invalid cell. "
        "Lower is better.",
        '    </p>',
    ])

    lines.append('    <details>')
    lines.append('      <summary>Per-cell metrics</summary>')
    lines.append('      <div class="table-container">')
    lines.append('        <table class="table is-fullwidth is-striped is-narrow">')
    lines.append('          <thead>')
    lines.append('            <tr>')
    for header in ("Workload", "Test", "Policy", "Valid n", "Mean (ms)",
                   "p50 (ms)", "p95 (ms)", "p99 (ms)", "Jank (%)", "Status"):
        align = (' class="has-text-right"' if header in (
            "Valid n", "Mean (ms)", "p50 (ms)", "p95 (ms)", "p99 (ms)",
            "Jank (%)") else "")
        lines.append(f'              <th{align}>{esc(header)}</th>')
    lines.append('            </tr>')
    lines.append('          </thead>')
    lines.append('          <tbody>')
    for cell in report["cells"]:
        metrics, marker = cell_display_metrics(cell, include_partial)
        n = f"{cell['valid_n']}/{cell['expected_n']}{marker}"
        if metrics:
            mean_s = fmt_ms(metrics.get("mean_ms"))
            p50_s = fmt_ms(metrics.get("p50_ms"))
            p95_s = fmt_ms(metrics.get("p95_ms"))
            p99_s = fmt_ms(metrics.get("p99_ms"))
            jank_s = fmt_jank(metrics.get("jank_pct"))
        else:
            mean_s = p50_s = p95_s = p99_s = jank_s = "&mdash;"
        lines.append(
            '            <tr>'
            f"<td>{esc(cell['workload'])}</td>"
            f"<td>{esc(cell['test_label'])}</td>"
            f"<td>{esc(POLICY_LABELS[cell['policy']])}</td>"
            f'<td class="has-text-right">{n}</td>'
            f'<td class="has-text-right">{mean_s}</td>'
            f'<td class="has-text-right">{p50_s}</td>'
            f'<td class="has-text-right">{p95_s}</td>'
            f'<td class="has-text-right">{p99_s}</td>'
            f'<td class="has-text-right">{jank_s}</td>'
            f"<td>{esc(cell['status'])}</td>"
            "</tr>")
    lines.extend([
        '          </tbody>',
        '        </table>',
        '      </div>',
        '    </details>',
    ])
    if report["integrity_errors"]:
        lines.append('    <div class="notification is-danger is-light">')
        lines.append('      <strong>Integrity errors</strong>')
        lines.append('      <ul>')
        for error in report["integrity_errors"]:
            lines.append(f'        <li>{esc(error)}</li>')
        lines.append('      </ul>')
        lines.append('    </div>')
    lines.append(
        f'    <p class="is-size-7 has-text-grey">Generated from '
        f"<code>{esc(report['root'])}</code> at {esc(report['generated_at'])}. "
        f"Valid runs/reps per cell: {report['runs_expected']}. "
        f"Excluded invalid attempts: {len(report['excluded_invalid_runs'])}. "
        "The frozen UiBench APK cannot run bitmap-upload; that test is "
        "reported as known-invalid, not as a scheduler result.</p>")
    lines.extend([
        '  </div>',
        '</section>',
        '',
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Optional validator refresh


def run_validators(root, benchmarks, policies, args):
    """Rerun the validators in place.  This writes validity.json files."""
    refreshed = 0
    for bid in sorted(benchmarks):
        spec = benchmarks[bid]
        slug_dir = os.path.join(root, "jankbench", spec["slug"])
        if not os.path.isdir(slug_dir):
            continue
        has_runs = has_jankbench_runs(slug_dir, policies)
        if not has_runs:
            continue
        cmd = [
            sys.executable, VALIDATE_JANKBENCH, slug_dir,
            "--expected-runs", str(args.runs),
            "--run-count", str(args.run_count),
            "--benchmark-id", str(bid),
            "--policies", ",".join(policies),
            "--quiet",
        ]
        if os.path.abspath(args.benchmarks_config) != os.path.abspath(
                DEFAULT_BENCHMARKS):
            cmd.extend(["--benchmarks-config", args.benchmarks_config])
        validity_path = os.path.join(slug_dir, "validity.json")
        before = file_sig(validity_path)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode not in (0, 1):
            raise IntegrityError(
                f"validate_jankbench.py failed for {spec['slug']} "
                f"(rc={result.returncode}): "
                f"{(result.stderr or result.stdout).strip()[:1000]}")
        after = file_sig(validity_path)
        if after is None or after == before:
            raise IntegrityError(
                f"validate_jankbench.py did not update {validity_path}; "
                "check write access (the validator falls back to "
                "plan/51_validity_output when the result root is read-only)")
        refreshed += 1
        eprint(f"refreshed {rel(root, validity_path)}")

    ui_root = os.path.join(root, "uibench")
    if os.path.isdir(ui_root) and os.listdir(ui_root):
        cmd = [
            sys.executable, VALIDATE_UIBENCH, ui_root,
            "--policies", ",".join(policies),
            "--tests", ",".join(test for test, _label in UI_TESTS),
            "--reps", str(args.runs),
        ]
        validity_path = os.path.join(ui_root, "validity.json")
        before = file_sig(validity_path)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode not in (0, 1):
            raise IntegrityError(
                "validate_uibench.py failed "
                f"(rc={result.returncode}): "
                f"{(result.stderr or result.stdout).strip()[:1000]}")
        after = file_sig(validity_path)
        if after is None or after == before:
            raise IntegrityError(
                f"validate_uibench.py did not update {validity_path}; "
                "check write access to the UiBench root")
        refreshed += 1
        eprint(f"refreshed {rel(root, validity_path)}")
    return refreshed


# ---------------------------------------------------------------------------
# CLI


def default_out_dir(root, dry_run):
    name = os.path.basename(os.path.abspath(root).rstrip(os.sep)) or "root"
    if dry_run:
        return os.path.join(PAPER, "tmp", "integration", name, "dryrun")
    candidate = os.path.join(root, "integration")
    if os.access(root, os.W_OK):
        return candidate
    return os.path.join(PAPER, "tmp", "integration", name, "post")


def is_within(path, root):
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", help="campaign root")
    parser.add_argument("--out-dir", default=None,
                        help="output directory (default: <root>/integration, "
                             "or <paper>/tmp/integration/<campaign>/post "
                             "when the root is not writable; with --dry-run: "
                             "<paper>/tmp/integration/<campaign>/dryrun)")
    parser.add_argument("--runs", type=int, default=EXPECTED_RUNS,
                        help="expected valid runs/reps per cell (default 10)")
    parser.add_argument("--run-count", type=int, default=EXPECTED_RUN_COUNT,
                        help="JankBench RUN_COUNT (default 4)")
    parser.add_argument("--warmup", type=int, default=EXPECTED_WARMUP,
                        help="JankBench WARMUP_ITERATIONS (default 1)")
    parser.add_argument("--allow-incomplete", action="store_true",
                        help="exit 0 for incomplete cells (dry runs); does "
                             "not suppress integrity errors")
    parser.add_argument("--allow-stale-validity", action="store_true",
                        help="use the last validated snapshot when complete "
                             "run dirs are newer than validity.json")
    parser.add_argument("--include-partial", action="store_true",
                        help="debug only: show partial values for incomplete "
                             "cells, computed from valid runs/reps")
    parser.add_argument("--require-known-invalid", action="store_true",
                        help="require UiBench bitmap-upload too (for a "
                             "rebuilt APK)")
    parser.add_argument("--refresh-validity", action="store_true",
                        help="rerun validate_jankbench.py/validate_uibench.py "
                             "in place before integrating; writes into the "
                             "campaign root")
    parser.add_argument("--dry-run", action="store_true",
                        help="never write inside the campaign root; outputs "
                             "go to the dry-run directory")
    parser.add_argument("--no-write", action="store_true",
                        help="print the summary but write no output files")
    parser.add_argument("--benchmarks-config", default=DEFAULT_BENCHMARKS,
                        help="JankBench catalogue JSON")
    parser.add_argument("--verbose", action="store_true",
                        help="print every non-passing cell")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be positive")
    if args.dry_run and args.refresh_validity:
        parser.error("--dry-run and --refresh-validity are incompatible")
    if args.dry_run and args.out_dir and is_within(args.out_dir, args.root):
        parser.error("--dry-run refuses an --out-dir inside the campaign root")
    return args


def main(argv=None):
    args = parse_args(argv)
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        eprint(f"error: campaign root is not a directory: {root}")
        return 2

    try:
        benchmarks = load_benchmarks(args.benchmarks_config)
        test_defs = test_definitions(benchmarks, args.require_known_invalid)

        if args.refresh_validity:
            refreshed = run_validators(root, benchmarks, POLICIES, args)
            eprint(f"refreshed {refreshed} validity report(s)")

        freeze, warnings, errors = check_freeze(
            root, POLICIES, args.runs, args.run_count, args.warmup)

        sources = {"jankbench": {}, "uibench": None}
        cells = []
        for bid in sorted(benchmarks):
            spec = dict(benchmarks[bid])
            spec["benchmark_id"] = bid
            test_def = next(t for t in test_defs
                            if t["key"] == f"jankbench:{bid}")
            slug_cells, slug_warnings, slug_errors, source = \
                load_jankbench_slug(root, spec, POLICIES, args.runs, freeze,
                                    args, test_def)
            cells.extend(slug_cells.values())
            warnings.extend(slug_warnings)
            errors.extend(slug_errors)
            sources["jankbench"][spec["slug"]] = source

        ui_test_defs = [t for t in test_defs if t["workload"] == "uibench"]
        ui_cells, ui_warnings, ui_errors, ui_source = load_uibench(
            root, POLICIES, UI_TESTS, args.runs, args, ui_test_defs)
        cells.extend(ui_cells.values())
        warnings.extend(ui_warnings)
        errors.extend(ui_errors)
        sources["uibench"] = ui_source

        for cell in cells:
            finalize_cell(cell, args.include_partial)

        report = build_report(args, benchmarks, test_defs, cells, sources,
                              freeze, warnings, errors)

        out_dir = os.path.abspath(args.out_dir or default_out_dir(
            root, args.dry_run))
        if args.dry_run and is_within(out_dir, root):
            raise IntegrityError(
                f"--dry-run refuses to write inside the campaign root: {out_dir}")
        if (not args.dry_run and not args.out_dir and not args.no_write
                and not is_within(out_dir, root)):
            eprint(f"note: campaign root is not writable; writing to {out_dir}")
        outputs = {
            "json": os.path.join(out_dir, "full14_summary.json"),
            "markdown": os.path.join(out_dir, "full14_summary.md"),
            "latex_jankbench": os.path.join(out_dir, "full14_jankbench.tex"),
            "latex_uibench": os.path.join(out_dir, "full14_uibench.tex"),
            "html": os.path.join(out_dir, "full14_website.html"),
        }
        report["outputs"] = outputs

        if not args.no_write:
            write_text(outputs["markdown"],
                       markdown_report(report, args.include_partial))
            write_text(outputs["latex_jankbench"],
                       latex_table_jankbench(report, args.include_partial))
            write_text(outputs["latex_uibench"],
                       latex_table_uibench(report, args.include_partial))
            write_text(outputs["html"],
                       html_report(report, args.include_partial))
            write_text(outputs["json"],
                       json.dumps(report, indent=2, ensure_ascii=False,
                                  allow_nan=False) + "\n")
    except IntegrityError as exc:
        eprint(f"error: {exc}")
        return 2

    print(f"campaign: {report['root']}")
    print(f"status: {report['status']}")
    print(f"required cells: {report['cells_required_complete']}/"
          f"{report['cells_required']} complete")
    print(f"all cells: {report['cells_pass']}/{report['cells_expected']} pass")
    print(f"all 14 tests at {report['runs_expected']}/"
          f"{report['runs_expected']}: {report['all_14_tests_10_of_10']}")
    print(f"integrity errors: {len(report['integrity_errors'])}")
    print(f"excluded invalid runs/reps: "
          f"{report['excluded_invalid_units']} in "
          f"{report['excluded_invalid_cells']} cell(s) "
          f"(required={report['excluded_invalid_units_required']}, "
          f"known-invalid={report['excluded_invalid_units_known_invalid']})")
    if report["warnings"]:
        print(f"warnings: {len(report['warnings'])}")
        for warning in report["warnings"]:
            print(f"  - {warning}")
    non_pass = [c for c in report["cells"] if c["status"] != "pass"]
    if non_pass:
        print(f"non-passing cells: {len(non_pass)}")
        limit = len(non_pass) if args.verbose else min(10, len(non_pass))
        for cell in non_pass[:limit]:
            reasons = "; ".join(cell["reasons"]) or "no detail"
            print(f"  - {cell['workload']}/{cell['test']}/{cell['policy']}: "
                  f"{cell['status']} ({cell['valid_n']}/{cell['expected_n']}) "
                  f"- {reasons}")
        if limit < len(non_pass):
            print(f"  ... and {len(non_pass) - limit} more (see JSON/Markdown)")
    if report["integrity_errors"]:
        print("integrity errors:")
        for error in report["integrity_errors"]:
            print(f"  - {error}")
    if not args.no_write:
        print("outputs:")
        for name, path in report["outputs"].items():
            print(f"  {name}: {path}")

    if report["integrity_errors"]:
        return 2
    if not report["complete"] and not args.allow_incomplete:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
