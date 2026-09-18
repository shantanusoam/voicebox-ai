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
static volatile bool s_turn_pending;     /* agent: commit sent, awaiting turn.done */
static uint32_t s_tx_seq, s_out_epoch;
static uint32_t s_turn_counter;

/* rings (external sync required by the ring contract) */
static cb_audio_ring s_in_ring, s_out_ring;
static SemaphoreHandle_t s_in_lock, s_out_lock;

/* mSBC frames from the BT callback */
#define MSBC_Q_LEN 24
static QueueHandle_t s_msbc_q;

static sbc_t s_sbc_dec, s_sbc_enc;
static bool s_sbc_ready;

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
#define RXQ_LEN 20
typedef struct { uint8_t *data; size_t len; } rx_msg;
static QueueHandle_t s_rxq;
static uint8_t s_accum[8192];
static size_t s_accum_len;

static void send_text(const char *str)
{
    esp_websocket_client_send_text(s_ws, str, strlen(str), portMAX_DELAY);
}

static void send_audio_frame(const uint8_t *pcm, uint32_t seq)
{
    static unsigned char b64[((CB_FRAME_BYTES + 2) / 3) * 4 + 1]; /* single net-tx writer */
    static char msg[4096];
    size_t b64_len = 0;
    if (mbedtls_base64_encode(b64, sizeof(b64), &b64_len, pcm, CB_FRAME_BYTES) != 0) return;

    int n = snprintf(msg, sizeof(msg),
        "{\"type\":\"audio\",\"call_id\":\"%s\",\"seq\":%" PRIu32
        ",\"epoch\":0,\"sample_rate\":16000,\"channels\":1,\"pcm16\":\"%s\"}",
        s_call_id, seq, b64);
    if (n > 0 && n < (int)sizeof(msg)) send_text(msg);
}

static void send_simple(const char *fmt, ...)
{
    char buf[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    send_text(buf);
}

static void handle_message(const char *msg, size_t len)
{
    cJSON *root = cJSON_ParseWithLength(msg, len);
    if (!root) return;
    const char *type = cJSON_GetStringValue(cJSON_GetObjectItem(root, "type"));
    if (!type) { cJSON_Delete(root); return; }

    if (strcmp(type, "ready") == 0) {
        ESP_LOGI(TAG, "READY (device authenticated)");
    } else if (strcmp(type, "call.started") == 0) {
        const char *cid = cJSON_GetStringValue(cJSON_GetObjectItem(root, "call_id"));
        snprintf(s_call_id, sizeof(s_call_id), "%s", cid ? cid : "");
        s_tx_seq = 0;
        s_call_active = true;
        s_turn_pending = false;
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
        if (code && strcmp(code, "frame") == 0) {
            /* Frame errors must be reconciled, not blindly incremented. */
            s_tx_seq = 0;
        }
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
        if (frames < 0 || written != 240) { ESP_LOGW(TAG, "msbc decode fail %d", (int)frames); continue; }

        /* agent mode: drop input while a generated turn is in flight */
        bool collect = s_call_active && !s_turn_pending &&
                       strcmp(CONFIG_CB_CALL_MODE, "agent") == 0;

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
                    if (s_collected >= 5 && s_collected >= 5 &&
                        (s_silence_frames >= 40 || s_collected >= 750)) {
                        s_turn_pending = true;
                        char rid[40];
                        snprintf(rid, sizeof(rid), "turn-%" PRIu32, ++s_turn_counter);
                        send_simple("{\"type\":\"audio.commit\",\"call_id\":\"%s\",\"request_id\":\"%s\"}",
                                    s_call_id, rid);
                        ESP_LOGI(TAG, "COMMIT %s after %d frames", rid, s_collected);
                        s_speech_frames = s_silence_frames = s_collected = 0;
                    }
                }

                lock_take(s_in_lock);
                cb_ring_push(&s_in_ring, frame, CB_FRAME_BYTES, s_tx_seq, 0);
                lock_give(s_in_lock);
                s_tx_seq++;
            }
        }
    }
}

