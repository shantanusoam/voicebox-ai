/* CallBox combined device firmware: HFP hands-free (mSBC) <-> CallBox WebSocket.
 *
 * Phone pairs with the ESP32 (HFP HF). SCO audio arrives as mSBC frames via the
 * external-codec callback, is decoded to PCM16LE mono 16 kHz, assembled into
 * 640-byte (20 ms) wire frames and streamed to the CallBox gateway. Server audio
 * comes back the other way. BT callbacks never touch the network; rings bound
 * both directions; epoch checks discard stale playback.
 */
#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/event_groups.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "cJSON.h"
#include "mbedtls/base64.h"
#include "esp_websocket_client.h"
#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_bt_device.h"
#include "esp_gap_bt_api.h"
#include "esp_hf_client_api.h"
#include "sbc.h"
#include "callbox_audio_ring.h"

static const char *TAG = "callbox";

esp_hf_sync_conn_hdl_t s_sync_conn_hdl(void);

/* ---------- shared state ---------- */
static EventGroupHandle_t s_events;
#define EV_WIFI_OK BIT0
#define EV_WS_OPEN BIT1

static esp_websocket_client_handle_t s_ws;
static char s_call_id[64];
static volatile bool s_call_active;      /* call.started received */
static volatile bool s_audio_up;         /* BT mSBC audio connected */
static volatile bool s_turn_pending;       /* agent: commit sent, awaiting turn.done */
static volatile bool s_commit_requested;    /* agent: end-of-turn seen; drain audio then commit */
static char s_pending_commit_id[40];
static volatile bool s_pending_call_start;  /* retry until the request is actually submitted */
static volatile bool s_call_start_inflight; /* submitted; waiting for call.started */
static volatile bool s_pending_call_end;    /* retry until the request is actually submitted */
static char s_pending_call_end_id[64];
static uint32_t s_tx_seq, s_out_epoch, s_capture_epoch;
static uint32_t s_turn_counter;

/* rings (external sync required by the ring contract) */
static cb_audio_ring s_in_ring, s_out_ring;
static SemaphoreHandle_t s_in_lock, s_out_lock;

/* mSBC frames from the BT callback */
#define MSBC_Q_LEN 24
static QueueHandle_t s_msbc_q;

/* Diagnostic mode that follows Espressif's external-codec HFP example:
 * buffer encoded SCO packets and send them straight back to the phone.
 * It deliberately uses no Wi-Fi, WebSocket, JSON, PCM conversion or SBC
 * re-encoding, so it isolates the Bluetooth/HFP path from the network path. */
#define LOCAL_ECHO_Q_LEN 50
#define LOCAL_ECHO_PREFILL 20
static QueueHandle_t s_local_echo_q;
static uint32_t s_local_echo_sent, s_local_echo_drop, s_local_echo_bad;
static uint8_t s_local_tone_frame[ESP_HF_MSBC_ENCODED_FRAME_SIZE];
static bool s_local_tone_ready;

static bool local_echo_enabled(void)
{
    return strcmp(CONFIG_CB_CALL_MODE, "local_echo") == 0;
}

static bool local_tone_enabled(void)
{
    return strcmp(CONFIG_CB_CALL_MODE, "local_tone") == 0;
}

static bool local_hfp_test_enabled(void)
{
    return local_echo_enabled() || local_tone_enabled();
}

static sbc_t s_sbc_dec, s_sbc_enc;
static bool s_sbc_ready;
static uint32_t s_msbc_decode_fail; /* mSBC decode/frame-size failures, cumulative */

/* assembly of decoded mSBC (120 samples = 240 B per frame) into 640-byte wire frames */
static uint8_t s_asm[CB_FRAME_BYTES + 256]; /* 320 samples + one partial frame slack */
static size_t s_asm_len;
static int s_speech_frames, s_silence_frames, s_collected;

/* 7.5 ms output clock */
static SemaphoreHandle_t s_out_tick;
static esp_timer_handle_t s_out_timer;

/* ---------- helpers ---------- */
static void lock_take(SemaphoreHandle_t m) { xSemaphoreTake(m, portMAX_DELAY); }
static void lock_give(SemaphoreHandle_t m) { xSemaphoreGive(m); }

static int16_t frame_rms_dbish(const uint8_t *pcm)
{
    uint64_t acc = 0;
    for (size_t i = 0; i < CB_FRAME_BYTES; i += 2) {
        int16_t v;
        memcpy(&v, &pcm[i], 2);
        acc += (int32_t)v * v;
    }
    return (int16_t)(acc / (CB_FRAME_BYTES / 2) >> 12);
}

/* ---------- WS receive pump state ---------- */
#define RXQ_LEN 24
#define CTRL_RXQ_LEN 12
typedef struct { uint8_t *data; size_t len; } rx_msg;
static QueueHandle_t s_rxq, s_ctrl_rxq;
static uint8_t s_accum[8192];
static size_t s_accum_len;
static uint32_t s_rxq_drop, s_ctrl_rxq_drop, s_rx_alloc_fail;

/* Binary PCM batch protocol shared with callbox.audio:
 * >4sBBHII : magic, frame_count, flags, frame_bytes, first_seq, epoch.
 * Four 20 ms frames amortize JSON/base64/WebSocket overhead while adding at
 * most ~60 ms of uplink batching latency. */
#define AUDIO_BATCH_MAX_FRAMES 4u
#define AUDIO_BATCH_HEADER_BYTES 16u
#define AUDIO_BATCH_WAIT_MS 60u
static uint8_t s_audio_batch[AUDIO_BATCH_HEADER_BYTES + AUDIO_BATCH_MAX_FRAMES * CB_FRAME_BYTES];

