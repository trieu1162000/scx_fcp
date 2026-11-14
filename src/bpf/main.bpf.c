/* SPDX-License-Identifier: GPL-2.0 */
/*
 * scx_fcp focus build: behavioral frame-root admission and bounded
 * critical-path token propagation for heterogeneous Android SoCs.
 *
 * The zero-hint build admits roots only from observed wakeup behavior. The
 * named build may additionally use the explicit PID map and the legacy comm
 * allowlist. In both builds the root unit is the task (TID), not the TGID.
 */
#include <scx/common.bpf.h>
#include "intf.h"

char _license[] SEC("license") = "GPL";

const volatile u32 nr_cpu_ids = 4;
const volatile u64 frame_budget_ns = 16666667ULL;      /* 60Hz = 16.67ms (8.33ms for 120Hz) */
const volatile u64 slice_crit_ns = 2000000ULL;         /* 2ms fast slice for critical path */
const volatile u64 slice_intr_ns = 3000000ULL;         /* 3ms slice for interactive tasks */
const volatile u64 slice_bg_ns = 6000000ULL;           /* 6ms slice for background tasks */
const volatile u32 starvation_period = 128;            /* Rescue 1 bg task per N dispatches */
const volatile bool preempt_on_crit_wakeup = true;

const volatile u64 cpu_capacity[MAX_CPUS];
const volatile u8 is_big_core[MAX_CPUS];

volatile u64 vtime_now;

/* Global statistics counters. The performance build is counters only. */
volatile u64 stat_crit_dispatches;
volatile u64 stat_intr_dispatches;
volatile u64 stat_bg_dispatches;
volatile u64 stat_fcp_tokens_issued;
volatile u64 stat_fcp_tokens_inherited;
volatile u64 stat_preempt_kicks;
volatile u64 stat_big_core_placements;
volatile u64 stat_little_core_placements;
volatile u64 stat_starvation_rescues;

volatile u64 stat_root_src_behavior;
volatile u64 stat_root_src_comm;
volatile u64 stat_root_src_pid;
volatile u64 stat_root_promotions;
volatile u64 stat_root_demotions;
volatile u64 stat_root_downstream_suppressed;

volatile u64 stat_token_select_calls;
volatile u64 stat_token_enqueue_calls;
volatile u64 stat_edges_eligible;
volatile u64 stat_edges_inherited;
volatile u64 stat_edges_sync_flag;
volatile u64 stat_edges_same_tgid;
volatile u64 stat_edges_async_rejected;
volatile u64 stat_depth_regress_blocked;
volatile u64 stat_token_revocations;
volatile u64 stat_token_expiry;
volatile u64 stat_first_big_hits;
volatile u64 stat_first_big_misses;
volatile u64 stat_depth_hist[MAX_FCP_DEPTH + 1];

/* Tuning counters. They are zero when the corresponding policy is inactive. */
volatile u64 stat_root_cooldown_blocks;
volatile u64 stat_edges_budget_rejected;
volatile u64 stat_edges_tgid_window_rejected;
volatile u64 stat_preempt_kick_capped;

#define FCP_ROOT_SLEEP_GATE_NS		(1200ULL * NSEC_PER_USEC)
#define FCP_ROOT_PERIOD_MIN_NS		(3500ULL * NSEC_PER_USEC)
#define FCP_ROOT_PERIOD_MAX_NS		(85000ULL * NSEC_PER_USEC)
#define FCP_ROOT_MIN_RUNTIME_NS		(80ULL * NSEC_PER_USEC)
#define FCP_ROOT_PROMOTE_SCORE		2
#define FCP_ROOT_MAX_SCORE		10
#define FCP_ROOT_COOLDOWN_NS		(50ULL * NSEC_PER_MSEC)
#define FCP_INHERIT_MAX_DEPTH		MAX_FCP_DEPTH
#define FCP_SAME_TGID_WINDOW_NS		(3500ULL * NSEC_PER_USEC)
#define FCP_REVOKE_SERVICE_NS		(4000ULL * NSEC_PER_USEC)
#define FCP_SHORT_YIELD_NS		(50ULL * NSEC_PER_USEC)

enum fcp_root_source {
	FCP_ROOT_SRC_NONE	= 0,
	FCP_ROOT_SRC_BEHAVIOR	= 1,
	FCP_ROOT_SRC_COMM	= 2,
	FCP_ROOT_SRC_PID	= 3,
};

