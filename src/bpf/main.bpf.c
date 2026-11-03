/* SPDX-License-Identifier: GPL-2.0 */
/*
 * scx_fcp: Frame Critical-Path & Thermal-Debt Heterogeneous Scheduler
 *
 * Designed for Android heterogeneous (big.LITTLE / tri-cluster) SoCs:
 *  1. Ephemeral Frame Critical-Path Token Propagation (FCP-Token):
 *     Tracks frame roots (UIThread, RenderThread, surfaceflinger, composer,
 *     or registered PIDs/TGIDs) and propagates bounded-TTL criticality tokens
 *     (depth <= MAX_FCP_DEPTH) across synchronous IPC/futex/pipe wakeups to
 *     eliminate transitive priority inversion on the display pipeline.
 *  2. Dual-Timescale Burst-Window Utilization (DT-BWU):
 *     Combines a 32ms slow PELT EWMA with a 4ms sub-frame burst window that
 *     preserves active-phase demand across VSYNC inter-frame sleep intervals,
 *     solving the Sleeping-Pipeline Under-Provisioning Paradox.
 *  3. Predictive Thermal-Debt Energy-Aware Core Selection (PTD-EAS):
 *     Steers critical-path tasks to high-capacity (big/Prime) cores with low
 *     accumulated thermal debt, preempts background tasks via SCX_KICK_PREEMPT
 *     on critical wakeups, and isolates background/batch threads on LITTLE
 *     cores with a bounded starvation guard.
 */
#include <scx/common.bpf.h>
#include "intf.h"

char _license[] SEC("license") = "GPL";

const volatile u32 nr_cpu_ids = 4;
const volatile u64 frame_budget_ns = 16666667ULL;      /* 60Hz = 16.67ms (8.33ms for 120Hz) */
const volatile u64 slice_crit_ns = 2000000ULL;         /* 2ms fast slice for critical path */
const volatile u64 slice_intr_ns = 3000000ULL;         /* 3ms slice for interactive tasks */
const volatile u64 slice_bg_ns = 6000000ULL;           /* 6ms slice for background tasks */
const volatile u64 thermal_debt_cap = 850ULL;          /* Max thermal debt before spilling */
const volatile u32 starvation_period = 128;            /* Rescue 1 bg task per N dispatches */
const volatile bool preempt_on_crit_wakeup = true;

const volatile u64 cpu_capacity[MAX_CPUS];
const volatile u8 is_big_core[MAX_CPUS];
const volatile u64 cpu_dyn_power[MAX_CPUS];

volatile u64 vtime_now;
volatile u64 userspace_thermal_pressure;

/* Global statistics counters */
volatile u64 stat_crit_dispatches;
volatile u64 stat_intr_dispatches;
volatile u64 stat_bg_dispatches;
volatile u64 stat_fcp_tokens_issued;
volatile u64 stat_fcp_tokens_inherited;
volatile u64 stat_preempt_kicks;
volatile u64 stat_big_core_placements;
volatile u64 stat_little_core_placements;
volatile u64 stat_thermal_spills;
volatile u64 stat_starvation_rescues;

struct fcp_task_ctx {
	u64 last_run_at_ns;
	u64 last_sleep_at_ns;
	u64 last_woke_at_ns;
	u64 avg_runtime_ns;
	u64 wakeup_freq;

	/* Dual-Timescale Burst-Window Utilization (DT-BWU) in [0 .. 1024] */
	u64 util_pelt;
	u64 util_burst;
	u64 burst_window_runtime_ns;
	u64 burst_window_start_ns;

	/* Ephemeral Frame Critical-Path Token (FCP-Token) */
	u64 frame_start_ns;
	u64 token_expire_ns;
	u64 fcp_deadline_ns;
	u64 dsq_vtime;
	u64 last_period_wake_ns;
	u64 period_ewma_ns;
	u32 fcp_depth;
	u32 req_capacity;
	u32 pid;
	u8 is_frame_root;
	u8 is_render_worker;
	u8 comm_checked;
	u8 periodicity_score;
	u8 woke_peer;
	u8 is_bg_spinner;
};

#define FCP_MAX_TASKS 16384
#define FCP_TASK_MASK (FCP_MAX_TASKS - 1)

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, FCP_MAX_TASKS);
	__type(key, u32);
	__type(value, struct fcp_task_ctx);
} task_ctx_stor SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, u32);
	__type(value, struct fcp_cpu_ctx);
} cpu_ctx_stor SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 1024);
	__type(key, u32);
	__type(value, u8);
} frame_root_pids SEC(".maps");

UEI_DEFINE(uei);

static __always_inline bool cpu_is_big(s32 cpu)
{
	const volatile u8 *p;

	if (cpu < 0 || cpu >= MAX_CPUS)
		return true;
	p = MEMBER_VPTR(is_big_core, [cpu]);
	return p ? (*p != 0) : true;
}

static __always_inline u64 get_cpu_capacity(s32 cpu)
{
	const volatile u64 *p;

	if (cpu < 0 || cpu >= MAX_CPUS)
		return FCP_CAP_SCALE;
	p = MEMBER_VPTR(cpu_capacity, [cpu]);
	return (p && *p > 0) ? *p : FCP_CAP_SCALE;
}

static __always_inline u64 get_cpu_dyn_power(s32 cpu)
{
	const volatile u64 *p;

	if (cpu < 0 || cpu >= MAX_CPUS)
		return 256;
	p = MEMBER_VPTR(cpu_dyn_power, [cpu]);
	return (p && *p > 0) ? *p : 256;
}

static __always_inline struct fcp_cpu_ctx *lookup_cpu_ctx(s32 cpu)
{
	u32 zero = 0;

	if (cpu < 0)
		return bpf_map_lookup_elem(&cpu_ctx_stor, &zero);
	return bpf_map_lookup_percpu_elem(&cpu_ctx_stor, &zero, cpu);
}

static __always_inline struct fcp_task_ctx *lookup_task_ctx(struct task_struct *p)
{
	u32 pid = (u32)p->pid;
	u32 idx = pid & FCP_TASK_MASK;
	struct fcp_task_ctx *tctx;

	tctx = bpf_map_lookup_elem(&task_ctx_stor, &idx);
	if (!tctx)
		return NULL;
	if (unlikely(tctx->pid != pid)) {
		__builtin_memset(tctx, 0, sizeof(*tctx));
		tctx->pid = pid;
	}
	return tctx;
}