static void put_be16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)(v >> 8); p[1] = (uint8_t)v; }
static void put_be32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)(v >> 24); p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);  p[3] = (uint8_t)v;
}

/* Important esp_websocket_client semantic: an expired write timeout is not a
 * harmless per-frame drop. transport_poll_write() returning 0 is treated as a
 * fatal transport error and the client tears the WebSocket down. Real hardware
 * reproduced this ~90 ms after call.start when audio used a 15 ms deadline.
 *
 * portMAX_DELAY maps to an infinite transport timeout in the vendored client,
 * so it is not a complete dead-link solution either. We keep it here to avoid
 * corrupting/aborting a partially emitted WebSocket frame, and reduce pressure
 * structurally with binary batching. Connection recovery is handled by the
 * explicit SCO/WS state below. */
static bool send_text_timeout(const char *str, TickType_t timeout)
{
    return s_ws && esp_websocket_client_send_text(s_ws, str, strlen(str), timeout) >= 0;
}

static bool send_text(const char *str)
{
    /* Control writes use the same rule as audio: a short application timeout
     * is connection-fatal in esp_websocket_client, not a harmless retry. */
    return send_text_timeout(str, portMAX_DELAY);
}

static uint32_t s_net_send_drop;

static bool send_audio_batch(cb_frame *frames, size_t count, uint32_t first_seq)
{
    if (!s_ws || count == 0 || count > AUDIO_BATCH_MAX_FRAMES) return false;
    uint8_t *p = s_audio_batch;
    memcpy(p, "CBA1", 4);
    p[4] = (uint8_t)count;
    p[5] = 0; /* flags */
    put_be16(p + 6, CB_FRAME_BYTES);
    put_be32(p + 8, first_seq);
    put_be32(p + 12, s_capture_epoch);
    for (size_t i = 0; i < count; ++i) {
        memcpy(p + AUDIO_BATCH_HEADER_BYTES + i * CB_FRAME_BYTES,
               frames[i].data, CB_FRAME_BYTES);
    }
    int bytes = AUDIO_BATCH_HEADER_BYTES + (int)(count * CB_FRAME_BYTES);
    int sent = esp_websocket_client_send_bin(
        s_ws, (const char *)s_audio_batch, bytes, portMAX_DELAY);
    if (sent != bytes) {
        s_net_send_drop += (uint32_t)count;
        return false;
    }
    return true;
}

