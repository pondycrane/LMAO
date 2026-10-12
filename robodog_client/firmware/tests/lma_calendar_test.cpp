// Host unit test for the family-calendar wire codec (CalendarRequest /
// CalEvent / CalendarBundle / reply LMAOEnvelope).
//
// Golden bytes verified against the reference Python encoder
// (cardputer_client/proto/lma_encoder.py) so the C++ port must produce
// byte-identical output (wire compatibility with the server's proto).
#include <cstdio>
#include <string>
#include <vector>

#include "lma_calendar.h"

static int failures = 0;

#define CHECK_EQ(actual, expected, label)                                    \
    do {                                                                     \
        std::string a = (actual);                                            \
        std::string e = std::string(expected);                               \
        if (a != e) {                                                        \
            std::printf("FAIL %s\n  actual  : '%s'\n  expected: '%s'\n",     \
                        label, a.c_str(), e.c_str());                        \
            failures++;                                                      \
        } else {                                                             \
            std::printf("ok   %s\n", label);                                 \
        }                                                                    \
    } while (0)

#define CHECK_TRUE(cond, label)                                              \
    do {                                                                     \
        if (!(cond)) {                                                       \
            std::printf("FAIL %s\n", label);                                 \
            failures++;                                                      \
        } else {                                                             \
            std::printf("ok   %s\n", label);                                 \
        }                                                                    \
    } while (0)

static std::string to_hex(const std::string& b) {
    static const char* H = "0123456789abcdef";
    std::string s;
    for (unsigned char c : b) {
        s.push_back(H[c >> 4]);
        s.push_back(H[c & 0x0f]);
    }
    return s;
}

// Golden bundle: calendar_id="family", fmt=1, watermark_ms=1700005000000,
// events:
//   [0] uid=u1 title=Swim notes=Pool start_ms=1700001000000
//       end_ms=1700004600000 pic=swim
//   [1] uid=u2 title=Old deleted=True
//   [2] uid=u3 title=Dentist   (minimal)
// Generated via:
//   encode_calendar_bundle("family", [
//       encode_cal_event(uid="u1", title="Swim", notes="Pool",
//                        start_ms=1700001000000, end_ms=1700004600000,
//                        pic="swim"),
//       encode_cal_event(uid="u2", title="Old", deleted=True),
//       encode_cal_event(uid="u3", title="Dentist"),
//   ], watermark_ms=1700005000000, fmt=1)
static const char* BUNDLE_HEX =
    "0801120666616d696c791a240a02753112045377696d1a04506f6f6c20c0d4d2ffbc31"
    "28c0b1ae81bd313a047377696d1a0b0a02753212034f6c6430011a0d0a0275331207"
    "44656e7469737420c0e6c681bd31";

static void check_bundle(const lma_calendar::CalendarBundle& b, const char* label) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%s format", label);
    CHECK_TRUE(b.format == 1, buf);
    std::snprintf(buf, sizeof(buf), "%s calendar_id", label);
    CHECK_TRUE(b.calendar_id == "family", buf);
    std::snprintf(buf, sizeof(buf), "%s event count", label);
    CHECK_TRUE(b.events.size() == 3, buf);
    std::snprintf(buf, sizeof(buf), "%s watermark", label);
    CHECK_TRUE(b.watermark_ms == 1700005000000ULL, buf);
    if (b.events.size() != 3) return;

    const lma_calendar::CalEvent& e0 = b.events[0];
    std::snprintf(buf, sizeof(buf), "%s ev0", label);
    CHECK_TRUE(e0.uid == "u1" && e0.title == "Swim" && e0.notes == "Pool" &&
               e0.start_ms == 1700001000000ULL &&
               e0.end_ms == 1700004600000ULL && !e0.deleted &&
               e0.pic == "swim", buf);

    const lma_calendar::CalEvent& e1 = b.events[1];
    std::snprintf(buf, sizeof(buf), "%s ev1", label);
    CHECK_TRUE(e1.uid == "u2" && e1.title == "Old" && e1.notes.empty() &&
               e1.start_ms == 0 && e1.end_ms == 0 && e1.deleted &&
               e1.pic.empty(), buf);

    const lma_calendar::CalEvent& e2 = b.events[2];
    std::snprintf(buf, sizeof(buf), "%s ev2", label);
    CHECK_TRUE(e2.uid == "u3" && e2.title == "Dentist" && e2.notes.empty() &&
               e2.start_ms == 0 && e2.end_ms == 0 && !e2.deleted &&
               e2.pic.empty(), buf);
}