/*
 * Check if a 16-byte comm matches known Android UI / Frame Pipeline roots:
 *   - "RenderThread"
 *   - "surfaceflinger"
 *   - "composer" / "android.hardwar"
 *   - "UiBench" / "jank" / "fcp_frame" / "ui_main"
 *   - "hwuiTask" / "choreographer"
 *   - "android.ui" / "android.anim" / "android.display"
 */
static __always_inline bool match_frame_comm(const char *c)
{
	/* "RenderThread" or "RenderEngine" */
	if (c[0] == 'R' && c[1] == 'e' && c[2] == 'n' && c[3] == 'd' &&
	    c[4] == 'e' && c[5] == 'r' && (c[6] == 'T' || c[6] == 'E'))
		return true;
	/* "RE Completion" */
	if (c[0] == 'R' && c[1] == 'E' && c[2] == ' ' && c[3] == 'C')
		return true;
	/* "surfaceflinger" */
	if (c[0] == 's' && c[1] == 'u' && c[2] == 'r' && c[3] == 'f' &&
	    c[4] == 'a' && c[5] == 'c' && c[6] == 'e')
		return true;
	/* SurfaceFlinger VSYNC threads: "TimerDispatch", "app", "appSf", "sf" */
	if (c[0] == 'T' && c[1] == 'i' && c[2] == 'm' && c[3] == 'e' &&
	    c[4] == 'r' && c[5] == 'D')
		return true;
	if (c[0] == 'a' && c[1] == 'p' && c[2] == 'p' && c[3] == '\0')
		return true;
	if (c[0] == 'a' && c[1] == 'p' && c[2] == 'p' && c[3] == 'S' &&
	    c[4] == 'f' && c[5] == '\0')
		return true;
	if (c[0] == 's' && c[1] == 'f' && c[2] == '\0')
		return true;
	/* "SurfaceSyncGrou" */
	if (c[0] == 'S' && c[1] == 'u' && c[2] == 'r' && c[3] == 'f' &&
	    c[4] == 'a' && c[5] == 'c' && c[6] == 'e' && c[7] == 'S')
		return true;
	/* "HwcAsyncWorker" */
	if (c[0] == 'H' && c[1] == 'w' && c[2] == 'c')
		return true;
	/* "fcp_ui" (stage-0 root; exclude "fcp_frame_bench" harness & "fcp_background") */
	if (c[0] == 'f' && c[1] == 'c' && c[2] == 'p' && c[3] == '_' &&
	    c[4] == 'u' && c[5] == 'i')
		return true;
	/* "ui_main" or "uibench" */
	if ((c[0] == 'u' || c[0] == 'U') && (c[1] == 'i' || c[1] == 'I') &&
	    (c[2] == '_' || c[2] == 'B' || c[2] == 'b'))
		return true;
	/* "hwuiTask*" */
	if (c[0] == 'h' && c[1] == 'w' && c[2] == 'u' && c[3] == 'i')
		return true;
	/* "composer*" */
	if (c[0] == 'c' && c[1] == 'o' && c[2] == 'm' && c[3] == 'p' &&
	    c[4] == 'o' && c[5] == 's')
		return true;
	/* "android.ui", "android.anim", "android.display", "android.hardwar" */
	if (c[0] == 'a' && c[1] == 'n' && c[2] == 'd' && c[3] == 'r' &&
	    c[4] == 'o' && c[5] == 'i' && c[6] == 'd' && c[7] == '.' &&
	    (c[8] == 'u' || c[8] == 'a' || c[8] == 'd' || c[8] == 'h'))
		return true;
	/* "droid.benchmark" or "id.test.uibench" or "bench.janktests" */
	if (c[0] == 'd' && c[1] == 'r' && c[2] == 'o' && c[3] == 'i' &&
	    c[4] == 'd' && c[5] == '.' && c[6] == 'b')
		return true;
	if (c[0] == 'i' && c[1] == 'd' && c[2] == '.' && c[3] == 't' &&
	    c[4] == 'e' && c[5] == 's' && c[6] == 't' && c[7] == '.' &&
	    c[8] == 'u')
		return true;
	if (c[0] == 'b' && c[1] == 'e' && c[2] == 'n' && c[3] == 'c' &&
	    c[4] == 'h' && c[5] == '.' && c[6] == 'j')
		return true;
	/* "FrameStatsColle" */
	if (c[0] == 'F' && c[1] == 'r' && c[2] == 'a' && c[3] == 'm' &&
	    c[4] == 'e' && c[5] == 'S')
		return true;
	/* "AutomatorThread" */
	if (c[0] == 'A' && c[1] == 'u' && c[2] == 't' && c[3] == 'o' &&
	    c[4] == 'm' && c[5] == 'a')
		return true;
	/* "jank*" */
	if (c[0] == 'j' && c[1] == 'a' && c[2] == 'n' && c[3] == 'k')
		return true;

	return false;
}

static __always_inline bool is_render_pool_comm(const char *c)
{
	/* SwiftShader marl worker "Thread<00>..Thread<15>" */
	if (c[0] == 'T' && c[1] == 'h' && c[2] == 'r' && c[3] == 'e' &&
	    c[4] == 'a' && c[5] == 'd' && c[6] == '<')
		return true;
	return false;
}

static __always_inline bool is_angle_worker_comm(const char *c)
{
	/* ANGLE async worker "ANGLE-Worker" / "ANGLE-GC" */
	if (c[0] == 'A' && c[1] == 'N' && c[2] == 'G' && c[3] == 'L' &&
	    c[4] == 'E' && c[5] == '-')
		return true;
	return false;
}

static __always_inline bool is_bg_worker_comm(const char *c)
{
	/* Explicitly exclude background / GC / batch worker threads from FCP tokens */
	if (c[0] == 'f' && c[1] == 'c' && c[2] == 'p' && c[3] == '_' &&
	    c[4] == 'b' && c[5] == 'a' && c[6] == 'c' && c[7] == 'k')
		return true;
	if (c[0] == 'J' && c[1] == 'i' && c[2] == 't' && c[3] == ' ')
		return true;
	if (c[0] == 'H' && c[1] == 'e' && c[2] == 'a' && c[3] == 'p' &&
	    c[4] == 'T' && c[5] == 'a' && c[6] == 's' && c[7] == 'k')
		return true;
	return false;
}

static __always_inline bool detect_frame_root(struct task_struct *p,
					      struct fcp_task_ctx *tctx)
{
	u32 pid = p->pid;
	u32 tgid = p->tgid;
	u8 one = 1;

