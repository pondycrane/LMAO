/*
 * codec2_shim.h - MP-agnostic bridge around libcodec2 for the MicroPython
 * `codec2` native module.
 *
 * The shim exists so that only THIS translation unit (codec2_shim.c) ever
 * includes codec2.h.  Including codec2.h in the same TU as the MicroPython
 * module glue (modcodec2.c) breaks the MP module-table parse
 * ("expected ';' before 'const'") because of the wide symbol macros codec2.h
 * pulls in.
 *
 * All fixed-point/float work stays in codec2; the shim just exposes the few
 * entry points the module needs, prefixed mp_c2_* to avoid name clashes with
 * codec2's own symbols.
 */
#ifndef MP_CODEC2_SHIM_H
#define MP_CODEC2_SHIM_H

#include <stddef.h>

/* codec2 voice modes we expose (numeric values = codec2.h CODEC2_MODE_* enum) */
#define MP_C2_MODE_1300 4
#define MP_C2_MODE_700C 8

#ifdef __cplusplus
extern "C" {
#endif

void *mp_c2_create(int mode);
void mp_c2_destroy(void *c);

/* Per-mode frame geometry (queries a scratch codec2 instance). */
int mp_c2_samples_per_frame(int mode);
int mp_c2_bits_per_frame(int mode);
/* Packed bytes per encoded frame: ceil(bits/8). */
int mp_c2_bytes_per_frame(int mode);

/* Process nframes at a time.  `spf` = samples/frame, `pk` = bytes/frame. */
void mp_c2_encode(void *c, unsigned char *out_bits, const short *in_pcm, int nframes, int spf, int pk);
void mp_c2_decode(void *c, short *out_pcm, const unsigned char *in_bits, int nframes, int spf, int pk);

#ifdef __cplusplus
}
#endif

#endif /* MP_CODEC2_SHIM_H */
