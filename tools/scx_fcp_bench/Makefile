# SPDX-License-Identifier: GPL-2.0

CC ?= cc
CFLAGS ?= -O2 -g -Wall -Wextra -Werror -std=c11
LDFLAGS ?= -pthread -lm

all: fcp_frame_bench

fcp_frame_bench: fcp_frame_bench.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f fcp_frame_bench

.PHONY: all clean
