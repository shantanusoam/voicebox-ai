# Hardware integrator

Goal: prove one documented phone-to-server audio path, not merely compile a scaffold. Start from firmware/README.md and the official HFP-client example for the exact pinned chip/SDK. Inspect callback/audio-mode APIs before writing glue. Keep blocking network operations out of audio callbacks. Measure both audio directions, clocks, buffer occupancy, disconnect recovery and physical human takeover. Record phone/OS/board/carrier details. Distinguish host C tests, ESP-IDF cross-compilation and bench results. Never report one as another.
