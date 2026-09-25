// Host unit test for the inbound-LXM msgpack reader (lma_lxm).
//
// The golden payload is msgpack generated from the reference LXMF frame shape
// [ts, title, content, fields] — see proto/lma_messages.proto for the LMAF
// envelope carried in `content`.
#include <cstdio>
#include <string>

#include "lma_lxm.h"

static int failures = 0;

static void check(bool ok, const char* label) {
    if (ok) {
        std::printf("ok   %s\n", label);
        return;
    }
    std::printf("FAIL %s\n", label);
    failures++;
}

static std::string from_hex(const char* h) {
    auto nib = [](char c) -> int {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        return 0;
    };
    std::string out;
    for (size_t i = 0; h[i] && h[i + 1]; i += 2) {
        out.push_back((char)((nib(h[i]) << 4) | nib(h[i + 1])));
    }
    return out;
}

// 33-byte LMAF chunk envelope (field 41, 30-byte chunk) carried as `content`.
static const char* CONTENT_HEX =
    "ca021e0a10000102030405060708090a0b0c0d0e0f10011a03010203251d80bc55";

// msgpack.packb([1758700000.5, b"p:Envelope", content, {}]) — reference LXMF.
static const char* PAYLOAD_FLOAT_HEX =
    "94cb41da34e878200000c40a703a456e76656c6f7065c421"
    "ca021e0a10000102030405060708090a0b0c0d0e0f10011a03010203251d80bc55"
    "80";

// msgpack.packb([1758700000, "p:Envelope", content, {}]) — int ts, str title.
static const char* PAYLOAD_INT_STR_HEX =
    "94ce68d3a1e0aa703a456e76656c6f7065c421"
    "ca021e0a10000102030405060708090a0b0c0d0e0f10011a03010203251d80bc55"
    "80";

// Same, with a trailing 4-byte stamp element (5-element array).
static const char* PAYLOAD_STAMP_HEX =
    "95ce68d3a1e0c40a703a456e76656c6f7065c421"
    "ca021e0a10000102030405060708090a0b0c0d0e0f10011a03010203251d80bc55"
    "80c40400000000";

// The same envelope carried in `fields` (map) instead of `content`: the reader
// must not confuse the two.
static const char* PAYLOAD_MAP_HEX =
    "94ce68d3a1e0c40a703a456e76656c6f7065c40081fc2c01"
    "ca021e0a10000102030405060708090a0b0c0d0e0f10011a03010203251d80bc55";

int main() {
    const std::string content = from_hex(CONTENT_HEX);
    std::string title, out;

    // Reference frame over the radio: decrypted plaintext carries a 80-byte
    // source-hash + signature header before the msgpack payload.
    const std::string plaintext = std::string(80, '\x11') + from_hex(PAYLOAD_FLOAT_HEX);
    check(lma_lxm::extract_content(plaintext, &title, &out), "extract: reference frame");
    check(title == "p:Envelope", "extract: title");
    check(out == content, "extract: content is the LMAF envelope");

    check(lma_lxm::parse_payload(from_hex(PAYLOAD_FLOAT_HEX), &title, &out), "parse: float ts");
    check(title == "p:Envelope" && out == content, "parse: float ts fields");

    check(lma_lxm::parse_payload(from_hex(PAYLOAD_INT_STR_HEX), &title, &out), "parse: int ts, str title");
    check(title == "p:Envelope" && out == content, "parse: int ts fields");

    check(lma_lxm::parse_payload(from_hex(PAYLOAD_STAMP_HEX), &title, &out), "parse: stamped 5-element array");
    check(title == "p:Envelope" && out == content, "parse: stamped fields");

    check(lma_lxm::parse_payload(from_hex(PAYLOAD_MAP_HEX), &title, &out), "parse: envelope in fields");
    check(out.empty(), "parse: empty content not mistaken for the payload");

    // Hostile / truncated inputs must fail closed, never crash or read out of
    // bounds.  Every prefix of a valid payload is exercised.
    const std::string full = from_hex(PAYLOAD_STAMP_HEX);
    bool all_prefixes_fail = true;
    for (size_t cut = 0; cut < full.size(); cut++) {
        std::string truncated = full.substr(0, cut);
        std::string t, c;
        if (lma_lxm::parse_payload(truncated, &t, &c)) {
            // Only accept a prefix that genuinely carries a complete array
            // prefix (msgpack allows trailing truncation of later elements,
            // but not of title/content).
            if (c != content) { all_prefixes_fail = false; break; }
        }
    }
    check(all_prefixes_fail, "parse: truncated inputs never yield a wrong content");

    check(!lma_lxm::parse_payload(std::string(), &title, &out), "parse: empty payload");
    check(!lma_lxm::parse_payload(std::string("\x01\x02\x03", 3), &title, &out), "parse: not an array");
    check(!lma_lxm::parse_payload(std::string("\x92\x01\x02", 3), &title, &out), "parse: too few elements");
    check(!lma_lxm::parse_payload(std::string("\x94\xc1", 2), &title, &out), "parse: unsupported 0xc1");
    check(!lma_lxm::extract_content(std::string(80, '\0'), &title, &out), "extract: header only");
    check(!lma_lxm::extract_content(from_hex(PAYLOAD_FLOAT_HEX), &title, &out),
          "extract: missing LXM header");

    if (failures) {
        std::printf("\n%d FAILURES\n", failures);
        return 1;
    }
    std::printf("\nall lma_lxm checks passed\n");
    return 0;
}
