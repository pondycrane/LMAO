# Bazel build file for the pinned @mbedtls http_archive (Mbed TLS 3.6.5).
#
# Sprout native-client only uses the narrow crypto subset that RTReticulum's
# host port consumes (issue #133, work item 4):
#   AES-CBC, SHA-256/512, MD (HMAC dispatch), HKDF.
# We therefore compile just those library/ sources against the minimal config
# installed by the WORKSPACE patch (include/mbedtls/bazel_min_config.h),
# selected via -DMBEDTLS_CONFIG_FILE. This keeps the external dep small and
# hermetic; the full libmbedcrypto is not pulled in.

cc_library(
    name = "crypto",
    srcs = [
        "library/aes.c",
        "library/constant_time.c",
        "library/hkdf.c",
        "library/md.c",
        "library/sha256.c",
        "library/sha512.c",
        "library/platform_util.c",
    ],
    hdrs = glob([
        "include/mbedtls/*.h",
        "include/psa/*.h",
        "library/common.h",
        "library/md_wrap.h",
        "library/md_psa.h",
        "library/psa_crypto_core.h",
        "library/psa_util_internal.h",
        "library/*.h",
    ]),
    includes = [
        "include",
        "library",
    ],
    # The WORKSPACE patch replaces include/mbedtls/mbedtls_config.h with the
    # minimal config (mbedtls_min_config.h), so build_info.h's default include
    # picks it up — no -DMBEDTLS_CONFIG_FILE needed.
    copts = ["-Wno-unused-parameter"],
    visibility = ["//visibility:public"],
)