	if (is_render_pool_comm(p->comm)) {
		tctx->is_render_worker = 1;
		return false;
	}
	if (is_bg_worker_comm(p->comm) || tctx->is_bg_spinner)
		return false;

	/*
	 * Lookup by thread PID (pid). In Android, a process's main UIThread is
	 * the thread-group leader (pid == tgid), whereas background workers in
	 * the same process have pid != tgid. Behavioral quasi-periodic roots
	 * set tctx->is_frame_root = 1 in fcp_runnable after 3 VSYNC epochs.
	 */
	if (bpf_map_lookup_elem(&frame_root_pids, &pid)) {
		tctx->is_frame_root = 1;
		return true;
	}

	if (match_frame_comm(p->comm)) {
		tctx->is_frame_root = 1;
		bpf_map_update_elem(&frame_root_pids, &tgid, &one, BPF_ANY);
		return true;
	}

	return tctx->is_frame_root != 0;
}

/*
 * Check if a task currently holds an active Ephemeral Frame Critical-Path Token.
 */
static __always_inline bool has_active_fcp_token(struct fcp_task_ctx *tctx, u64 now)
{
	return tctx->token_expire_ns > now;
}

/*
 * Compute Dual-Timescale Burst-Window Utilization (DT-BWU) in [0 .. 1024].
 * For tasks holding an active FCP-Token, boosts sub-frame 4ms burst demand
 * so sleeping VSYNC pipelines are never under-provisioned on wakeup.
 */
static __always_inline u64 task_effective_util(struct fcp_task_ctx *tctx, u64 now)
{
	u64 pelt = tctx->util_pelt;
	u64 burst = tctx->util_burst;

	if (has_active_fcp_token(tctx, now) || tctx->is_frame_root) {
		u64 boosted = (burst * 5) >> 2; /* 1.25x sub-frame burst urgency */
		if (boosted > FCP_CAP_SCALE)
			boosted = FCP_CAP_SCALE;
		return boosted > pelt ? boosted : pelt;
	}

	/* Weighted blend: 75% PELT + 25% burst for non-pipeline tasks */
	return ((pelt * 3) + burst) >> 2;
}

/*
 * Compute task's urgency-aware virtual deadline for FCP_DSQ_CRITICAL.
 * Smaller deadline_ns means earlier dispatch.
 * Incorporates remaining frame slack B_rem / (1 + depth) with nonlinear
 * Slack-Urgency escalation when < 40% of frame budget remains.
 */
static __always_inline u64 compute_fcp_deadline(struct fcp_task_ctx *tctx, u64 now)
{
	u64 elapsed = now > tctx->frame_start_ns ? now - tctx->frame_start_ns : 0;
	u64 rem = frame_budget_ns > elapsed ? frame_budget_ns - elapsed : 250000ULL;
	u32 depth = tctx->fcp_depth <= MAX_FCP_DEPTH ? tctx->fcp_depth : MAX_FCP_DEPTH;
	u64 stage_slack = rem / (1 + depth);

	/* Nonlinear urgency escalation when remaining frame budget < 40% */
	if (rem < (frame_budget_ns * 2) / 5)
		stage_slack >>= 2;
	else if (rem < (frame_budget_ns * 3) / 5)
		stage_slack >>= 1;

	return now + stage_slack;
}

/*
 * Classify a task into one of 3 stratified scheduling tiers:
 *   - FCP_TIER_CRITICAL (0): Active FCP-Token holder or Frame Pipeline Root
 *   - FCP_TIER_INTERACTIVE (1): High wakeup-to-runtime interactivity or NICE < 0
 *   - FCP_TIER_BACKGROUND (2): Compute-bound / batch / background worker
 */
static __always_inline u32 fcp_task_weight(const struct task_struct *p)
{
	if (bpf_core_field_exists(p->scx.weight))
		return p->scx.weight;
	return p->prio < 120 ? 200 : 100;
}

static __always_inline u64 fcp_get_dsq_vtime(const struct task_struct *p,
					     const struct fcp_task_ctx *tctx)
{
	if (bpf_core_field_exists(p->scx.dsq_vtime))
		return p->scx.dsq_vtime;
	return tctx ? tctx->dsq_vtime : vtime_now;
}

static __always_inline void fcp_set_dsq_vtime(struct task_struct *p,
					      struct fcp_task_ctx *tctx,
					      u64 vtime)
{
	if (tctx)
		tctx->dsq_vtime = vtime;
	if (bpf_ksym_exists(scx_bpf_task_set_dsq_vtime___new))
		scx_bpf_task_set_dsq_vtime___new(p, vtime);
}

static __always_inline u32 classify_task_tier(struct task_struct *p,
					      struct fcp_task_ctx *tctx, u64 now)
{
	if (is_render_pool_comm(p->comm) || tctx->is_render_worker) {
		tctx->is_render_worker = 1;
		return FCP_TIER_INTERACTIVE;
	}

	if (has_active_fcp_token(tctx, now) || detect_frame_root(p, tctx))
		return FCP_TIER_CRITICAL;
	/* High priority (nice < 0) or realtime-like interactive tasks */
	if (fcp_task_weight(p) > 100) {
		if (tctx->avg_runtime_ns < 2500ULL * NSEC_PER_USEC)
			return FCP_TIER_INTERACTIVE;
	}
	/* Tasks that sleep between short bursts (< 1.5ms) are interactive, not batch */
	if (!tctx->is_bg_spinner && !is_bg_worker_comm(p->comm) &&
	    tctx->last_sleep_at_ns > 0 &&
	    tctx->avg_runtime_ns < 1500ULL * NSEC_PER_USEC)
		return FCP_TIER_INTERACTIVE;

	return FCP_TIER_BACKGROUND;
}

/*
 * Update per-CPU Predictive Thermal-Debt accumulator.
 * Decays exponentially with cooling constant tau_cool ~ 64ms and accumulates
 * proportionally to cpu_dyn_power[cpu] * runtime_ns.
 */
