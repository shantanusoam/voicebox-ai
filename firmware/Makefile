CC ?= cc
CFLAGS ?= -std=c11 -Wall -Wextra -Werror -pedantic -O2
.PHONY: test clean

test:
	$(CC) $(CFLAGS) -Iinclude src/callbox_audio_ring.c tests/test_audio_ring.c -o /tmp/callbox-ring-test
	/tmp/callbox-ring-test

clean:
	rm -f /tmp/callbox-ring-test
