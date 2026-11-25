CC ?= cc
CFLAGS ?= -O2 -g
LDFLAGS ?=

all: schbench

schbench: schbench.c
	$(CC) $(CFLAGS) -pthread schbench.c -lm $(LDFLAGS) -o $@

clean:
	rm -f schbench

.PHONY: all clean
