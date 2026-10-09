/*
 * codec2_wrap.h - native (ESP-IDF) speech-codec bridge around libcodec2.
 *
 * Only codec2_wrap.c ever includes "codec2.h"; the C++ firmware (main.cpp and
 * the LXMF voice path) consumes this small C API.  Mode 700C (8) is the
 * default low-bandwidth voice mode: 20 ms / 21-bit frames, 7 packed bytes per
 * frame, 160 samples/frame at 8 kHz (uiflow-codec2 shim constants).
 */
#ifndef CODEC2_WRAP_H
#define CODEC2_WRAP_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

enum {
    C2W_MODE_1300 = 4,
    C2W_MODE_700C = 8,
};

/* Create/destroy a codec2 instance for `mode`. */
void *c2w_create(int mode);
void c2w_destroy(void *c);

/* Frame geometry for `mode`. */
int c2w_samples_per_frame(int mode);   /* 160 @ 8 kHz            */
int c2w_bytes_per_frame(int mode);     /* 7 for 700C, 5 for 1300 */

/* Encode nframes (nframes*spf samples -> nframes*pk packed bytes). */
void c2w_encode(void *state, unsigned char *out_bits, const short *in_pcm,
                int nframes, int spf, int pk);
/* Decode nframes (nframes*pk bytes -> nframes*spf PCM samples). */
void c2w_decode(void *state, short *out_pcm, const unsigned char *in_bits,
                int nframes, int spf, int pk);

/* Synchronous round-trip self-test: returns 0 when encode->decode of a
 * known PCM buffer produces samples within the codec's lossy tolerance. */
int c2w_self_test_mode(int mode);   /* explicit mode: C2W_MODE_* */
int c2w_self_test(void);            /* default 700C */

#ifdef __cplusplus
}
#endif
#endif /* CODEC2_WRAP_H */