struct fcp_task_ctx {
	u64 last_run_at_ns;
	u64 last_sleep_at_ns;
	u64 last_woke_at_ns;
	u64 avg_runtime_ns;
	u64 period_ewma_ns;
	u64 last_period_wake_ns;
	u64 frame_start_ns;
	u64 token_expire_ns;
	u64 fcp_deadline_ns;
	u64 demote_cooldown_ns;
	u64 dsq_vtime;
	u64 token_service_ns;
	u32 fcp_depth;
	u32 tid;
	u32 token_epoch;
	u8 is_frame_root;
	u8 inherited_token;
	u8 last_stop_was_block;
	u8 woke_peer;
	u8 periodicity_score;
	u8 missed_periods;
	u8 first_big_checked;
	u8 root_source;
};

struct {
	__uint(type, BPF_MAP_TYPE_TASK_STORAGE);
	__uint(map_flags, BPF_F_NO_PREALLOC);
	__type(key, int);
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

static __always_inline struct fcp_cpu_ctx *lookup_cpu_ctx(s32 cpu)
{
	u32 zero = 0;

	if (cpu < 0)
		return bpf_map_lookup_elem(&cpu_ctx_stor, &zero);
	return bpf_map_lookup_percpu_elem(&cpu_ctx_stor, &zero, cpu);
}

static __always_inline struct fcp_task_ctx *lookup_task_ctx(struct task_struct *p)
{
	return bpf_task_storage_get(&task_ctx_stor, p, 0, 0);
}

static __always_inline struct fcp_task_ctx *create_task_ctx(struct task_struct *p)
{
	return bpf_task_storage_get(&task_ctx_stor, p, 0,
				    BPF_LOCAL_STORAGE_GET_F_CREATE);
}

#ifdef SCX_FCP_ZERO_HINT
#define match_frame_comm(c) false
#define is_render_pool_comm(c) false
#define is_angle_worker_comm(c) false
#define is_bg_worker_comm(c) false
#else
/*
 * Legacy named hints. These strings are compiled out of the zero-hint build.
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
	/* "fcp_ui" (excluding the "fcp_frame_bench" harness and "fcp_background") */
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
	/* Explicitly exclude background / GC / batch worker threads */
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
#endif

static __always_inline bool fcp_root_read(const struct fcp_task_ctx *tctx)
{
	return tctx->is_frame_root != 0;
}

static __always_inline bool fcp_token_active(const struct fcp_task_ctx *tctx,
					     u64 now)
{
	return tctx->token_expire_ns > now;
}

static __always_inline void fcp_depth_hist_inc(u32 depth)
{
	switch (depth) {
	case 0:
		stat_depth_hist[0]++;
		break;
	case 1:
		stat_depth_hist[1]++;
		break;
	case 2:
		stat_depth_hist[2]++;
		break;
	case 3:
		stat_depth_hist[3]++;
		break;
	case 4:
		stat_depth_hist[4]++;
		break;
	case MAX_FCP_DEPTH:
		stat_depth_hist[MAX_FCP_DEPTH]++;
		break;
	default:
		break;
	}
}

static __always_inline void fcp_token_clear(struct fcp_task_ctx *tctx)
{
	tctx->token_expire_ns = 0;
	tctx->fcp_depth = 0;
	tctx->token_service_ns = 0;
	tctx->token_epoch = 0;
	tctx->inherited_token = 0;
	tctx->first_big_checked = 0;
}

static __always_inline void fcp_token_revoke(struct fcp_task_ctx *tctx)
{
	if (tctx->token_expire_ns) {
		fcp_token_clear(tctx);
		stat_token_revocations++;
	}
}

static __always_inline void fcp_token_expire(struct fcp_task_ctx *tctx, u64 now)
{
	if (tctx->token_expire_ns && tctx->token_expire_ns <= now) {
		fcp_token_clear(tctx);
		stat_token_expiry++;
	}
}

static __always_inline void fcp_root_promote(struct fcp_task_ctx *tctx,
					     u8 source)
{
	if (tctx->is_frame_root && tctx->root_source == source)
		return;

	tctx->is_frame_root = 1;
	tctx->root_source = source;
	tctx->inherited_token = 0;
	tctx->first_big_checked = 0;
	tctx->missed_periods = 0;
	stat_root_promotions++;

	if (source == FCP_ROOT_SRC_BEHAVIOR)
		stat_root_src_behavior++;
	else if (source == FCP_ROOT_SRC_COMM)
		stat_root_src_comm++;
	else if (source == FCP_ROOT_SRC_PID)
		stat_root_src_pid++;
}

static __always_inline void fcp_root_demote(struct fcp_task_ctx *tctx, u64 now)
{
	if (!tctx->is_frame_root)
		return;
	/* Named roots are explicit policy and are not behaviorally demoted. */
	if (tctx->root_source != FCP_ROOT_SRC_BEHAVIOR)
		return;

	tctx->is_frame_root = 0;
	tctx->root_source = FCP_ROOT_SRC_NONE;
	tctx->periodicity_score = 0;
	tctx->missed_periods = 0;
	tctx->inherited_token = 0;
	tctx->demote_cooldown_ns = now + FCP_ROOT_COOLDOWN_NS;
	fcp_token_revoke(tctx);
	stat_root_demotions++;
}

/*
 * Behavioral admission. This function reads only task-local history. It is
 * called from the single wake step after inheritance is attempted, so a task
 * that just inherited a token cannot also promote in the same epoch.
 */
static __always_inline void fcp_root_admit_behavior(struct fcp_task_ctx *tctx,
						    u64 now)
{
	u64 since_sleep, period_ns, old_ewma;
	bool qualify = false;

	if (!tctx->last_stop_was_block)
		return;

	since_sleep = (tctx->last_sleep_at_ns > 0 && now > tctx->last_sleep_at_ns) ?
		      now - tctx->last_sleep_at_ns : 0;
	if (since_sleep < FCP_ROOT_SLEEP_GATE_NS)
		return;

	if (!tctx->last_period_wake_ns) {
		tctx->last_period_wake_ns = now;
		tctx->woke_peer = 0;
		return;
	}

	period_ns = now - tctx->last_period_wake_ns;
	/* Sub-frame wakeups (< 3.5ms) are intra-frame blocks, not new frame epochs. */
	if (period_ns < FCP_ROOT_PERIOD_MIN_NS)
		return;

	tctx->last_period_wake_ns = now;
	old_ewma = tctx->period_ewma_ns;

	if (period_ns > FCP_ROOT_PERIOD_MAX_NS) {
		/* Idle pause between user gestures: keep root status without cooldown. */
		tctx->missed_periods = 0;
		tctx->woke_peer = 0;
		return;
	}
	if (period_ns >= FCP_ROOT_PERIOD_MIN_NS &&
	    period_ns <= FCP_ROOT_PERIOD_MAX_NS) {
		qualify = tctx->avg_runtime_ns >= FCP_ROOT_MIN_RUNTIME_NS &&
			  tctx->avg_runtime_ns <= ((period_ns * 3) >> 2) &&
			  tctx->woke_peer;
		tctx->period_ewma_ns = old_ewma ?
			(((old_ewma * 3) + period_ns) >> 2) : period_ns;
	}

	if (qualify) {
		if (now < tctx->demote_cooldown_ns) {
			/*
			 * A demoted task must not rebuild its score during the
			 * cooldown, otherwise it re-promotes on the first wake
			 * after the timer expires.
			 */
			stat_root_cooldown_blocks++;
		} else if (tctx->periodicity_score <= FCP_ROOT_MAX_SCORE - 2) {
			tctx->periodicity_score += 2;
		} else {
			tctx->periodicity_score = FCP_ROOT_MAX_SCORE;
		}
	} else if (tctx->periodicity_score > 0) {
		tctx->periodicity_score--;
	}

	/*
	 * Demotion is driven by two consecutive missed periods only. The old
	 * score-zero demotion path was a second, much faster demotion source
	 * and produced most of the root-set churn.
	 */
	if (!tctx->is_frame_root && now >= tctx->demote_cooldown_ns &&
	    tctx->periodicity_score >= FCP_ROOT_PROMOTE_SCORE)
		fcp_root_promote(tctx, FCP_ROOT_SRC_BEHAVIOR);

	tctx->woke_peer = 0;
}

static __always_inline void fcp_root_admit(struct task_struct *p,
					   struct fcp_task_ctx *tctx,
					   u64 now)
{
	struct task_struct *waker;
	bool internal_peer_wake;

	/* Downstream suppression applies to every build and every source. */
	if (tctx->inherited_token && fcp_token_active(tctx, now)) {
		stat_root_downstream_suppressed++;
		tctx->woke_peer = 0;
		return;
	}

#ifndef SCX_FCP_ZERO_HINT
	u32 pid = p->pid;

	if (tctx->is_frame_root &&
	    tctx->root_source != FCP_ROOT_SRC_BEHAVIOR)
		return;

	if (bpf_map_lookup_elem(&frame_root_pids, &pid)) {
		fcp_root_promote(tctx, FCP_ROOT_SRC_PID);
		return;
	}

	if (match_frame_comm(p->comm)) {
		u32 tgid = p->tgid;
		u8 one = 1;

		fcp_root_promote(tctx, FCP_ROOT_SRC_COMM);
		bpf_map_update_elem(&frame_root_pids, &tgid, &one, BPF_ANY);
		return;
	}
#endif

	/*
	 * DAG source invariant: a thread woken by a non-leader peer in the
	 * same process (e.g. RenderThread waking Thread<00..15> pool workers)
	 * is an internal node (d >= 1), not a root (d = 0).
	 */
	waker = (struct task_struct *)bpf_get_current_task_btf();
	internal_peer_wake = waker && waker->pid != p->pid &&
			     waker->tgid == p->tgid &&
			     waker->pid != waker->tgid &&
			     p->pid != p->tgid;
	if (internal_peer_wake) {
		tctx->woke_peer = 0;
		return;
	}

	fcp_root_admit_behavior(tctx, now);
}

/*
 * Compute the token deadline from the absolute frame start carried across
 * inheritance. It never extends token expiry.
 */
static __always_inline u64 compute_fcp_deadline(struct fcp_task_ctx *tctx,
						u64 now)
{
	u64 elapsed = now > tctx->frame_start_ns ? now - tctx->frame_start_ns : 0;
	u64 rem = frame_budget_ns > elapsed ? frame_budget_ns - elapsed : 250000ULL;
	u32 depth = tctx->fcp_depth <= MAX_FCP_DEPTH ? tctx->fcp_depth : MAX_FCP_DEPTH;
	u64 stage_slack = rem / (1 + depth);

	if (rem < (frame_budget_ns * 2) / 5)
		stage_slack >>= 2;
	else if (rem < (frame_budget_ns * 3) / 5)
		stage_slack >>= 1;

	return now + stage_slack;
}

/*
 * Inheritance. The gate accepts a kernel synchronous wakeup or an intra
 * process handoff. The waker must still hold an unexpired token. The child
 * inherits the absolute expiry and never lowers its existing depth.
 */
static __always_inline void fcp_token_try_inherit(struct task_struct *p,
						  struct fcp_task_ctx *tctx,
						  u64 wake_flags, u64 now,
						  bool allow_inherit)
{
	struct task_struct *waker = (struct task_struct *)bpf_get_current_task_btf();
	struct fcp_task_ctx *wctx;
	bool sync_wake, same_tgid;
	u32 new_depth;
	u64 expiry;

	if (!waker || waker->pid == p->pid || (waker->flags & PF_KTHREAD))
		return;

#ifndef SCX_FCP_ZERO_HINT
	if (is_render_pool_comm(p->comm) || is_angle_worker_comm(p->comm) ||
	    is_bg_worker_comm(p->comm))
		return;
#endif

	sync_wake = (wake_flags & SCX_WAKE_SYNC) != 0;
	same_tgid = (waker->tgid == p->tgid);

	if (!sync_wake && !same_tgid) {
		stat_edges_async_rejected++;
		return;
	}

	if (sync_wake)
		stat_edges_sync_flag++;
	else
		stat_edges_same_tgid++;

	wctx = lookup_task_ctx(waker);
	if (!wctx)
		return;

	/*
	 * Same-TGID edges are trusted when the waker ran within the stage
	 * window (<= 3.5ms) and woke from a real block within the last 10ms
	 * (excluding continuous CPU-bound spinners).
	 */
	/* Record causal peer wake for any non-spinner waker (slept within 50ms). */
	if (wctx->last_sleep_at_ns > 0 && now >= wctx->last_sleep_at_ns &&
	    now - wctx->last_sleep_at_ns <= 50ULL * NSEC_PER_MSEC) {
		if (wctx->woke_peer < 255)
			wctx->woke_peer++;
	}

	if (!sync_wake &&
	    (!wctx->last_run_at_ns || now < wctx->last_run_at_ns ||
	     now - wctx->last_run_at_ns > FCP_SAME_TGID_WINDOW_NS ||
	     !wctx->last_woke_at_ns || now < wctx->last_woke_at_ns ||
	     now - wctx->last_woke_at_ns > 15ULL * NSEC_PER_MSEC)) {
		stat_edges_tgid_window_rejected++;
		return;
	}

	if (!allow_inherit)
		return;

	/* Cap same-TGID fan-out per burst to 1 sequential stage handoff. */
	if (!sync_wake && wctx->woke_peer > 1) {
		stat_edges_budget_rejected++;
		return;
	}
	expiry = wctx->token_expire_ns;
	if (wctx->frame_start_ns > 0) {
		u64 frame_expiry = wctx->frame_start_ns + frame_budget_ns;

		if (frame_expiry < expiry)
			expiry = frame_expiry;
	}
	if (expiry <= now)
		return;

	stat_edges_eligible++;

	if (wctx->fcp_depth >= FCP_INHERIT_MAX_DEPTH)
		return;

	new_depth = wctx->fcp_depth + 1;

	if (fcp_token_active(tctx, now) &&
	    tctx->token_epoch == wctx->token_epoch) {
		if (new_depth <= tctx->fcp_depth) {
			stat_depth_regress_blocked++;
			return;
		}
		if (expiry < tctx->token_expire_ns)
			tctx->token_expire_ns = expiry;
		tctx->fcp_depth = new_depth;
	} else {
		tctx->frame_start_ns = wctx->frame_start_ns;
		tctx->token_expire_ns = expiry;
		tctx->fcp_depth = new_depth;
		tctx->token_service_ns = 0;
		tctx->token_epoch = wctx->token_epoch;
	}

	tctx->inherited_token = 1;
	tctx->first_big_checked = 0;
	tctx->fcp_deadline_ns = compute_fcp_deadline(tctx, now);
	stat_fcp_tokens_inherited++;
	stat_edges_inherited++;
	fcp_depth_hist_inc(new_depth);
}

/*
 * Root token issue. A new epoch starts after a real inter-frame block, never
 * after a preemption. Within an ongoing frame, only the deadline is refreshed.
 */
static __always_inline void fcp_token_root_step(struct fcp_task_ctx *tctx,
						u64 now)
{
	bool new_frame_sleep = (tctx->token_epoch == 0) ||
			       (tctx->last_stop_was_block &&
				tctx->last_sleep_at_ns > 0 &&
				now > tctx->last_sleep_at_ns &&
				now - tctx->last_sleep_at_ns >= FCP_ROOT_SLEEP_GATE_NS);

	if (!new_frame_sleep) {
		if (fcp_token_active(tctx, now))
			tctx->fcp_deadline_ns = compute_fcp_deadline(tctx, now);
		return;
	}

	tctx->frame_start_ns = now;
	tctx->token_expire_ns = now + frame_budget_ns;
	tctx->fcp_depth = 0;
	tctx->token_service_ns = 0;
	tctx->token_epoch++;
	tctx->inherited_token = 0;
	tctx->first_big_checked = 0;
	tctx->fcp_deadline_ns = compute_fcp_deadline(tctx, now);
	stat_fcp_tokens_issued++;
}

/*
 * Single token step. It is called exactly once, from select_cpu, with the real
 * wake flags. enqueue no longer calls it and no longer fabricates
 * SCX_WAKE_SYNC.
 */
static __always_inline void fcp_token_step(struct task_struct *p,
					   struct fcp_task_ctx *tctx,
					   u64 wake_flags, u64 now)
{
	bool was_root;

	stat_token_select_calls++;

	fcp_token_expire(tctx, now);
	was_root = fcp_root_read(tctx);

	fcp_token_try_inherit(p, tctx, wake_flags, now, !was_root);
	fcp_root_admit(p, tctx, now);
	tctx->woke_peer = 0;

	if (fcp_root_read(tctx))
		fcp_token_root_step(tctx, now);

	tctx->last_woke_at_ns = now;
}

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

/*
 * Stratified tiers are unchanged. Thermal debt, DT-BWU and capacity probing
 * are not consulted on the focus decision path.
 */
static __always_inline u32 classify_task_tier(struct task_struct *p,
					      struct fcp_task_ctx *tctx,
					      u64 now)
{
	if (fcp_token_active(tctx, now))
		return FCP_TIER_CRITICAL;

#ifndef SCX_FCP_ZERO_HINT
	if (is_render_pool_comm(p->comm) || is_angle_worker_comm(p->comm))
		return FCP_TIER_INTERACTIVE;
	if (is_bg_worker_comm(p->comm))
		return FCP_TIER_BACKGROUND;
#endif

	if (fcp_task_weight(p) > 100 &&
	    tctx->avg_runtime_ns < 2500ULL * NSEC_PER_USEC)
		return FCP_TIER_INTERACTIVE;

	if (tctx->last_woke_at_ns > 0 && now >= tctx->last_woke_at_ns &&
	    (now - tctx->last_woke_at_ns) <= 10ULL * NSEC_PER_MSEC &&
	    tctx->avg_runtime_ns < 3000ULL * NSEC_PER_USEC)
		return FCP_TIER_INTERACTIVE;
	return FCP_TIER_BACKGROUND;
}

static __always_inline s32 get_cluster_home_cpu(struct task_struct *p,
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
	cpu = want_big ? (s32)(half + idx) : (s32)((u32)p->pid & mask);
	if (cpu < 0 || (u32)cpu >= nr_cpu_ids)
		cpu = want_big ? (s32)(nr_cpu_ids - 1) : 0;
	if (cpu >= 0 && cpu < MAX_CPUS && bpf_cpumask_test_cpu(cpu, p->cpus_ptr))
		return cpu;
	return scx_bpf_task_cpu(p);
}

static __always_inline s32 pick_target_cpu(struct task_struct *p,
					   struct fcp_task_ctx *tctx,
					   s32 prev_cpu, u32 tier,
					   bool *direct_idle,
					   s32 *preempt_cpu)
{
	const struct cpumask *idle_cpumask;
	s32 cpu, best_big_idle_cpu = -1, best_little_idle_cpu = -1;
	s32 best_preempt_cpu = -1;
	u64 best_big_idle_score = 0, best_little_idle_score = 0;
	bool want_big = (tier == FCP_TIER_CRITICAL);
	s32 home_cpu = get_cluster_home_cpu(p, want_big);

	*direct_idle = false;
	*preempt_cpu = -1;
	idle_cpumask = scx_bpf_get_idle_cpumask();

	/* 1. Previous CPU fast path, but critical tasks always prefer big. */
	if (prev_cpu >= 0 && prev_cpu < MAX_CPUS &&
	    bpf_cpumask_test_cpu(prev_cpu, p->cpus_ptr) &&
	    bpf_cpumask_test_cpu(prev_cpu, idle_cpumask)) {
		bool prev_is_big = cpu_is_big(prev_cpu);

		if ((want_big && prev_is_big) || (!want_big && !prev_is_big)) {
			if (scx_bpf_test_and_clear_cpu_idle(prev_cpu)) {
				scx_bpf_put_cpumask(idle_cpumask);
				*direct_idle = true;
				return prev_cpu;
			}
		}
	}

	/* 2. Capacity-steered home CPU in the desired cluster. */
	if (home_cpu >= 0 && home_cpu < MAX_CPUS &&
	    bpf_cpumask_test_cpu(home_cpu, p->cpus_ptr) &&
	    bpf_cpumask_test_cpu(home_cpu, idle_cpumask) &&
	    (!want_big || cpu_is_big(home_cpu))) {
		if (scx_bpf_test_and_clear_cpu_idle(home_cpu)) {
			scx_bpf_put_cpumask(idle_cpumask);
			*direct_idle = true;
			return home_cpu;
		}
	}

	/*
	 * 3. Bounded scan. Crucial path tasks take a big idle core first, then
	 * a big preemption target. No thermal or utilization term is used.
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
		u64 spread = (u64)(((u32)p->pid + (u32)cpu) & 15) * 8;

		if (bpf_cpumask_test_cpu(cpu, idle_cpumask)) {
			u64 score = 1024 + cap + spread;

			if (cpu == prev_cpu)
				score += 150;
			if (big) {
				if (want_big)
					score += 200;
				if (score > best_big_idle_score) {
					best_big_idle_score = score;
					best_big_idle_cpu = cpu;
				}
			} else if (score > best_little_idle_score) {
				best_little_idle_score = score;
				best_little_idle_cpu = cpu;
			}
		} else if (tier == FCP_TIER_CRITICAL && big) {
			struct fcp_cpu_ctx *cctx = lookup_cpu_ctx(cpu);

			if (cctx && cctx->curr_tier != FCP_TIER_CRITICAL &&
			    cctx->curr_tier != FCP_TIER_NONE) {
				if (best_preempt_cpu < 0 ||
				    cctx->curr_tier == FCP_TIER_BACKGROUND)
					best_preempt_cpu = cpu;
			}
		}
	}

	scx_bpf_put_cpumask(idle_cpumask);

	if (want_big) {
		if (best_big_idle_cpu >= 0 &&
		    scx_bpf_test_and_clear_cpu_idle(best_big_idle_cpu)) {
			*direct_idle = true;
			return best_big_idle_cpu;
		}
		if (tier == FCP_TIER_CRITICAL && best_preempt_cpu >= 0) {
			*preempt_cpu = best_preempt_cpu;
			return best_preempt_cpu;
		}
		if (home_cpu >= 0)
			return home_cpu;
		if (best_little_idle_cpu >= 0 &&
		    scx_bpf_test_and_clear_cpu_idle(best_little_idle_cpu)) {
			*direct_idle = true;
			return best_little_idle_cpu;
		}
		return scx_bpf_task_cpu(p);
	}

	if (tier == FCP_TIER_INTERACTIVE && best_big_idle_cpu >= 0 &&
	    scx_bpf_test_and_clear_cpu_idle(best_big_idle_cpu)) {
		*direct_idle = true;
		return best_big_idle_cpu;
	}
	if (best_little_idle_cpu >= 0 &&
	    scx_bpf_test_and_clear_cpu_idle(best_little_idle_cpu)) {
		*direct_idle = true;
		return best_little_idle_cpu;
	}
	return home_cpu >= 0 ? home_cpu : scx_bpf_task_cpu(p);
}
/* Zero-hint uses the same cluster placement after behavioral tier classification. */

static __always_inline void fcp_note_first_big(struct fcp_task_ctx *tctx,
					       u32 tier, s32 cpu)
{
	if (tier != FCP_TIER_CRITICAL || tctx->first_big_checked)
		return;

	tctx->first_big_checked = 1;
	if (cpu >= 0 && cpu < MAX_CPUS && cpu_is_big(cpu))
		stat_first_big_hits++;
	else
		stat_first_big_misses++;
}

s32 BPF_STRUCT_OPS(fcp_select_cpu, struct task_struct *p, s32 prev_cpu,
		   u64 wake_flags)
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

	/* The one and only token step, with the real wake flags. */
	fcp_token_step(p, tctx, wake_flags, now);
	tier = classify_task_tier(p, tctx, now);

	target_cpu = pick_target_cpu(p, tctx, prev_cpu, tier,
				     &direct_idle, &preempt_cpu);
	fcp_note_first_big(tctx, tier, target_cpu);

	if (target_cpu >= 0 && target_cpu < MAX_CPUS) {
		if (cpu_is_big(target_cpu))
			stat_big_core_placements++;
		else
			stat_little_core_placements++;
	}

	if (direct_idle) {
		u64 slice = (tier == FCP_TIER_CRITICAL) ? slice_crit_ns :
			    (tier == FCP_TIER_INTERACTIVE ? slice_intr_ns :
			     slice_bg_ns);

		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, slice, 0);
		if (tier == FCP_TIER_CRITICAL)
			stat_crit_dispatches++;
		else if (tier == FCP_TIER_INTERACTIVE)
			stat_intr_dispatches++;
		else
			stat_bg_dispatches++;
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

	/* Per-CPU kthreads go directly to the local DSQ. */
	if ((p->flags & PF_KTHREAD) && p->nr_cpus_allowed == 1) {
		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, slice_crit_ns, enq_flags);
		return;
	}

