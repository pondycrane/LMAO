# RoboDog LVGL UI framework firmware — PR 1 scope (UI only).
#
# Data source for this PR: the demo dataset below (static events + ticking
# clock) so the screen renders without any network dependency. The framed-TCP
# GetCalendar client (lma_proto, the same wire protocol the stable
# robodog_native board build already speaks) and the MicroPython/urns RNS-TCP
# runtime land in the follow-up PR.
set(SRCS
    "main.cpp"
    "lma_proto.c"
    "robodog_core.c"
    "st7305_disp.cpp"
    "calendar_ui.cpp"
    "calendar_model.cpp"
)

idf_component_register(
    SRCS ${SRCS}
    INCLUDE_DIRS "."
    EMBED_TXTFILES "ui_xml/calendar_screen.xml"
    REQUIRES
        lvgl
        nvs_flash
        esp_timer
        esp_wifi
        esp_event
        esp_netif
        lwip
        esp_driver_spi
        esp_driver_gpio
        log
)

# LVGL internals use exceptions on some code paths; keep the default off but
# compile the framework units with exceptions enabled to match the binding.
target_compile_options(${COMPONENT_LIB} PRIVATE -fexceptions)
