idf_component_register(SRCS "callbox_main.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp_wifi esp_event esp_netif nvs_flash json mbedtls esp_websocket_client callbox_ring)
