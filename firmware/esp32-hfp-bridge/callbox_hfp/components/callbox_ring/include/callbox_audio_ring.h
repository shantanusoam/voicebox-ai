#ifndef CALLBOX_AUDIO_RING_H
#define CALLBOX_AUDIO_RING_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Normalized SERVER format, not the raw HFP callback format.
 * An HFP adapter must decode/resample/reframe to PCM16LE mono 16 kHz first.
 * Externally synchronize push/pop/interrupt across tasks or interrupt handlers.
 * No allocation, blocking network operation, or JSON work belongs in HFP callbacks.
 */
#define CB_FRAME_BYTES 640u
#define CB_RING_CAPACITY 8u /* Maximum queue: 160 ms at 20 ms per frame. */
typedef struct {
    uint8_t data[CB_FRAME_BYTES];
    uint32_t sequence;
    uint32_t epoch;
} cb_frame;
typedef struct {
    cb_frame frames[CB_RING_CAPACITY];
    size_t read_index, write_index, count;
    uint32_t epoch;
    uint64_t dropped, underflows;
} cb_audio_ring;

void cb_ring_init(cb_audio_ring *ring);
bool cb_ring_push(cb_audio_ring *ring, const uint8_t *pcm, size_t bytes,
                  uint32_t sequence, uint32_t epoch);
/* Returns false AND writes silence on underflow. Caller still services audio clock. */
bool cb_ring_pop(cb_audio_ring *ring, cb_frame *output);
/* Discard pending playback and switch epoch atomically under caller's lock. */
void cb_ring_interrupt(cb_audio_ring *ring, uint32_t next_epoch);
#endif
