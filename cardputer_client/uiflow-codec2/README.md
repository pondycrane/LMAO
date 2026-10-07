# codec2 native module for the Cardputer ADV

A MicroPython **native C module** that embeds [Codec 2](https://github.com/drowe67/codec2)
into the lean upstream MicroPython firmware so the cardputer can encode /
decode low-bit-rate voice **on-device** (no host round-trip over the ~13 B/s
link).  Voice rides the existing LMAO `AudioMessage` proto payload
(field 21, `codec:"codec2"`).

## Mode + size

| Mode   | samples/frame | bits/frame | packed bytes/frame | 2 s clip (~640 B PCM) |
|--------|---------------|------------|--------------------|-----------------------|
| 700C   | 320           | 28         | 4                  | **200 B**             |
| 1300   | 320           | 52         | 7                  | **350 B**             |

Packed bytes are `ceil(bits/8)` per frame — the correct codec2 layout
(an earlier version used floor division and dropped bits per frame).

## Python API

```python
import codec2

b   = codec2.encode(codec2.CODEC2_MODE_700C, pcm_bytes)  # int16 LE -> codec2 bytes
pcm = codec2.decode(codec2.CODEC2_MODE_700C, b)           # codec2 bytes -> int16 LE
spf = codec2.samples_per_frame(codec2.CODEC2_MODE_700C)   # 320
bpf = codec2.bits_per_frame(codec2.CODEC2_MODE_700C)      # 28
```
Input/output are little-endian 16-bit samples at the codec2 rate (8 kHz).

## Layout

```
uiflow-codec2/
  modcodec2.c        MicroPython module glue (NEVER includes codec2.h)
  codec2_shim.c/h    thin bridge; the only TU that includes codec2.h
  micropython.cmake  build glue: compiles codec2 as a STATIC lib + links it
  codec2/codec2/     vendored Codec 2 source (src/ + generated codebooks
                     + codec2/version.h)
```

Design notes:

* **Shim split**: `codec2.h` may only be included from `codec2_shim.c`.
  Including it in the same TU as the MP module table breaks the module-table
  parse (`expected ';' before 'const'`).
* **Static lib**: Codec 2 is compiled into a static archive and only the
  referenced objects (MBE + LPC core for 700C/1300) are pulled into the final
  image, so the FreeDV / OFDM / LPCNet objects (and their optional deps) never
  surface.
* `freedv_api.c` needs `-DGIT_HASH="codec2-1.2.0-lmao"` (defined in the cmake).

## Building the firmware

Vendor the Codec 2 sources + generated codebooks first (see `codec2/`):

```sh
git clone --depth 1 https://github.com/drowe67/codec2.git /tmp/c2
cmake -S /tmp/c2 -B /tmp/c2/build -DUNITTEST=OFF -DBUILD_TESTING=OFF \
      -DBUILD_SHARED_LIBS=OFF
cmake --build /tmp/c2/build --target codec2 -j4
# copy /tmp/c2/src/*.c *.h + /tmp/c2/build/src/codebook*.c + version.h
#   -> codec2/codec2/src/ and codec2/codec2/codec2/version.h
```

Then build the esp32 MicroPython firmware with the module:

```sh
cd micropython/ports/esp32
export PATH=~/.idf-shim:$PATH IDF_PATH=~/esp-idf-v5.5.4 \
    IDF_PYTHON_ENV_PATH="$HOME/.espressif/python_env/idf5.5_py3.11_env"
. $IDF_PATH/export.sh
make BOARD=CARD_PUTER_ADV \
     USER_C_MODULES=~/lmao-sprout-log/cardputer_client/uiflow-codec2
```

The board (`CARD_PUTER_ADV`) uses the **factory** (non-OTA) partition table at
`0x10000`, 8 MB QIO flash, and the **USB-Serial-JTAG console only**
(`MICROPY_HW_ENABLE_UART_REPL=0`) so the REPL comes up on the USB-C connector:
USB Serial JTAG is the console with the UART0 pins left untouched.
