// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE
#include <errno.h>
#include <linux/bpf.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

static int sys_bpf(enum bpf_cmd cmd, union bpf_attr *attr, unsigned int size)
{
	return (int)syscall(__NR_bpf, cmd, attr, size);
}

int main(void)
{
	uint32_t id = 0;
	int detached = 0;

	for (;;) {
		union bpf_attr next = {};
		next.start_id = id;
		if (sys_bpf(BPF_MAP_GET_NEXT_ID, &next, sizeof(next)) < 0)
			break;
		id = next.next_id;

		union bpf_attr getfd = {};
		getfd.map_id = id;
		int fd = sys_bpf(BPF_MAP_GET_FD_BY_ID, &getfd, sizeof(getfd));
		if (fd < 0)
			continue;

		struct bpf_map_info info = {};
		union bpf_attr inf = {};
		inf.info.bpf_fd = fd;
		inf.info.info_len = sizeof(info);
		inf.info.info = (uintptr_t)&info;
		if (sys_bpf(BPF_OBJ_GET_INFO_BY_FD, &inf, sizeof(inf)) == 0 &&
		    info.type == BPF_MAP_TYPE_STRUCT_OPS) {
			uint32_t zero = 0;
			union bpf_attr del = {};
			del.map_fd = fd;
			del.key = (uintptr_t)&zero;
			int rc = sys_bpf(BPF_MAP_DELETE_ELEM, &del, sizeof(del));
			printf("struct_ops map id=%u name=%s delete_rc=%d errno=%d\n",
			       id, info.name, rc, rc < 0 ? errno : 0);
			if (rc == 0)
				detached++;
		}
		close(fd);
	}
	return detached >= 0 ? 0 : 1;
}
