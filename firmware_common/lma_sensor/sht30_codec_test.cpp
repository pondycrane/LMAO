// Host test for the pure SHT30 codec (//firmware_common:sht30_codec_test).
//
// The CRC vector is the one in the SHT30 datasheet, so a wrong polynomial (a
// silent "no sensor is ever detected" bug on the board) fails here instead of
// on the bench.
#include <cstdio>

#include "sht30_codec.h"

using namespace lma_sensor;

static int failures = 0;
#define CHECK(cond)                                            \
    do {                                                       \
        if (!(cond)) {                                         \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            failures++;                                        \
        }                                                      \
    } while (0)

int main() {
    // Datasheet vector: CRC-8 of 0xBE 0xEF is 0x92.
    const uint8_t v[] = {0xBE, 0xEF};
    CHECK(sht30_crc8(v, 2) == 0x92);

    // 0x6666 = 26214 -> 25.00 C and 40.00 %RH.
    uint8_t frame[6] = {0x66, 0x66, 0, 0x66, 0x66, 0};
    frame[2] = sht30_crc8(frame, 2);
    frame[5] = sht30_crc8(frame + 3, 2);
    float t = 0.0f, h = 0.0f;
    CHECK(sht30_decode(frame, &t, &h));
    CHECK(t > 24.99f && t < 25.01f);
    CHECK(h > 39.99f && h < 40.01f);

    // A single flipped bit must be rejected, both halves.
    frame[1] ^= 0x01;
    CHECK(!sht30_decode(frame, &t, &h));
    frame[1] ^= 0x01;
    frame[4] ^= 0x01;
    CHECK(!sht30_decode(frame, &t, &h));

    // The floating-bus case: pulled-up SDA reads 0xFF, which without the CRC
    // decodes to 130 C / 100 %RH — a "full, hot room" the law would trust.
    uint8_t idle[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    CHECK(!sht30_decode(idle, &t, &h));

    if (failures) {
        printf("%d check(s) failed\n", failures);
        return 1;
    }
    printf("sht30_codec: all checks passed\n");
    return 0;
}