static bool send_simple(const char *fmt, ...)
{
    char buf[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    return send_text(buf);
}

static void handle_message(const char *msg, size_t len)
{
    cJSON *root = cJSON_ParseWithLength(msg, len);
    if (!root) return;
    const char *type = cJSON_GetStringValue(cJSON_GetObjectItem(root, "type"));
    if (!type) { cJSON_Delete(root); return; }

    if (strcmp(type, "ready") == 0) {
        ESP_LOGI(TAG, "READY (device authenticated)");
        /* Automatic WS reconnect must re-bind an already-live SCO call to a
         * fresh server call. The old server connection finalizes its call on
         * disconnect, so call.start is the recovery operation. */
        if (s_audio_up && !s_call_active && !s_call_start_inflight) s_pending_call_start = true;
    } else if (strcmp(type, "call.started") == 0) {
        const char *cid = cJSON_GetStringValue(cJSON_GetObjectItem(root, "call_id"));
        snprintf(s_call_id, sizeof(s_call_id), "%s", cid ? cid : "");
        s_tx_seq = 0;
        s_capture_epoch = 0;
        s_call_start_inflight = false;
        s_call_active = true;
        s_turn_pending = false;
        s_commit_requested = false;
        s_pending_commit_id[0] = '\0';
        ESP_LOGI(TAG, "CALL_STARTED id=%s", s_call_id);
    } else if (strcmp(type, "audio.output") == 0) {
        cJSON *pcm = cJSON_GetObjectItem(root, "pcm16");
        cJSON *ep  = cJSON_GetObjectItem(root, "epoch");
        uint32_t epoch = (ep && cJSON_IsNumber(ep)) ? (uint32_t)ep->valuedouble : s_out_epoch;
        if (pcm && cJSON_IsString(pcm) && epoch == s_out_epoch) {
            static uint8_t out[CB_FRAME_BYTES];
            size_t olen = 0;
            if (mbedtls_base64_decode(out, sizeof(out), &olen,
                    (const unsigned char *)pcm->valuestring,
                    strlen(pcm->valuestring)) == 0 && olen == CB_FRAME_BYTES) {
                lock_take(s_out_lock);
                cb_ring_push(&s_out_ring, out, olen, 0, epoch);
                lock_give(s_out_lock);
            }
        }
    } else if (strcmp(type, "playback.clear") == 0) {
        cJSON *ep = cJSON_GetObjectItem(root, "epoch");
        s_out_epoch = (ep && cJSON_IsNumber(ep)) ? (uint32_t)ep->valuedouble : s_out_epoch + 1;
        /* playback.clear is the server's epoch barrier. Keep future microphone
         * audio on the same epoch so a barge-in does not make all subsequent
         * capture stale at AudioBuffer.add(). */
        s_capture_epoch = s_out_epoch;
        lock_take(s_out_lock);
        cb_ring_interrupt(&s_out_ring, s_out_epoch);
        lock_give(s_out_lock);
        ESP_LOGW(TAG, "INTERRUPTED — playback cleared, epoch=%" PRIu32, s_out_epoch);
    } else if (strcmp(type, "turn.processing") == 0) {
        ESP_LOGI(TAG, "TURN_PROCESSING");
    } else if (strcmp(type, "turn.result") == 0) {
        const char *text = cJSON_GetStringValue(cJSON_GetObjectItem(root, "text"));
        ESP_LOGI(TAG, "TURN_RESULT: %s", text ? text : "(none)");
    } else if (strcmp(type, "turn.done") == 0) {
        s_turn_pending = false;
        s_commit_requested = false;
        s_pending_commit_id[0] = '\0';
        s_speech_frames = s_silence_frames = s_collected = 0;
        ESP_LOGI(TAG, "TURN_DONE");
    } else if (strcmp(type, "call.ended") == 0) {
        ESP_LOGI(TAG, "CALL_ENDED (server)");
        s_call_active = false;
        s_call_id[0] = '\0';
    } else if (strcmp(type, "error") == 0) {
        const char *code = cJSON_GetStringValue(cJSON_GetObjectItem(root, "code"));
        const char *emsg = cJSON_GetStringValue(cJSON_GetObjectItem(root, "message"));
        ESP_LOGE(TAG, "SERVER_ERROR code=%s msg=%s", code ? code : "?", emsg ? emsg : "?");
    } else if (strcmp(type, "pong") == 0) {
        /* heartbeat ack */
    }
    cJSON_Delete(root);
}

/* ---------- tasks ---------- */
static void codec_in_task(void *arg)
{
    esp_hf_audio_buff_t *buf;
    while (1) {
        if (xQueueReceive(s_msbc_q, &buf, portMAX_DELAY) != pdTRUE) continue;
        if (!s_audio_up || !s_sbc_ready) { esp_hf_client_audio_buff_free(buf); continue; }

        size_t in_len = buf->data_len > ESP_HF_MSBC_ENCODED_FRAME_SIZE
                        ? ESP_HF_MSBC_ENCODED_FRAME_SIZE : buf->data_len;
        uint8_t pcm_out[240];
        size_t written = 0;
        ssize_t frames = sbc_decode(&s_sbc_dec, buf->data, in_len, pcm_out, sizeof(pcm_out), &written);
        esp_hf_client_audio_buff_free(buf);
        if (frames < 0 || written != 240) {
            s_msbc_decode_fail++;
            ESP_LOGW(TAG, "msbc decode fail %d (total %" PRIu32 ")", (int)frames, s_msbc_decode_fail);
            continue;
        }

        bool agent_mode = strcmp(CONFIG_CB_CALL_MODE, "agent") == 0;
        bool collect = s_call_active && agent_mode && !s_turn_pending && !s_commit_requested;

        /* append to assembly and emit 640-byte wire frames */
        size_t off = 0;
        while (off < written) {
            size_t space = sizeof(s_asm) - s_asm_len;
            size_t take = written - off > space ? space : written - off;
            memcpy(s_asm + s_asm_len, pcm_out + off, take);
            s_asm_len += take; off += take;
            if (s_asm_len >= CB_FRAME_BYTES) {
                uint8_t frame[CB_FRAME_BYTES];
                memcpy(frame, s_asm, CB_FRAME_BYTES);
                memmove(s_asm, s_asm + CB_FRAME_BYTES, s_asm_len - CB_FRAME_BYTES);
                s_asm_len -= CB_FRAME_BYTES;

                bool request_commit = false;
                if (collect) {
                    int16_t r = frame_rms_dbish(frame);
                    if (r > CONFIG_CB_VAD_THRESHOLD) {
                        s_speech_frames++; s_silence_frames = 0;
                        if (s_speech_frames >= 2 && s_collected == 0) {
                            ESP_LOGI(TAG, "speech detected (rms %d)", r);
                        }
                    } else if (s_speech_frames > 0) {
                        s_silence_frames++;
                    }
                    if (s_speech_frames >= 2) s_collected++;
                    if (s_collected >= 5 &&
                        (s_silence_frames >= 40 || s_collected >= 750)) {
                        snprintf(s_pending_commit_id, sizeof(s_pending_commit_id),
                                 "turn-%" PRIu32, ++s_turn_counter);
                        request_commit = true;
                    }
                }

                /* Do not queue pre-call audio, generated-turn audio, or audio
                 * after an end-of-turn marker. The frame that triggered the
                 * commit is queued first; net_tx_task sends the commit only
                 * after this ring drains, preserving speech-before-commit
                 * ordering on the wire. */
                bool should_queue = s_call_active && !s_turn_pending && !s_commit_requested;
                if (should_queue) {
                    lock_take(s_in_lock);
                    (void)cb_ring_push(&s_in_ring, frame, CB_FRAME_BYTES, 0, 0);
                    lock_give(s_in_lock);
                }

                if (request_commit) {
                    s_commit_requested = true;
                    ESP_LOGI(TAG, "COMMIT_QUEUED %s after %d frames",
                             s_pending_commit_id, s_collected);
                    s_speech_frames = s_silence_frames = s_collected = 0;
                    collect = false;
                }
            }
        }
    }
}

/* Periodic playout/decode health line, active-call only. Read under each
 * ring's own lock since dropped/underflows are updated by producer/consumer
 * tasks that already hold s_in_lock/s_out_lock. */
static void stats_task(void *arg)
{
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(5000));
        if (!s_call_active && !(local_hfp_test_enabled() && s_audio_up)) continue;

        if (local_hfp_test_enabled()) {
            (void)esp_hf_client_pkt_stat_nums_get(s_sync_conn_hdl());
            ESP_LOGI(TAG, "LOCAL_HFP mode=%s queued=%u sent=%" PRIu32
                     " drop=%" PRIu32 " bad=%" PRIu32,
                     CONFIG_CB_CALL_MODE,
                     (unsigned)uxQueueMessagesWaiting(s_local_echo_q),
                     s_local_echo_sent, s_local_echo_drop, s_local_echo_bad);
            continue;
        }

        /* Compare SCO health with Wi-Fi/WebSocket active. This was decisive
         * in local_tone mode and should stay visible in echo/agent mode too. */
        (void)esp_hf_client_pkt_stat_nums_get(s_sync_conn_hdl());

        uint64_t in_dropped, in_under, out_dropped, out_under;
        lock_take(s_in_lock);
        in_dropped = s_in_ring.dropped; in_under = s_in_ring.underflows;
        lock_give(s_in_lock);
        lock_take(s_out_lock);
        out_dropped = s_out_ring.dropped; out_under = s_out_ring.underflows;
        lock_give(s_out_lock);
        ESP_LOGI(TAG, "STATS msbc_fail=%" PRIu32 " net_send_drop=%" PRIu32
                 " rxq_drop=%" PRIu32 " ctrl_drop=%" PRIu32 " rx_alloc_fail=%" PRIu32
                 " in(drop=%llu,under=%llu,high=%u) out(drop=%llu,under=%llu,high=%u) tx_seq=%" PRIu32,
                 s_msbc_decode_fail, s_net_send_drop, s_rxq_drop, s_ctrl_rxq_drop, s_rx_alloc_fail,
                 (unsigned long long)in_dropped, (unsigned long long)in_under,
                 (unsigned)s_in_ring.high_water,
                 (unsigned long long)out_dropped, (unsigned long long)out_under,
                 (unsigned)s_out_ring.high_water, s_tx_seq);
    }
}

