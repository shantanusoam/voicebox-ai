#include "callbox_audio_ring.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

int main(void) {
    cb_audio_ring ring;
    cb_frame frame;
    uint8_t pcm[CB_FRAME_BYTES];
    memset(pcm, 42, sizeof(pcm));
    cb_ring_init(&ring);
    assert(ring.count == 0);
    assert(!cb_ring_pop(&ring, &frame));
    for (size_t i=0;i<CB_FRAME_BYTES;i++) assert(frame.data[i]==0);
    assert(ring.underflows == 1);
    assert(!cb_ring_push(&ring, pcm, 8, 0, 0));
    for (unsigned i=0;i<CB_RING_CAPACITY;i++) assert(cb_ring_push(&ring, pcm, sizeof(pcm), i, 0));
    assert(ring.high_water == CB_RING_CAPACITY);
    assert(!cb_ring_push(&ring, pcm, sizeof(pcm), 9, 0));
    assert(ring.dropped == 1);
    for (unsigned i=0;i<CB_RING_CAPACITY;i++) {
        assert(cb_ring_pop(&ring, &frame));
        assert(frame.sequence == i);
        assert(memcmp(frame.data, pcm, sizeof(pcm)) == 0);
    }
    for (unsigned i=0;i<10000;i++) {
        assert(cb_ring_push(&ring, pcm, sizeof(pcm), i, 0));
        assert(cb_ring_pop(&ring, &frame));
        assert(frame.sequence == i);
    }
    assert(cb_ring_push(&ring, pcm, sizeof(pcm), 10001, 0));
    cb_ring_interrupt(&ring, 1);
    assert(ring.count==0 && ring.epoch==1);
    assert(!cb_ring_push(&ring, pcm, sizeof(pcm), 10002, 0));
    assert(cb_ring_push(&ring, pcm, sizeof(pcm), 10003, 1));
    cb_ring_interrupt(&ring, 0);
    assert(ring.epoch==1 && ring.count==1);
    assert(cb_ring_pop(&ring, &frame) && frame.epoch==1);
    assert(!cb_ring_push(NULL, pcm, sizeof(pcm), 1, 1));
    assert(!cb_ring_pop(&ring, NULL));
    puts("PASS: init, silence, fixed-frame validation, capacity, FIFO, 10000 wrap cycles, interrupt, stale epoch, null guards.");
    return 0;
}
