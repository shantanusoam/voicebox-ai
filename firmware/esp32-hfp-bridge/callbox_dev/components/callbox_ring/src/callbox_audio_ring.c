#include "callbox_audio_ring.h"
#include <string.h>

void cb_ring_init(cb_audio_ring *ring) {
    if (ring) memset(ring, 0, sizeof(*ring));
}
bool cb_ring_push(cb_audio_ring *ring, const uint8_t *pcm, size_t bytes,
                  uint32_t sequence, uint32_t epoch) {
    if (!ring || !pcm || bytes != CB_FRAME_BYTES) return false;
    if (epoch != ring->epoch || ring->count == CB_RING_CAPACITY) {
        ring->dropped++;
        return false; /* Bound latency by rejecting, never allocating more space. */
    }
    cb_frame *frame = &ring->frames[ring->write_index];
    memcpy(frame->data, pcm, CB_FRAME_BYTES);
    frame->sequence = sequence;
    frame->epoch = epoch;
    ring->write_index = (ring->write_index + 1u) % CB_RING_CAPACITY;
    ring->count++;
    return true;
}
bool cb_ring_pop(cb_audio_ring *ring, cb_frame *output) {
    if (!ring || !output) return false;
    memset(output, 0, sizeof(*output));
    output->epoch = ring->epoch;
    if (ring->count == 0u) {
        ring->underflows++;
        return false;
    }
    *output = ring->frames[ring->read_index];
    ring->read_index = (ring->read_index + 1u) % CB_RING_CAPACITY;
    ring->count--;
    return true;
}
void cb_ring_interrupt(cb_audio_ring *ring, uint32_t next_epoch) {
    if (!ring || next_epoch <= ring->epoch) return;
    ring->read_index = ring->write_index = ring->count = 0u;
    ring->epoch = next_epoch;
}