static void net_tx_task(void *arg)
{
    cb_frame batch[AUDIO_BATCH_MAX_FRAMES];
    uint32_t idle_ticks = 0;

    while (1) {
        EventBits_t bits = xEventGroupGetBits(s_events);
        bool ws_ready = (bits & EV_WS_OPEN) != 0;

        if (s_pending_call_start && !s_call_start_inflight && ws_ready) {
            if (send_simple("{\"type\":\"call.start\",\"mode\":\"%s\",\"consent\":true}",
                            CONFIG_CB_CALL_MODE)) {
                s_pending_call_start = false;
                s_call_start_inflight = true;
            }
        }
        if (s_pending_call_end && ws_ready) {
            if (send_simple("{\"type\":\"call.end\",\"call_id\":\"%s\"}",
                            s_pending_call_end_id)) {
                s_pending_call_end = false;
            }
        }

        size_t count = 0;
        if (s_call_active && !s_turn_pending) {
            TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(AUDIO_BATCH_WAIT_MS);
            while (count < AUDIO_BATCH_MAX_FRAMES) {
                bool got;
                lock_take(s_in_lock);
                got = cb_ring_pop(&s_in_ring, &batch[count]);
                lock_give(s_in_lock);
                if (got) {
                    count++;
                    if (count == AUDIO_BATCH_MAX_FRAMES) break;
                    continue;
                }
                if (count == 0 || xTaskGetTickCount() < deadline) {
                    vTaskDelay(pdMS_TO_TICKS(5));
                    continue;
                }
                break;
            }
        } else {
            /* Drain stale capture while no server call/turn is accepting it. */
            cb_frame stale;
            lock_take(s_in_lock);
            bool got = cb_ring_pop(&s_in_ring, &stale);
            lock_give(s_in_lock);
            (void)got;
        }

        if (count > 0) {
            if (send_audio_batch(batch, count, s_tx_seq)) {
                s_tx_seq += (uint32_t)count;
            }
            idle_ticks = 0;
        }

        /* audio.commit is a control barrier: it is emitted only after every
         * PCM frame queued before the marker has been submitted. */
        if (s_commit_requested && s_call_active && !s_turn_pending) {
            size_t remaining;
            lock_take(s_in_lock);
            remaining = s_in_ring.count;
            lock_give(s_in_lock);
            if (remaining == 0 && count == 0 && ws_ready) {
                if (send_simple("{\"type\":\"audio.commit\",\"call_id\":\"%s\",\"request_id\":\"%s\"}",
                                s_call_id, s_pending_commit_id)) {
                    s_turn_pending = true;
                    s_commit_requested = false;
                    ESP_LOGI(TAG, "COMMIT_SENT %s", s_pending_commit_id);
                }
            }
        }

        if (count == 0) {
            vTaskDelay(pdMS_TO_TICKS(5));
            if (++idle_ticks >= 6000) { /* ~30 s */
                idle_ticks = 0;
                if (ws_ready) (void)send_simple("{\"type\":\"ping\"}");
            }
        }
    }
}

static void out_timer_cb(void *arg)
{
    xSemaphoreGive(s_out_tick);
}

