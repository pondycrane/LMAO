/*
 * codec2_shim.c - the ONLY translation unit that includes codec2.h.
 * Wraps the few codec2 entry points the MicroPython module needs behind
 * mp_c2_* names.
 */
#include "codec2.h"
#include "codec2_shim.h"

void *mp_c2_create(int mode) {
    return codec2_create(mode);
}

void mp_c2_destroy(void *c) {
    codec2_destroy(c);
}

int mp_c2_samples_per_frame(int mode) {
    struct CODEC2 *c = codec2_create(mode);
    if (c == NULL) {
        return 0;
    }
    int v = codec2_samples_per_frame(c);
    codec2_destroy(c);
    return v;
}

int mp_c2_bits_per_frame(int mode) {
    struct CODEC2 *c = codec2_create(mode);
    if (c == NULL) {
        return 0;
    }
    int v = codec2_bits_per_frame(c);
    codec2_destroy(c);
    return v;
}

int mp_c2_bytes_per_frame(int mode) {
    int bits = mp_c2_bits_per_frame(mode);
    return (bits + 7) / 8; /* ceil(bits/8) */
}

void mp_c2_encode(void *c, unsigned char *out_bits, const short *in_pcm, int nframes, int spf, int pk) {
    for (int i = 0; i < nframes; i++) {
        codec2_encode(c, out_bits + i * pk, (short *)in_pcm + i * spf);
    }
}

void mp_c2_decode(void *c, short *out_pcm, const unsigned char *in_bits, int nframes, int spf, int pk) {
    for (int i = 0; i < nframes; i++) {
        codec2_decode(c, out_pcm + i * spf, in_bits + i * pk);
    }
}
