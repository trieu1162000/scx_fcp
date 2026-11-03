// SPDX-License-Identifier: GPL-2.0
/*
 * Synthetic Android-like frame pipeline benchmark.
 *
 * The critical path is a synchronous futex chain:
 *
 *   UI -> Render -> Binder -> GPU
 *
 * Background workers continuously consume CPU time.  The benchmark is
 * deliberately scheduler-neutral so the same binary can compare EEVDF and
 * sched_ext policies on the same guest image.
 */
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <linux/futex.h>
#include <math.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define MAX_STAGES 4
#define MAX_BG_WORKERS 32
#define NS_PER_US 1000ULL
#define NS_PER_MS 1000000ULL
#define NS_PER_SEC 1000000000ULL

struct options {
	unsigned frames;
	unsigned hz;
	unsigned stage_us;
	unsigned stages;
	unsigned bg_workers;
	unsigned little_power_mw;
	unsigned big_power_mw;
	bool anonymous_comms;
	const char *scheduler;
};

struct pipeline {
	struct options opts;
	_Atomic int trigger[MAX_STAGES];
	_Atomic int frame_done;
	_Atomic bool stop;
	pthread_t stage_threads[MAX_STAGES];
	pthread_t bg_threads[MAX_BG_WORKERS];
	uint64_t stage_cpu_ns[MAX_STAGES];
	uint64_t stage_little_samples[MAX_STAGES];
	uint64_t stage_big_samples[MAX_STAGES];
	uint64_t stage_gold_samples[MAX_STAGES];
	uint64_t stage_prime_samples[MAX_STAGES];
	unsigned cpu_cap[128];
	unsigned cpu_count;
};

static uint64_t now_ns(clockid_t clock_id)
{
	struct timespec ts;

	if (clock_gettime(clock_id, &ts) != 0) {
		perror("clock_gettime");
		exit(EXIT_FAILURE);
	}
	return (uint64_t)ts.tv_sec * NS_PER_SEC + (uint64_t)ts.tv_nsec;
}

static int futex_wait(_Atomic int *addr, int expected)
{
	return (int)syscall(SYS_futex, (int *)addr, FUTEX_WAIT_PRIVATE,
			    expected, NULL, NULL, 0);
}

static int futex_wake(_Atomic int *addr)
{
	return (int)syscall(SYS_futex, (int *)addr, FUTEX_WAKE_PRIVATE,
			    INT32_MAX, NULL, NULL, 0);
}

static void wait_for_sequence(_Atomic int *word, int sequence)
{
	for (;;) {
		int current = atomic_load_explicit(word, memory_order_acquire);

		if (current >= sequence)
			return;
		if (futex_wait(word, current) < 0 && errno != EAGAIN && errno != EINTR) {
			perror("futex wait");
			exit(EXIT_FAILURE);
		}
	}
}

static void busy_for_ns(uint64_t duration_ns)
{
	uint64_t end = now_ns(CLOCK_MONOTONIC_RAW) + duration_ns;
	volatile uint64_t accumulator = 0;

	while (now_ns(CLOCK_MONOTONIC_RAW) < end) {
		accumulator = accumulator * 1664525ULL + 1013904223ULL;
		accumulator ^= accumulator >> 13;
	}
	(void)accumulator;
}

static void set_thread_name(const char *name)
{
	char truncated[16];

	snprintf(truncated, sizeof(truncated), "%s", name);
	(void)pthread_setname_np(pthread_self(), truncated);
}

