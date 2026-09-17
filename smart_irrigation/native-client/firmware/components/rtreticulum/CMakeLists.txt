# ESP-IDF component wrapping the RTReticulum library.
# Pulls source files from ../../../../ (the repo root) and adds the ESP-IDF
# HAL backend. Vendored monocypher source is built in-place; mbedtls comes
# from ESP-IDF's bundled component.

set(RTRETICULUM_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../../..")

idf_component_register(
    SRCS
        "${RTRETICULUM_ROOT}/src/log.cpp"
        "${RTRETICULUM_ROOT}/src/os.cpp"
        "${RTRETICULUM_ROOT}/src/crc.cpp"
        "${RTRETICULUM_ROOT}/src/memory.cpp"
        "${RTRETICULUM_ROOT}/src/bytes.cpp"
        "${RTRETICULUM_ROOT}/src/cryptography/hashes.cpp"
        "${RTRETICULUM_ROOT}/src/cryptography/hmac.cpp"
        "${RTRETICULUM_ROOT}/src/cryptography/hkdf.cpp"
        "${RTRETICULUM_ROOT}/src/cryptography/aes.cpp"
        "${RTRETICULUM_ROOT}/src/cryptography/ed25519.cpp"
        "${RTRETICULUM_ROOT}/src/cryptography/x25519.cpp"
        "${RTRETICULUM_ROOT}/src/cryptography/fernet.cpp"
        "${RTRETICULUM_ROOT}/src/cryptography/token.cpp"
        "${RTRETICULUM_ROOT}/src/identity.cpp"
        "${RTRETICULUM_ROOT}/src/destination.cpp"
        "${RTRETICULUM_ROOT}/src/packet.cpp"
        "${RTRETICULUM_ROOT}/src/interface.cpp"
        "${RTRETICULUM_ROOT}/src/transport.cpp"
        "${RTRETICULUM_ROOT}/src/link.cpp"
        "${RTRETICULUM_ROOT}/src/msgpack.cpp"
        "${RTRETICULUM_ROOT}/src/resource.cpp"
        "${RTRETICULUM_ROOT}/src/reticulum.cpp"
        "${RTRETICULUM_ROOT}/src/filesystem.cpp"
        "${RTRETICULUM_ROOT}/src/interfaces/loopback.cpp"
        "${RTRETICULUM_ROOT}/third_party/tlsf/tlsf.c"
        "${RTRETICULUM_ROOT}/third_party/monocypher/monocypher.c"
        "${RTRETICULUM_ROOT}/third_party/monocypher/monocypher-ed25519.c"
        "${RTRETICULUM_ROOT}/port/esp_idf/hal_esp_idf.c"
    INCLUDE_DIRS
        "${RTRETICULUM_ROOT}/include"
        "${RTRETICULUM_ROOT}/third_party/tlsf"
        "${RTRETICULUM_ROOT}/third_party/monocypher"
    REQUIRES
        freertos
        esp_timer
        esp_hw_support
        log
        mbedtls
)

# RTReticulum core uses C++ exceptions and RTTI in a few places (Identity
# error paths, doctest test build). The library itself doesn't strictly
# need exceptions on the embedded target, but enabling them avoids a
# rewrite of every throw site for now.
target_compile_options(${COMPONENT_LIB} PRIVATE
    -fexceptions
    -Wall
    -Wno-unused-parameter
)

# Force C++17 for the component
set_source_files_properties(
    "${RTRETICULUM_ROOT}/src/log.cpp"
    "${RTRETICULUM_ROOT}/src/os.cpp"
    "${RTRETICULUM_ROOT}/src/bytes.cpp"
    "${RTRETICULUM_ROOT}/src/cryptography/hashes.cpp"
    "${RTRETICULUM_ROOT}/src/cryptography/hmac.cpp"
    "${RTRETICULUM_ROOT}/src/cryptography/hkdf.cpp"
    "${RTRETICULUM_ROOT}/src/cryptography/aes.cpp"
    "${RTRETICULUM_ROOT}/src/cryptography/ed25519.cpp"
    "${RTRETICULUM_ROOT}/src/cryptography/x25519.cpp"
    "${RTRETICULUM_ROOT}/src/cryptography/fernet.cpp"
    "${RTRETICULUM_ROOT}/src/cryptography/token.cpp"
    "${RTRETICULUM_ROOT}/src/identity.cpp"
    "${RTRETICULUM_ROOT}/src/destination.cpp"
    "${RTRETICULUM_ROOT}/src/packet.cpp"
    "${RTRETICULUM_ROOT}/src/interface.cpp"
    "${RTRETICULUM_ROOT}/src/transport.cpp"
    "${RTRETICULUM_ROOT}/src/link.cpp"
    "${RTRETICULUM_ROOT}/src/msgpack.cpp"
    "${RTRETICULUM_ROOT}/src/resource.cpp"
    "${RTRETICULUM_ROOT}/src/reticulum.cpp"
    "${RTRETICULUM_ROOT}/src/memory.cpp"
    "${RTRETICULUM_ROOT}/src/crc.cpp"
    "${RTRETICULUM_ROOT}/src/filesystem.cpp"
    "${RTRETICULUM_ROOT}/src/interfaces/loopback.cpp"
    PROPERTIES COMPILE_FLAGS "-std=gnu++17"
)
