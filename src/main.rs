// SPDX-License-Identifier: GPL-2.0
//
// scx_fcp: Frame Critical-Path & Thermal-Debt Heterogeneous Scheduler
// Userspace loader, asymmetric big.LITTLE topology configurator, and telemetry loop.

mod bpf_skel;
pub use bpf_skel::*;
pub mod bpf_intf;
pub use bpf_intf::*;

use std::fs;
use std::mem::MaybeUninit;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use anyhow::{Context, Result};
use clap::Parser;
use libbpf_rs::{MapCore, MapFlags, OpenObject};
use log::info;
use scx_utils::build_id;
use scx_utils::libbpf_clap_opts::LibbpfOpts;
use scx_utils::scx_ops_attach;
use scx_utils::scx_ops_load;
use scx_utils::scx_ops_open;
use scx_utils::try_set_rlimit_infinity;
use scx_utils::uei_exited;
use scx_utils::uei_report;

#[derive(Debug, Parser)]
#[command(
    name = "scx_fcp",
    about = "Frame Critical-Path & Thermal-Debt Heterogeneous sched_ext scheduler for Android big.LITTLE SoCs"
)]
struct Opts {
    /// Frame budget in microseconds (default 16667us = 60Hz, use 8333us for 120Hz).
    #[clap(short = 'f', long, default_value = "16667")]
    frame_budget_us: u64,

    /// Critical-path task time slice in microseconds.
    #[clap(short = 'c', long, default_value = "2000")]
    slice_crit_us: u64,

    /// Interactive task time slice in microseconds.
    #[clap(short = 'i', long, default_value = "3000")]
    slice_intr_us: u64,

    /// Background task time slice in microseconds.
    #[clap(short = 'b', long, default_value = "6000")]
    slice_bg_us: u64,

    /// Thermal debt cap (0..2048) before spilling from big cores.
    #[clap(short = 't', long, default_value = "850")]
    thermal_debt_cap: u64,

    /// Comma-separated list of big/Prime CPU IDs (if omitted, reads cpu_capacity or defaults to top half of CPUs).
    #[clap(long, default_value = "")]
    big_cpus: String,

    /// Comma-separated list of PIDs/TGIDs to register as explicit Frame Pipeline Roots.
    #[clap(long, default_value = "")]
    frame_pids: String,

    /// Stats logging interval in seconds (0 = quiet).
    #[clap(short = 's', long, default_value = "2")]
    stats_interval_s: u64,

    #[clap(flatten, next_help_heading = "Libbpf Options")]
    pub libbpf: LibbpfOpts,
}

fn detect_cpu_capacities(nr_cpus: usize, big_cpus_arg: &str) -> (Vec<u64>, Vec<u8>, Vec<u64>) {
    let mut caps = vec![1024u64; nr_cpus];
    let mut is_big = vec![0u8; nr_cpus];
    let mut dyn_power = vec![256u64; nr_cpus];

    let mut has_asymmetric_sysfs = false;
    let mut min_cap = u64::MAX;
    let mut max_cap = 0u64;

    for cpu in 0..nr_cpus {
        let path = format!("/sys/devices/system/cpu/cpu{}/cpu_capacity", cpu);
        if let Ok(content) = fs::read_to_string(&path)
            && let Ok(val) = content.trim().parse::<u64>()
            && val > 0
        {
            caps[cpu] = val;
            min_cap = min_cap.min(val);
            max_cap = max_cap.max(val);
        }
    }

    if max_cap > min_cap && min_cap < u64::MAX {
        has_asymmetric_sysfs = true;
    }

    if !big_cpus_arg.trim().is_empty() {
        for cpu in 0..nr_cpus {
            caps[cpu] = 446;
            is_big[cpu] = 0;
            dyn_power[cpu] = 140;
        }
        for tok in big_cpus_arg.split(',') {
            if let Ok(c) = tok.trim().parse::<usize>()
                && c < nr_cpus
            {
                caps[c] = 1024;
                is_big[c] = 1;
                dyn_power[c] = 520;
            }
        }
    } else if has_asymmetric_sysfs {
        let threshold = (min_cap + max_cap) / 2;
        for cpu in 0..nr_cpus {
            if caps[cpu] >= threshold {
                is_big[cpu] = 1;
                dyn_power[cpu] = (caps[cpu] * 520) / 1024;
            } else {
                is_big[cpu] = 0;
                dyn_power[cpu] = (caps[cpu] * 320) / 1024;
            }
        }
    } else {
        // Default big.LITTLE partition on symmetric VMs (e.g. 4-vCPU Cuttlefish: CPUs 0..N/2 LITTLE, N/2..N big)
        let split = (nr_cpus / 2).max(1);
        for cpu in 0..nr_cpus {
            if cpu >= split {
                caps[cpu] = 1024;
                is_big[cpu] = 1;
                dyn_power[cpu] = 520;
            } else {
                caps[cpu] = 446;
                is_big[cpu] = 0;
                dyn_power[cpu] = 140;
            }
        }
    }

    (caps, is_big, dyn_power)
}

