/* SPDX-License-Identifier: GPL-2.0 */
#ifndef __FCP_INTF_H
#define __FCP_INTF_H

#ifndef __VMLINUX_H__
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef unsigned long u64;

typedef signed char s8;
typedef signed short s16;
typedef signed int s32;
typedef signed long s64;
#endif /* __VMLINUX_H__ */

enum fcp_consts {
	MAX_CPUS		= 128,
	NSEC_PER_USEC		= 1000ULL,
	NSEC_PER_MSEC		= 1000000ULL,
	NSEC_PER_SEC		= 1000000000ULL,

	/* Stratified Dispatch Queue IDs */
	FCP_DSQ_CRITICAL	= 0x1000ULL,
	FCP_DSQ_INTERACTIVE	= 0x1001ULL,
	FCP_DSQ_BACKGROUND	= 0x1002ULL,

	/* Maximum transitive critical-path hop depth (UI -> Render -> Binder -> Worker) */
	MAX_FCP_DEPTH		= 5,

	/* Fixed-point capacity and utilization scale [0 .. 1024] */
	FCP_CAP_SCALE		= 1024ULL,

	/* Task classification tiers. NONE is a CPU-state sentinel, not a tier. */
	FCP_TIER_CRITICAL	= 0,
	FCP_TIER_INTERACTIVE	= 1,
	FCP_TIER_BACKGROUND	= 2,
	FCP_TIER_NONE		= 3,
};

struct fcp_cpu_ctx {
	u64 tot_runtime_ns;
	u64 last_run_at_ns;
	u32 curr_tier;
	u32 curr_is_fcp;
	u32 last_perf_level;
	u64 curr_deadline_ns;
	u32 consec_crit_dispatches;
	u32 pad;
};

struct fcp_stats {
	u64 nr_crit_dispatches;
	u64 nr_intr_dispatches;
	u64 nr_bg_dispatches;
	u64 nr_fcp_tokens_issued;
	u64 nr_fcp_tokens_inherited;
	u64 nr_preempt_kicks;
	u64 nr_big_core_placements;
	u64 nr_little_core_placements;
	u64 nr_starvation_rescues;
	u64 nr_root_src_behavior;
	u64 nr_root_src_comm;
	u64 nr_root_src_pid;
	u64 nr_root_promotions;
	u64 nr_root_demotions;
	u64 nr_root_downstream_suppressed;
	u64 nr_token_select_calls;
	u64 nr_token_enqueue_calls;
	u64 nr_edges_eligible;
	u64 nr_edges_inherited;
	u64 nr_edges_sync_flag;
	u64 nr_edges_same_tgid;
	u64 nr_edges_async_rejected;
	u64 nr_depth_regress_blocked;
	u64 nr_token_revocations;
	u64 nr_token_expiry;
	u64 nr_first_big_hits;
	u64 nr_first_big_misses;
	u64 nr_depth_hist[MAX_FCP_DEPTH + 1];
};

#endif /* __FCP_INTF_H */
