/* CallBox Gate B step 1: on-device synthetic loopback over Wi-Fi/WebSocket.
 * Mirrors scripts/device_simulator.py: authenticate, run an echo-mode call,
 * verify byte-exact returned frames, exercise interruption, end the call.
 * No Bluetooth, no codec work, no paid calls in this step.
 */
#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "cJSON.h"
#include "mbedtls/base64.h"
#include "esp_websocket_client.h"
#include "callbox_audio_ring.h"

static const char *TAG = "callbox";

static EventGroupHandle_t s_events;
#define EV_WIFI_OK BIT0
#define EV_WS_OPEN BIT1

#define RXQ_LEN 16
typedef struct {
    uint8_t *data;
    size_t len;
} rx_msg;
static QueueHandle_t s_rxq;

/* Partial-message reassembly */
static uint8_t s_accum[8192];
static size_t s_accum_len;

static esp_websocket_client_handle_t s_ws;
static char s_call_id[64];
static uint32_t s_seq;
static uint32_t s_epoch;

static void send_json(const cJSON *obj)
{
    char *str = cJSON_PrintUnformatted(obj);
    if (str) {
        esp_websocket_client_send_text(s_ws, str, strlen(str), portMAX_DELAY);
        cJSON_free(str);
    }
}

static void send_hello(void)
{
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "type", "hello");
    cJSON_AddStringToObject(o, "protocol", "callbox.v1");
    cJSON_AddStringToObject(o, "device_id", CONFIG_CB_DEVICE_ID);
    cJSON_AddStringToObject(o, "token", CONFIG_CB_DEVICE_TOKEN);
    send_json(o);
    cJSON_Delete(o);
}

static void send_raw_json(const char *fmt, ...)
{
    char buf[256];
    va_list args;
    va_start(args, fmt);
    vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);
    esp_websocket_client_send_text(s_ws, buf, strlen(buf), portMAX_DELAY);
}

static void send_audio_frame(void)
{
    uint8_t pcm[CB_FRAME_BYTES];
    for (int i = 0; i < CB_FRAME_BYTES; i += 2) {
        int16_t v = (int16_t)((s_seq % 50) * 100 + i);
        memcpy(&pcm[i], &v, 2);
    }
    unsigned char b64[((CB_FRAME_BYTES + 2) / 3) * 4 + 1];
    size_t b64_len = 0;
    mbedtls_base64_encode(b64, sizeof(b64), &b64_len, pcm, CB_FRAME_BYTES);

    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "type", "audio");
    cJSON_AddStringToObject(o, "call_id", s_call_id);
    cJSON_AddNumberToObject(o, "seq", s_seq);
    cJSON_AddNumberToObject(o, "epoch", s_epoch);
    cJSON_AddNumberToObject(o, "sample_rate", 16000);
    cJSON_AddNumberToObject(o, "channels", 1);
    cJSON_AddStringToObject(o, "pcm16", (const char *)b64);
    send_json(o);
    cJSON_Delete(o);
    s_seq++;
}

static void handle_message(const char *msg, size_t len)
{
    cJSON *root = cJSON_ParseWithLength(msg, len);
    if (!root) {
        ESP_LOGW(TAG, "unparseable rx (%d bytes)", (int)len);
        return;
    }
    const char *type = cJSON_GetStringValue(cJSON_GetObjectItem(root, "type"));
    if (!type) { cJSON_Delete(root); return; }

    if (strcmp(type, "ready") == 0) {
        ESP_LOGI(TAG, "READY");
        send_raw_json("{\"type\":\"call.start\",\"mode\":\"echo\",\"consent\":true}");
    } else if (strcmp(type, "call.started") == 0) {
        const char *cid = cJSON_GetStringValue(cJSON_GetObjectItem(root, "call_id"));
        snprintf(s_call_id, sizeof(s_call_id), "%s", cid ? cid : "");
        cJSON *ep = cJSON_GetObjectItem(root, "epoch");
        s_epoch = (uint32_t)(ep && cJSON_IsNumber(ep) ? ep->valuedouble : 0);
        s_seq = 0;
        ESP_LOGI(TAG, "CALL_STARTED id=%s epoch=%" PRIu32, s_call_id, s_epoch);
    } else if (strcmp(type, "audio.output") == 0) {
        /* Echo verification happens in the test loop via echo_verify queue */
        cJSON *pcm = cJSON_GetObjectItem(root, "pcm16");
        cJSON *sq = cJSON_GetObjectItem(root, "seq");
        if (pcm && sq && cJSON_IsNumber(sq)) {
            uint8_t out[CB_FRAME_BYTES];
            size_t olen = 0;
            if (mbedtls_base64_decode(out, sizeof(out), &olen,
                    (const unsigned char *)pcm->valuestring, strlen(pcm->valuestring)) == 0
                    && olen == CB_FRAME_BYTES) {
                uint8_t want[CB_FRAME_BYTES];
                uint32_t seq = (uint32_t)sq->valuedouble;
                for (int i = 0; i < CB_FRAME_BYTES; i += 2) {
                    int16_t v = (int16_t)((seq % 50) * 100 + i);
                    memcpy(&want[i], &v, 2);
                }
                if (memcmp(want, out, CB_FRAME_BYTES) == 0) {
                    ESP_LOGI(TAG, "ECHO_OK seq=%" PRIu32, seq);
                } else {
                    ESP_LOGE(TAG, "ECHO_MISMATCH seq=%" PRIu32, seq);
                }
            }
        }
    } else if (strcmp(type, "playback.clear") == 0) {
        cJSON *ep = cJSON_GetObjectItem(root, "epoch");
        s_epoch = (uint32_t)(ep && cJSON_IsNumber(ep) ? ep->valuedouble : s_epoch + 1);
        ESP_LOGI(TAG, "PLAYBACK_CLEAR new epoch=%" PRIu32, s_epoch);
    } else if (strcmp(type, "call.ended") == 0) {
        ESP_LOGI(TAG, "CALL_ENDED — loopback test complete");
    } else if (strcmp(type, "error") == 0) {
        const char *code = cJSON_GetStringValue(cJSON_GetObjectItem(root, "code"));
        const char *emsg = cJSON_GetStringValue(cJSON_GetObjectItem(root, "message"));
        ESP_LOGE(TAG, "SERVER_ERROR code=%s msg=%s", code ? code : "?", emsg ? emsg : "?");
    } else {
        ESP_LOGD(TAG, "rx type=%s", type);
    }
    cJSON_Delete(root);
}