static void *stage_main(void *arg)
{
	struct {
		struct pipeline *p;
		unsigned stage;
	} *ctx = arg;
	struct pipeline *p = ctx->p;
	unsigned stage = ctx->stage;
	char name[16];

	if (p->opts.anonymous_comms) {
		snprintf(name, sizeof(name), "anon_s%u", stage);
	} else {
		snprintf(name, sizeof(name), "fcp_%s", stage == 0 ? "ui" :
			 stage == 1 ? "render" : stage == 2 ? "binder" : "gpu");
	}
	set_thread_name(name);
	free(ctx);

	for (unsigned frame = 1; frame <= p->opts.frames; frame++) {
		uint64_t before_cpu;
		uint64_t after_cpu;
		int cpu;

		wait_for_sequence(&p->trigger[stage], (int)frame);
		before_cpu = now_ns(CLOCK_THREAD_CPUTIME_ID);
		busy_for_ns((uint64_t)p->opts.stage_us * NS_PER_US);
		after_cpu = now_ns(CLOCK_THREAD_CPUTIME_ID);
		p->stage_cpu_ns[stage] += after_cpu - before_cpu;

		cpu = sched_getcpu();
		if (cpu >= 0 && (unsigned)cpu < p->cpu_count) {
			if ((unsigned)cpu < p->cpu_count / 2) {
				p->stage_little_samples[stage]++;
			} else {
				p->stage_big_samples[stage]++;
				if (cpu < 128 && p->cpu_cap[cpu] >= 1000)
					p->stage_prime_samples[stage]++;
				else
					p->stage_gold_samples[stage]++;
			}
		}

		if (stage + 1 < p->opts.stages) {
			atomic_store_explicit(&p->trigger[stage + 1], (int)frame,
					      memory_order_release);
			futex_wake(&p->trigger[stage + 1]);
		} else {
			atomic_store_explicit(&p->frame_done, (int)frame,
					      memory_order_release);
			futex_wake(&p->frame_done);
		}
	}
	return NULL;
}

static void *background_main(void *arg)
{
	struct pipeline *p = arg;
	set_thread_name(p->opts.anonymous_comms ? "anon_bg" : "fcp_background");

	while (!atomic_load_explicit(&p->stop, memory_order_relaxed))
		busy_for_ns(200000ULL);
	return NULL;
}

static bool read_thermal_hot(void)
{
	char path[128];
	char value[32];

	for (unsigned zone = 0; zone < 16; zone++) {
		int fd;
		ssize_t n;

		snprintf(path, sizeof(path), "/sys/class/thermal/thermal_zone%u/temp",
			 zone);
		fd = open(path, O_RDONLY | O_CLOEXEC);
		if (fd < 0)
			continue;
		n = read(fd, value, sizeof(value) - 1);
		close(fd);
		if (n <= 0)
			continue;
		value[n] = '\0';
		if (strtoll(value, NULL, 10) >= 80000)
			return true;
	}
	return false;
}

static int compare_u64(const void *lhs, const void *rhs)
{
	uint64_t a = *(const uint64_t *)lhs;
	uint64_t b = *(const uint64_t *)rhs;

	return a > b ? 1 : a < b ? -1 : 0;
}

static uint64_t percentile(uint64_t *values, unsigned count, double fraction)
{
	unsigned index = (unsigned)ceil(fraction * (double)count) - 1;

	if (index >= count)
		index = count - 1;
	return values[index];
}

static void usage(const char *program)
{
	fprintf(stderr,
		"usage: %s [options]\n"
		"  --scheduler NAME       label in JSON output (default: unknown)\n"
		"  --frames N             frame count (default: 120)\n"
		"  --hz N                 display rate, 60, 90, or 120 (default: 60)\n"
		"  --stage-us N           busy time per stage (default: 700)\n"
		"  --stages N             synchronous stages, 1..4 (default: 4)\n"
		"  --bg-workers N         background CPU workers (default: 2)\n"
		"  --little-power-mw N    energy estimate (default: 450)\n"
		"  --big-power-mw N       energy estimate (default: 1400)\n"
		"  --anonymous-comms      use generic thread names (anon_s0..3, anon_bg)\n",
		program);
}