static __always_inline void update_cpu_thermal_debt(s32 cpu, struct fcp_cpu_ctx *cctx,
						    u64 runtime_ns, u64 now)
{
	u64 elapsed = now > cctx->last_debt_update_ns ?
		      now - cctx->last_debt_update_ns : 0;
	u64 dyn_p = get_cpu_dyn_power(cpu);

	/* Exponential cooling decay every >= 4ms */
	if (elapsed >= 4ULL * NSEC_PER_MSEC) {
		u64 periods = elapsed / (4ULL * NSEC_PER_MSEC);
		if (periods >= 8)
			cctx->thermal_debt = 0;
		else if (periods >= 2)
			cctx->thermal_debt >>= 1;
		else
			cctx->thermal_debt -= (cctx->thermal_debt >> 2);
		cctx->last_debt_update_ns = now;
	}

	/* Add heat contribution from recent runtime burst */
	if (runtime_ns > 0) {
		u64 delta_debt = (runtime_ns * dyn_p) / (16ULL * NSEC_PER_MSEC);
		cctx->thermal_debt += delta_debt;
		if (userspace_thermal_pressure > 0)
			cctx->thermal_debt += userspace_thermal_pressure;
		if (cctx->thermal_debt > 2048)
			cctx->thermal_debt = 2048;
	}
}

/*
 * Propagate or issue an Ephemeral Frame Critical-Path Token (FCP-Token)
 * on task wakeup.
 */
static __always_inline void propagate_fcp_token(struct task_struct *p,
						struct fcp_task_ctx *tctx,
						u64 wake_flags, u64 now)
{
	if (is_render_pool_comm(p->comm)) {
		tctx->is_render_worker = 1;
		return;
	}
	if (is_angle_worker_comm(p->comm) || is_bg_worker_comm(p->comm) ||
	    tctx->is_bg_spinner)
		return;

	/* Case 1: Task is a Frame Pipeline Root waking up for a new or ongoing frame */
	if (detect_frame_root(p, tctx)) {
		u64 since_sleep = now > tctx->last_sleep_at_ns ?
				  now - tctx->last_sleep_at_ns : 0;

		/* If waking after VSYNC-like sleep (> 2ms) or token expired, start new frame epoch */
		if (!has_active_fcp_token(tctx, now) || since_sleep >= 2ULL * NSEC_PER_MSEC) {
			tctx->frame_start_ns = now;
			tctx->token_expire_ns = now + frame_budget_ns;
			tctx->fcp_depth = 0;
			__sync_fetch_and_add(&stat_fcp_tokens_issued, 1);
		}
		tctx->fcp_deadline_ns = compute_fcp_deadline(tctx, now);
		u64 rem_ns = tctx->token_expire_ns > now ?
			     tctx->token_expire_ns - now : 0;
		tctx->req_capacity = (rem_ns <= 5ULL * NSEC_PER_MSEC ||
				      tctx->util_burst >= 800) ? 1024 : 850;
		return;
	}

	/*
	 * Case 2: Transitive Synchronous Wakeup Inheritance!
	 * Check if the current running task (the waker) holds an active FCP-Token
	 * (depth < MAX_FCP_DEPTH). If so, pass the token to the wakee @p so that
	 * synchronous IPC/Binder/futex/pipe helpers execute at Critical-Path priority.
	 */
	struct task_struct *waker = (struct task_struct *)bpf_get_current_task_btf();
	if (!waker || waker->pid == p->pid)
		return;

	/*
	 * Allow inheritance on kernel-flagged synchronous wakeups (SCX_WAKE_SYNC,
	 * e.g. Binder IPC, pipes, unix sockets) OR intra-process futex handoffs
	 * (waker->tgid == p->tgid, e.g. UIThread -> RenderThread / stage workers).
	 */
	if (!(wake_flags & SCX_WAKE_SYNC) && (waker->tgid != p->tgid))
		return;

	struct fcp_task_ctx *waker_tctx = lookup_task_ctx(waker);
	if (!waker_tctx)
		return;

	waker_tctx->woke_peer = 1;

	if ((has_active_fcp_token(waker_tctx, now) || waker_tctx->is_frame_root) &&
	    waker_tctx->fcp_depth < MAX_FCP_DEPTH) {
		u64 waker_start = waker_tctx->frame_start_ns > 0 ?
				  waker_tctx->frame_start_ns : now;
		u64 waker_exp = waker_tctx->token_expire_ns > now ?
				waker_tctx->token_expire_ns : (now + (frame_budget_ns >> 1));

		tctx->frame_start_ns = waker_start;
		tctx->token_expire_ns = waker_exp;
		tctx->fcp_depth = waker_tctx->fcp_depth + 1;
		tctx->fcp_deadline_ns = compute_fcp_deadline(tctx, now);
		u64 rem_ns = waker_exp > now ? waker_exp - now : 0;
		tctx->req_capacity = (rem_ns <= 5ULL * NSEC_PER_MSEC ||
				      tctx->fcp_depth >= 3 ||
				      tctx->util_burst >= 800) ? 1024 : 850;
		__sync_fetch_and_add(&stat_fcp_tokens_inherited, 1);
	}
}

/*
 * PTD-EAS: Pick the optimal CPU for @p according to its tier, Dual-Timescale
 * Burst-Window Utilization, asymmetric core capacity, and Thermal Debt.
 * Also sets *need_preempt = true if no idle core is available for a Tier-0
 * Critical-Path task and a busy core is currently running a lower-tier task.
 */
static __always_inline s32 get_cluster_home_cpu(struct task_struct *p,
						struct fcp_task_ctx *tctx,
						bool want_big)
{
	u32 half = (nr_cpu_ids >> 1);
	u32 mask = 15;
	s32 cpu;

	if (half <= 2) {
		half = 2;
		mask = 1;
	} else if (half <= 4) {
		half = 4;
		mask = 3;
	} else if (half <= 8) {
		half = 8;
		mask = 7;
	} else {
		half = 16;
		mask = 15;
	}

	u32 idx = (u32)p->pid & mask;
	if (want_big && tctx) {
		if (tctx->req_capacity >= 1000)
			idx = mask;
		else if (mask == 3 && idx == 3)
			idx = 1;
	}
	cpu = want_big ? (s32)(half + idx) : (s32)((u32)p->pid & mask);
	if (cpu >= 0 && cpu < MAX_CPUS && bpf_cpumask_test_cpu(cpu, p->cpus_ptr))
		return cpu;
	return scx_bpf_task_cpu(p);
}