	/*
	 * Token propagation is intentionally absent here. The old call used a
	 * fabricated SCX_WAKE_SYNC flag and ran the token step twice.
	 */

	tier = classify_task_tier(p, tctx, now);

	if (tier == FCP_TIER_CRITICAL) {
		u64 dl = compute_fcp_deadline(tctx, now);
		s32 target_cpu = scx_bpf_task_cpu(p);

		if (!(enq_flags & SCX_ENQ_WAKEUP))
			dl += slice_crit_ns;
		scx_bpf_dsq_insert_vtime(p, FCP_DSQ_CRITICAL, slice_crit_ns,
					 dl, enq_flags);
		stat_crit_dispatches++;

		if (preempt_on_crit_wakeup && (enq_flags & SCX_ENQ_WAKEUP)) {
			if (cpu_is_big(target_cpu)) {
				struct fcp_cpu_ctx *cctx = lookup_cpu_ctx(target_cpu);

				if (cctx &&
				    cctx->curr_tier != FCP_TIER_CRITICAL) {
					scx_bpf_kick_cpu(target_cpu,
							 SCX_KICK_PREEMPT);
					stat_preempt_kicks++;
					return;
				}
			}

			bool direct_idle = false;
			s32 preempt_cpu = -1;
			s32 cand = pick_target_cpu(p, tctx, target_cpu,
						   FCP_TIER_CRITICAL,
						   &direct_idle, &preempt_cpu);

			if (direct_idle && cand >= 0) {
				scx_bpf_kick_cpu(cand, SCX_KICK_IDLE);
			} else if (preempt_cpu >= 0) {
				scx_bpf_kick_cpu(preempt_cpu, SCX_KICK_PREEMPT);
				stat_preempt_kicks++;
			}
		}
		return;
	}