static void codec_out_task(void *arg)
{
    uint8_t pcm_in[CB_FRAME_BYTES];
    size_t pcm_avail = 0;
    uint8_t leftover[CB_FRAME_BYTES];
    size_t leftover_len = 0;

    while (1) {
        if (xSemaphoreTake(s_out_tick, portMAX_DELAY) != pdTRUE) continue;
        if (!s_audio_up || !s_sbc_ready) { pcm_avail = leftover_len = 0; continue; }

        /* feed 240 B (120 samples) per 7.5 ms tick */
        uint8_t chunk[240];
        size_t chunk_len = 0;
        if (leftover_len) {
            size_t take = leftover_len > 240 ? 240 : leftover_len;
            memcpy(chunk, leftover, take);
            chunk_len = take;
            memmove(leftover, leftover + take, leftover_len - take);
            leftover_len -= take;
        }
        while (chunk_len < 240) {
            cb_frame f;
            bool got;
            lock_take(s_out_lock);
            got = cb_ring_pop(&s_out_ring, &f); /* underflow -> silence frame */
            lock_give(s_out_lock);
            size_t take = (240 - chunk_len) > CB_FRAME_BYTES ? CB_FRAME_BYTES : (240 - chunk_len);
            memcpy(chunk + chunk_len, f.data, take);
            chunk_len += take;
            if (take < CB_FRAME_BYTES) {
                memcpy(leftover, f.data + take, CB_FRAME_BYTES - take);
                leftover_len = CB_FRAME_BYTES - take;
            }
            (void)pcm_avail; (void)pcm_in;
        }

        uint8_t msbc[64];
        ssize_t written = 0;
        if (sbc_encode(&s_sbc_enc, chunk, 240, msbc, sizeof(msbc), &written) >= 0
                && written == ESP_HF_MSBC_ENCODED_FRAME_SIZE) {
            esp_hf_audio_buff_t *ab = esp_hf_client_audio_buff_alloc(ESP_HF_MSBC_ENCODED_FRAME_SIZE);
            if (ab) {
                memcpy(ab->data, msbc, ESP_HF_MSBC_ENCODED_FRAME_SIZE);
                ab->data_len = ESP_HF_MSBC_ENCODED_FRAME_SIZE;
                if (esp_hf_client_audio_data_send(s_sync_conn_hdl(), ab) != ESP_OK) {
                    esp_hf_client_audio_buff_free(ab);
                }
            }
        }
    }
}

/* ---------- BT hooks (defined in main.c below) ---------- */
esp_hf_sync_conn_hdl_t s_sync_conn_hdl(void);

/* ---------- WS event handler ---------- */
static void ws_event_handler(void *handler_args, esp_event_base_t base,
                             int32_t event_id, void *event_data)
{
    esp_websocket_event_data_t *e = event_data;
    switch (event_id) {
    case WEBSOCKET_EVENT_CONNECTED: {
        ESP_LOGI(TAG, "WS_CONNECTED — hello");
        cJSON *o = cJSON_CreateObject();
        cJSON_AddStringToObject(o, "type", "hello");
        cJSON_AddStringToObject(o, "protocol", "callbox.v1");
        cJSON_AddStringToObject(o, "device_id", CONFIG_CB_DEVICE_ID);
        cJSON_AddStringToObject(o, "token", CONFIG_CB_DEVICE_TOKEN);
        char *str = cJSON_PrintUnformatted(o);
        if (str) { send_text(str); cJSON_free(str); }
        cJSON_Delete(o);
        xEventGroupSetBits(s_events, EV_WS_OPEN);
        break;
    }
    case WEBSOCKET_EVENT_DATA:
        if (e->data_len <= 0 || e->op_code != 0x1) break;
        if (e->payload_offset == 0) s_accum_len = 0;
        if (s_accum_len + e->data_len <= sizeof(s_accum)) {
            memcpy(s_accum + s_accum_len, e->data_ptr, e->data_len);
            s_accum_len += e->data_len;
        }
        if (e->payload_offset + e->data_len >= e->payload_len && s_accum_len > 0) {
            rx_msg m = { .data = malloc(s_accum_len + 1), .len = s_accum_len };
            if (m.data) {
                memcpy(m.data, s_accum, s_accum_len);
                m.data[s_accum_len] = '\0';
                /* Audio playback may be dropped under pressure; state/control
                 * messages must not sit behind a burst of audio.output. */
                bool is_audio = strstr((const char *)m.data, "\"audio.output\"") != NULL;
                QueueHandle_t target = is_audio ? s_rxq : s_ctrl_rxq;
                if (xQueueSend(target, &m, 0) != pdTRUE) {
                    if (is_audio) s_rxq_drop++; else s_ctrl_rxq_drop++;
                    free(m.data);
                }
            } else {
                s_rx_alloc_fail++;
            }
            s_accum_len = 0;
        }
        break;
    case WEBSOCKET_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "WS_DISCONNECTED");
        xEventGroupClearBits(s_events, EV_WS_OPEN);
        s_call_active = false;
        s_call_id[0] = '\0';
        s_turn_pending = false;
        s_commit_requested = false;
        s_pending_commit_id[0] = '\0';
        /* The server finalizes the old call when its socket disappears.
         * A stale call.end from that connection is no longer useful. */
        s_pending_call_end = false;
        s_call_start_inflight = false;
        if (s_audio_up) s_pending_call_start = true;
        break;
    case WEBSOCKET_EVENT_ERROR:
        ESP_LOGE(TAG,
                 "WS_ERROR type=%d transport=%s tls=0x%x sock_errno=%d handshake=%d",
                 (int)e->error_handle.error_type,
                 esp_err_to_name(e->error_handle.esp_tls_last_esp_err),
                 (unsigned)e->error_handle.esp_tls_stack_err,
                 e->error_handle.esp_transport_sock_errno,
                 e->error_handle.esp_ws_handshake_status_code);
        break;
    default:
        break;
    }
}

static void rx_pump_task(void *arg)
{
    while (1) {
        rx_msg m;
        /* Drain control first. Audio may wait a few milliseconds; call state
         * must not be lost behind a playback burst. */
        if (xQueueReceive(s_ctrl_rxq, &m, 0) == pdTRUE ||
            xQueueReceive(s_rxq, &m, pdMS_TO_TICKS(10)) == pdTRUE) {
            handle_message((const char *)m.data, m.len);
            free(m.data);
        }
    }
}

/* ---------- BT callbacks ---------- */
static esp_hf_sync_conn_hdl_t s_hfp_hdl;
esp_hf_sync_conn_hdl_t s_sync_conn_hdl(void) { return s_hfp_hdl; }

