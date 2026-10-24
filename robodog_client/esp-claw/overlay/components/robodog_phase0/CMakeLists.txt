# LMAO-owned RoboDog Phase 0 overlay component for the ESP-Claw edge_agent.
#
# Phase 0 (spike) responsibilities:
#   - RD_SHOT capture publishing: the patched waveshare setup_device.c calls
#     robodog_rdshot_publish() after each 1-bpp frame flush; this component
#     streams the exact 15000 B ST7305 panel memory over the USB-JTAG console
#     using the RD_SHOT protocol so the host capture.py reconstructs the
#     display pixel-for-pixel (the same capture contract as the hermes and
#     calendar RoboDog firmwares).
#   - Console probes proving the device-initiated model-endpoint HTTP paths
#     (DESIGN.md Phase 0): rd_asr (device -> Whisper), rd_tts (device ->
#     Kokoro), rd_chat (device -> LLM via the native claw_agent_mgr per-turn
#     API, the chat-injection seam these probes validate).
idf_component_register(
    SRC_DIRS "src"
    INCLUDE_DIRS "include"
    REQUIRES claw_manager claw_core esp_http_client
    PRIV_REQUIRES console
)