int main() {
    using namespace lma_calendar;

    // (a) CalendarRequest{ calendar_id="family" } — defaults omitted.
    CHECK_EQ(to_hex(encode_calendar_request("family", 0, 0)),
             "0a0666616d696c79", "request defaults");

    // (b) CalendarRequest{ calendar_id="family", since_ms=1700000000000,
    //                    max_events=50 }.
    CHECK_EQ(to_hex(encode_calendar_request("family", 1700000000000ULL, 50)),
             "0a0666616d696c791080d095ffbc311832", "request full");

    // (c) LMAOEnvelope{ seq=7, request_ack=1, request=3{ calendar=3{...} } }.
    CHECK_EQ(to_hex(encode_calendar_request_envelope(7, "family",
                                                     1700000000000ULL, 50,
                                                     true)),
             "080710011a131a110a0666616d696c791080d095ffbc311832",
             "request envelope");

    // (d) Bundle roundtrip: golden bytes from the Python encoder must decode
    //     back to every field exactly.
    std::string bundle;
    for (const char* p = BUNDLE_HEX; p[0] && p[1]; p += 2) {
        auto nib = [](char c) -> unsigned {
            if (c >= '0' && c <= '9') return c - '0';
            if (c >= 'a' && c <= 'f') return c - 'a' + 10;
            return c - 'A' + 10;
        };
        bundle.push_back((char)((nib(p[0]) << 4) | nib(p[1])));
    }
    CalendarBundle b;
    CHECK_TRUE(decode_calendar_bundle((const uint8_t*)bundle.data(),
                                      bundle.size(), &b),
               "bundle decode ok");
    check_bundle(b, "bundle");

    // (e) Reply envelope: LMAOEnvelope{ seq=7, calendar=24(bundle) } built by
    //     the Python encoder (encode_field(1,0,varint(7)) +
    //     encode_field(24,2,encode_length_delimited(bundle))).
    std::string reply_hex =
        "0807c20153";
    reply_hex += BUNDLE_HEX;
    std::string reply;
    for (const char* p = reply_hex.c_str(); p[0] && p[1]; p += 2) {
        auto nib = [](char c) -> unsigned {
            if (c >= '0' && c <= '9') return c - '0';
            if (c >= 'a' && c <= 'f') return c - 'a' + 10;
            return c - 'A' + 10;
        };
        reply.push_back((char)((nib(p[0]) << 4) | nib(p[1])));
    }
    uint32_t seq = 0;
    CalendarBundle rb;
    CHECK_TRUE(decode_calendar_reply((const uint8_t*)reply.data(),
                                     reply.size(), &seq, &rb),
               "reply decode ok");
    CHECK_TRUE(seq == 7, "reply seq");
    check_bundle(rb, "reply bundle");

    // ── Negative tests: no out-of-bounds, false on truncation/garbage ──

    // Truncated bundle: every prefix that cuts mid-field must be rejected.
    // (Prefixes landing exactly on a field boundary are valid shorter
    // messages; the boundary offsets of BUNDLE_HEX are 0,2,10,48,61,76,83.)
    const size_t boundaries[] = {0, 2, 10, 48, 61, 76, 83};
    int trunc_fail = 0;
    for (size_t n = 1; n < bundle.size(); n++) {
        bool boundary = false;
        for (size_t bnd : boundaries) if (n == bnd) boundary = true;
        if (boundary) continue;
        CalendarBundle t;
        if (decode_calendar_bundle((const uint8_t*)bundle.data(), n, &t)) {
            trunc_fail++;
        }
    }
    CHECK_TRUE(trunc_fail == 0, "mid-field truncated bundle prefixes all rejected");

    // Garbage bytes.
    static const uint8_t garbage1[] = {0xff, 0xff, 0xff, 0xff, 0xff};
    CalendarBundle g;
    CHECK_TRUE(!decode_calendar_bundle(garbage1, sizeof(garbage1), &g),
               "garbage varint rejected");
    // Field 2 (string) claiming 200 bytes with only 3 present.
    static const uint8_t garbage2[] = {0x12, 0xc8, 0x01, 'a', 'b', 'c'};
    CHECK_TRUE(!decode_calendar_bundle(garbage2, sizeof(garbage2), &g),
               "overrun length rejected");
    // Truncated nested event (field 3 len longer than remaining).
    static const uint8_t garbage3[] = {0x1a, 0x24, 0x0a, 0x02, 'u', '1'};
    CHECK_TRUE(!decode_calendar_bundle(garbage3, sizeof(garbage3), &g),
               "truncated nested event rejected");
    // Truncated fixed32/fixed64 skip paths (unknown fields 100 wt5, 101 wt1).
    static const uint8_t garbage4[] = {0xa5, 0x06, 0x01, 0x02};  // f100 wt5, 2 bytes
    CHECK_TRUE(!decode_calendar_bundle(garbage4, sizeof(garbage4), &g),
               "truncated fixed32 skip rejected");
    static const uint8_t garbage5[] = {0xa9, 0x06, 0x01, 0x02};  // f101 wt1, 2 bytes
    CHECK_TRUE(!decode_calendar_bundle(garbage5, sizeof(garbage5), &g),
               "truncated fixed64 skip rejected");
    // Unknown wire type 3.
    static const uint8_t garbage6[] = {0x0b, 0x00};
    CHECK_TRUE(!decode_calendar_bundle(garbage6, sizeof(garbage6), &g),
               "unknown wire type rejected");
    // Zero-length field number.
    static const uint8_t garbage7[] = {0x00, 0x00};
    CHECK_TRUE(!decode_calendar_bundle(garbage7, sizeof(garbage7), &g),
               "zero field number rejected");

    // Reply envelope without a calendar payload → false.
    static const uint8_t no_cal[] = {0x08, 0x07};  // seq=7 only
    uint32_t s2 = 0;
    CalendarBundle b2;
    CHECK_TRUE(!decode_calendar_reply(no_cal, sizeof(no_cal), &s2, &b2),
               "reply without calendar rejected");
    // Truncated reply (calendar field header but payload cut off).
    static const uint8_t trunc_reply[] = {0x08, 0x07, (uint8_t)0xc2, 0x01, 0x53, 0x08};
    CHECK_TRUE(!decode_calendar_reply(trunc_reply, sizeof(trunc_reply), &s2, &b2),
               "truncated reply rejected");
    // Empty input.
    CHECK_TRUE(!decode_calendar_reply(nullptr, 0, &s2, &b2),
               "empty reply rejected");

    // Unknown fields interleaved in a reply are skipped (fixed32/fixed64/varint).
    std::string mixed;
    mixed.push_back((char)0x85); mixed.push_back((char)0x06);  // f96 wt5
    mixed.append(4, '\x00');
    mixed.push_back((char)0x89); mixed.push_back((char)0x06);  // f97 wt1
    mixed.append(8, '\x00');
    mixed.push_back((char)0x90); mixed.push_back((char)0x06);  // f98 wt0
    mixed.push_back((char)0x2a);
    mixed += reply;  // seq + calendar
    CalendarBundle mb;
    uint32_t mseq = 0;
    CHECK_TRUE(decode_calendar_reply((const uint8_t*)mixed.data(),
                                     mixed.size(), &mseq, &mb),
               "reply with unknown fields ok");
    CHECK_TRUE(mseq == 7, "reply unknown-fields seq");

    if (failures == 0) {
        std::printf("ALL PASS\n");
        return 0;
    }
    std::printf("%d failure(s)\n", failures);
    return 1;
}
