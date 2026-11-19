#pragma once

#include <cstddef>
#include <cstdint>

// HLW8032 energy-metering frame parser for the M5Stack Atom Socket.
//
// The chip transmits 24-byte frames continuously at 4800 baud 8E1:
//   [0] 0x55 sync, [1] 0x5A, [2..4] voltage parameter, [5..7] voltage data,
//   [8..10] current parameter, [11..13] current data, [14..16] power parameter,
//   [17..19] power data, [20] data-update register (bit7 = energy pulse),
//   [21..22] power factor, [23] checksum = sum(bytes 2..22) & 0xFF.
//
// Scaling matches the M5Stack driver (AtomSocket.cpp), which is what the
// factory firmware on this unit reports (118.38 V on US mains):
//   V = VolPar/VolData * (R1/R2/1000), R1=1.88 MOhm, R2=1 kOhm
//   A = CurPar/CurData * 1/(Rf*1000) - 0.06, Rf=0.001
//   W = PowPar/PowData * VF * CF
// A zero *Data divisor means "no load" (the chip has nothing to divide), so
// current/power read 0 instead of NaN.
namespace hlw8032 {

    constexpr size_t kFrameLen = 24;

    struct Sample {
        bool valid = false;
        float voltage = 0.0f;       // V
        float current_a = 0.0f;     // A
        float power_w = 0.0f;       // W
        uint16_t pf_raw = 0;        // power-factor register (scale unverified)
        uint32_t energy_pulses = 0; // accumulated frame[20] bit7 transitions
        bool meter_fresh = false;   // frame arrived within HF_METER_STALE_MS
    };

    // Validate + decode one complete frame.  Returns false on a short frame,
    // a missing 0x55/0x5A header, or a checksum mismatch.
    bool parse_frame(const uint8_t* frame, size_t len, Sample* out);

    // Incremental byte assembler with resync.  Feed bytes from the UART; when
    // a checksum-valid frame completes, it is copied to *out* and true is
    // returned.  A false 0x55 inside payload data cannot wedge the parser:
    // a bad frame drops its first byte and rescans for the next header pair.
    class FrameAssembler {
    public:
        bool feed(uint8_t byte, uint8_t out[kFrameLen]);
        void reset();

    private:
        uint8_t buf_[kFrameLen] = {};
        size_t len_ = 0;
    };

}