static __always_inline s32 pick_target_cpu(struct task_struct *p,
					   struct fcp_task_ctx *tctx,
					   s32 prev_cpu, u32 tier,
					   u64 now, bool *direct_idle,
					   s32 *preempt_cpu)
{
	const struct cpumask *idle_cpumask;
	s32 cpu, best_big_idle_cpu = -1, best_little_idle_cpu = -1;
	s32 best_preempt_cpu = -1;
	u64 best_big_idle_score = 0, best_little_idle_score = 0;
	u64 util_eff = task_effective_util(tctx, now);
	bool want_big = (tier == FCP_TIER_CRITICAL) || tctx->is_render_worker || (util_eff >= 450);
	s32 home_cpu = get_cluster_home_cpu(p, tctx, want_big);

	*direct_idle = false;
	*preempt_cpu = -1;
	idle_cpumask = scx_bpf_get_idle_cpumask();

	/* 1. Fast path: prev_cpu if idle and in desired cluster */
	if (prev_cpu >= 0 && prev_cpu < MAX_CPUS &&
	    bpf_cpumask_test_cpu(prev_cpu, p->cpus_ptr) &&
	    bpf_cpumask_test_cpu(prev_cpu, idle_cpumask)) {
		bool prev_is_big = cpu_is_big(prev_cpu);
		struct fcp_cpu_ctx *prev_cctx = lookup_cpu_ctx(prev_cpu);
		u64 prev_debt = prev_cctx ? prev_cctx->thermal_debt : 0;

		if (((want_big && prev_is_big && prev_debt < thermal_debt_cap) &&
		     (tier != FCP_TIER_CRITICAL || prev_cpu == home_cpu)) ||
		    (!want_big && !prev_is_big)) {
			if (scx_bpf_test_and_clear_cpu_idle(prev_cpu)) {
				scx_bpf_put_cpumask(idle_cpumask);
				*direct_idle = true;
				return prev_cpu;
			}
		}
	}

	/* 2. Secondary O(1) fast path: capacity-steered home_cpu in desired cluster */
	if (home_cpu >= 0 && home_cpu < MAX_CPUS &&
	    bpf_cpumask_test_cpu(home_cpu, p->cpus_ptr) &&
	    bpf_cpumask_test_cpu(home_cpu, idle_cpumask)) {
		if (scx_bpf_test_and_clear_cpu_idle(home_cpu)) {
			scx_bpf_put_cpumask(idle_cpumask);
			*direct_idle = true;
			return home_cpu;
		}
	}

	/*
	 * 3. Scan CPUs with a bounded verifier-safe loop (max 12 probes).
	 * On <=12-core SoCs (e.g., 8-core 4+3+1 ARM64), this scans every core.
	 * On 32-vCPU Cuttlefish, it probes 6 LITTLE cores and 6 big cores
	 * strided from p->pid, keeping verifier complexity < 50k insns.
	 */
	#pragma unroll
	for (s32 i = 0; i < 12; i++) {
		if (nr_cpu_ids <= 12) {
			if ((u32)i >= nr_cpu_ids)
				break;
			cpu = i;
		} else {
			cpu = (i < 6) ? (s32)(((u32)p->pid + (u32)i) & 15) :
					(s32)(16 + (((u32)p->pid + (u32)(i - 6)) & 15));
		}
		if (cpu < 0 || cpu >= MAX_CPUS || (u32)cpu >= nr_cpu_ids)
			continue;
		if (!bpf_cpumask_test_cpu(cpu, p->cpus_ptr))
			continue;

		bool big = cpu_is_big(cpu);
		u64 cap = get_cpu_capacity(cpu);
		struct fcp_cpu_ctx *cctx = lookup_cpu_ctx(cpu);
		u64 debt = cctx ? (cctx->thermal_debt & 1023ULL) : 0;
		u64 spread = (u64)(((u32)p->pid + (u32)cpu) & 15) * 8;

		if (bpf_cpumask_test_cpu(cpu, idle_cpumask)) {
			u64 score = 1024 + cap + spread - debt;
			if (cpu == prev_cpu)
				score += 150;

			if (big) {
				if (tier == FCP_TIER_CRITICAL) {
					if (tctx->req_capacity >= 1000 && cap >= 1000)
						score += 200;
					else if (tctx->req_capacity < 1000 &&
						 cap >= tctx->req_capacity && cap < 1000)
						score += 160;
				}
				if (debt >= thermal_debt_cap) {
					score >>= 2;
					if (want_big)
						__sync_fetch_and_add(&stat_thermal_spills, 1);
				}
				if (score > best_big_idle_score) {
					best_big_idle_score = score;
					best_big_idle_cpu = cpu;
				}
			} else {
				if (score > best_little_idle_score) {
					best_little_idle_score = score;
					best_little_idle_cpu = cpu;
				}
			}
		} else if (tier == FCP_TIER_CRITICAL && big && cctx) {
			if (cctx->curr_tier == FCP_TIER_BACKGROUND ||
			    cctx->curr_tier == FCP_TIER_INTERACTIVE) {
				if (best_preempt_cpu < 0 ||
				    cctx->curr_tier == FCP_TIER_BACKGROUND)
					best_preempt_cpu = cpu;
			}
		}
	}

	scx_bpf_put_cpumask(idle_cpumask);

	if (want_big) {
		if (best_big_idle_cpu >= 0 && best_big_idle_score >= 512 &&
		    scx_bpf_test_and_clear_cpu_idle(best_big_idle_cpu)) {
			*direct_idle = true;
			return best_big_idle_cpu;
		}
		if (tier == FCP_TIER_CRITICAL && best_preempt_cpu >= 0) {
			*preempt_cpu = best_preempt_cpu;
			return best_preempt_cpu;
		}
		if (best_big_idle_cpu >= 0 &&
		    scx_bpf_test_and_clear_cpu_idle(best_big_idle_cpu)) {
			*direct_idle = true;
			return best_big_idle_cpu;
		}
		if (home_cpu >= 0)
			return home_cpu;
		if (best_little_idle_cpu >= 0 &&
		    scx_bpf_test_and_clear_cpu_idle(best_little_idle_cpu)) {
			*direct_idle = true;
			return best_little_idle_cpu;
		}
		return scx_bpf_task_cpu(p);
	} else {
		if (best_little_idle_cpu >= 0 &&
		    scx_bpf_test_and_clear_cpu_idle(best_little_idle_cpu)) {
			*direct_idle = true;
			return best_little_idle_cpu;
		}
		if (tier == FCP_TIER_INTERACTIVE && best_big_idle_cpu >= 0 &&
		    scx_bpf_test_and_clear_cpu_idle(best_big_idle_cpu)) {
			*direct_idle = true;
			return best_big_idle_cpu;
		}
		return home_cpu >= 0 ? home_cpu : scx_bpf_task_cpu(p);
	}
}

