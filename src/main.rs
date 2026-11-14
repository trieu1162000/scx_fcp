// SPDX-License-Identifier: GPL-2.0
//
// scx_fcp: behavioral frame-root and critical-path token scheduler
// Userspace loader, big.LITTLE topology configurator, and counters loop.

mod bpf_skel;
pub use bpf_skel::*;
pub mod bpf_intf;
pub use bpf_intf::*;

use std::fs;
use std::mem::MaybeUninit;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use anyhow::{Context, Result, bail};
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
    about = "Behavioral frame-root and critical-path token sched_ext scheduler for Android big.LITTLE SoCs"
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

    /// Comma-separated list of big/Prime CPU IDs (if omitted, reads cpu_capacity or defaults to top half of CPUs).
    #[clap(long, default_value = "")]
    big_cpus: String,

    /// Comma-separated list of thread IDs to register as explicit frame roots. Disabled in the zero-hint build.
    #[clap(long, default_value = "")]
    frame_pids: String,

    /// Stats logging interval in seconds (0 = quiet).
    #[clap(short = 's', long, default_value = "2")]
    stats_interval_s: u64,

    #[clap(flatten, next_help_heading = "Libbpf Options")]
    pub libbpf: LibbpfOpts,
}

fn detect_cpu_capacities(nr_cpus: usize, big_cpus_arg: &str) -> (Vec<u64>, Vec<u8>) {
    let mut caps = vec![1024u64; nr_cpus];
    let mut is_big = vec![0u8; nr_cpus];

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
        }
        for tok in big_cpus_arg.split(',') {
            if let Ok(c) = tok.trim().parse::<usize>()
                && c < nr_cpus
            {
                caps[c] = 1024;
                is_big[c] = 1;
            }
        }
    } else if has_asymmetric_sysfs {
        let threshold = (min_cap + max_cap) / 2;
        for cpu in 0..nr_cpus {
            is_big[cpu] = if caps[cpu] >= threshold { 1 } else { 0 };
        }
    } else {
        // Default big.LITTLE partition on symmetric VMs (e.g. 4-vCPU Cuttlefish: CPUs 0..N/2 LITTLE, N/2..N big)
        let split = (nr_cpus / 2).max(1);
        for cpu in 0..nr_cpus {
            if cpu >= split {
                caps[cpu] = 1024;
                is_big[cpu] = 1;
            } else {
                caps[cpu] = 446;
                is_big[cpu] = 0;
            }
        }
    }

    (caps, is_big)
}

fn main() -> Result<()> {
    let opts = Opts::parse();

    if option_env!("SCX_FCP_BUILD_PROFILE") == Some("zero-hint")
        && !opts.frame_pids.trim().is_empty()
    {
        bail!("--frame-pids is disabled in the zero-hint build");
    }

    simplelog::TermLogger::init(
        simplelog::LevelFilter::Info,
        simplelog::Config::default(),
        simplelog::TerminalMode::Stderr,
        simplelog::ColorChoice::Auto,
    )?;

    try_set_rlimit_infinity();

    let nr_cpus = libbpf_rs::num_possible_cpus()
        .unwrap_or(8)
        .min(fcp_consts_MAX_CPUS as usize);

    let (caps, is_big) = detect_cpu_capacities(nr_cpus, &opts.big_cpus);

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

        for cpu in 0..nr_cpus {
            rodata.cpu_capacity[cpu] = caps[cpu];
            rodata.is_big_core[cpu] = is_big[cpu];
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
            if opts.stats_interval_s > 0 && (ticks % (opts.stats_interval_s * 10) == 0) {
                info!(
                    "[scx_fcp] crit={} intr={} bg={} big={} little={} preempt_kicks={} rescues={}",
                    bss.stat_crit_dispatches,
                    bss.stat_intr_dispatches,
                    bss.stat_bg_dispatches,
                    bss.stat_big_core_placements,
                    bss.stat_little_core_placements,
                    bss.stat_preempt_kicks,
                    bss.stat_starvation_rescues,
                );
                info!(
                    "[scx_fcp] roots beh={} comm={} pid={} prom={} demote={} suppressed={} token_sel={} token_enq={}",
                    bss.stat_root_src_behavior,
                    bss.stat_root_src_comm,
                    bss.stat_root_src_pid,
                    bss.stat_root_promotions,
                    bss.stat_root_demotions,
                    bss.stat_root_downstream_suppressed,
                    bss.stat_token_select_calls,
                    bss.stat_token_enqueue_calls,
                );
                info!(
                    "[scx_fcp] token issued={} inherited={} edges elig={} inh={} sync={} tgid={} async_rej={} depth_block={} revoke={} expire={} first_big={}/{} depth_hist={:?}",
                    bss.stat_fcp_tokens_issued,
                    bss.stat_fcp_tokens_inherited,
                    bss.stat_edges_eligible,
                    bss.stat_edges_inherited,
                    bss.stat_edges_sync_flag,
                    bss.stat_edges_same_tgid,
                    bss.stat_edges_async_rejected,
                    bss.stat_depth_regress_blocked,
                    bss.stat_token_revocations,
                    bss.stat_token_expiry,
                    bss.stat_first_big_hits,
                    bss.stat_first_big_misses,
                    bss.stat_depth_hist,
                );
                info!(
                    "[scx_fcp] tune cooldown_blocks={} edge_budget_rej={} tgid_window_rej={} kick_capped={}",
                    bss.stat_root_cooldown_blocks,
                    bss.stat_edges_budget_rejected,
                    bss.stat_edges_tgid_window_rejected,
                    bss.stat_preempt_kick_capped,
                );
            }
        }
    }

    drop(struct_ops);
    let ei = uei_report!(&mut skel, uei)?;
    info!("scx_fcp exited: {:?}", ei);
    Ok(())
}