static void ws_event_handler(void *handler_args, esp_event_base_t base,
                             int32_t event_id, void *event_data)
{
    esp_websocket_event_data_t *e = event_data;
    switch (event_id) {
    case WEBSOCKET_EVENT_CONNECTED:
        ESP_LOGI(TAG, "WS_CONNECTED — sending hello");
        send_hello();
        xEventGroupSetBits(s_events, EV_WS_OPEN);
        break;
    case WEBSOCKET_EVENT_DATA: {
        if (e->data_len <= 0 || e->op_code != 0x1) break; /* text frames only */
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
    }
    case WEBSOCKET_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "WS_DISCONNECTED");
        xEventGroupClearBits(s_events, EV_WS_OPEN);
        break;
    case WEBSOCKET_EVENT_ERROR:
        ESP_LOGE(TAG, "WS_ERROR");
        break;
    default:
        break;
    }
}

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

void app_main(void)
{
    s_events = xEventGroupCreate();
    s_rxq = xQueueCreate(RXQ_LEN, sizeof(rx_msg));

    wifi_init();
    EventBits_t bits = xEventGroupWaitBits(s_events, EV_WIFI_OK, pdFALSE, pdFALSE,
                                           pdMS_TO_TICKS(30000));
    if (!(bits & EV_WIFI_OK)) {
        ESP_LOGE(TAG, "no Wi-Fi in 30s, rebooting");
        esp_restart();
    }

    const esp_websocket_client_config_t ws_cfg = {
        .uri = CONFIG_CB_SERVER_URI,
        .buffer_size = 4096,
        .network_timeout_ms = 10000,
    };
    s_ws = esp_websocket_client_init(&ws_cfg);
    esp_websocket_register_events(s_ws, WEBSOCKET_EVENT_ANY, ws_event_handler, NULL);
    esp_websocket_client_start(s_ws);

    bits = xEventGroupWaitBits(s_events, EV_WS_OPEN, pdFALSE, pdFALSE,
                               pdMS_TO_TICKS(15000));
    if (!(bits & EV_WS_OPEN)) {
        ESP_LOGE(TAG, "no WebSocket in 15s, rebooting");
        esp_restart();
    }

    /* Test sequence: wait ready/call.started, stream frames, interrupt, end. */
    bool started = false;
    int sent = 0, wait_ready_ms = 0;
    while (1) {
        rx_msg m;
        if (xQueueReceive(s_rxq, &m, pdMS_TO_TICKS(25)) == pdTRUE) {
            handle_message((const char *)m.data, m.len);
            free(m.data);
            if (s_call_id[0] != '\0') started = true;
            continue;
        }
        wait_ready_ms += 25;
        if (!started) {
            if (wait_ready_ms > 20000) {
                ESP_LOGE(TAG, "call never started");
                break;
            }
            continue;
        }
        if (sent == 60) {
            ESP_LOGI(TAG, "sending interrupt at frame %d", sent);
            send_raw_json("{\"type\":\"interrupt\",\"call_id\":\"%s\"}", s_call_id);
        }
        if (sent < CONFIG_CB_TEST_FRAMES) {
            send_audio_frame();
            sent++;
            vTaskDelay(pdMS_TO_TICKS(20)); /* 20 ms frame pacing */
        } else if (sent == CONFIG_CB_TEST_FRAMES) {
            send_raw_json("{\"type\":\"call.end\",\"call_id\":\"%s\"}", s_call_id);
            sent++;
        } else {
            vTaskDelay(pdMS_TO_TICKS(1000)); /* idle heartbeat window */
            esp_websocket_client_send_text(s_ws, "{\"type\":\"ping\"}",
                                           sizeof("{\"type\":\"ping\"}") - 1, portMAX_DELAY);
        }
    }
    ESP_LOGI(TAG, "done: sent %d frames, epoch now %" PRIu32, sent, s_epoch);
}
