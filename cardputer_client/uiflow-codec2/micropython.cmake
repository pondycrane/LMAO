# micropython.cmake - build glue for the `codec2` MicroPython native module.
#
# Point USER_C_MODULES at this directory when building the esp32 MicroPython
# firmware, e.g.:
#   make BOARD=CARD_PUTER_ADV USER_C_MODULES=~/lmao-sprout-log/cardputer_client/uiflow-codec2
#
# Design:
#   * codec2 sources are built into a STATIC library (`codec2_core`).  The
#     final image only pulls in the object files actually referenced (700C /
#     1300 needs just the MBE+LPC core), so the FreeDV/OFDM/LPCNet objects are
#     never linked and their optional deps never surface.
#   * The module glue (modcodec2.c) + the shim (codec2_shim.c) live on the
#     `usermod` target and link against codec2_core.

set(CODEC2_SRC_DIR "${CMAKE_CURRENT_LIST_DIR}/codec2/codec2/src")

# codec2 library sources (src/CMakeLists.txt `codec2` target), with the
# forward-only sources (c2enc.c/c2dec.c/...) excluded.  Static archive means
# unreferenced objects are dropped at final link.
set(CODEC2_CORE_SRCS
    ${CODEC2_SRC_DIR}/dump.c
    ${CODEC2_SRC_DIR}/lpc.c
    ${CODEC2_SRC_DIR}/nlp.c
    ${CODEC2_SRC_DIR}/postfilter.c
    ${CODEC2_SRC_DIR}/sine.c
    ${CODEC2_SRC_DIR}/codec2.c
    ${CODEC2_SRC_DIR}/codec2_fft.c
    ${CODEC2_SRC_DIR}/cohpsk.c
    ${CODEC2_SRC_DIR}/codec2_fifo.c
    ${CODEC2_SRC_DIR}/fdmdv.c
    ${CODEC2_SRC_DIR}/fm.c
    ${CODEC2_SRC_DIR}/fsk.c
    ${CODEC2_SRC_DIR}/fmfsk.c
    ${CODEC2_SRC_DIR}/kiss_fft.c
    ${CODEC2_SRC_DIR}/kiss_fftr.c
    ${CODEC2_SRC_DIR}/linreg.c
    ${CODEC2_SRC_DIR}/interp.c
    ${CODEC2_SRC_DIR}/lsp.c
    ${CODEC2_SRC_DIR}/mbest.c
    ${CODEC2_SRC_DIR}/newamp1.c
    ${CODEC2_SRC_DIR}/ofdm.c
    ${CODEC2_SRC_DIR}/ofdm_mode.c
    ${CODEC2_SRC_DIR}/phase.c
    ${CODEC2_SRC_DIR}/quantise.c
    ${CODEC2_SRC_DIR}/pack.c
    ${CODEC2_SRC_DIR}/codebook.c
    ${CODEC2_SRC_DIR}/codebookd.c
    ${CODEC2_SRC_DIR}/codebookjmv.c
    ${CODEC2_SRC_DIR}/codebookge.c
    ${CODEC2_SRC_DIR}/codebooknewamp1.c
    ${CODEC2_SRC_DIR}/codebooknewamp1_energy.c
    ${CODEC2_SRC_DIR}/codebooknewamp2.c
    ${CODEC2_SRC_DIR}/codebooknewamp2_energy.c
    ${CODEC2_SRC_DIR}/golay23.c
    ${CODEC2_SRC_DIR}/freedv_api.c
    ${CODEC2_SRC_DIR}/freedv_1600.c
    ${CODEC2_SRC_DIR}/freedv_700.c
    ${CODEC2_SRC_DIR}/freedv_2020.c
    ${CODEC2_SRC_DIR}/freedv_fsk.c
    ${CODEC2_SRC_DIR}/freedv_vhf_framing.c
    ${CODEC2_SRC_DIR}/freedv_data_channel.c
    ${CODEC2_SRC_DIR}/varicode.c
    ${CODEC2_SRC_DIR}/modem_stats.c
    ${CODEC2_SRC_DIR}/mpdecode_core.c
    ${CODEC2_SRC_DIR}/phi0.c
    ${CODEC2_SRC_DIR}/gp_interleaver.c
    ${CODEC2_SRC_DIR}/interldpc.c
    ${CODEC2_SRC_DIR}/filter.c
    ${CODEC2_SRC_DIR}/HRA_112_112.c
    ${CODEC2_SRC_DIR}/HRA_56_56.c
    ${CODEC2_SRC_DIR}/HRAb_396_504.c
    ${CODEC2_SRC_DIR}/H_256_768_22.c
    ${CODEC2_SRC_DIR}/H_256_512_4.c
    ${CODEC2_SRC_DIR}/HRAa_1536_512.c
    ${CODEC2_SRC_DIR}/H_128_256_5.c
    ${CODEC2_SRC_DIR}/H_2064_516_sparse.c
    ${CODEC2_SRC_DIR}/H_4096_8192_3d.c
    ${CODEC2_SRC_DIR}/H_16200_9720.c
    ${CODEC2_SRC_DIR}/H_1024_2048_4f.c
    ${CODEC2_SRC_DIR}/H_212_158.c
    ${CODEC2_SRC_DIR}/ldpc_codes.c
)

# Header dir that makes `#include <codec2/version.h>` resolve: version.h is
# vendored at codec2/codec2/codec2/version.h.
set(CODEC2_INC_DIR "${CMAKE_CURRENT_LIST_DIR}/codec2/codec2")

add_library(codec2_core STATIC ${CODEC2_CORE_SRCS})
target_include_directories(codec2_core PRIVATE ${CODEC2_SRC_DIR} ${CODEC2_INC_DIR})
target_compile_definitions(codec2_core PRIVATE
    GIT_HASH="codec2-1.2.0-lmao"
    _GNU_SOURCE=1
)
target_compile_options(codec2_core PRIVATE -std=gnu11)

# The MicroPython module glue (this is the `usermod` the esp32 port consumes).
add_library(usermod_codec2 INTERFACE)
target_sources(usermod_codec2 INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/modcodec2.c
    ${CMAKE_CURRENT_LIST_DIR}/codec2_shim.c
)
target_include_directories(usermod_codec2 INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
    ${CODEC2_SRC_DIR}
    ${CODEC2_INC_DIR}
)
target_compile_options(usermod_codec2 INTERFACE -std=gnu11)
# statically link codec2 into the module; unreferenced codec2 objects are
# dropped by the linker, so no FreeDV/LPCNet deps surface.
target_link_libraries(usermod_codec2 INTERFACE codec2_core)

target_link_libraries(usermod INTERFACE usermod_codec2)
