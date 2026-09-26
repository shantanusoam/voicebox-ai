#include <assert.h>
#include <stdint.h>
#include <string.h>
#include "playback_queue.h"

int main(void)
{
    cb_playback_queue q;
    cb_frame out;
    uint8_t pcm[CB_FRAME_BYTES];
    cb_playback_init(&q);
    assert(!cb_playback_pop(&q, &out));
    assert(q.underflows == 0);

    /* Two unpaced CBB1 batches must fit before the playback task runs. */
    for (uint32_t seq = 0; seq < 12; ++seq) {
        memset(pcm, (int)seq, sizeof(pcm));
        assert(cb_playback_push(&q, pcm, sizeof(pcm), seq, 0));
    }
    assert(q.high_water == 12 && q.dropped == 0);
    for (uint32_t seq = 0; seq < 12; ++seq) {
        assert(cb_playback_pop(&q, &out));
        assert(out.sequence == seq && out.data[0] == seq);
    }
    assert(!cb_playback_pop(&q, &out));
    assert(q.underflows == 1 && !q.primed);

    /* Full queue reports loss; interrupt removes old-epoch audio. */
    for (uint32_t seq = 0; seq < CB_PLAYBACK_CAPACITY; ++seq)
        assert(cb_playback_push(&q, pcm, sizeof(pcm), seq, 0));
    assert(!cb_playback_push(&q, pcm, sizeof(pcm), 20, 0));
    assert(q.dropped == 1);
    cb_playback_clear(&q, 1);
    assert(q.count == 0 && !q.primed);
    assert(!cb_playback_push(&q, pcm, sizeof(pcm), 21, 0));
    assert(q.dropped == 2);
    for (uint32_t seq = 0; seq < 12; ++seq)
        assert(cb_playback_push(&q, pcm, sizeof(pcm), seq, 1));
    assert(cb_playback_pop(&q, &out) && out.epoch == 1);
    return 0;
}