static void parse_options(struct options *opts, int argc, char **argv)
{
	static const struct option long_options[] = {
		{"scheduler", required_argument, NULL, 's'},
		{"frames", required_argument, NULL, 'f'},
		{"hz", required_argument, NULL, 'h'},
		{"stage-us", required_argument, NULL, 'u'},
		{"stages", required_argument, NULL, 't'},
		{"bg-workers", required_argument, NULL, 'b'},
		{"little-power-mw", required_argument, NULL, 'l'},
		{"big-power-mw", required_argument, NULL, 'g'},
		{"anonymous-comms", no_argument, NULL, 'a'},
		{NULL, 0, NULL, 0},
	};
	int option;

	*opts = (struct options){
		.frames = 120,
		.hz = 60,
		.stage_us = 700,
		.stages = MAX_STAGES,
		.bg_workers = 2,
		.little_power_mw = 450,
		.big_power_mw = 1400,
		.anonymous_comms = false,
		.scheduler = "unknown",
	};

	while ((option = getopt_long(argc, argv, "", long_options, NULL)) != -1) {
		switch (option) {
		case 's':
			opts->scheduler = optarg;
			break;
		case 'f':
			opts->frames = (unsigned)strtoul(optarg, NULL, 10);
			break;
		case 'h':
			opts->hz = (unsigned)strtoul(optarg, NULL, 10);
			break;
		case 'u':
			opts->stage_us = (unsigned)strtoul(optarg, NULL, 10);
			break;
		case 't':
			opts->stages = (unsigned)strtoul(optarg, NULL, 10);
			break;
		case 'b':
			opts->bg_workers = (unsigned)strtoul(optarg, NULL, 10);
			break;
		case 'l':
			opts->little_power_mw = (unsigned)strtoul(optarg, NULL, 10);
			break;
		case 'g':
			opts->big_power_mw = (unsigned)strtoul(optarg, NULL, 10);
			break;
		case 'a':
			opts->anonymous_comms = true;
			break;
		default:
			usage(argv[0]);
			exit(EXIT_FAILURE);
		}
	}

	if (!opts->frames || !opts->hz || opts->stages == 0 ||
	    opts->stages > MAX_STAGES || opts->bg_workers > MAX_BG_WORKERS) {
		usage(argv[0]);
		exit(EXIT_FAILURE);
	}
}

