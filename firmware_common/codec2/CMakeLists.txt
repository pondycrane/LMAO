# codec2 - ESP-IDF speech-codec component (vendored codec2 1.2.0).
#
# On-device Codec-2 for the Cardputer kitchen voice path.  The same static-core
# subset as the MicroPython cmodule experiment (only the codec/lpc/mdbe core +
# the codebooks actually referenced by 700C/1300 are linked; unreferenced
# FreeDV/LPCNet/OFDM objects are dropped by the static archive).  Only
# codec2_wrap.c includes "codec2.h".

idf_component_register(
    SRCS
        "codec2/codec2/src/dump.c"
        "codec2/codec2/src/lpc.c"
        "codec2/codec2/src/nlp.c"
        "codec2/codec2/src/postfilter.c"
        "codec2/codec2/src/sine.c"
        "codec2/codec2/src/codec2.c"
        "codec2/codec2/src/codec2_fft.c"
        "codec2/codec2/src/cohpsk.c"
        "codec2/codec2/src/codec2_fifo.c"
        "codec2/codec2/src/fdmdv.c"
        "codec2/codec2/src/fm.c"
        "codec2/codec2/src/fsk.c"
        "codec2/codec2/src/fmfsk.c"
        "codec2/codec2/src/kiss_fft.c"
        "codec2/codec2/src/kiss_fftr.c"
        "codec2/codec2/src/linreg.c"
        "codec2/codec2/src/interp.c"
        "codec2/codec2/src/lsp.c"
        "codec2/codec2/src/mbest.c"
        "codec2/codec2/src/newamp1.c"
        "codec2/codec2/src/ofdm.c"
        "codec2/codec2/src/ofdm_mode.c"
        "codec2/codec2/src/phase.c"
        "codec2/codec2/src/quantise.c"
        "codec2/codec2/src/pack.c"
        "codec2/codec2/src/codebook.c"
        "codec2/codec2/src/codebookd.c"
        "codec2/codec2/src/codebookjmv.c"
        "codec2/codec2/src/codebookge.c"
        "codec2/codec2/src/codebooknewamp1.c"
        "codec2/codec2/src/codebooknewamp1_energy.c"
        "codec2/codec2/src/codebooknewamp2.c"
        "codec2/codec2/src/codebooknewamp2_energy.c"
        "codec2/codec2/src/golay23.c"
        "codec2/codec2/src/freedv_api.c"
        "codec2/codec2/src/freedv_1600.c"
        "codec2/codec2/src/freedv_700.c"
        "codec2/codec2/src/freedv_2020.c"
        "codec2/codec2/src/freedv_fsk.c"
        "codec2/codec2/src/freedv_vhf_framing.c"
        "codec2/codec2/src/freedv_data_channel.c"
        "codec2/codec2/src/varicode.c"
        "codec2/codec2/src/modem_stats.c"
        "codec2/codec2/src/mpdecode_core.c"
        "codec2/codec2/src/phi0.c"
        "codec2/codec2/src/gp_interleaver.c"
        "codec2/codec2/src/interldpc.c"
        "codec2/codec2/src/filter.c"
        "codec2/codec2/src/HRA_112_112.c"
        "codec2/codec2/src/HRA_56_56.c"
        "codec2/codec2/src/HRAb_396_504.c"
        "codec2/codec2/src/H_256_768_22.c"
        "codec2/codec2/src/H_256_512_4.c"
        "codec2/codec2/src/HRAa_1536_512.c"
        "codec2/codec2/src/H_128_256_5.c"
        "codec2/codec2/src/H_2064_516_sparse.c"
        "codec2/codec2/src/H_4096_8192_3d.c"
        "codec2/codec2/src/H_16200_9720.c"
        "codec2/codec2/src/H_1024_2048_4f.c"
        "codec2/codec2/src/H_212_158.c"
        "codec2/codec2/src/ldpc_codes.c"
        "codec2_wrap.c"
    INCLUDE_DIRS
        "."
        "codec2/codec2"
        "codec2/codec2/src"
    REQUIRES
        log
)

# Vendored codec2 needs gnu11 + the GIT_HASH define (codec2_version()); the
# ESP-IDF project defaults to -Os, which codec2's DSP is not safe under
# (heap corruption observed on-device) — force -O2 + disable strict aliasing
# for the codec component only.
target_compile_options(${COMPONENT_LIB} PRIVATE -std=gnu11 -O2 -fno-strict-aliasing)
target_compile_definitions(${COMPONENT_LIB} PRIVATE
    GIT_HASH="codec2-1.2.0-lmao"
    _GNU_SOURCE=1
)
