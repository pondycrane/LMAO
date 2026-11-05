# ESP-IDF component: Reticulum TCPClientInterface (lwIP BSD sockets).
# Shared with the POSIX host harness — the same source compiles both ways
# (see firmware_common/tcp_interface/tcp_interface.cpp), so the board firmware
# and the host interop tool cannot drift apart.
idf_component_register(
    SRCS "tcp_interface.cpp"
    INCLUDE_DIRS "."
    REQUIRES
        rtreticulum
        lwip
        esp_timer
        log
)

target_compile_options(${COMPONENT_LIB} PRIVATE -fexceptions)