static void hfp_audio_data_cb(esp_hf_sync_conn_hdl_t hdl, esp_hf_audio_buff_t *buf, bool bad)
{
    if (local_tone_enabled()) {
        if (bad) s_local_echo_bad++;
        esp_hf_client_audio_buff_free(buf);

        if (!s_audio_up || !s_local_tone_ready) return;

        /* Use the incoming SCO packet cadence as the transmit clock, but send
         * a locally generated 800 Hz test tone. Unlike an exact loopback this
         * cannot be removed as acoustic echo by the phone's EC/NR. */
        esp_hf_audio_buff_t *tone =
            esp_hf_client_audio_buff_alloc(ESP_HF_MSBC_ENCODED_FRAME_SIZE);
        if (!tone) {
            s_local_echo_drop++;
            return;
        }
        memcpy(tone->data, s_local_tone_frame, ESP_HF_MSBC_ENCODED_FRAME_SIZE);
        tone->data_len = ESP_HF_MSBC_ENCODED_FRAME_SIZE;
        esp_err_t err = esp_hf_client_audio_data_send(hdl, tone);
        if (err != ESP_OK) {
            s_local_echo_drop++;
            esp_hf_client_audio_buff_free(tone);
        } else {
            s_local_echo_sent++;
        }
        return;
    }

    if (local_echo_enabled()) {
        if (bad) {
            s_local_echo_bad++;
            esp_hf_client_audio_buff_free(buf);
            return;
        }

        /* Follow Espressif's HFP-HF external-codec example literally:
         * keep 20 encoded packets of latency and send one old packet back
         * directly from this callback for each new packet that arrives. */
        if (xQueueSend(s_local_echo_q, &buf, 0) != pdTRUE) {
            s_local_echo_drop++;
            esp_hf_client_audio_buff_free(buf);
            return;
        }
        if (uxQueueMessagesWaiting(s_local_echo_q) < LOCAL_ECHO_PREFILL) return;

        esp_hf_audio_buff_t *echo = NULL;
        if (xQueueReceive(s_local_echo_q, &echo, 0) != pdTRUE || !echo) return;
        if (echo->data_len > ESP_HF_MSBC_ENCODED_FRAME_SIZE) {
            echo->data_len = ESP_HF_MSBC_ENCODED_FRAME_SIZE;
        }
        esp_err_t err = esp_hf_client_audio_data_send(hdl, echo);
        if (err != ESP_OK) {
            s_local_echo_drop++;
            esp_hf_client_audio_buff_free(echo);
        } else {
            s_local_echo_sent++;
        }
        return;
    }

    if (bad || xQueueSend(s_msbc_q, &buf, 0) != pdTRUE) {
        esp_hf_client_audio_buff_free(buf);
    }
}

/* Runs on the Bluedroid BTC task context, not a task we own. Per
 * docs/ARCHITECTURE.md: BT callbacks only queue and free; network I/O must
 * happen in a dedicated task. send_simple() used to be called directly from
 * here, blocking this BT stack thread on a WS send (up to portMAX_DELAY,
 * later bounded to 2s) right as a call starts/ends — plausible cause of the
 * call instability seen on real hardware, independent of Wi-Fi RF quality.
 * Defer the actual send to net_tx_task instead. */
static void bridge_on_audio_up(esp_hf_sync_conn_hdl_t hdl)
{
    s_hfp_hdl = hdl;
    s_audio_up = true;

    /* Match Espressif's HFP HF example: while SCO is active, disable both
     * page scan and inquiry scan to free over-the-air bandwidth for audio. */
    (void)esp_bt_gap_set_scan_mode(ESP_BT_NON_CONNECTABLE, ESP_BT_NON_DISCOVERABLE);

    lock_take(s_in_lock);   cb_ring_init(&s_in_ring);  lock_give(s_in_lock);
    lock_take(s_out_lock);  cb_ring_init(&s_out_ring); lock_give(s_out_lock);
    s_asm_len = 0; s_tx_seq = 0; s_out_epoch = 0; s_capture_epoch = 0;
    s_turn_pending = false; s_speech_frames = s_silence_frames = s_collected = 0;

    if (local_hfp_test_enabled()) {
        s_local_echo_sent = s_local_echo_drop = s_local_echo_bad = 0;
        esp_hf_audio_buff_t *stale = NULL;
        while (xQueueReceive(s_local_echo_q, &stale, 0) == pdTRUE) {
            esp_hf_client_audio_buff_free(stale);
        }
        ESP_ERROR_CHECK(esp_hf_client_register_audio_data_callback(hfp_audio_data_cb));
        ESP_LOGI(TAG, "BT AUDIO UP (mSBC) — %s, Wi-Fi bypassed", CONFIG_CB_CALL_MODE);
        return;
    }

    ESP_ERROR_CHECK(esp_hf_client_register_audio_data_callback(hfp_audio_data_cb));

    s_pending_call_start = true;
    esp_timer_start_periodic(s_out_timer, 7500);
    ESP_LOGI(TAG, "BT AUDIO UP (mSBC) — call.start queued");
}

static void bridge_on_audio_down(void)
{
    if (!local_hfp_test_enabled()) {
        (void)esp_timer_stop(s_out_timer);
    }
    s_audio_up = false;
    (void)esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_GENERAL_DISCOVERABLE);

    if (local_hfp_test_enabled()) {
        esp_hf_audio_buff_t *stale = NULL;
        while (xQueueReceive(s_local_echo_q, &stale, 0) == pdTRUE) {
            esp_hf_client_audio_buff_free(stale);
        }
        ESP_LOGW(TAG, "BT AUDIO DOWN — %s stopped", CONFIG_CB_CALL_MODE);
        return;
    }

    if (s_call_active && s_call_id[0]) {
        snprintf(s_pending_call_end_id, sizeof(s_pending_call_end_id), "%s", s_call_id);
        s_pending_call_end = true;
    }
    ESP_LOGW(TAG, "BT AUDIO DOWN — call.end queued");
}

