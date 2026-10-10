#ifndef CALLBOX_PLAYBACK_QUEUE_H
#define CALLBOX_PLAYBACK_QUEUE_H

#include "callbox_audio_ring.h"

/* A single bounded queue from WebSocket receive to the mSBC playout task.
 * Both users hold s_out_lock; the queue itself never allocates. */
#define CB_PLAYBACK_CAPACITY 20u
#define CB_PLAYBACK_PRIME 12u

typedef struct {
    cb_frame frames[CB_PLAYBACK_CAPACITY];
    size_t head, count, high_water;
    uint32_t epoch;
    uint64_t accepted, dropped, underflows;
    bool primed;
} cb_playback_queue;

void cb_playback_init(cb_playback_queue *q);
void cb_playback_clear(cb_playback_queue *q, uint32_t epoch);
bool cb_playback_push(cb_playback_queue *q, const uint8_t *pcm, size_t bytes,
                      uint32_t sequence, uint32_t epoch);
bool cb_playback_pop(cb_playback_queue *q, cb_frame *out);

#endif