int main(int argc, char **argv)
{
	struct options opts;
	struct pipeline p = {0};
	uint64_t *durations;
	uint64_t total_ns = 0;
	uint64_t critical_cpu_ns = 0;
	uint64_t little_cpu_ns = 0;
	uint64_t big_cpu_ns = 0;
	uint64_t gold_samples = 0;
	uint64_t prime_samples = 0;
	uint64_t frame_period_ns;
	uint64_t thermal_hot_ns = 0;
	uint64_t next_release;
	uint64_t start;
	unsigned jank60 = 0;
	unsigned jank90 = 0;
	unsigned jank120 = 0;
	unsigned thermal_samples = 0;
	unsigned thermal_hot_samples = 0;

	parse_options(&opts, argc, argv);
	p.cpu_count = (unsigned)sysconf(_SC_NPROCESSORS_ONLN);
	if (!p.cpu_count)
		p.cpu_count = 1;
	p.opts = opts;
	for (unsigned c = 0; c < p.cpu_count && c < 128; c++) {
		char cap_path[128], cap_buf[32];
		int fd;
		p.cpu_cap[c] = 1024;
		snprintf(cap_path, sizeof(cap_path),
			 "/sys/devices/system/cpu/cpu%u/cpu_capacity", c);
		fd = open(cap_path, O_RDONLY | O_CLOEXEC);
		if (fd >= 0) {
			ssize_t n = read(fd, cap_buf, sizeof(cap_buf) - 1);
			close(fd);
			if (n > 0) {
				cap_buf[n] = '\0';
				p.cpu_cap[c] = (unsigned)strtoul(cap_buf, NULL, 10);
			}
		}
	}
	atomic_init(&p.frame_done, 0);
	atomic_init(&p.stop, false);
	for (unsigned i = 0; i < MAX_STAGES; i++)
		atomic_init(&p.trigger[i], 0);

	durations = calloc(opts.frames, sizeof(*durations));
	if (!durations) {
		perror("calloc");
		return EXIT_FAILURE;
	}

	for (unsigned i = 0; i < opts.stages; i++) {
		struct {
			struct pipeline *p;
			unsigned stage;
		} *ctx = malloc(sizeof(*ctx));
		int error;

		if (!ctx) {
			perror("malloc");
			return EXIT_FAILURE;
		}
		ctx->p = &p;
		ctx->stage = i;
		error = pthread_create(&p.stage_threads[i], NULL, stage_main, ctx);
		if (error) {
			errno = error;
			perror("pthread_create");
			return EXIT_FAILURE;
		}
	}
	for (unsigned i = 0; i < opts.bg_workers; i++) {
		int error = pthread_create(&p.bg_threads[i], NULL, background_main, &p);

		if (error) {
			errno = error;
			perror("pthread_create");
			return EXIT_FAILURE;
		}
	}

	frame_period_ns = NS_PER_SEC / opts.hz;
	next_release = now_ns(CLOCK_MONOTONIC_RAW) + 100000000ULL;
	for (unsigned frame = 1; frame <= opts.frames; frame++) {
		uint64_t frame_start;
		uint64_t frame_end;

		while ((start = now_ns(CLOCK_MONOTONIC_RAW)) < next_release) {
			struct timespec sleep_for = {
				.tv_sec = 0,
				.tv_nsec = (long)(next_release - start > 1000000ULL ?
						  1000000ULL : next_release - start),
			};
			nanosleep(&sleep_for, NULL);
		}
		frame_start = now_ns(CLOCK_MONOTONIC_RAW);
		atomic_store_explicit(&p.trigger[0], (int)frame, memory_order_release);
		futex_wake(&p.trigger[0]);
		wait_for_sequence(&p.frame_done, (int)frame);
		frame_end = now_ns(CLOCK_MONOTONIC_RAW);
		durations[frame - 1] = frame_end - frame_start;
		total_ns += durations[frame - 1];
		if (durations[frame - 1] > 16666667ULL)
			jank60++;
		if (durations[frame - 1] > 11111111ULL)
			jank90++;
		if (durations[frame - 1] > 8333333ULL)
			jank120++;
		if (read_thermal_hot()) {
			thermal_hot_samples++;
			thermal_hot_ns += durations[frame - 1];
		}
		thermal_samples++;
		next_release += frame_period_ns;
	}

	atomic_store_explicit(&p.stop, true, memory_order_release);
	for (unsigned i = 0; i < opts.bg_workers; i++)
		pthread_join(p.bg_threads[i], NULL);
	for (unsigned i = 0; i < opts.stages; i++)
		pthread_join(p.stage_threads[i], NULL);

	for (unsigned i = 0; i < opts.stages; i++) {
		critical_cpu_ns += p.stage_cpu_ns[i];
		little_cpu_ns += p.stage_little_samples[i] * opts.stage_us * NS_PER_US;
		big_cpu_ns += p.stage_big_samples[i] * opts.stage_us * NS_PER_US;
		gold_samples += p.stage_gold_samples[i];
		prime_samples += p.stage_prime_samples[i];
	}
	qsort(durations, opts.frames, sizeof(*durations), compare_u64);

	{
		double mean_ms = (double)total_ns / (double)opts.frames / 1000000.0;
		double p50_ms = (double)percentile(durations, opts.frames, 0.50) /
				1000000.0;
		double p95_ms = (double)percentile(durations, opts.frames, 0.95) /
				1000000.0;
		double p99_ms = (double)percentile(durations, opts.frames, 0.99) /
				1000000.0;
		double energy_mj = ((double)little_cpu_ns * opts.little_power_mw +
				   (double)big_cpu_ns * opts.big_power_mw) /
				  (double)NS_PER_SEC;

		printf("{\"scheduler\":\"%s\",\"frames\":%u,\"hz\":%u,"
		       "\"stages\":%u,\"stage_us\":%u,\"bg_workers\":%u,"
		       "\"mean_ms\":%.3f,\"p50_ms\":%.3f,\"p95_ms\":%.3f,"
		       "\"p99_ms\":%.3f,\"jank60_pct\":%.3f,\"jank90_pct\":%.3f,\"jank120_pct\":%.3f,"
		       "\"little_stage_samples\":%llu,\"big_stage_samples\":%llu,"
		       "\"gold_stage_samples\":%llu,\"prime_stage_samples\":%llu,"
		       "\"critical_cpu_ms\":%.3f,\"energy_mj_per_frame\":%.3f,"
		       "\"thermal_samples\":%u,\"thermal_hot_samples\":%u,"
		       "\"thermal_hot_ms\":%.3f,\"cpu_count\":%u}\n",
		       opts.scheduler, opts.frames, opts.hz, opts.stages,
		       opts.stage_us, opts.bg_workers, mean_ms, p50_ms, p95_ms,
		       p99_ms, 100.0 * (double)jank60 / opts.frames,
		       100.0 * (double)jank90 / opts.frames,
		       100.0 * (double)jank120 / opts.frames,
		       (unsigned long long)(little_cpu_ns / (opts.stage_us * NS_PER_US)),
		       (unsigned long long)(big_cpu_ns / (opts.stage_us * NS_PER_US)),
		       (unsigned long long)gold_samples,
		       (unsigned long long)prime_samples,
		       (double)critical_cpu_ns / 1000000.0, energy_mj / opts.frames,
		       thermal_samples, thermal_hot_samples,
		       (double)thermal_hot_ns / 1000000.0, p.cpu_count);
	}

	free(durations);
	return EXIT_SUCCESS;
}