static void hfp_cb(esp_hf_client_cb_event_t event, esp_hf_client_cb_param_t *param)
{
    switch (event) {
    case ESP_HF_CLIENT_CONNECTION_STATE_EVT:
        ESP_LOGI(TAG, "HF connection state %d peer_feat=0x%" PRIx32,
                 param->conn_stat.state, param->conn_stat.peer_feat);
        if (param->conn_stat.state == ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED &&
            local_hfp_test_enabled()) {
            /* Exact audio loopback can be suppressed by the phone's echo
             * canceller. Ask the AG to disable EC/NR for this diagnostic. */
            esp_err_t nrec = esp_hf_client_send_nrec();
            ESP_LOGI(TAG, "NREC=0 diagnostic request: %s", esp_err_to_name(nrec));
        }
        if (param->conn_stat.state == ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED) {
            (void)esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_GENERAL_DISCOVERABLE);
        }
        break;
    case ESP_HF_CLIENT_AUDIO_STATE_EVT:
        ESP_LOGI(TAG, "HF audio state %d preferred_frame_size=%u",
                 param->audio_stat.state, (unsigned)param->audio_stat.preferred_frame_size);
        if (param->audio_stat.state == ESP_HF_CLIENT_AUDIO_STATE_CONNECTED_MSBC) {
            bridge_on_audio_up(param->audio_stat.sync_conn_handle);
        } else if (param->audio_stat.state == ESP_HF_CLIENT_AUDIO_STATE_CONNECTED) {
            ESP_LOGE(TAG, "CVSD air mode — 8 kHz not supported by this bridge; hang up and pair with WBS/mSBC");
        } else if (param->audio_stat.state == ESP_HF_CLIENT_AUDIO_STATE_DISCONNECTED) {
            bridge_on_audio_down();
        }
        break;
    case ESP_HF_CLIENT_CIND_CALL_SETUP_EVT:
        ESP_LOGI(TAG, "call setup %d", param->call_setup.status);
        break;
    case ESP_HF_CLIENT_PKT_STAT_NUMS_GET_EVT:
        ESP_LOGI(TAG,
                 "SCO PKTS rx(total=%" PRIu32 ",ok=%" PRIu32 ",err=%" PRIu32
                 ",none=%" PRIu32 ",lost=%" PRIu32 ") tx(total=%" PRIu32
                 ",discard=%" PRIu32 ")",
                 param->pkt_nums.rx_total, param->pkt_nums.rx_correct,
                 param->pkt_nums.rx_err, param->pkt_nums.rx_none,
                 param->pkt_nums.rx_lost, param->pkt_nums.tx_total,
                 param->pkt_nums.tx_discarded);
        break;
    default:
        break;
    }
}

static void gap_cb(esp_bt_gap_cb_event_t event, esp_bt_gap_cb_param_t *param)
{
    switch (event) {
    case ESP_BT_GAP_AUTH_CMPL_EVT:
        if (param->auth_cmpl.stat == ESP_BT_STATUS_SUCCESS)
            ESP_LOGI(TAG, "paired with %s", param->auth_cmpl.device_name);
        else
            ESP_LOGE(TAG, "pairing failed status %d", param->auth_cmpl.stat);
        break;
    case ESP_BT_GAP_CFM_REQ_EVT:
        ESP_LOGI(TAG, "SSP confirm %06" PRIu32 " (auto-confirming)", param->cfm_req.num_val);
        esp_bt_gap_ssp_confirm_reply(param->cfm_req.bda, true);
        break;
    default:
        break;
    }
}

static void bt_init(void)
{
    ESP_ERROR_CHECK(esp_bt_controller_mem_release(ESP_BT_MODE_BLE));
    esp_bt_controller_config_t bt_cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_bt_controller_init(&bt_cfg));
    ESP_ERROR_CHECK(esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT));
    ESP_ERROR_CHECK(esp_bluedroid_init());
    ESP_ERROR_CHECK(esp_bluedroid_enable());

    ESP_ERROR_CHECK(esp_bt_gap_register_callback(gap_cb));
    ESP_ERROR_CHECK(esp_hf_client_register_callback(hfp_cb));
    ESP_ERROR_CHECK(esp_hf_client_init());

    /* Register the external-codec audio callback only after mSBC audio comes
     * up. This matches Espressif's HFP HF example instead of registering it
     * before an SCO connection exists. */
    ESP_ERROR_CHECK(esp_bt_dev_set_device_name("ESP_HFP_HF"));
    ESP_ERROR_CHECK(esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_GENERAL_DISCOVERABLE));
    ESP_LOGI(TAG, "BT up: discoverable as ESP_HFP_HF");
}

/* ---------- Wi-Fi (same as callbox_dev) ---------- */
static void on_wifi_event(void *arg, esp_event_base_t b, int32_t id, void *d)
{
    if (id == WIFI_EVENT_STA_START) esp_wifi_connect();
    if (id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "wifi lost, reconnecting");
        esp_wifi_connect();
    }
}

static void on_got_ip(void *arg, esp_event_base_t b, int32_t id, void *d)
{
    ip_event_got_ip_t *ev = d;
    ESP_LOGI(TAG, "GOT_IP " IPSTR, IP2STR(&ev->ip_info.ip));
    xEventGroupSetBits(s_events, EV_WIFI_OK);
}