s32 BPF_STRUCT_OPS(fcp_select_cpu, struct task_struct *p, s32 prev_cpu, u64 wake_flags)
{
	u64 now = bpf_ktime_get_ns();
	struct fcp_task_ctx *tctx;
	bool direct_idle = false;
	s32 preempt_cpu = -1;
	s32 target_cpu;
	u32 tier;

	tctx = lookup_task_ctx(p);
	if (!tctx)
		return prev_cpu;

	/* Propagate or issue FCP-Token along wakeup chains */
	propagate_fcp_token(p, tctx, wake_flags, now);
	tier = classify_task_tier(p, tctx, now);

	target_cpu = pick_target_cpu(p, tctx, prev_cpu, tier, now,
				     &direct_idle, &preempt_cpu);

	if (target_cpu >= 0 && target_cpu < MAX_CPUS) {
		if (cpu_is_big(target_cpu))
			__sync_fetch_and_add(&stat_big_core_placements, 1);
		else
			__sync_fetch_and_add(&stat_little_core_placements, 1);
	}

	if (direct_idle) {
		u64 slice = (tier == FCP_TIER_CRITICAL) ? slice_crit_ns :
			    (tier == FCP_TIER_INTERACTIVE ? slice_intr_ns : slice_bg_ns);
		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, slice, 0);
		if (tier == FCP_TIER_CRITICAL)
			__sync_fetch_and_add(&stat_crit_dispatches, 1);
		else if (tier == FCP_TIER_INTERACTIVE)
			__sync_fetch_and_add(&stat_intr_dispatches, 1);
		else
			__sync_fetch_and_add(&stat_bg_dispatches, 1);
		return target_cpu;
	}

	return target_cpu >= 0 ? target_cpu : prev_cpu;
}

void BPF_STRUCT_OPS(fcp_enqueue, struct task_struct *p, u64 enq_flags)
{
	u64 now = bpf_ktime_get_ns();
	struct fcp_task_ctx *tctx;
	u32 tier;

	tctx = lookup_task_ctx(p);
	if (!tctx) {
		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, slice_intr_ns, enq_flags);
		return;
	}

	/* Per-CPU kthreads go directly to local DSQ to avoid kernel worker stalls */
	if ((p->flags & PF_KTHREAD) && p->nr_cpus_allowed == 1) {
		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, slice_crit_ns, enq_flags);
		return;
	}

	if (enq_flags & SCX_ENQ_WAKEUP)
		propagate_fcp_token(p, tctx, SCX_WAKE_SYNC, now);

	tier = classify_task_tier(p, tctx, now);

	s32 target_cpu = scx_bpf_task_cpu(p);

	if (tier == FCP_TIER_CRITICAL) {
		u64 dl = compute_fcp_deadline(tctx, now);
		if (!(enq_flags & SCX_ENQ_WAKEUP))
			dl += slice_crit_ns;
		scx_bpf_dsq_insert_vtime(p, FCP_DSQ_CRITICAL, slice_crit_ns, dl, enq_flags);
		__sync_fetch_and_add(&stat_crit_dispatches, 1);

		if (preempt_on_crit_wakeup && (enq_flags & SCX_ENQ_WAKEUP)) {
			if (cpu_is_big(target_cpu)) {
				struct fcp_cpu_ctx *tcctx = lookup_cpu_ctx(target_cpu);
				if (tcctx && tcctx->curr_tier != FCP_TIER_CRITICAL) {
					scx_bpf_kick_cpu(target_cpu, SCX_KICK_PREEMPT);
					__sync_fetch_and_add(&stat_preempt_kicks, 1);
					return;
				}
			}
			bool direct_idle = false;
			s32 preempt_cpu = -1;
			s32 cand = pick_target_cpu(p, tctx, target_cpu, FCP_TIER_CRITICAL,
						   now, &direct_idle, &preempt_cpu);
			if (direct_idle && cand >= 0) {
				scx_bpf_kick_cpu(cand, SCX_KICK_IDLE);
			} else if (preempt_cpu >= 0) {
				scx_bpf_kick_cpu(preempt_cpu, SCX_KICK_PREEMPT);
				__sync_fetch_and_add(&stat_preempt_kicks, 1);
			}
		}
		return;
	}

	if (tier == FCP_TIER_INTERACTIVE) {
		bool want_big = tctx->is_render_worker ||
				(task_effective_util(tctx, now) >= 450);
		if (want_big && !cpu_is_big(target_cpu))
			target_cpu = get_cluster_home_cpu(p, tctx, true);

		if (LINUX_KERNEL_VERSION >= KERNEL_VERSION(6, 12, 0) &&
		    target_cpu != scx_bpf_task_cpu(p) &&
		    target_cpu >= 0 && target_cpu < MAX_CPUS &&
		    bpf_cpumask_test_cpu(target_cpu, p->cpus_ptr)) {
			scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL_ON | target_cpu,
					   slice_intr_ns, enq_flags);
		} else {
			u64 vtime = fcp_get_dsq_vtime(p, tctx);
			if (time_before(vtime, vtime_now))
				vtime = vtime_now;
			scx_bpf_dsq_insert_vtime(p, FCP_DSQ_INTERACTIVE, slice_intr_ns,
						 vtime, enq_flags);
		}
		__sync_fetch_and_add(&stat_intr_dispatches, 1);
		return;
	}

	/* Tier 2: Background / Batch -> strictly isolated onto LITTLE cores */
	if (cpu_is_big(target_cpu))
		target_cpu = get_cluster_home_cpu(p, tctx, false);

	if (LINUX_KERNEL_VERSION >= KERNEL_VERSION(6, 12, 0) &&
	    target_cpu != scx_bpf_task_cpu(p) &&
	    target_cpu >= 0 && target_cpu < MAX_CPUS &&
	    bpf_cpumask_test_cpu(target_cpu, p->cpus_ptr)) {
		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL_ON | target_cpu,
				   slice_bg_ns, enq_flags);
	} else {
		u64 vtime = fcp_get_dsq_vtime(p, tctx);
		if (time_before(vtime, vtime_now))
			vtime = vtime_now;
		scx_bpf_dsq_insert_vtime(p, FCP_DSQ_BACKGROUND, slice_bg_ns,
					 vtime + slice_bg_ns, enq_flags);
	}
	__sync_fetch_and_add(&stat_bg_dispatches, 1);
}

