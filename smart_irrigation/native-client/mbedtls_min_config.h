/*
 * Minimal Mbed TLS configuration for the Sprout native-client host Bazel
 * build (issue #133, work item 4).
 *
 * Enables only the crypto subset that RTReticulum's host port consumes:
 *   - AES (CBC)            -> src/cryptography/aes.cpp
 *   - SHA-256 / SHA-512    -> src/cryptography/hashes.cpp  (one-shot)
 *   - MD (HMAC dispatch)   -> src/cryptography/hmac.cpp
 *   - HKDF                 -> src/cryptography/hkdf.cpp
 *
 * This is installed by the WORKSPACE http_archive patch as
 * include/mbedtls/bazel_min_config.h and selected via -DMBEDTLS_CONFIG_FILE.
 * No PSA / TLS / X.509 / bignum — everything RTReticulum does not use is
 * left off so the external dependency stays small and audit-friendly.
 */
#ifndef MBEDTLS_BAZEL_MIN_CONFIG_H
#define MBEDTLS_BAZEL_MIN_CONFIG_H

/* System support -------------------------------------------------------- */
#define MBEDTLS_HAVE_ASM

/* Feature support ------------------------------------------------------- */
#define MBEDTLS_CIPHER_MODE_CBC

/* Modules --------------------------------------------------------------- */
#define MBEDTLS_AES_C
#define MBEDTLS_SHA224_C
#define MBEDTLS_SHA256_C
#define MBEDTLS_SHA384_C
#define MBEDTLS_SHA512_C
#define MBEDTLS_MD_C
#define MBEDTLS_HKDF_C

#endif /* MBEDTLS_BAZEL_MIN_CONFIG_H */
