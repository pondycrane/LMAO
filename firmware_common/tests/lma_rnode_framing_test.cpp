// Host unit test for RNode wire framing and split reassembly.
//
// The rule this pins was measured on air, from the addressed node's own frame
// log, for an LMAF packet from the server's RNode:
//
//   RX frame 255 B header=0xcb split=1 tag=c0     <- first half, flagged
//   RX frame 102 B header=0x50 split=0 tag=50     <- second half, NOT flagged,
//                                                    different tag
//
// So a split is "a flagged frame, then the very next frame", and the second
// half carries neither the flag nor the first half's tag.  Implementations that
// require either — and that includes the host test this file used to be — can
// never reassemble those packets; the addressed node logged `dropping stale
// split fragment (254 B)` for exactly those halves all session long.  The
// assembler therefore pairs by "flagged opens, next frame completes".
#include <cstdio>
#include <string>

#include "lma_rnode_framing.h"

using namespace lma_attachment;

static int failures = 0;

static void check(bool ok, const char* label) {
    if (ok) {
        std::printf("ok   %s\n", label);
        return;
    }
    std::printf("FAIL %s\n", label);
    failures++;
}

static void check_u32(uint32_t actual, uint32_t expected, const char* label) {
    if (actual == expected) {
        std::printf("ok   %s\n", label);
        return;
    }
    std::printf("FAIL %s (actual %u, expected %u)\n", label, (unsigned)actual,
                (unsigned)expected);
    failures++;
}

// One on-air frame: header byte (tag in the upper nibble, split in bit 0) + payload.
static std::string frame(uint8_t tag, bool split, const std::string& payload) {
    std::string f;
    f.push_back((char)((tag & RNODE_SEQ_MASK) | (split ? RNODE_FLAG_SPLIT : 0)));
    f += payload;
    return f;
}

static RnodeFrame parse(const std::string& f) {
    return parse_rnode_frame((const uint8_t*)f.data(), f.size());
}

static void test_parse_header_bits() {
    const RnodeFrame single = parse(frame(0x30, false, "abc"));
    check(!single.split, "parse: unflagged frame is not split");
    check_u32(single.seq, 0x30, "parse: tag comes from the upper nibble");
    check(single.len == 3, "parse: payload excludes the header");

    const RnodeFrame split = parse(frame(0xD0, true, "abcd"));
    check(split.split, "parse: flagged frame is split");
    check_u32(split.seq, 0xD0, "parse: tag kept for a flagged frame");
}

static void test_single_frame_delivers() {
    RnodeSplitAssembler asm_;
    const auto r = asm_.push(parse(frame(0x10, false, "PACKET")), 1000);
    check(r.complete, "single: delivers immediately");
    check(r.data == "PACKET", "single: byte-identical payload");
    check_u32(asm_.in_flight(), 0, "single: nothing left in flight");
}

static void test_unflagged_partner_completes_pair() {
    // The measured server-wire case: first half flagged, partner unflagged
    // with a different tag.  255 + 102 = 357 B was the server's chart packet.
    RnodeSplitAssembler asm_;
    const auto first = asm_.push(parse(frame(0xC0, true, "AAA-")), 1000);
    check(!first.complete, "pair: first half waits for its partner");
    check_u32(asm_.in_flight(), 1, "pair: one fragment held");

    const auto second = asm_.push(parse(frame(0x50, false, "BBB")), 1001);
    check(second.complete, "pair: unflagged partner completes the packet");
    check(second.data == "AAA-BBB", "pair: halves concatenate in order");
    check_u32(asm_.completed(), 1, "pair: counted once");
    check_u32(asm_.in_flight(), 0, "pair: nothing left in flight");
}

static void test_flagged_partner_also_completes_pair() {
    // The nominal case (both halves flagged) still works.
    RnodeSplitAssembler asm_;
    asm_.push(parse(frame(0x20, true, "HELLO-")), 1000);
    const auto second = asm_.push(parse(frame(0x20, true, "WORLD")), 1001);
    check(second.complete && second.data == "HELLO-WORLD",
          "pair: a flagged partner completes too");
}