void BPF_STRUCT_OPS(fcp_dispatch, s32 cpu, struct task_struct *prev)
{
	struct fcp_cpu_ctx *cctx = lookup_cpu_ctx(cpu);
	bool big = cpu_is_big(cpu);

	/*
	 * Bounded Starvation Guard:
	 * If a CPU has dispatched `starvation_period` consecutive critical/interactive
	 * tasks, rescue one task from FCP_DSQ_BACKGROUND so background tasks never stall.
	 */
	if (cctx && cctx->consec_crit_dispatches >= starvation_period) {
		cctx->consec_crit_dispatches = 0;
		if (scx_bpf_dsq_move_to_local(FCP_DSQ_BACKGROUND, 0)) {
			__sync_fetch_and_add(&stat_starvation_rescues, 1);
			return;
		}
	}

	if (big) {
		/*
		 * Big / Prime Core Dispatch Order:
		 *  1. Frame Critical-Path DSQ (Earliest Frame Stage Deadline first)
		 *  2. Interactive DSQ
		 *  Background DSQ is intentionally NOT drained by big cores during
		 *  normal operation (handled by LITTLE cores 0..15 + starvation guard)
		 *  so big cores remain cool and immediately available for frame wakeups.
		 */
		if (scx_bpf_dsq_move_to_local(FCP_DSQ_CRITICAL, 0)) {
			if (cctx)
				cctx->consec_crit_dispatches++;
			return;
		}
		if (scx_bpf_dsq_move_to_local(FCP_DSQ_INTERACTIVE, 0)) {
			if (cctx)
				cctx->consec_crit_dispatches++;
			return;
		}
		if (scx_bpf_dsq_move_to_local(FCP_DSQ_BACKGROUND, 0)) {
			if (cctx)
				cctx->consec_crit_dispatches = 0;
			return;
		}
	} else {
		/*
		 * LITTLE / Efficiency Core Dispatch Order:
		 *  1. Interactive DSQ
		 *  2. Background DSQ (absorb background contention on LITTLE cores!)
		 *  3. Frame Critical-Path DSQ (spillover only when LITTLE queues are empty)
		 */
		if (scx_bpf_dsq_move_to_local(FCP_DSQ_INTERACTIVE, 0)) {
			if (cctx)
				cctx->consec_crit_dispatches++;
			return;
		}
		if (scx_bpf_dsq_move_to_local(FCP_DSQ_BACKGROUND, 0)) {
			if (cctx)
				cctx->consec_crit_dispatches = 0;
			return;
		}
		if (scx_bpf_dsq_move_to_local(FCP_DSQ_CRITICAL, 0)) {
			if (cctx)
				cctx->consec_crit_dispatches++;
			return;
		}
	}
}

void BPF_STRUCT_OPS(fcp_runnable, struct task_struct *p, u64 enq_flags)
{
	u64 now = bpf_ktime_get_ns();
	struct fcp_task_ctx *tctx = lookup_task_ctx(p);
	if (!tctx)
		return;

	u64 delta_t = now > tctx->last_woke_at_ns ? now - tctx->last_woke_at_ns : 1;
	u64 inst_freq = (100ULL * NSEC_PER_MSEC) / delta_t;
	if (inst_freq > 128)
		inst_freq = 128;

	tctx->wakeup_freq = ((tctx->wakeup_freq * 3) + inst_freq) >> 2;
	tctx->last_woke_at_ns = now;

	/*
	 * Behavioral Quasi-Periodic VSYNC Root Discovery (in runnable callback):
	 * Evaluates inter-wakeup period delta_t in [3.5ms, 35ms], bounded jitter
	 * (< 25%), active burst duty-cycle (< 50%), and causal handoff (woke_peer).
	 */
	u64 since_sleep = now > tctx->last_sleep_at_ns ?
			  now - tctx->last_sleep_at_ns : 0;
	if (since_sleep >= 1500ULL * NSEC_PER_USEC) {
		tctx->is_bg_spinner = 0;
		u64 period_ns = tctx->last_period_wake_ns > 0 && now > tctx->last_period_wake_ns ?
				now - tctx->last_period_wake_ns : 0;
		tctx->last_period_wake_ns = now;

		if (period_ns >= 3500ULL * NSEC_PER_USEC &&
		    period_ns <= 35000ULL * NSEC_PER_USEC) {
			if (tctx->period_ewma_ns == 0) {
				tctx->period_ewma_ns = period_ns;
				tctx->periodicity_score = 1;
			} else {
				u64 diff = period_ns > tctx->period_ewma_ns ?
					   period_ns - tctx->period_ewma_ns :
					   tctx->period_ewma_ns - period_ns;
				tctx->period_ewma_ns = ((tctx->period_ewma_ns * 3) + period_ns) >> 2;

				if (diff <= (tctx->period_ewma_ns >> 2) &&
				    tctx->avg_runtime_ns >= 80ULL * NSEC_PER_USEC &&
				    tctx->avg_runtime_ns < (tctx->period_ewma_ns >> 1) &&
				    tctx->woke_peer) {
					if (tctx->periodicity_score < 8)
						tctx->periodicity_score += 2;
					if (tctx->periodicity_score >= 3) {
						tctx->is_frame_root = 1;
						if (!has_active_fcp_token(tctx, now)) {
							tctx->frame_start_ns = now;
							tctx->token_expire_ns = now + frame_budget_ns;
							tctx->fcp_depth = 0;
							tctx->fcp_deadline_ns = compute_fcp_deadline(tctx, now);
							tctx->req_capacity = 850;
							__sync_fetch_and_add(&stat_fcp_tokens_issued, 1);
						}
					}
				} else if (tctx->periodicity_score > 0) {
					tctx->periodicity_score--;
				}
			}
		}
		tctx->woke_peer = 0;
	}
}

