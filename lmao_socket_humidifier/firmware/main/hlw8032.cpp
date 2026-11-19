#include "hlw8032.h"

#include <cstring>

namespace {

constexpr uint32_t kVolR1 = 1880000;  // 1.88 MOhm
constexpr uint32_t kVolR2 = 1000;     // 1 kOhm
constexpr float kCurrentRf = 0.001f;
constexpr float kVolFactor = (float)kVolR1 / (float)kVolR2 / 1000.0f;   // 1.88
constexpr float kCurrentFactor = 1.0f / (kCurrentRf * 1000.0f);         // 1.0
constexpr float kCurrentOffset = 0.06f;  // M5 driver's lead-compensation term

uint32_t be24(const uint8_t* p) {
    return ((uint32_t)p[0] << 16) | ((uint32_t)p[1] << 8) | (uint32_t)p[2];
}

bool checksum_ok(const uint8_t* frame) {
    uint8_t sum = 0;
    for (size_t i = 2; i <= 22; i++) sum = (uint8_t)(sum + frame[i]);
    return sum == frame[23];
}

}  // namespace

namespace hlw8032 {

bool parse_frame(const uint8_t* frame, size_t len, Sample* out) {
    if (out == nullptr) return false;
    if (frame == nullptr || len < kFrameLen) return false;
    if (frame[0] != 0x55 || frame[1] != 0x5A) return false;
    if (!checksum_ok(frame)) return false;

    const uint32_t vol_par = be24(frame + 2);
    const uint32_t vol_data = be24(frame + 5);
    const uint32_t cur_par = be24(frame + 8);
    const uint32_t cur_data = be24(frame + 11);
    const uint32_t pow_par = be24(frame + 14);
    const uint32_t pow_data = be24(frame + 17);

    if (vol_data == 0) return false;  // no meaningful voltage reading

    Sample s;
    s.valid = true;
    s.voltage = ((float)vol_par / (float)vol_data) * kVolFactor;
    s.current_a = cur_data ? (((float)cur_par / (float)cur_data) * kCurrentFactor -
                              kCurrentOffset)
                           : 0.0f;
    if (s.current_a < 0.0f) s.current_a = 0.0f;
    s.power_w = pow_data ? ((float)pow_par / (float)pow_data) * kVolFactor *
                               kCurrentFactor
                         : 0.0f;
    s.pf_raw = (uint16_t)(((uint16_t)frame[21] << 8) | frame[22]);
    *out = s;
    return true;
}

void FrameAssembler::reset() { len_ = 0; }

bool FrameAssembler::feed(uint8_t byte, uint8_t out[kFrameLen]) {
    if (len_ == 0) {
        if (byte == 0x55) buf_[len_++] = byte;
        return false;
    }
    if (len_ == 1) {
        if (byte == 0x5A) {
            buf_[len_++] = byte;
        } else if (byte != 0x55) {
            len_ = 0;  // neither the 0x5A partner nor another sync byte
        }
        return false;
    }

    buf_[len_++] = byte;
    if (len_ < kFrameLen) return false;

    Sample s;
    if (parse_frame(buf_, kFrameLen, &s)) {
        memcpy(out, buf_, kFrameLen);
        len_ = 0;
        return true;
    }

    // Bad frame: drop the leading sync byte, then scan the remaining bytes for
    // the next 0x55 0x5A pair so payload data containing 0x55 cannot wedge us.
    len_ = kFrameLen - 1;
    memmove(buf_, buf_ + 1, len_);
    while (len_ >= 2 && !(buf_[0] == 0x55 && buf_[1] == 0x5A)) {
        memmove(buf_, buf_ + 1, len_ - 1);
        len_--;
    }
    if (len_ == 1 && buf_[0] != 0x55) len_ = 0;
    return false;
}

}  // namespace hlw8032
