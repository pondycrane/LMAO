# LMAO-owned RoboDog overlay component for the ESP-Claw edge_agent.
#
# Phase 0 (spike) responsibilities:
#   - RD_SHOT capture publishing: the LMAO-owned overlay setup_device.c calls
#     robodog_rdshot_publish() after each 1-bpp frame flush; this component
#     streams the exact 15000 B ST7305 panel memory over the USB-JTAG console
#     using the RD_SHOT protocol so the host capture.py reconstructs the
#     display pixel-for-pixel (same contract as the hermes/calendar firmwares).
#   - Console probes proving the device-initiated model-endpoint HTTP paths
#     (DESIGN.md Phase 0): rd_asr (Whisper), rd_tts (Kokoro), rd_chat (LLM via
#     the native claw_agent_mgr per-turn API).
#
# Phase 1 (board + display) additions:
#   - Deterministic test-only UI (robodog_ui.c): brings the custom RLCD panel +
#     LVGL adapter up itself (esp-claw's display_service is gated behind a
#     Kconfig symbol the board manager drops for this custom panel), renders
#     PR #278 text-only parity, and renders the agent user/assistant transcript
#     stale-caption-safely.
#   - Deterministic RD_SHOT: publisher self-ticks and reads the panel's live
#     frame directly as a fallback, so a capture always has a frame.
idf_component_register(
    SRC_DIRS "src"
    INCLUDE_DIRS "include"
    REQUIRES claw_manager claw_core esp_http_client
        esp_lvgl_adapter lvgl esp_board_manager esp_netif
        audio_hub wifi_manager
    PRIV_REQUIRES console
)