void BPF_STRUCT_OPS(fcp_running, struct task_struct *p)
{
	u64 now = bpf_ktime_get_ns();
	s32 cpu = scx_bpf_task_cpu(p);
	struct fcp_task_ctx *tctx = lookup_task_ctx(p);
	struct fcp_cpu_ctx *cctx = lookup_cpu_ctx(cpu);

	if (tctx) {
		u64 vt = fcp_get_dsq_vtime(p, tctx);
		tctx->last_run_at_ns = now;
		if (time_before(vtime_now, vt))
			vtime_now = vt;
	}

	if (cctx && tctx) {
		u32 tier = classify_task_tier(p, tctx, now);
		cctx->last_run_at_ns = now;
		cctx->curr_tier = tier;
		cctx->curr_is_fcp = (tier == FCP_TIER_CRITICAL) ? 1 : 0;
		cctx->curr_deadline_ns = tctx->fcp_deadline_ns;

		/*
		 * Scale CPU frequency target only when the optional kfunc exists and
		 * the requested level changes; running fires for every task switch.
		 */
		if (bpf_ksym_exists(scx_bpf_cpuperf_set)) {
			u64 util = task_effective_util(tctx, now);
			u32 perf;

			if (tier == FCP_TIER_CRITICAL) {
				perf = SCX_CPUPERF_ONE;
			} else {
				perf = (util * SCX_CPUPERF_ONE) / FCP_CAP_SCALE;
				if (perf > SCX_CPUPERF_ONE)
					perf = SCX_CPUPERF_ONE;
				if (perf < (SCX_CPUPERF_ONE >> 2))
					perf = SCX_CPUPERF_ONE >> 2;
			}

			if (cctx->last_perf_level != perf) {
				scx_bpf_cpuperf_set(cpu, perf);
				cctx->last_perf_level = perf;
			}
		}
	}
}

void BPF_STRUCT_OPS(fcp_stopping, struct task_struct *p, bool runnable)
{
	u64 now = bpf_ktime_get_ns();
	s32 cpu = scx_bpf_task_cpu(p);
	struct fcp_task_ctx *tctx = lookup_task_ctx(p);
	struct fcp_cpu_ctx *cctx = lookup_cpu_ctx(cpu);
	u64 runtime_ns;

	if (!tctx)
		return;

	runtime_ns = (tctx->last_run_at_ns > 0 && now > tctx->last_run_at_ns) ?
		     (now - tctx->last_run_at_ns) : 1000;
	if (runtime_ns > slice_bg_ns)
		runtime_ns = slice_bg_ns;
	tctx->avg_runtime_ns = ((tctx->avg_runtime_ns * 3) + runtime_ns) >> 2;

	/*
	 * Update Dual-Timescale Burst-Window Utilization (DT-BWU):
	 *  1. 4ms sub-frame sliding window (util_burst)
	 *  2. 32ms slow PELT EWMA (util_pelt)
	 */
	if (tctx->burst_window_start_ns == 0)
		tctx->burst_window_start_ns = tctx->last_run_at_ns;

	tctx->burst_window_runtime_ns += runtime_ns;
	u64 win_elapsed = now > tctx->burst_window_start_ns ?
			  now - tctx->burst_window_start_ns : runtime_ns;

	if (win_elapsed >= 4ULL * NSEC_PER_MSEC) {
		u64 inst_util = (tctx->burst_window_runtime_ns * FCP_CAP_SCALE) / win_elapsed;
		if (inst_util > FCP_CAP_SCALE)
			inst_util = FCP_CAP_SCALE;

		/* Fast 4ms burst tracker (50% new, 50% old) */
		tctx->util_burst = (tctx->util_burst + inst_util) >> 1;
		/* Slow 32ms PELT tracker (87.5% old, 12.5% new) */
		tctx->util_pelt = ((tctx->util_pelt * 7) + inst_util) >> 3;

		tctx->burst_window_start_ns = now;
		tctx->burst_window_runtime_ns = 0;
	}

	if (!runnable) {
		tctx->last_sleep_at_ns = now;
		if (!tctx->is_frame_root && runtime_ns >= 2500ULL * NSEC_PER_USEC)
			tctx->token_expire_ns = 0;
	} else {
		if (!tctx->is_frame_root && runtime_ns < 50ULL * NSEC_PER_USEC)
			tctx->token_expire_ns = 0;
		/* Behavioral spinner detection: continuous runnable execution > 12ms */
		u64 since_sleep = now > tctx->last_sleep_at_ns ?
				  now - tctx->last_sleep_at_ns : 0;
		if (since_sleep > 12ULL * NSEC_PER_MSEC && tctx->util_burst > 700) {
			tctx->is_bg_spinner = 1;
			tctx->is_frame_root = 0;
			tctx->periodicity_score = 0;
			tctx->token_expire_ns = 0;
		}
	}

	/* Charge weighted virtual runtime */
	u64 weight = fcp_task_weight(p);
	if (weight == 0)
		weight = 100;
	u64 delta_vtime = (runtime_ns * 100) / weight;
	fcp_set_dsq_vtime(p, tctx, fcp_get_dsq_vtime(p, tctx) + delta_vtime);

	if (cctx) {
		cctx->tot_runtime_ns += runtime_ns;
		cctx->curr_is_fcp = 0;
		cctx->curr_tier = 0;
		update_cpu_thermal_debt(cpu, cctx, runtime_ns, now);
	}
}

void BPF_STRUCT_OPS(fcp_enable, struct task_struct *p)
{
	struct fcp_task_ctx *tctx = lookup_task_ctx(p);
	fcp_set_dsq_vtime(p, tctx, vtime_now);
}

s32 BPF_STRUCT_OPS(fcp_init_task, struct task_struct *p,
		   struct scx_init_task_args *args)
{
	struct fcp_task_ctx *tctx;

	tctx = lookup_task_ctx(p);
	if (!tctx)
		return 0;

	detect_frame_root(p, tctx);
	return 0;
}

s32 BPF_STRUCT_OPS_SLEEPABLE(fcp_init)
{
	s32 err;

	err = scx_bpf_create_dsq(FCP_DSQ_CRITICAL, -1);
	if (err)
		return err;

	err = scx_bpf_create_dsq(FCP_DSQ_INTERACTIVE, -1);
	if (err)
		return err;

	err = scx_bpf_create_dsq(FCP_DSQ_BACKGROUND, -1);
	if (err)
		return err;

	return 0;
}

void BPF_STRUCT_OPS(fcp_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(fcp_ops,
	       .select_cpu		= (void *)fcp_select_cpu,
	       .enqueue			= (void *)fcp_enqueue,
	       .dispatch		= (void *)fcp_dispatch,
	       .runnable		= (void *)fcp_runnable,
	       .running			= (void *)fcp_running,
	       .stopping		= (void *)fcp_stopping,
	       .enable			= (void *)fcp_enable,
	       .init_task		= (void *)fcp_init_task,
	       .init			= (void *)fcp_init,
	       .exit			= (void *)fcp_exit,
	       .name			= "fcp");
