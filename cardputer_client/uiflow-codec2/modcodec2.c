/*
 * modcodec2.c - MicroPython native module `codec2`.
 *
 *   import codec2
 *   b = codec2.encode(codec2.CODEC2_MODE_700C, pcm_bytes)   # pcm: int16 LE
 *   pcm = codec2.decode(codec2.CODEC2_MODE_700C, b)          # int16 LE bytes
 *
 * Exposes the low-rate voice modes (700C = 8, 1300 = 4) plus per-frame
 * geometry.  Rust-free, plain C, compiled into the firmware via
 * micropython.cmake.
 *
 * IMPORTANT: this TU must NOT include codec2.h (see codec2_shim.h).
 */
#include "py/obj.h"
#include "py/runtime.h"
#include "codec2_shim.h"

static mp_obj_t mod_codec2_samples_per_frame(mp_obj_t mode_in) {
    return mp_obj_new_int(mp_c2_samples_per_frame((int)mp_obj_get_int(mode_in)));
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_codec2_samples_per_frame_obj, mod_codec2_samples_per_frame);

static mp_obj_t mod_codec2_bits_per_frame(mp_obj_t mode_in) {
    return mp_obj_new_int(mp_c2_bits_per_frame((int)mp_obj_get_int(mode_in)));
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_codec2_bits_per_frame_obj, mod_codec2_bits_per_frame);

static mp_obj_t mod_codec2_bytes_per_frame(mp_obj_t mode_in) {
    return mp_obj_new_int(mp_c2_bytes_per_frame((int)mp_obj_get_int(mode_in)));
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_codec2_bytes_per_frame_obj, mod_codec2_bytes_per_frame);

static mp_obj_t mod_codec2_encode(mp_obj_t mode_in, mp_obj_t pcm_in) {
    int mode = (int)mp_obj_get_int(mode_in);
    mp_buffer_info_t buf;
    mp_get_buffer_raise(pcm_in, &buf, MP_BUFFER_READ);

    void *c = mp_c2_create(mode);
    if (c == NULL) {
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("codec2 create failed"));
    }

    int spf = mp_c2_samples_per_frame(mode);
    int pk = mp_c2_bytes_per_frame(mode);
    int nframes = (buf.len / 2) / spf; /* int16 samples / samples per frame */

    if (nframes == 0) {
        mp_c2_destroy(c);
        return mp_obj_new_bytes((const byte *)"", 0);
    }

    vstr_t vstr;
    vstr_init_len(&vstr, nframes * pk);
    mp_c2_encode(c, (unsigned char *)vstr.buf, (const short *)buf.buf, nframes, spf, pk);
    mp_c2_destroy(c);

    return mp_obj_new_bytes_from_vstr(&vstr);
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_codec2_encode_obj, mod_codec2_encode);

static mp_obj_t mod_codec2_decode(mp_obj_t mode_in, mp_obj_t bits_in) {
    int mode = (int)mp_obj_get_int(mode_in);
    mp_buffer_info_t buf;
    mp_get_buffer_raise(bits_in, &buf, MP_BUFFER_READ);

    int spf = mp_c2_samples_per_frame(mode);
    int pk = mp_c2_bytes_per_frame(mode);
    int nframes = (pk > 0) ? buf.len / pk : 0;

    if (nframes == 0) {
        return mp_obj_new_bytes((const byte *)"", 0);
    }

    void *c = mp_c2_create(mode);
    if (c == NULL) {
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("codec2 create failed"));
    }

    vstr_t vstr;
    vstr_init_len(&vstr, nframes * spf * 2);
    mp_c2_decode(c, (short *)vstr.buf, (const unsigned char *)buf.buf, nframes, spf, pk);
    mp_c2_destroy(c);

    return mp_obj_new_bytes_from_vstr(&vstr);
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_codec2_decode_obj, mod_codec2_decode);

static const mp_rom_map_elem_t codec2_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_codec2) },
    { MP_ROM_QSTR(MP_QSTR_CODEC2_MODE_1300), MP_ROM_INT(MP_C2_MODE_1300) },
    { MP_ROM_QSTR(MP_QSTR_CODEC2_MODE_700C), MP_ROM_INT(MP_C2_MODE_700C) },
    { MP_ROM_QSTR(MP_QSTR_samples_per_frame), MP_ROM_PTR(&mod_codec2_samples_per_frame_obj) },
    { MP_ROM_QSTR(MP_QSTR_bits_per_frame), MP_ROM_PTR(&mod_codec2_bits_per_frame_obj) },
    { MP_ROM_QSTR(MP_QSTR_bytes_per_frame), MP_ROM_PTR(&mod_codec2_bytes_per_frame_obj) },
    { MP_ROM_QSTR(MP_QSTR_encode), MP_ROM_PTR(&mod_codec2_encode_obj) },
    { MP_ROM_QSTR(MP_QSTR_decode), MP_ROM_PTR(&mod_codec2_decode_obj) },
};
static MP_DEFINE_CONST_DICT(codec2_module_globals, codec2_module_globals_table);

const mp_obj_module_t codec2_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&codec2_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_codec2, codec2_user_cmodule);
