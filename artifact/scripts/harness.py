#!/usr/bin/env python3
"""Clean, deterministic benchmark harness for scx_fcp on Cuttlefish and ARM64 SoCs.

Eliminates legacy shell/heredoc duplication and guarantees:
1. Host crosvm_vcpu SCHED_DEADLINE (dl) vs SCHED_OTHER (sym) verification before & after every run.
2. Non-blocking daemon launch & exact pidof cleanup (immune to 15-char TASK_COMM_LEN truncation).
3. One-way sched_proxy_exec sysfs ordering (all proxy=0 policies interleaved first, proxy=1 last,
   automatic fast reboot + vCPU re-shaping whenever resetting proxy=1 -> 0).
4. Pure background CPU contention (--bg-only) during macro, overhead, and ablation suites.
5. Strict separation of timing runs (bpf_stats_enabled=0) and eBPF overhead runs (bpf_stats_enabled=1).
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ARTIFACT_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = ARTIFACT_DIR.parent
LAUNCHER_CF = ARTIFACT_DIR / "scripts" / "launch_cuttlefish.sh"
LAUNCHER_DEV = ARTIFACT_DIR / "scripts" / "launch_device.sh"
STRUCT_OPS_PROG_TYPE = 27

JANKBENCH_NAMES = (
    "List View Fling",
    "Image List View Fling",
    "Shadow Grid Fling",
    "High-hitrate text render",
    "Low-hitrate text render",
    "Edit Text Input",
    "Overdraw Test",
    "Bitmap Upload Test",
)

UIBENCH_CLASSES = (
    "com.android.uibench.janktests.UiBenchRenderingJankTests#testBitmapUploadJank",
    "com.android.uibench.janktests.UiBenchRenderingJankTests#testShadowGridListFling",
    "com.android.uibench.janktests.UiBenchJankTests#testInflatingListViewFling",
    "com.android.uibench.janktests.UiBenchJankTests#testSlowBindRecyclerViewFling",
    "com.android.uibench.janktests.UiBenchJankTests#testTrivialRecyclerListViewFling",
    "com.android.uibench.janktests.UiBenchTextJankTests#testLayoutCacheLowHitrateFling",
)
UIBENCH_TESTS = tuple(c.split("#", 1)[1] for c in UIBENCH_CLASSES)

ALL_DAEMON_BINS = (
    "scx_fcp",
    "scx_fcp_behavioral",
    "scx_fcp_notoken",
    "scx_fcp_nofanout",
    "scx_fcp_named",
    "scx_lavd",
    "scx_p2dq",
    "scx_bpfland",
    "fcp_frame_bench",
)

POLICY_SPECS: dict[str, dict[str, object]] = {
    "eevdf": {"bin": None, "args": [], "ops": None, "proxy": 0, "needs_big_cpus": False},
    "eevdf_proxy_exec": {"bin": None, "args": [], "ops": None, "proxy": 1, "needs_big_cpus": False},
    "scx_p2dq": {"bin": "scx_p2dq", "args": ["--enable-pelt", "false"], "ops": "p2dq", "proxy": 0, "needs_big_cpus": False},
    "scx_p2dq_pelt_off": {"bin": "scx_p2dq", "args": ["--enable-pelt", "false"], "ops": "p2dq", "proxy": 0, "needs_big_cpus": False},
    "scx_lavd": {"bin": "scx_lavd", "args": [], "ops": "lavd", "proxy": 0, "needs_big_cpus": False},
    "scx_fcp_behavioral": {"bin": "scx_fcp_behavioral", "args": [], "ops": "fcp_zh", "proxy": 0, "needs_big_cpus": True},
    "scx_fcp": {"bin": "scx_fcp", "args": [], "ops": "fcp", "proxy": 0, "needs_big_cpus": True},
    "scx_fcp_notoken": {"bin": "scx_fcp_notoken", "args": [], "ops": "fcp_nt", "proxy": 0, "needs_big_cpus": True},
    "scx_fcp_nofanout": {"bin": "scx_fcp_nofanout", "args": [], "ops": "fcp_nf", "proxy": 0, "needs_big_cpus": True},
    "scx_fcp_named": {"bin": "scx_fcp_named", "args": [], "ops": "fcp_nh", "proxy": 0, "needs_big_cpus": True},
}


class HarnessError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_policy(name: str) -> str:
    return "scx_p2dq" if name == "scx_p2dq_pelt_off" else name


def ops_matches(expected: str | None, actual: str) -> bool:
    if expected is None:
        return actual in ("", "none")
    if expected == "fcp":
        return actual == "fcp" or (
            actual.startswith("fcp_")
            and not actual.startswith(("fcp_zh", "fcp_nh", "fcp_nt", "fcp_nf", "fcp_or"))
        )
    return actual == expected or actual.startswith(expected + "_")


class TargetEnv:
    def __init__(self, log_path: Path):
        self.platform = os.environ.get("PLATFORM", "x86_64")
        self.adb_bin = os.environ.get("ADB", "adb")
        self.adb_port = str(os.environ.get("ANDROID_ADB_SERVER_PORT", "5038" if self.platform == "x86_64" else "5037"))
        self.serial = os.environ.get("SERIAL", "127.0.0.1:6520" if self.platform == "x86_64" else "")
        self.bin_dir = Path(os.environ.get("BIN_DIR", str(ARTIFACT_DIR / "build" / "bin" / self.platform)))
        self.workloads_dir = Path(os.environ.get("WORKLOADS_DIR", str(ARTIFACT_DIR / "workloads" / self.platform)))
        self.remote_dir = os.environ.get("REMOTE_DIR", "/data/local/tmp/scx")
        self.cf_home = Path(os.environ.get("CF_HOME", str(Path.home() / "workspace" / "aosp_research" / "cf")))
        self.dl_runtime_ns = int(os.environ.get("DL_RUNTIME_NS", "892000"))
        self.dl_period_ns = int(os.environ.get("DL_PERIOD_NS", "2000000"))
        self.log_path = log_path
        self.use_su = False

    def log(self, msg: str) -> None:
        line = f"[{utc_now()}] {msg}"
        print(line, flush=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def adb_cmd(self, *args: str) -> list[str]:
        return [self.adb_bin, "-P", self.adb_port, "-s", self.serial, *args]

    def adb(self, *args: str, timeout: int = 120, check: bool = True) -> str:
        proc = subprocess.run(
            self.adb_cmd(*args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise HarnessError(f"adb {' '.join(args)} failed (rc={proc.returncode}): {detail}")
        return proc.stdout.replace("\r", "")

    def _wrap_root(self, script: str) -> str:
        if not self.use_su:
            return script
        prefixed = "export PATH=/product/bin:/apex/com.android.runtime/bin:/apex/com.android.art/bin:/system_ext/bin:/system/bin:/system/xbin:/odm/bin:/vendor/bin:/vendor/xbin:$PATH; " + script
        return f"su 0 sh -c {shlex.quote(prefixed)}"

    def shell(self, script: str, timeout: int = 120, check: bool = True) -> str:
        return self.adb("shell", self._wrap_root(script), timeout=timeout, check=check)

    def _maybe_connect_tcp(self) -> None:
        if ":" in self.serial:
            subprocess.run(
                [self.adb_bin, "-P", self.adb_port, "connect", self.serial],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=15,
            )

    def ensure_root_and_ready(self) -> None:
        self._maybe_connect_tcp()
        state_now = self.adb("get-state", timeout=15, check=False).strip()
        if state_now == "device":
            raw_uid = self.adb("shell", "id -u", timeout=15, check=False).strip()
            if raw_uid == "0":
                self.use_su = False
            else:
                su_uid = self.adb("shell", "su 0 sh -c 'id -u'", timeout=15, check=False).strip()
                if su_uid == "0":
                    self.use_su = True
                else:
                    subprocess.run(self.adb_cmd("root"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=30)
        else:
            if self.platform == "x86_64":
                subprocess.run(self.adb_cmd("root"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=30)

        deadline = time.time() + 180
        while time.time() < deadline:
            self._maybe_connect_tcp()
            state = self.adb("get-state", timeout=15, check=False).strip()
            if state == "device":
                if not self.use_su:
                    raw_uid = self.adb("shell", "id -u", timeout=15, check=False).strip()
                    if raw_uid != "0":
                        su_uid = self.adb("shell", "su 0 sh -c 'id -u'", timeout=15, check=False).strip()
                        if su_uid == "0":
                            self.use_su = True
                boot = self.shell("getprop sys.boot_completed", timeout=15, check=False).strip()
                act = self.shell("service check activity", timeout=15, check=False)
                ss = self.shell("pidof system_server", timeout=15, check=False).strip()
                sui = self.shell("pidof com.android.systemui", timeout=15, check=False).strip()
                if boot == "1" and "found" in act and ss and sui:
                    break
            time.sleep(2)
        else:
            raise HarnessError(f"device {self.serial} did not reach boot_completed + framework ready")

        uid = self.shell("id -u", timeout=15).strip()
        if uid != "0":
            raise HarnessError(f"adb shell is not root (uid={uid})")

        home_cmd = "input keyevent KEYCODE_HOME >/dev/null 2>&1 || true" if self.platform == "x86_64" else "true"
        self.shell(
            "setenforce 0; "
            "setprop debug.hwui.renderer opengl; "
            "settings put global window_animation_scale 0; "
            "settings put global transition_animation_scale 0; "
            "settings put global animator_duration_scale 1; "
            "svc power stayon true; "
            "settings put global stay_on_while_plugged_in 7; "
            "settings put system screen_off_timeout 2147483647; "
            "input keyevent KEYCODE_WAKEUP >/dev/null 2>&1 || true; "
            f"wm dismiss-keyguard >/dev/null 2>&1 || true; {home_cmd}",
            timeout=30,
        )
        if self.platform == "x86_64":
            settle_dl = time.time() + 45
            stable = 0
            while time.time() < settle_dl:
                act_dump = self.shell(
                    "dumpsys activity activities | grep -E 'KeyguardShowing=|topResumedActivity=' || true",
                    timeout=15, check=False,
                )
                if "KeyguardShowing=false" in act_dump and "launcher" in act_dump.lower():
                    stable += 1
                    if stable >= 2:
                        break
                else:
                    stable = 0
                    self.shell("wm dismiss-keyguard >/dev/null 2>&1 || true; input keyevent KEYCODE_HOME >/dev/null 2>&1 || true", timeout=10, check=False)
                time.sleep(2)
    def find_crosvm_pid(self, expected_cpus: int | None = None) -> int:
        for cmdf in glob.glob("/proc/[0-9]*/cmdline"):
            try:
                raw = Path(cmdf).read_bytes().decode("utf-8", "ignore").replace("\x00", " ")
            except OSError:
                continue
            if "/crosvm " in raw and "--cpus=" in raw and "process_restarter" not in raw:
                if expected_cpus is not None and f"--cpus={expected_cpus}" not in raw:
                    continue
                return int(cmdf.split("/")[2])
        raise HarnessError(f"active crosvm instance (cpus={expected_cpus}) not found")

    def get_host_vcpu_threads(self, crosvm_pid: int) -> dict[int, tuple[int, str, str]]:
        proc = subprocess.run(
            ["/bin/ps", "-L", "-p", str(crosvm_pid), "-o", "tid=,ni=,cls=,comm="],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        vcpus: dict[int, tuple[int, str, str]] = {}
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) != 4:
                continue
            tid_s, ni_s, cls_s, comm = parts
            m = re.fullmatch(r"crosvm_vcpu(\d+)", comm)
            if m:
                vcpus[int(m.group(1))] = (int(tid_s), ni_s, cls_s)
        return vcpus

    def _run_priv(self, cmd: list[str]) -> None:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if r.returncode != 0:
            r2 = subprocess.run(["sudo", "-n", *cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            if r2.returncode != 0:
                raise HarnessError(f"privileged command failed ({' '.join(cmd)}): {r2.stderr.strip()}")

    def ensure_host_vcpu_topology(self, cpus: int, topo: str, apply_if_needed: bool = True) -> dict[str, object]:
        if self.platform != "x86_64":
            return {"platform": self.platform, "topology": "hardware"}
        if topo not in ("sym", "dl"):
            raise HarnessError(f"Cuttlefish topology must be 'sym' or 'dl' (got {topo!r})")

        pid = self.find_crosvm_pid(expected_cpus=cpus)
        vcpus = self.get_host_vcpu_threads(pid)
        if len(vcpus) != cpus:
            raise HarnessError(f"expected {cpus} crosvm_vcpu threads in pid {pid}, found {len(vcpus)}")

        half = cpus // 2

        def matches(current: dict[int, tuple[int, str, str]]) -> bool:
            for v in range(cpus):
                _, ni, cls = current[v]
                if topo == "sym":
                    if cls != "TS" or ni != "0":
                        return False
                else:
                    if v < half:
                        if cls not in ("DLN", "#6"):
                            return False
                    else:
                        if cls != "TS" or ni != "-10":
                            return False
            return True

        if not matches(vcpus):
            if not apply_if_needed:
                raise HarnessError(f"host crosvm_vcpu scheduling drifted during run under TOPO={topo}: {vcpus}")
            self.log(f"applying host vCPU topology TOPO={topo} on crosvm pid={pid} ({cpus} vCPUs)")
            for v in range(cpus):
                tid = vcpus[v][0]
                if topo == "sym":
                    self._run_priv(["chrt", "-o", "--pid", "0", str(tid)])
                    self._run_priv(["renice", "-n", "0", "-p", str(tid)])
                else:
                    if v < half:
                        self._run_priv([
                            "chrt", "-d",
                            "-T", str(self.dl_runtime_ns),
                            "-P", str(self.dl_period_ns),
                            "-D", str(self.dl_period_ns),
                            "--pid", "0", str(tid),
                        ])
                    else:
                        self._run_priv(["chrt", "-o", "--pid", "0", str(tid)])
                        self._run_priv(["renice", "-n", "-10", "-p", str(tid)])
            vcpus = self.get_host_vcpu_threads(pid)
            if not matches(vcpus):
                raise HarnessError(f"failed to enforce TOPO={topo} on crosvm pid={pid}: {vcpus}")

        return {
            "crosvm_pid": pid,
            "cpus": cpus,
            "topology": topo,
            "dl_runtime_ns": self.dl_runtime_ns if topo == "dl" else None,
            "dl_period_ns": self.dl_period_ns if topo == "dl" else None,
            "vcpu0": vcpus[0],
            "vcpu_last": vcpus[cpus - 1],
        }

    def relaunch_or_prepare_cf(self, cpus: int, topo: str) -> None:
        if self.platform != "x86_64":
            return
        cur_nproc = ""
        try:
            if self.adb("get-state", timeout=10, check=False).strip() == "device":
                cur_nproc = self.shell("nproc", timeout=10, check=False).strip()
        except Exception:
            cur_nproc = ""

        if cur_nproc != str(cpus):
            self.log(f"launching Cuttlefish with CPUS={cpus} TOPO={topo}")
            env = os.environ.copy()
            env.update({
                "CPUS": str(cpus),
                "TOPO": topo,
                "PROXY_EXEC": "0",
                "ANDROID_ADB_SERVER_PORT": self.adb_port,
                "SERIAL": self.serial,
                "ADB": self.adb_bin,
                "BIN_DIR": str(self.bin_dir),
                "WORKLOADS_DIR": str(self.workloads_dir),
            })
            subprocess.run(["bash", str(LAUNCHER_CF)], env=env, check=True)
        self.ensure_root_and_ready()
        self.stage_binaries()
        self.ensure_host_vcpu_topology(cpus, topo, apply_if_needed=True)

    def reboot_guest_reset_proxy(self, cpus: int, topo: str) -> None:
        if self.platform != "x86_64":
            raise HarnessError("reboot prohibited on physical ARM64 device while port 5037 USB is unstable")
        self.log("rebooting Cuttlefish guest to reset one-way sched_proxy_exec static branch to 0")
        self.adb("reboot", timeout=30, check=False)
        time.sleep(6)
        self.ensure_root_and_ready()
        self.ensure_host_vcpu_topology(cpus, topo, apply_if_needed=True)
        px = self.read_proxy_exec()
        if px != "0":
            raise HarnessError(f"sched_proxy_exec/enable is {px!r} after reboot, expected '0'")

    def stage_binaries(self) -> None:
        self.shell(f"mkdir -p '{self.remote_dir}' '{self.remote_dir}/stats' && chmod 777 '{self.remote_dir}' '{self.remote_dir}/stats'", timeout=20)
        for name in (*ALL_DAEMON_BINS, "schbench", "scx_detach"):
            local = self.bin_dir / name
            if local.is_file():
                r_sha = self.shell(f"[ -x '{self.remote_dir}/{name}' ] && sha256sum '{self.remote_dir}/{name}' | awk '{{print $1}}' || true", timeout=15).strip()
                l_sha = sha256_file(local)
                if r_sha != l_sha:
                    self.shell(f"rm -f '{self.remote_dir}/{name}'", timeout=15)
                    self.adb("push", str(local), f"{self.remote_dir}/{name}", timeout=90)
                    self.shell(f"chmod 755 '{self.remote_dir}/{name}'", timeout=15)

    def read_scx_state(self) -> tuple[str, str]:
        out = self.shell(
            "st=$(cat /sys/kernel/sched_ext/state 2>/dev/null || echo unknown); "
            "ops=$(cat /sys/kernel/sched_ext/root/ops 2>/dev/null || cat /sys/kernel/sched_ext/ops 2>/dev/null || true); "
            "printf '%s|%s' \"$st\" \"${ops:-none}\"",
            timeout=15,
        ).strip()
        st, _, ops = out.partition("|")
        return st.strip(), (ops.strip() or "none")

    def read_proxy_exec(self) -> str:
        return self.shell(
            "if [ -r /sys/kernel/sched_proxy_exec/enable ]; then cat /sys/kernel/sched_proxy_exec/enable; else echo n/a; fi",
            timeout=15,
        ).strip()

    def set_bpf_stats(self, enabled: int) -> None:
        self.shell(f"echo {enabled} > /proc/sys/kernel/bpf_stats_enabled", timeout=15)
        got = self.shell("cat /proc/sys/kernel/bpf_stats_enabled", timeout=15).strip()
        if got != str(enabled):
            raise HarnessError(f"bpf_stats_enabled is {got!r}, expected {enabled}")

    def stop_all_daemons(self) -> None:
        names = " ".join(ALL_DAEMON_BINS)
        self.shell(
            f"for n in {names}; do "
            f"  for p in $(pidof \"$n\" 2>/dev/null); do kill -TERM \"$p\" 2>/dev/null || true; done; "
            f"done; "
            f"for _i in 1 2 3 4 5 6 7 8; do "
            f"  alive=''; "
            f"  for n in {names}; do p=$(pidof \"$n\" 2>/dev/null || true); [ -n \"$p\" ] && alive=1; done; "
            f"  [ -z \"$alive\" ] && break; "
            f"  sleep 0.5; "
            f"done; "
            f"for n in {names}; do "
            f"  for p in $(pidof \"$n\" 2>/dev/null); do kill -KILL \"$p\" 2>/dev/null || true; done; "
            f"done; "
            f"if [ \"$(cat /sys/kernel/sched_ext/state 2>/dev/null)\" = 'enabled' ] && [ -x '{self.remote_dir}/scx_detach' ]; then "
            f"  '{self.remote_dir}/scx_detach' >/dev/null 2>&1 || true; "
            f"fi",
            timeout=45,
            check=False,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            st, _ = self.read_scx_state()
            if st == "disabled":
                return
            if (self.bin_dir / "scx_detach").is_file():
                self.shell(f"'{self.remote_dir}/scx_detach' >/dev/null 2>&1 || true", timeout=15, check=False)
            time.sleep(0.5)
        st, ops = self.read_scx_state()
        raise HarnessError(f"sched_ext did not return to disabled (state={st}, ops={ops})")

    def start_remote_daemon(self, cmd_inside_remote: str) -> None:
        inner = f"cd '{self.remote_dir}' && ulimit -l unlimited && SCX_STATS_BASE_PATH='{self.remote_dir}/stats' exec {cmd_inside_remote} </dev/null"
        subprocess.Popen(
            self.adb_cmd("shell", self._wrap_root(inner)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def big_cpus_arg(self, cpus: int, topo: str) -> str:
        if self.platform != "x86_64" or topo == "sym":
            return ""
        return ",".join(str(i) for i in range(cpus // 2, cpus))

    def attach_policy(self, policy: str, cpus: int, topo: str, bg_workers: int = 0) -> dict[str, object]:
        spec = POLICY_SPECS[policy]
        want_proxy = int(spec["proxy"])  # type: ignore[arg-type]
        cur_proxy = self.read_proxy_exec()

        self.stop_all_daemons()
        if cur_proxy != "n/a":
            if want_proxy == 0 and cur_proxy != "0":
                self.reboot_guest_reset_proxy(cpus, topo)
            elif want_proxy == 1 and cur_proxy != "1":
                self.shell("echo 1 > /sys/kernel/sched_proxy_exec/enable", timeout=15)
                if self.read_proxy_exec() != "1":
                    raise HarnessError("failed to enable sched_proxy_exec via sysfs")

        bin_name = spec["bin"]
        sched_args = list(spec["args"])  # type: ignore[arg-type]
        big_cpus = self.big_cpus_arg(cpus, topo)
        if bin_name and spec["needs_big_cpus"] and big_cpus:
            sched_args.extend(["--big-cpus", big_cpus])

        if bin_name:
            arg_str = " ".join(sched_args)
            log_file = f"{self.remote_dir}/sched_active.log"
            self.shell(f": > '{log_file}' && chmod 666 '{log_file}'", timeout=10)
            self.start_remote_daemon(f"./{bin_name} {arg_str} >'{log_file}' 2>&1")
            deadline = time.time() + 90
            while time.time() < deadline:
                st, ops = self.read_scx_state()
                if st == "enabled" and ops_matches(str(spec["ops"]), ops):
                    break
                time.sleep(0.3)
            else:
                st, ops = self.read_scx_state()
                tail = self.shell(f"tail -n 30 '{log_file}' 2>/dev/null || true", timeout=10, check=False)
                raise HarnessError(f"policy {policy} failed to attach (state={st}, ops={ops}): {tail}")
        else:
            st, ops = self.read_scx_state()
            if st != "disabled":
                raise HarnessError(f"expected sched_ext disabled for {policy}, got {st}/{ops}")

        if bg_workers > 0:
            self.start_remote_daemon(
                f"./fcp_frame_bench --bg-only --bg-workers {bg_workers} >/dev/null 2>&1"
            )
            deadline = time.time() + 10
            while time.time() < deadline:
                if self.shell("pidof fcp_frame_bench || true", timeout=10).strip():
                    break
                time.sleep(0.2)
            else:
                raise HarnessError(f"fcp_frame_bench --bg-only ({bg_workers} workers) did not start")

        st, ops = self.read_scx_state()
        return {
            "policy": policy,
            "binary": bin_name or "none",
            "args": sched_args,
            "scx_state": st,
            "scx_ops": ops,
            "proxy_exec": self.read_proxy_exec(),
            "bg_workers": bg_workers,
            "big_cpus": big_cpus,
        }

    def verify_policy_still_active(self, policy: str, bg_workers: int = 0) -> dict[str, object]:
        spec = POLICY_SPECS[policy]
        st, ops = self.read_scx_state()
        px = self.read_proxy_exec()
        if spec["bin"]:
            if st != "enabled" or not ops_matches(str(spec["ops"]), ops):
                raise HarnessError(f"scheduler drifted mid-run for {policy}: state={st}, ops={ops}")
        else:
            if st != "disabled":
                raise HarnessError(f"sched_ext unexpectedly enabled during {policy}: {st}/{ops}")
        if px != "n/a" and px != str(spec["proxy"]):
            raise HarnessError(f"proxy_exec drifted mid-run for {policy}: {px}")
        if bg_workers > 0:
            if not self.shell("pidof fcp_frame_bench || true", timeout=10).strip():
                raise HarnessError("background contention daemon died mid-run")
        return {"scx_state": st, "scx_ops": ops, "proxy_exec": px}

    def snapshot_bpf_fdinfo(self, bin_name: str) -> list[dict[str, int | str]]:
        script = (
            f"set -eu; pid=$(pidof '{bin_name}' | awk '{{print $1}}'); "
            f"[ -n \"$pid\" ]; "
            f"for f in /proc/$pid/fdinfo/*; do "
            f"  [ -r \"$f\" ] || continue; "
            f"  t=$(awk '/^prog_type:/{{print $2; exit}}' \"$f\"); "
            f"  [ \"$t\" = '{STRUCT_OPS_PROG_TYPE}' ] || continue; "
            f"  id=$(awk '/^prog_id:/{{print $2; exit}}' \"$f\"); "
            f"  ns=$(awk '/^run_time_ns:/{{print $2; exit}}' \"$f\"); "
            f"  cnt=$(awk '/^run_cnt:/{{print $2; exit}}' \"$f\"); "
            f"  printf '%s %s %s\\n' \"$id\" \"$ns\" \"$cnt\"; "
            f"done"
        )
        out = self.shell(script, timeout=20)
        by_id: dict[int, dict[str, int | str]] = {}
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) == 3:
                pid_i, ns_i, cnt_i = (int(x) for x in parts)
                by_id.setdefault(pid_i, {"id": pid_i, "run_time_ns": ns_i, "run_cnt": cnt_i})
        if not by_id:
            raise HarnessError(f"no struct_ops fdinfo entries found for {bin_name}")
        return [by_id[k] for k in sorted(by_id)]


def parse_and_validate_jankbench_db(db_path: Path, expected_ids: list[int]) -> dict[str, object]:
    if not db_path.is_file() or db_path.stat().st_size == 0:
        raise HarnessError(f"missing or empty JankBench DB: {db_path}")
    con = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = [
        "name", "total_duration", "jank_frame",
        "unknown_delay", "input", "animation", "layout", "draw", "sync", "command_issue", "swap_buffers",
    ]
    rows = con.execute(f"SELECT {', '.join(cols)} FROM ui_results ORDER BY _id").fetchall()
    con.close()
    if not rows:
        raise HarnessError("JankBench ui_results table is empty")

    expected_names = {JANKBENCH_NAMES[i] for i in expected_ids}
    found_names = {str(r["name"]) for r in rows}
    missing = sorted(expected_names - found_names)
    if missing:
        raise HarnessError(f"JankBench DB missing workloads: {missing}")

    total_frames = 0
    per_bench: dict[str, dict[str, object]] = {}
    for bname in sorted(expected_names):
        sub = [r for r in rows if r["name"] == bname and r["total_duration"] is not None]
        durs = sorted(float(r["total_duration"]) for r in sub if 0.0 < float(r["total_duration"]) < 5000.0)
        if len(durs) < (20 if bname == "Edit Text Input" else 200):
            raise HarnessError(f"too few frames for {bname}: {len(durs)}")
        n = len(durs)
        total_frames += n
        janks = sum(1 for r in sub if int(r["jank_frame"] or 0) > 0)
        ge12 = sum(1 for d in durs if d >= 12.0)

        def pct(vals: list[float], q: float) -> float:
            k = (len(vals) - 1) * (q / 100.0)
            lo = int(k)
            hi = min(lo + 1, len(vals) - 1)
            return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)

        p50, p95, p99 = pct(durs, 50), pct(durs, 95), pct(durs, 99)
        if not (0 < p50 <= p95 <= p99):
            raise HarnessError(f"invalid percentile ordering in {bname}: {p50}, {p95}, {p99}")
        per_bench[bname] = {
            "frames": n,
            "mean_ms": sum(durs) / n,
            "p50_ms": p50,
            "p95_ms": p95,
            "p99_ms": p99,
            "jank_frames": janks,
            "jank_pct": 100.0 * janks / n,
            "ge12_pct": 100.0 * ge12 / n,
        }
    return {"total_frames": total_frames, "workloads": per_bench}


def parse_and_validate_uibench_log(log_path: Path) -> dict[str, object]:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    if "INSTRUMENTATION_FAILED:" in text or "FAILURES!!!" in text:
        raise HarnessError("UiBench instrumentation reported failure")
    codes = re.findall(r"(?m)^INSTRUMENTATION_CODE:\s*(-?\d+)\s*$", text)
    if codes != ["-1"]:
        raise HarnessError(f"UiBench instrumentation code {codes!r} != ['-1']")

    starts = list(re.finditer(r"(?m)^INSTRUMENTATION_STATUS:\s*test=(\w+)\s*$", text))
    per_test: dict[str, dict[str, float]] = {}
    keys = (
        "total-frames", "frame-time-50", "frame-time-90", "frame-time-95", "frame-time-99",
        "jank", "slow-draw", "slow-ui-thread", "slow-bitmap-uploads", "missed-vsync",
        "num-frame-deadline-missed", "high-input-latency",
    )
    for idx, m in enumerate(starts):
        tname = m.group(1)
        end = starts[idx + 1].start() if idx + 1 < len(starts) else len(text)
        block = text[m.end():end]
        kv: dict[str, float] = {}
        for k in keys:
             hit = re.findall(rf"gfx-avg-{re.escape(k)}=([0-9]+(?:\.[0-9]+)?)", block)
             if hit:
                 kv[k] = float(hit[-1])
        if "total-frames" in kv and kv["total-frames"] >= 100:
            p50 = kv["frame-time-50"]
            p90 = kv["frame-time-90"]
            p95 = kv["frame-time-95"]
            p99 = kv["frame-time-99"]
            if not (0 < p50 <= p90 <= p95 <= p99):
                raise HarnessError(f"UiBench {tname} invalid percentiles: {p50}, {p90}, {p95}, {p99}")
            frames = kv["total-frames"]
            missed = kv.get("num-frame-deadline-missed", 0.0)
            per_test[tname] = {
                "frames": frames,
                "p50_ms": p50,
                "p90_ms": p90,
                "p95_ms": p95,
                "p99_ms": p99,
                "jank_frames": missed,
                "jank_pct": 100.0 * missed / frames,
                "gfx_jank_pct": kv.get("jank", 0.0),
                "slow_draw_pct": kv.get("slow-draw", 0.0),
                "slow_ui_pct": kv.get("slow-ui-thread", 0.0),
                "slow_bitmap_pct": kv.get("slow-bitmap-uploads", 0.0),
            }
    missing = sorted(set(UIBENCH_TESTS) - set(per_test))
    if missing:
        raise HarnessError(f"UiBench missing valid tests: {missing}")
    total_frames = sum(v["frames"] for v in per_test.values())
    return {"total_frames": int(total_frames), "per_test": per_test}


def run_jankbench_once(
    env: TargetEnv,
    run_dir: Path,
    policy: str,
    run_idx: int,
    cpus: int,
    topo: str,
    bg_workers: int,
    bench_ids: list[int],
    bpf_stats: int,
) -> None:
    vcpu_before = env.ensure_host_vcpu_topology(cpus, topo, apply_if_needed=True)
    env.set_bpf_stats(bpf_stats)
    attached = env.attach_policy(policy, cpus, topo, bg_workers=bg_workers)

    user_id = env.shell("am get-current-user", timeout=15).strip() or "0"
    db_remote = f"/data/user/{user_id}/com.android.benchmark/databases/BenchmarkResults"
    ids_csv = ",".join(str(i) for i in bench_ids)

    env.shell(
        f"svc power stayon true; input keyevent KEYCODE_WAKEUP >/dev/null 2>&1 || true; "
        f"wm dismiss-keyguard >/dev/null 2>&1 || true; "
        f"am force-stop --user {user_id} com.android.benchmark; "
        f"rm -f {db_remote}*",
        timeout=30,
    )

    bpf_before = env.snapshot_bpf_fdinfo(str(attached["binary"])) if (bpf_stats and attached["binary"] != "none") else []
    t_start = time.time()
    host_start = utc_now()

    am_cmd = (
        f"am start -W --user {user_id} -n com.android.benchmark/.app.RunLocalBenchmarksActivity "
        f"--eia com.android.benchmark.EXTRA_ENABLED_BENCHMARK_IDS {ids_csv} "
        f"--ei com.android.benchmark.EXTRA_RUN_COUNT 1 "
        f"--ez com.android.benchmark.FINISH_WHEN_DONE true"
    )
    am_out = env.shell(am_cmd, timeout=90)
    (run_dir / "am_start.txt").write_text(am_out, encoding="utf-8")
    if "Status: ok" not in am_out:
        raise HarnessError(f"JankBench am start failed: {am_out.strip()}")

    saw_fg = False
    gone_streak = 0
    deadline = time.time() + 720
    step = 0
    while time.time() < deadline:
        step += 1
        focus = env.shell(
            "dumpsys activity activities | grep -E 'topResumedActivity|ResumedActivity' || true",
            timeout=20,
            check=False,
        )
        if "com.android.benchmark" in focus:
            saw_fg = True
            gone_streak = 0
        elif saw_fg and step >= 8:
            gone_streak += 1
            if gone_streak >= 3:
                break
        time.sleep(1.5)
    else:
        raise HarnessError("JankBench timed out waiting to finish foreground execution")

    wall_s = time.time() - t_start
    host_end = utc_now()
    bpf_after = env.snapshot_bpf_fdinfo(str(attached["binary"])) if (bpf_stats and attached["binary"] != "none") else []
    after_state = env.verify_policy_still_active(policy, bg_workers=bg_workers)
    vcpu_after = env.ensure_host_vcpu_topology(cpus, topo, apply_if_needed=False)

    env.shell(
        f"rm -rf '{env.remote_dir}/jb_pull' && mkdir -p '{env.remote_dir}/jb_pull' && "
        f"(sqlite3 '{db_remote}' \".backup '{env.remote_dir}/jb_pull/BenchmarkResults.db'\" || "
        f"cp '{db_remote}' '{env.remote_dir}/jb_pull/BenchmarkResults.db') && "
        f"chmod -R a+rX '{env.remote_dir}/jb_pull'",
        timeout=30,
    )
    db_local = run_dir / "BenchmarkResults.db"
    env.adb("pull", f"{env.remote_dir}/jb_pull/BenchmarkResults.db", str(db_local), timeout=60)
    if attached["binary"] != "none":
        env.shell(f"chmod a+r '{env.remote_dir}/sched_active.log' 2>/dev/null || true", timeout=10, check=False)
        env.adb("pull", f"{env.remote_dir}/sched_active.log", str(run_dir / "scheduler.log"), timeout=30, check=False)

    env.stop_all_daemons()
    if bpf_stats:
        env.set_bpf_stats(0)

    parsed = parse_and_validate_jankbench_db(db_local, bench_ids)
    manifest = {
        "suite": "jankbench",
        "policy": policy,
        "run": run_idx,
        "platform": env.platform,
        "cpus": cpus,
        "topology": topo,
        "bg_workers": bg_workers,
        "bpf_stats_enabled": bpf_stats,
        "host_start": host_start,
        "host_end": host_end,
        "wall_seconds": round(wall_s, 3),
        "attached": attached,
        "after_state": after_state,
        "vcpu_before": vcpu_before,
        "vcpu_after": vcpu_after,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")

    if bpf_stats and bpf_before and bpf_after:
        write_bpf_overhead_record(run_dir, policy, "jankbench", run_idx, wall_s, int(parsed["total_frames"]), bpf_before, bpf_after)


def run_uibench_once(
    env: TargetEnv,
    run_dir: Path,
    policy: str,
    run_idx: int,
    cpus: int,
    topo: str,
    bg_workers: int,
    bpf_stats: int,
) -> None:
    vcpu_before = env.ensure_host_vcpu_topology(cpus, topo, apply_if_needed=True)
    env.set_bpf_stats(bpf_stats)
    attached = env.attach_policy(policy, cpus, topo, bg_workers=bg_workers)

    user_id = env.shell("am get-current-user", timeout=15).strip() or "0"
    classes_csv = ",".join(UIBENCH_CLASSES)
    env.shell(
        f"svc power stayon true; input keyevent KEYCODE_WAKEUP >/dev/null 2>&1 || true; "
        f"wm dismiss-keyguard >/dev/null 2>&1 || true; "
        f"am force-stop --user {user_id} com.android.test.uibench; "
        f"am force-stop --user {user_id} com.android.uibench.janktests",
        timeout=30,
    )

    bpf_before = env.snapshot_bpf_fdinfo(str(attached["binary"])) if (bpf_stats and attached["binary"] != "none") else []
    t_start = time.time()
    host_start = utc_now()

    inst_cmd = (
        f"am instrument --user {user_id} -w -r -e waitForIdleTimeout 500 "
        f"-e class '{classes_csv}' -e iterations 1 "
        f"com.android.uibench.janktests/androidx.test.runner.AndroidJUnitRunner"
    )
    inst_out = env.shell(inst_cmd, timeout=600)
    log_path = run_dir / "instrument.txt"
    log_path.write_text(inst_out, encoding="utf-8")

    wall_s = time.time() - t_start
    host_end = utc_now()
    bpf_after = env.snapshot_bpf_fdinfo(str(attached["binary"])) if (bpf_stats and attached["binary"] != "none") else []
    after_state = env.verify_policy_still_active(policy, bg_workers=bg_workers)
    vcpu_after = env.ensure_host_vcpu_topology(cpus, topo, apply_if_needed=False)

    if attached["binary"] != "none":
        env.shell(f"chmod a+r '{env.remote_dir}/sched_active.log' 2>/dev/null || true", timeout=10, check=False)
        env.adb("pull", f"{env.remote_dir}/sched_active.log", str(run_dir / "scheduler.log"), timeout=30, check=False)

    env.stop_all_daemons()
    if bpf_stats:
        env.set_bpf_stats(0)

    parsed = parse_and_validate_uibench_log(log_path)
    manifest = {
        "suite": "uibench",
        "policy": policy,
        "run": run_idx,
        "platform": env.platform,
        "cpus": cpus,
        "topology": topo,
        "bg_workers": bg_workers,
        "bpf_stats_enabled": bpf_stats,
        "host_start": host_start,
        "host_end": host_end,
        "wall_seconds": round(wall_s, 3),
        "attached": attached,
        "after_state": after_state,
        "vcpu_before": vcpu_before,
        "vcpu_after": vcpu_after,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")

    if bpf_stats and bpf_before and bpf_after:
        write_bpf_overhead_record(run_dir, policy, "uibench", run_idx, wall_s, int(parsed["total_frames"]), bpf_before, bpf_after)


def write_bpf_overhead_record(
    run_dir: Path,
    policy: str,
    suite: str,
    run_idx: int,
    wall_s: float,
    frames: int,
    before: list[dict[str, int | str]],
    after: list[dict[str, int | str]],
) -> None:
    b_map = {int(x["id"]): x for x in before}
    a_map = {int(x["id"]): x for x in after}
    total_ns = 0
    total_cnt = 0
    progs = []
    for pid in sorted(set(b_map) & set(a_map)):
        dns = max(0, int(a_map[pid]["run_time_ns"]) - int(b_map[pid]["run_time_ns"]))
        dcnt = max(0, int(a_map[pid]["run_cnt"]) - int(b_map[pid]["run_cnt"]))
        total_ns += dns
        total_cnt += dcnt
        progs.append({
            "prog_id": pid,
            "run_time_ns": dns,
            "run_cnt": dcnt,
            "ns_per_call": (dns / dcnt) if dcnt > 0 else 0.0,
        })
    if total_cnt <= 0 or total_ns <= 0:
        raise HarnessError(f"zero BPF struct_ops delta for {policy} ({suite} run {run_idx})")
    rec = {
        "policy": policy,
        "suite": suite,
        "run": run_idx,
        "wall_seconds": round(wall_s, 3),
        "frames": frames,
        "run_time_ns": total_ns,
        "bpf_cpu_s": total_ns / 1e9,
        "run_cnt": total_cnt,
        "ns_per_call": total_ns / total_cnt,
        "us_per_frame": (total_ns / 1e3 / frames) if frames > 0 else 0.0,
        "programs": progs,
    }
    (run_dir / "bpf_overhead.json").write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")


def execute_with_retries(
    env: TargetEnv,
    target_dir: Path,
    max_attempts: int,
    resume: bool,
    fn,
) -> None:
    if resume and (target_dir / "summary.json").is_file() and (target_dir / "manifest.json").is_file():
        env.log(f"resume: skipping completed {target_dir}")
        return
    if target_dir.exists():
        inv_root = target_dir.parent / "_invalid"
        inv_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target_dir), str(inv_root / f"{target_dir.name}-stale-{int(time.time())}"))

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, max_attempts + 1):
        stage = Path(tempfile.mkdtemp(prefix=f".{target_dir.name}-try{attempt}-", dir=str(target_dir.parent)))
        try:
            fn(stage)
            stage.rename(target_dir)
            return
        except Exception as exc:
            env.log(f"WARN {target_dir.name} attempt {attempt}/{max_attempts} failed: {exc}")
            try:
                env.stop_all_daemons()
            except Exception:
                pass
            (stage / "error.txt").write_text(str(exc) + "\n", encoding="utf-8")
            inv_root = target_dir.parent / "_invalid"
            inv_root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(stage), str(inv_root / f"{target_dir.name}-attempt{attempt}-{int(time.time())}"))
    raise HarnessError(f"{target_dir} failed after {max_attempts} attempts")


def split_proxy_order(policies: list[str]) -> tuple[list[str], list[str]]:
    non_proxy = [p for p in policies if int(POLICY_SPECS[p]["proxy"]) == 0]
    proxy = [p for p in policies if int(POLICY_SPECS[p]["proxy"]) == 1]
    return non_proxy, proxy


def rotate(items: list[str], r: int) -> list[str]:
    if not items:
        return []
    k = (r - 1) % len(items)
    return items[k:] + items[:k]


def write_freeze(out_dir: Path, env: TargetEnv, meta: dict[str, object]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    bins = {}
    for b in (*ALL_DAEMON_BINS, "schbench", "scx_detach"):
        p = env.bin_dir / b
        if p.is_file():
            bins[b] = sha256_file(p)
    freeze = {"created_at": utc_now(), "platform": env.platform, "serial": env.serial, "binaries_sha256": bins, **meta}
    (out_dir / "FREEZE.json").write_text(json.dumps(freeze, indent=2) + "\n", encoding="utf-8")


def cmd_micro(args: argparse.Namespace) -> None:
    out_dir = Path(args.out).resolve()
    env = TargetEnv(out_dir / "campaign.log")
    cpus_list = [4] if args.quick else [int(x) for x in args.cpus.split()]
    topos = ["dl"] if args.quick else args.topos.split()
    runs = 1 if args.quick else args.runs
    policies = [normalize_policy(p) for p in args.policies.split(",") if p.strip()]
    if env.platform == "arm64":
        policies = [p for p in policies if p != "eevdf_proxy_exec"]
        cpus_list = [8]
        topos = ["device"]

    write_freeze(out_dir, env, {
        "mode": "micro", "suite": args.suite, "cpus_list": cpus_list, "topos": topos,
        "runs": runs, "frames": args.frames, "hz": args.hz, "stages": args.stages, "stage_us": args.stage_us,
        "policies": policies,
    })

    non_proxy, proxy = split_proxy_order(policies)
    for cpus in cpus_list:
        bg = 6 if env.platform == "arm64" else max(2, (3 * cpus) // 4)
        for topo in topos:
            cell_dir = out_dir / f"{cpus}vcpu_{topo}"
            if args.resume and all(
                (cell_dir / pol / f"run-{r:02d}" / "summary.json").is_file()
                for pol in policies for r in range(1, runs + 1)
            ):
                env.log(f"resume: skipping completed cell {cell_dir.name}")
                continue
            if env.platform == "x86_64":
                env.relaunch_or_prepare_cf(cpus, topo)
            else:
                env.ensure_root_and_ready()
                env.stage_binaries()
            env.set_bpf_stats(0)
            for group in (non_proxy, proxy):
                if not group:
                    continue
                for r in range(1, runs + 1):
                    for pol in rotate(group, r):
                        rdir = cell_dir / pol / f"run-{r:02d}"

                        def do_micro_run(stage: Path, _pol=pol, _r=r, _cpus=cpus, _topo=topo, _bg=bg) -> None:
                            vb = env.ensure_host_vcpu_topology(_cpus, _topo, apply_if_needed=True) if env.platform == "x86_64" else {}
                            att = env.attach_policy(_pol, _cpus, _topo, bg_workers=0)
                            bench_big = ",".join(str(i) for i in range(_cpus)) if (env.platform == "x86_64" and _topo == "sym") else str(att["big_cpus"])
                            big_arg = f"--big-cpus {bench_big}" if bench_big else ""
                            summary: dict[str, object] = {}

                            if args.suite in ("fcp", "all"):
                                raw = env.shell(
                                    f"'{env.remote_dir}/fcp_frame_bench' --scheduler '{_pol}' "
                                    f"--frames {args.frames} --hz {args.hz} --stages {args.stages} "
                                    f"--stage-us {args.stage_us} --bg-workers {_bg} {big_arg}",
                                    timeout=90,
                                ).strip().splitlines()[-1]
                                fcp_obj = json.loads(raw)
                                if not (0 < fcp_obj["p50_ms"] <= fcp_obj["p95_ms"] <= fcp_obj["p99_ms"]):
                                    raise HarnessError(f"invalid fcp_frame_bench percentiles: {fcp_obj}")
                                (stage / "fcp_frame_bench.json").write_text(json.dumps(fcp_obj, indent=2) + "\n", encoding="utf-8")
                                summary["fcp_frame_bench"] = fcp_obj

                            if args.suite in ("schbench", "all") and (_r == 1 or args.suite == "schbench"):
                                sb_dur = 3 if args.quick else 8
                                sb_threads = max(1, _cpus // 2)
                                sb_raw = env.shell(
                                    f"'{env.remote_dir}/schbench' -m 2 -t {sb_threads} -r {sb_dur} -w 1 -j -",
                                    timeout=sb_dur + 45,
                                ).strip()
                                m_json = re.search(r"(\{.*\})\s*$", sb_raw, flags=re.S)
                                if not m_json:
                                    raise HarnessError(f"schbench produced no JSON output: {sb_raw[-300:]}")
                                sb_obj = json.loads(m_json.group(1))
                                (stage / "schbench.json").write_text(json.dumps(sb_obj, indent=2) + "\n", encoding="utf-8")
                                summary["schbench_recorded"] = True

                            af = env.verify_policy_still_active(_pol, bg_workers=0)
                            va = env.ensure_host_vcpu_topology(_cpus, _topo, apply_if_needed=False) if env.platform == "x86_64" else {}
                            env.stop_all_daemons()

                            (stage / "manifest.json").write_text(json.dumps({
                                "mode": "micro", "policy": _pol, "run": _r, "cpus": _cpus, "topology": _topo,
                                "bg_workers": _bg, "attached": att, "after_state": af, "vcpu_before": vb, "vcpu_after": va,
                            }, indent=2) + "\n", encoding="utf-8")
                            (stage / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

                        execute_with_retries(env, rdir, args.max_attempts, args.resume, do_micro_run)
                        env.log(f"micro ok: {cpus}vcpu_{topo} {pol} run-{r:02d}")
    env.log(f"MICRO_CAMPAIGN_COMPLETE: {out_dir}")


def run_suite_matrix(
    mode: str,
    out_dir: Path,
    cpus: int,
    topo: str,
    runs: int,
    bg_workers: int,
    policies: list[str],
    suite: str,
    bench_ids: list[int],
    bpf_stats: int,
    max_attempts: int,
    resume: bool,
) -> None:
    env = TargetEnv(out_dir / "campaign.log")
    if env.platform == "arm64":
        policies = [p for p in policies if p != "eevdf_proxy_exec"]
        env.ensure_root_and_ready()
        env.stage_binaries()
    else:
        env.relaunch_or_prepare_cf(cpus, topo)

    write_freeze(out_dir, env, {
        "mode": mode, "suite": suite, "cpus": cpus, "topology": topo, "runs": runs,
        "bg_workers": bg_workers, "bpf_stats": bpf_stats, "policies": policies, "bench_ids": bench_ids,
    })

    non_proxy, proxy = split_proxy_order(policies)
    for group in (non_proxy, proxy):
        if not group:
            continue
        for r in range(1, runs + 1):
            for pol in rotate(group, r):
                if suite in ("jankbench", "all"):
                    jb_dir = out_dir / "jankbench" / pol / f"run-{r:02d}"
                    execute_with_retries(
                        env, jb_dir, max_attempts, resume,
                        lambda stg, _p=pol, _r=r: run_jankbench_once(env, stg, _p, _r, cpus, topo, bg_workers, bench_ids, bpf_stats),
                    )
                    env.log(f"{mode} jankbench ok: {topo} {pol} run-{r:02d}")
                if suite in ("uibench", "all"):
                    ub_dir = out_dir / "uibench" / pol / f"run-{r:02d}"
                    execute_with_retries(
                        env, ub_dir, max_attempts, resume,
                        lambda stg, _p=pol, _r=r: run_uibench_once(env, stg, _p, _r, cpus, topo, bg_workers, bpf_stats),
                    )
                    env.log(f"{mode} uibench ok: {topo} {pol} run-{r:02d}")
    env.log(f"{mode.upper()}_CAMPAIGN_COMPLETE: {out_dir}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    default_cpus = int(os.environ.get("CPUS", "32"))
    default_topo = os.environ.get("TOPO", "dl")
    default_runs = int(os.environ.get("RUNS", "10"))
    default_bg = int(os.environ.get("BG_WORKERS", str((3 * default_cpus) // 4)))
    default_max_att = int(os.environ.get("MAX_ATTEMPTS", "3"))
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    p_smoke = sub.add_parser("smoke")
    p_smoke.add_argument("--topo", default=default_topo)
    p_smoke.add_argument("--cpus", type=int, default=default_cpus)
    p_smoke.add_argument("--out", default=str(ARTIFACT_DIR / "results" / f"smoke_{stamp}"))

    p_micro = sub.add_parser("micro")
    p_micro.add_argument("--cpus", default=os.environ.get("MICRO_CPUS_LIST", "4 8 16 32"))
    p_micro.add_argument("--topos", default=os.environ.get("MICRO_TOPOS", "sym dl"))
    p_micro.add_argument("--runs", type=int, default=default_runs)
    p_micro.add_argument("--frames", type=int, default=int(os.environ.get("MICRO_FRAMES", "100")))
    p_micro.add_argument("--hz", type=int, default=int(os.environ.get("MICRO_HZ", "90")))
    p_micro.add_argument("--stages", type=int, default=int(os.environ.get("MICRO_STAGES", "4")))
    p_micro.add_argument("--stage-us", type=int, default=int(os.environ.get("MICRO_STAGE_US", "1800")))
    p_micro.add_argument("--suite", choices=("fcp", "schbench", "all"), default="all")
    p_micro.add_argument("--policies", default=os.environ.get("POLICIES", "eevdf,eevdf_proxy_exec,scx_p2dq,scx_lavd,scx_fcp_behavioral,scx_fcp"))
    p_micro.add_argument("--max-attempts", type=int, default=default_max_att)
    p_micro.add_argument("--quick", action="store_true")
    p_micro.add_argument("--resume", action="store_true")
    p_micro.add_argument("--out", default=str(ARTIFACT_DIR / "results" / f"micro_{stamp}"))

    p_macro = sub.add_parser("macro")
    p_macro.add_argument("--cpus", type=int, default=default_cpus)
    p_macro.add_argument("--topo", default=default_topo)
    p_macro.add_argument("--runs", type=int, default=default_runs)
    p_macro.add_argument("--bg-workers", type=int, default=default_bg)
    p_macro.add_argument("--suite", choices=("all", "jankbench", "uibench"), default="all")
    p_macro.add_argument("--policies", default=os.environ.get("POLICIES", "eevdf,eevdf_proxy_exec,scx_p2dq,scx_lavd,scx_fcp_behavioral,scx_fcp"))
    p_macro.add_argument("--bench-ids", default="0,1,2,3,4,5,6,7")
    p_macro.add_argument("--max-attempts", type=int, default=default_max_att)
    p_macro.add_argument("--quick", action="store_true")
    p_macro.add_argument("--resume", action="store_true")
    p_macro.add_argument("--out", default=str(ARTIFACT_DIR / "results" / f"macro_{default_topo}_{stamp}"))

    p_ovh = sub.add_parser("overhead")
    p_ovh.add_argument("--cpus", type=int, default=default_cpus)
    p_ovh.add_argument("--topo", default=default_topo)
    p_ovh.add_argument("--runs", type=int, default=3)
    p_ovh.add_argument("--bg-workers", type=int, default=0)
    p_ovh.add_argument("--suite", choices=("all", "jankbench", "uibench"), default="all")
    p_ovh.add_argument("--policies", default=os.environ.get("OVERHEAD_POLICIES", "scx_p2dq,scx_lavd,scx_fcp_behavioral,scx_fcp"))
    p_ovh.add_argument("--bench-ids", default="0,1,2,3,4,5,6,7")
    p_ovh.add_argument("--max-attempts", type=int, default=default_max_att)
    p_ovh.add_argument("--quick", action="store_true")
    p_ovh.add_argument("--resume", action="store_true")
    p_ovh.add_argument("--out", default=str(ARTIFACT_DIR / "results" / f"overhead_{default_topo}_{stamp}"))

    p_abl = sub.add_parser("ablation")
    p_abl.add_argument("--cpus", type=int, default=default_cpus)
    p_abl.add_argument("--topo", default="dl")
    p_abl.add_argument("--runs", type=int, default=5)
    p_abl.add_argument("--bg-workers", type=int, default=default_bg)
    p_abl.add_argument("--policies", default=os.environ.get("ABLATION_POLICIES", "scx_fcp_notoken,scx_fcp_nofanout,scx_fcp_behavioral,scx_fcp"))
    p_abl.add_argument("--bench-ids", default="0,1,2,3,4,5,6,7")
    p_abl.add_argument("--max-attempts", type=int, default=default_max_att)
    p_abl.add_argument("--quick", action="store_true")
    p_abl.add_argument("--resume", action="store_true")
    p_abl.add_argument("--out", default=str(ARTIFACT_DIR / "results" / f"ablation_{stamp}"))

    args = ap.parse_args()
    if args.cmd == "micro":
        cmd_micro(args)
        return 0

    if args.cmd == "smoke":
        pols = [normalize_policy(p) for p in os.environ.get("POLICIES", "eevdf,eevdf_proxy_exec,scx_p2dq,scx_lavd,scx_fcp_behavioral,scx_fcp").split(",")]
        run_suite_matrix(
            mode="smoke", out_dir=Path(args.out).resolve(), cpus=args.cpus, topo=args.topo,
            runs=1, bg_workers=(3 * args.cpus) // 4, policies=pols, suite="jankbench",
            bench_ids=[0], bpf_stats=0, max_attempts=2, resume=False,
        )
        return 0

    pols = [normalize_policy(p) for p in args.policies.split(",") if p.strip()]
    bids = [0] if args.quick else [int(x) for x in args.bench_ids.split(",") if x.strip()]
    runs = 1 if args.quick else args.runs
    suite = "jankbench" if (args.cmd == "ablation" or (args.cmd == "overhead" and args.quick)) else args.suite
    bpf_stats = 1 if args.cmd == "overhead" else 0

    run_suite_matrix(
        mode=args.cmd, out_dir=Path(args.out).resolve(), cpus=args.cpus, topo=args.topo,
        runs=runs, bg_workers=args.bg_workers, policies=pols, suite=suite,
        bench_ids=bids, bpf_stats=bpf_stats, max_attempts=args.max_attempts, resume=args.resume,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