static void wifi_init(void)
{
    ESP_ERROR_CHECK(nvs_flash_init());
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    (void)esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_got_ip, NULL));
    wifi_config_t wc = { 0 };
    strlcpy((char *)wc.sta.ssid, CONFIG_CB_WIFI_SSID, sizeof(wc.sta.ssid));
    strlcpy((char *)wc.sta.password, CONFIG_CB_WIFI_PASS, sizeof(wc.sta.password));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());

    /* HFP/SCO is latency-sensitive. Modem sleep can add avoidable scheduling
     * gaps while BT and Wi-Fi are already time-sharing the same 2.4 GHz radio. */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
}

/* ---------- main ---------- */
void app_main(void)
{
    s_events = xEventGroupCreate();
    s_rxq = xQueueCreate(RXQ_LEN, sizeof(rx_msg));
    s_ctrl_rxq = xQueueCreate(CTRL_RXQ_LEN, sizeof(rx_msg));
    s_msbc_q = xQueueCreate(MSBC_Q_LEN, sizeof(esp_hf_audio_buff_t *));
    s_local_echo_q = xQueueCreate(LOCAL_ECHO_Q_LEN, sizeof(esp_hf_audio_buff_t *));
    s_in_lock = xSemaphoreCreateMutex();
    s_out_lock = xSemaphoreCreateMutex();
    s_out_tick = xSemaphoreCreateBinary();
    cb_ring_init(&s_in_ring);
    cb_ring_init(&s_out_ring);

    const esp_timer_create_args_t timer_args = {
        .callback = out_timer_cb, .name = "out_tick",
    };
    ESP_ERROR_CHECK(esp_timer_create(&timer_args, &s_out_timer));

    if (strcmp(CONFIG_CB_CALL_MODE, "local_echo") != 0 &&
        strcmp(CONFIG_CB_CALL_MODE, "local_tone") != 0 &&
        strcmp(CONFIG_CB_CALL_MODE, "echo") != 0 &&
        strcmp(CONFIG_CB_CALL_MODE, "agent") != 0) {
        ESP_LOGE(TAG, "invalid CONFIG_CB_CALL_MODE=%s (use local_echo, local_tone, echo or agent)",
                 CONFIG_CB_CALL_MODE);
        return;
    }

    if (local_hfp_test_enabled()) {
        if (local_tone_enabled()) {
            if (sbc_init_msbc(&s_sbc_enc, 0L) != 0) {
                ESP_LOGE(TAG, "mSBC tone encoder init failed");
                return;
            }
            uint8_t pcm[240];
            for (int i = 0; i < 120; i++) {
                int16_t sample = ((i / 10) & 1) ? -9000 : 9000; /* 800 Hz square wave */
                memcpy(&pcm[i * 2], &sample, sizeof(sample));
            }
            ssize_t written = 0;
            if (sbc_encode(&s_sbc_enc, pcm, sizeof(pcm), s_local_tone_frame,
                           sizeof(s_local_tone_frame), &written) < 0 ||
                written != ESP_HF_MSBC_ENCODED_FRAME_SIZE) {
                ESP_LOGE(TAG, "failed to prepare local mSBC test tone");
                return;
            }
            s_local_tone_ready = true;
        }

        xTaskCreate(stats_task, "stats", 3072, NULL, 3, NULL);
        bt_init();
        ESP_LOGI(TAG, "LOCAL HFP TEST ready mode=%s — Wi-Fi/WebSocket disabled",
                 CONFIG_CB_CALL_MODE);
        ESP_LOGI(TAG, "local_echo: remote speech should return; local_tone: remote side should hear an 800 Hz tone");
        return;
    }

    if (sbc_init_msbc(&s_sbc_dec, 0L) != 0 || sbc_init_msbc(&s_sbc_enc, 0L) != 0) {
        ESP_LOGE(TAG, "sbc init failed");
        return;
    }
    s_sbc_ready = true;

    wifi_init();
    EventBits_t bits = xEventGroupWaitBits(s_events, EV_WIFI_OK, pdFALSE, pdFALSE,
                                           pdMS_TO_TICKS(30000));
    if (!(bits & EV_WIFI_OK)) { ESP_LOGE(TAG, "no Wi-Fi"); return; }

    /* Keep the default coexistence policy. Earlier experiments that blamed
     * RF/coexistence for the broken echo were superseded by hardware logs:
     * the fatal 15 ms WS write timeout and non-contiguous wire sequence were
     * the actual call-breaking bugs. Coexistence still affects throughput,
     * but it is not the primary correctness failure. */

    const esp_websocket_client_config_t ws_cfg = {
        .uri = CONFIG_CB_SERVER_URI,
        .buffer_size = 4096,
        .network_timeout_ms = 10000,
    };
    s_ws = esp_websocket_client_init(&ws_cfg);
    esp_websocket_register_events(s_ws, WEBSOCKET_EVENT_ANY, ws_event_handler, NULL);
    esp_websocket_client_start(s_ws);

    xTaskCreate(rx_pump_task, "rxpump", 6144, NULL, 5, NULL);
    xTaskCreate(codec_in_task, "codecin", 8192, NULL, 6, NULL);
    xTaskCreate(net_tx_task, "nettx", 8192, NULL, 5, NULL);
    xTaskCreate(codec_out_task, "codecout", 6144, NULL, 7, NULL);
    xTaskCreate(stats_task, "stats", 3072, NULL, 3, NULL);

    bt_init();
    ESP_LOGI(TAG, "bridge ready — pair your phone with ESP_HFP_HF");
}
