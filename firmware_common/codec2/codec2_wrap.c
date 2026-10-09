/*
 * codec2_wrap.c - native (ESP-IDF) speech-codec bridge around libcodec2.
 * Only this TU includes "codec2.h" (the C++ firmware sees only codec2_wrap.h).
 * CODEC2_MODE_* are plain int #defines; codec2_create takes the raw int.
 */
#include "codec2_wrap.h"

#include "codec2.h"
#include <string.h>
#include <math.h>
#include <stdlib.h>

void *c2w_create(int mode) {
    return codec2_create(mode);
}

void c2w_destroy(void *state) {
    codec2_destroy((struct CODEC2 *)state);
}

int c2w_samples_per_frame(int mode) {
    void *c = codec2_create(mode);
    if (!c) return 0;
    const int spf = codec2_samples_per_frame((struct CODEC2 *)c);
    codec2_destroy((struct CODEC2 *)c);
    return spf;
}

int c2w_bytes_per_frame(int mode) {
    void *c = codec2_create(mode);
    if (!c) return 0;
    const int bits = codec2_bits_per_frame((struct CODEC2 *)c);
    codec2_destroy((struct CODEC2 *)c);
    return (bits + 7) / 8;
}

void c2w_encode(void *state, unsigned char *out_bits, const short *in_pcm,
                int nframes, int spf, int pk) {
    struct CODEC2 *c = (struct CODEC2 *)state;
    for (int f = 0; f < nframes; f++) {
        /* codec2_encode does not alter speech_in; drop const at the boundary. */
        codec2_encode(c, out_bits + f * (size_t)pk, (short *)(in_pcm + f * (size_t)spf));
    }
}

void c2w_decode(void *state, short *out_pcm, const unsigned char *in_bits,
                int nframes, int spf, int pk) {
    struct CODEC2 *c = (struct CODEC2 *)state;
    for (int f = 0; f < nframes; f++) {
        codec2_decode(c, out_pcm + f * (size_t)spf, in_bits + f * (size_t)pk);
    }
}

int c2w_self_test_mode(int mode) {
    /* Encode+decode a known 8 kHz tone; heap-allocate exact per-mode buffers
     * (+headroom) and verify heap integrity + decoded RMS.  Mode 700C is the
     * low-bandwidth voice target; 1300 is the fallback. */
    void *c = c2w_create(mode);
    if (!c) return -1;
    const int spf = c2w_samples_per_frame(mode);
    const int pk = c2w_bytes_per_frame(mode);
    const int frames = 4;
    short *pcm = (short *)malloc(frames * (size_t)spf * 2 + 64);
    unsigned char *packed = (unsigned char *)calloc(frames * (size_t)pk + 16, 1);
    short *out = (short *)malloc(frames * (size_t)spf * 2 + 64);
    if (!pcm || !packed || !out) {
        free(pcm); free(packed); free(out);
        c2w_destroy(c);
        return -3;
    }
    for (int i = 0; i < frames * spf; i++) {
        pcm[i] = (short)(8000.0f * sinf(2.0f * 3.14159265f * 440.0f * i / 8000.0f));
    }
    c2w_encode(c, packed, pcm, frames, spf, pk);
    c2w_decode(c, out, packed, frames, spf, pk);
    c2w_destroy(c);

    double e = 0.0;
    for (int i = 0; i < frames * spf; i++) e += (double)out[i] * (double)out[i];
    const double rms = sqrt(e / (frames * (double)spf));

    free(pcm); free(out); free(packed);
    return rms > 40.0 ? 0 : -2;
}

int c2w_self_test(void) {
    const int r = c2w_self_test_mode(C2W_MODE_700C);
    return r;
}
