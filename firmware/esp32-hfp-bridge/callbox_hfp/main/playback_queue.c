#include "playback_queue.h"
#include <string.h>

void cb_playback_init(cb_playback_queue *q)
{
    if (q) memset(q, 0, sizeof(*q));
}

void cb_playback_clear(cb_playback_queue *q, uint32_t epoch)
{
    if (!q || epoch <= q->epoch) return;
    q->head = q->count = 0;
    q->primed = false;
    q->epoch = epoch;
}

bool cb_playback_push(cb_playback_queue *q, const uint8_t *pcm, size_t bytes,
                      uint32_t sequence, uint32_t epoch)
{
    if (!q || !pcm || bytes != CB_FRAME_BYTES) return false;
    if (epoch != q->epoch || q->count == CB_PLAYBACK_CAPACITY) {
        q->dropped++;
        return false;
    }
    cb_frame *f = &q->frames[(q->head + q->count) % CB_PLAYBACK_CAPACITY];
    memcpy(f->data, pcm, CB_FRAME_BYTES);
    f->sequence = sequence;
    f->epoch = epoch;
    q->count++;
    q->accepted++;
    if (q->count > q->high_water) q->high_water = q->count;
    return true;
}

bool cb_playback_pop(cb_playback_queue *q, cb_frame *out)
{
    if (!q || !out) return false;
    if (!q->primed && q->count >= CB_PLAYBACK_PRIME) q->primed = true;
    if (!q->primed) return false;
    if (q->count == 0) {
        q->underflows++;
        q->primed = false;
        return false;
    }
    *out = q->frames[q->head];
    q->head = (q->head + 1) % CB_PLAYBACK_CAPACITY;
    q->count--;
    return true;
}