	if (tier == FCP_TIER_INTERACTIVE) {
		u64 vtime = fcp_get_dsq_vtime(p, tctx);

		if (time_before(vtime, vtime_now))
			vtime = vtime_now;
		scx_bpf_dsq_insert_vtime(p, FCP_DSQ_INTERACTIVE,
					 slice_intr_ns, vtime,
					 enq_flags);
		stat_intr_dispatches++;
		return;
	}

	/* Tier 2: background tasks are steered to LITTLE cores and FCP_DSQ_BACKGROUND. */
	s32 target_cpu = scx_bpf_task_cpu(p);

	if (cpu_is_big(target_cpu))
		target_cpu = get_cluster_home_cpu(p, false);

	if (LINUX_KERNEL_VERSION >= KERNEL_VERSION(6, 12, 0) &&
	    target_cpu != scx_bpf_task_cpu(p) &&
	    target_cpu >= 0 && target_cpu < MAX_CPUS &&
	    !cpu_is_big(target_cpu) &&
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
	stat_bg_dispatches++;
}

void BPF_STRUCT_OPS(fcp_dispatch, s32 cpu, struct task_struct *prev)
{
	struct fcp_cpu_ctx *cctx = lookup_cpu_ctx(cpu);
	bool big = cpu_is_big(cpu);

	/* Bounded starvation guard, tier order unchanged. */
	if (cctx && cctx->consec_crit_dispatches >= starvation_period) {
		cctx->consec_crit_dispatches = 0;
		if (scx_bpf_dsq_move_to_local(FCP_DSQ_BACKGROUND, 0)) {
			stat_starvation_rescues++;
			return;
		}
	}

	if (big) {
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

	if (cctx)
		cctx->curr_tier = FCP_TIER_NONE;
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

	fcp_token_expire(tctx, now);
	if (fcp_token_active(tctx, now))
		tctx->token_service_ns += runtime_ns;

	if (!runnable) {
		tctx->last_sleep_at_ns = now;
		tctx->last_stop_was_block = 1;

		/*
		 * Revoke a token on a real block after 2.5ms of service, or
		 * after a short run. Preemption does not start a new epoch.
		 */
		if (!fcp_root_read(tctx) && fcp_token_active(tctx, now) &&
		    tctx->token_service_ns >= FCP_REVOKE_SERVICE_NS)
			fcp_token_revoke(tctx);
	} else {
		tctx->last_stop_was_block = 0;
		if (tctx->is_frame_root &&
		    tctx->root_source == FCP_ROOT_SRC_BEHAVIOR &&
		    tctx->last_woke_at_ns > 0 && now > tctx->last_woke_at_ns &&
		    now - tctx->last_woke_at_ns > 100ULL * NSEC_PER_MSEC)
			fcp_root_demote(tctx, now);
		if (!fcp_root_read(tctx) && fcp_token_active(tctx, now) &&
		    (tctx->token_service_ns >= FCP_REVOKE_SERVICE_NS ||
		     runtime_ns < FCP_SHORT_YIELD_NS))
			fcp_token_revoke(tctx);
	}

	/* Charge weighted virtual runtime. */
	u64 weight = fcp_task_weight(p);
	if (weight == 0)
		weight = 100;
	u64 delta_vtime = (runtime_ns * 100) / weight;
	fcp_set_dsq_vtime(p, tctx, fcp_get_dsq_vtime(p, tctx) + delta_vtime);

	if (cctx) {
		cctx->tot_runtime_ns += runtime_ns;
		cctx->curr_is_fcp = 0;
		cctx->curr_tier = FCP_TIER_NONE;
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

	tctx = create_task_ctx(p);
	if (!tctx)
		return -ENOMEM;

	/* Reset root and token state across exec. */
	if (args && !args->fork) {
		__builtin_memset(tctx, 0, sizeof(*tctx));
	}

	tctx->tid = p->pid;

#ifndef SCX_FCP_ZERO_HINT
	u32 pid = p->pid;

	if (bpf_map_lookup_elem(&frame_root_pids, &pid))
		fcp_root_promote(tctx, FCP_ROOT_SRC_PID);
	else if (match_frame_comm(p->comm))
		fcp_root_promote(tctx, FCP_ROOT_SRC_COMM);
#endif

	return 0;
}

void BPF_STRUCT_OPS(fcp_exit_task, struct task_struct *p,
		    struct scx_exit_task_args *args)
{
	(void)args;
	bpf_task_storage_delete(&task_ctx_stor, p);
}

s32 BPF_STRUCT_OPS_SLEEPABLE(fcp_init)
{
	u32 zero = 0;
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

	#pragma unroll
	for (s32 cpu = 0; cpu < MAX_CPUS; cpu++) {
		struct fcp_cpu_ctx *cctx;

		if ((u32)cpu >= nr_cpu_ids)
			break;
		cctx = bpf_map_lookup_percpu_elem(&cpu_ctx_stor, &zero, cpu);
		if (cctx)
			cctx->curr_tier = FCP_TIER_NONE;
	}

	return 0;
}

void BPF_STRUCT_OPS(fcp_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

#ifdef SCX_FCP_ZERO_HINT
#define FCP_OPS_NAME "fcp_zh"
#elif defined(SCX_FCP_NAMED)
#define FCP_OPS_NAME "fcp_nh"
#elif defined(SCX_FCP_ORACLE)
#define FCP_OPS_NAME "fcp_or"
#else
#define FCP_OPS_NAME "fcp"
#endif

SCX_OPS_DEFINE(fcp_ops,
	       .select_cpu		= (void *)fcp_select_cpu,
	       .enqueue			= (void *)fcp_enqueue,
	       .dispatch		= (void *)fcp_dispatch,
	       .running			= (void *)fcp_running,
	       .stopping		= (void *)fcp_stopping,
	       .enable			= (void *)fcp_enable,
	       .init_task		= (void *)fcp_init_task,
	       .exit_task		= (void *)fcp_exit_task,
	       .init			= (void *)fcp_init,
	       .exit			= (void *)fcp_exit,
	       .name			= FCP_OPS_NAME);