fn read_thermal_pressure() -> u64 {
    // Poll /sys/class/thermal/thermal_zone*/temp if available
    for zone in 0..4 {
        let path = format!("/sys/class/thermal/thermal_zone{}/temp", zone);
        if let Ok(s) = fs::read_to_string(&path)
            && let Ok(temp_mc) = s.trim().parse::<i64>()
        {
            if temp_mc >= 80000 {
                return ((temp_mc - 75000) / 250) as u64;
            }
        }
    }
    0
}

fn main() -> Result<()> {
    let opts = Opts::parse();

    simplelog::TermLogger::init(
        simplelog::LevelFilter::Info,
        simplelog::Config::default(),
        simplelog::TerminalMode::Stderr,
        simplelog::ColorChoice::Auto,
    )?;

    try_set_rlimit_infinity();

    let nr_cpus = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4)
        .min(fcp_consts_MAX_CPUS as usize);

    let (caps, is_big, dyn_power) = detect_cpu_capacities(nr_cpus, &opts.big_cpus);

    let mut open_object = MaybeUninit::<OpenObject>::uninit();
    let skel_builder = BpfSkelBuilder::default();
    let open_opts = opts.libbpf.clone().into_bpf_open_opts();
    let mut open_skel = scx_ops_open!(skel_builder, &mut open_object, fcp_ops, open_opts)?;

    if let Some(rodata) = open_skel.maps.rodata_data.as_mut() {
        rodata.nr_cpu_ids = nr_cpus as u32;
        rodata.frame_budget_ns = opts.frame_budget_us * 1000;
        rodata.slice_crit_ns = opts.slice_crit_us * 1000;
        rodata.slice_intr_ns = opts.slice_intr_us * 1000;
        rodata.slice_bg_ns = opts.slice_bg_us * 1000;
        rodata.thermal_debt_cap = opts.thermal_debt_cap;

        for cpu in 0..nr_cpus {
            rodata.cpu_capacity[cpu] = caps[cpu];
            rodata.is_big_core[cpu] = is_big[cpu];
            rodata.cpu_dyn_power[cpu] = dyn_power[cpu];
        }
    }

    let mut skel = scx_ops_load!(open_skel, fcp_ops, uei)?;

    if !opts.frame_pids.trim().is_empty() {
        for tok in opts.frame_pids.split(',') {
            if let Ok(pid) = tok.trim().parse::<u32>() {
                let val: u8 = 1;
                let _ = skel.maps.frame_root_pids.update(
                    &pid.to_ne_bytes(),
                    &val.to_ne_bytes(),
                    MapFlags::ANY,
                );
            }
        }
    }

    let struct_ops = scx_ops_attach!(skel, fcp_ops)?;
    info!(
        "scx_fcp {} attached: nr_cpus={} frame_budget={}us big_cores={:?} capacities={:?}",
        build_id::full_version(env!("CARGO_PKG_VERSION")),
        nr_cpus,
        opts.frame_budget_us,
        is_big,
        caps
    );

    let running = Arc::new(AtomicBool::new(true));
    let r = running.clone();
    ctrlc::set_handler(move || {
        r.store(false, Ordering::SeqCst);
    })
    .context("Error setting Ctrl-C handler")?;

    let mut ticks = 0u64;
    while running.load(Ordering::SeqCst) && !uei_exited!(&skel, uei) {
        std::thread::sleep(Duration::from_millis(100));
        ticks += 1;

        if let Some(bss) = skel.maps.bss_data.as_mut() {
            bss.userspace_thermal_pressure = read_thermal_pressure();

            if opts.stats_interval_s > 0 && (ticks % (opts.stats_interval_s * 10) == 0) {
                info!(
                    "[scx_fcp] crit={} intr={} bg={} tokens_issued={} tokens_inherited={} preempt_kicks={} big={} little={} therm_spills={} rescues={}",
                    bss.stat_crit_dispatches,
                    bss.stat_intr_dispatches,
                    bss.stat_bg_dispatches,
                    bss.stat_fcp_tokens_issued,
                    bss.stat_fcp_tokens_inherited,
                    bss.stat_preempt_kicks,
                    bss.stat_big_core_placements,
                    bss.stat_little_core_placements,
                    bss.stat_thermal_spills,
                    bss.stat_starvation_rescues,
                );
            }
        }
    }

    drop(struct_ops);
    let ei = uei_report!(&mut skel, uei)?;
    info!("scx_fcp exited: {:?}", ei);
    Ok(())
}
