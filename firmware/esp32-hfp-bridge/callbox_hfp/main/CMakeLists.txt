idf_component_register(SRCS "main.c" "playback_queue.c" "adpcm.c"
                       INCLUDE_DIRS "."
                       REQUIRES bt esp_wifi esp_event esp_netif nvs_flash json mbedtls esp_websocket_client callbox_ring sbc esp_timer)