static void net_tx_task(void *arg)
{
    cb_frame f;
    uint32_t idle_ticks = 0;
    while (1) {
        bool got = false;
        lock_take(s_in_lock);
        got = cb_ring_pop(&s_in_ring, &f);
        lock_give(s_in_lock);

        if (got && s_call_active && !s_turn_pending) {
            send_audio_frame(f.data, f.sequence);
            idle_ticks = 0;
        } else if (got) {
            /* turn in flight or no call: drop and reconcile */
        } else {
            vTaskDelay(pdMS_TO_TICKS(10));
            if (++idle_ticks >= 3000) { /* ~30 s */
                idle_ticks = 0;
                send_simple("{\"type\":\"ping\"}");
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
            rx_msg m = { .data = malloc(s_accum_len), .len = s_accum_len };
            if (m.data) {
                memcpy(m.data, s_accum, s_accum_len);
                if (xQueueSend(s_rxq, &m, 0) != pdTRUE) free(m.data);
            }
            s_accum_len = 0;
        }
        break;
    case WEBSOCKET_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "WS_DISCONNECTED");
        xEventGroupClearBits(s_events, EV_WS_OPEN);
        s_call_active = false;
        s_call_id[0] = '\0';
        break;
    case WEBSOCKET_EVENT_ERROR:
        ESP_LOGE(TAG, "WS_ERROR");
        break;
    default:
        break;
    }
}

static void rx_pump_task(void *arg)
{
    while (1) {
        rx_msg m;
        if (xQueueReceive(s_rxq, &m, portMAX_DELAY) == pdTRUE) {
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
    if (bad || xQueueSend(s_msbc_q, &buf, 0) != pdTRUE) {
        esp_hf_client_audio_buff_free(buf);
    }
}

static void bridge_on_audio_up(esp_hf_sync_conn_hdl_t hdl)
{
    s_hfp_hdl = hdl;
    s_audio_up = true;
    lock_take(s_in_lock);   cb_ring_init(&s_in_ring);  lock_give(s_in_lock);
    lock_take(s_out_lock);  cb_ring_init(&s_out_ring); lock_give(s_out_lock);
    s_asm_len = 0; s_tx_seq = 0; s_out_epoch = 0;
    s_turn_pending = false; s_speech_frames = s_silence_frames = s_collected = 0;
    send_simple("{\"type\":\"call.start\",\"mode\":\"%s\",\"consent\":true}",
                CONFIG_CB_CALL_MODE);
    esp_timer_start_periodic(s_out_timer, 7500);
    ESP_LOGI(TAG, "BT AUDIO UP (mSBC) — call.start sent");
}

static void bridge_on_audio_down(void)
{
    esp_timer_stop(s_out_timer);
    s_audio_up = false;
    if (s_call_active && s_call_id[0]) {
        send_simple("{\"type\":\"call.end\",\"call_id\":\"%s\"}", s_call_id);
    }
    ESP_LOGW(TAG, "BT AUDIO DOWN — call.end sent");
}

static void hfp_cb(esp_hf_client_cb_event_t event, esp_hf_client_cb_param_t *param)
{
    switch (event) {
    case ESP_HF_CLIENT_CONNECTION_STATE_EVT:
        ESP_LOGI(TAG, "HF connection state %d", param->conn_stat.state);
        break;
    case ESP_HF_CLIENT_AUDIO_STATE_EVT:
        ESP_LOGI(TAG, "HF audio state %d", param->audio_stat.state);
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
    ESP_ERROR_CHECK(esp_hf_client_register_audio_data_callback(hfp_audio_data_cb));

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
}

/* ---------- main ---------- */
void app_main(void)
{
    s_events = xEventGroupCreate();
    s_rxq = xQueueCreate(RXQ_LEN, sizeof(rx_msg));
    s_msbc_q = xQueueCreate(MSBC_Q_LEN, sizeof(esp_hf_audio_buff_t *));
    s_in_lock = xSemaphoreCreateMutex();
    s_out_lock = xSemaphoreCreateMutex();
    s_out_tick = xSemaphoreCreateBinary();
    cb_ring_init(&s_in_ring);
    cb_ring_init(&s_out_ring);

    const esp_timer_create_args_t timer_args = {
        .callback = out_timer_cb, .name = "out_tick",
    };
    ESP_ERROR_CHECK(esp_timer_create(&timer_args, &s_out_timer));

    if (sbc_init_msbc(&s_sbc_dec, 0L) != 0 || sbc_init_msbc(&s_sbc_enc, 0L) != 0) {
        ESP_LOGE(TAG, "sbc init failed");
        return;
    }
    s_sbc_ready = true;

    wifi_init();
    EventBits_t bits = xEventGroupWaitBits(s_events, EV_WIFI_OK, pdFALSE, pdFALSE,
                                           pdMS_TO_TICKS(30000));
    if (!(bits & EV_WIFI_OK)) { ESP_LOGE(TAG, "no Wi-Fi"); return; }

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

    bt_init();
    ESP_LOGI(TAG, "bridge ready — pair your phone with ESP_HFP_HF");
}
