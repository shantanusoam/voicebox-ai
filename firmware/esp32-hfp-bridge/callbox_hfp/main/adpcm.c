#include "adpcm.h"

static const int16_t steps[89] = {
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
    34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130,
    143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408,
    449, 494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166,
    1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749,
    3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484,
    7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767
};
static const int8_t index_delta[8] = {-1, -1, -1, -1, 2, 4, 6, 8};

bool cb_adpcm_decode(const uint8_t *block, size_t len, uint8_t *pcm, size_t pcm_len)
{
    if (!block || !pcm || len != CB_ADPCM_BYTES || pcm_len != 640 ||
        block[2] > 88 || block[3] != 0) return false;
    int predictor = (int16_t)((uint16_t)block[0] | ((uint16_t)block[1] << 8));
    int index = block[2];
    for (size_t n = 0; n < 320; ++n) {
        uint8_t packed = block[4 + n / 2];
        int code = (n & 1) ? packed >> 4 : packed & 15;
        int step = steps[index];
        int delta = step >> 3;
        if (code & 1) delta += step >> 2;
        if (code & 2) delta += step >> 1;
        if (code & 4) delta += step;
        predictor += (code & 8) ? -delta : delta;
        if (predictor > 32767) predictor = 32767;
        if (predictor < -32768) predictor = -32768;
        index += index_delta[code & 7];
        if (index < 0) index = 0;
        if (index > 88) index = 88;
        pcm[2 * n] = (uint8_t)predictor;
        pcm[2 * n + 1] = (uint8_t)((uint16_t)predictor >> 8);
    }
    return true;
}