static void test_false_flag_consumes_neighbor() {
    // Trade-off of the empirical rule, asserted so it is a decision, not a bug:
    // a sender that flags a single frame (as the Sprout's DTU path does) makes
    // that frame consume its neighbor, producing a corrupted-but-bounded
    // assembly that Reticulum will UNPACK-FAIL.  Nothing is dropped or held
    // forever, and the assembler returns to an empty state.
    RnodeSplitAssembler asm_;
    asm_.push(parse(frame(0x40, true, "STRAY-")), 1000);
    const auto r = asm_.push(parse(frame(0x90, false, "REAL")), 1001);
    check(r.complete, "tradeoff: the false flag consumes the next frame");
    check(r.data == "STRAY-REAL", "tradeoff: corrupted-but-bounded assembly");
    check_u32(asm_.dropped(), 0, "tradeoff: nothing counted dropped");
    check_u32(asm_.in_flight(), 0, "tradeoff: assembler empty again");

    // A genuine pair afterwards is unaffected.
    asm_.push(parse(frame(0xA0, true, "HELLO-")), 1100);
    const auto ok = asm_.push(parse(frame(0x40, false, "WORLD")), 1101);
    check(ok.complete && ok.data == "HELLO-WORLD", "tradeoff: a real pair still works");
    check_u32(asm_.completed(), 2, "tradeoff: both assemblies counted");
}

static void test_flagged_frame_waits_and_expires() {
    RnodeSplitAssembler asm_;
    const auto stray = asm_.push(parse(frame(0x50, true, "STRAY")), 1000);
    check(!stray.complete, "held: a lone flagged frame completes nothing");

    // After the timeout the sweep retires it before the next frame is handled,
    // so the packet that follows still delivers cleanly.
    const auto late = asm_.push(parse(frame(0x10, false, "LATER")), 1600);
    check(late.stale, "held: the aged fragment is reported (so it can be logged)");
    check(late.complete && late.data == "LATER", "held: the next real packet delivers");
    check_u32(late.stale_len, 5, "held: stale length is the held half's size");
    check_u32(asm_.dropped(), 1, "held: counted as dropped, not silently ignored");

    // A fresh pair after the sweep completes normally.
    asm_.push(parse(frame(0x60, true, "REAL-")), 1700);
    const auto ok = asm_.push(parse(frame(0x60, true, "PAIR")), 1701);
    check(ok.complete && ok.data == "REAL-PAIR", "held: a fresh pair completes after the sweep");
}

static void test_oversized_assembly_is_refused() {
    RnodeSplitAssembler small(8, 500);
    small.push(parse(frame(0x80, true, "12345")), 1000);
    const auto r = small.push(parse(frame(0x30, false, "67890")), 1001);
    check(!r.complete, "oversize: nothing is delivered");
    check(r.dropped, "oversize: refusal is reported");
    check_u32(small.in_flight(), 0, "oversize: fragment discarded");
}

static void test_tag_reuse_after_completion() {
    RnodeSplitAssembler asm_;
    asm_.push(parse(frame(0xC0, true, "one-")), 1000);
    const auto first = asm_.push(parse(frame(0xC0, false, "two")), 1001);
    check(first.complete && first.data == "one-two", "reuse: first packet completes");

    asm_.push(parse(frame(0xC0, true, "three-")), 1100);
    const auto second = asm_.push(parse(frame(0xC0, true, "four")), 1101);
    check(second.complete && second.data == "three-four", "reuse: same tag works again");
}

int main() {
    test_parse_header_bits();
    test_single_frame_delivers();
    test_unflagged_partner_completes_pair();
    test_flagged_partner_also_completes_pair();
    test_false_flag_consumes_neighbor();
    test_flagged_frame_waits_and_expires();
    test_oversized_assembly_is_refused();
    test_tag_reuse_after_completion();

    if (failures) {
        std::printf("\n%d FAILURES\n", failures);
        return 1;
    }
    std::printf("\nall RNode framing checks passed\n");
    return 0;
}
