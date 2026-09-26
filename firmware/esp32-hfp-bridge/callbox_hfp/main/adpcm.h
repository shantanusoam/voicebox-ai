#ifndef CALLBOX_ADPCM_H
#define CALLBOX_ADPCM_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define CB_ADPCM_BYTES 164u
/* Independent 20 ms block: <predictor:i16,index:u8,reserved:u8,320 nibbles>. */
bool cb_adpcm_decode(const uint8_t *block, size_t len, uint8_t *pcm, size_t pcm_len);
#endif
