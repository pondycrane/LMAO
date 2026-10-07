"""On-device voice for the Cardputer ADV: ES8311 codec + I2S + codec2.

Captures 8 kHz mono from the on-board ES8311 mic, runs it through the codec2
native module (firmware custom build), and plays decoded PCM over the speaker.

Wire contract with the LMAO wire format:
    AudioMessage.audio_data = codec2.encode(CODEC2_MODE_700C, pcm)
    AudioMessage.codec      = "codec2"

Registers (I2C + I2S) follow the Cardputer ADV layout:
    ES8311 I2C : I2C(0, scl=9, sda=8)
    I2S        : sck=41, ws=43, sd=42 (DAC out), sdin=46 (ADC in)

NOTE: the ADV exposes no MCLK pin; the ES8311 clock source is board-specific.
The init below is best-effort and needs live on-device MCLK tuning against a
real capture before it is trustworthy in production.
"""
import machine
import math
import time

try:
    import codec2
except ImportError:
    codec2 = None

CODEC2_MODE = getattr(codec2, "CODEC2_MODE_700C", 8)
SAMPLE_RATE = 8000

# ES8311 I2C + I2S pin map (Cardputer ADV)
ES8311_I2C = (9, 8)          # (SCL, SDA)
I2S_PINS = dict(sck=41, ws=43, sd=42, sdin=46)

# --- ES8311 register init (best-effort; see module docstring) -------------
_ES8311_I2C_ADDR = 0x18

# Reset + mode config
_ES8311_REG_RESET = 0x00
_ES8311_REG_CLOCK_MANAGER2 = 0x02
_ES8311_REG_CLOCK_MANAGER3 = 0x03
_ES8311_REG_CLOCK_MANAGER4 = 0x04
_ES8311_REG_CLOCK_MANAGER5 = 0x05
_ES8311_REG_CLOCK_MANAGER6 = 0x06
_ES8311_REG_CLOCK_MANAGER7 = 0x07
_ES8311_REG_CLOCK_MANAGER8 = 0x08
_ES8311_REG_ADC_ADC28BITS = 0x0C
_ES8311_REG_ADC_MISC2 = 0x0D
_ES8311_REG_ADC_MISC = 0x14
_ES8311_REG_ADC_VOLUME = 0x11
_ES8311_REG_ADC_DIGITAL = 0x12
_ES8311_REG_DAC_VOLUME = 0x29
_ES8311_REG_GPIO = 0x31


def _es8311_write(i2c, reg, val):
    try:
        i2c.writeto_mem(_ES8311_I2C_ADDR, reg, bytes([val]))
    except OSError:
        pass  # best-effort; tuning will pin exact values


def setup():
    """Power up + config the ES8311 for 8 kHz I2S in/out (best-effort)."""
    i2c = machine.I2C(0, scl=machine.Pin(ES8311_I2C[0]), sda=machine.Pin(ES8311_I2C[1]), freq=400000)
    try:
        _es8311_write(i2c, _ES8311_REG_RESET, 0x00)      # soft reset
        time.sleep_ms(10)
        _es8311_write(i2c, _ES8311_REG_RESET, 0x80)      # release reset / power-up
        time.sleep_ms(10)

        # clock: BCLK/LRCK slave, own MCLK fallback; 8 kHz LRCK
        _es8311_write(i2c, _ES8311_REG_CLOCK_MANAGER2, 0x0C)
        _es8311_write(i2c, _ES8311_REG_CLOCK_MANAGER3, 0x40)
        _es8311_write(i2c, _ES8311_REG_CLOCK_MANAGER4, 0x07)
        _es8311_write(i2c, _ES8311_REG_CLOCK_MANAGER5, 0x02)
        _es8311_write(i2c, _ES8311_REG_CLOCK_MANAGER6, 0x06)
        _es8311_write(i2c, _ES8311_REG_CLOCK_MANAGER7, 0x00)
        _es8311_write(i2c, _ES8311_REG_CLOCK_MANAGER8, 0x78)

        # ADC: 16-bit, both channels -> mono, enable
        _es8311_write(i2c, _ES8311_REG_ADC_ADC28BITS, 0x02)
        _es8311_write(i2c, _ES8311_REG_ADC_MISC2, 0x14)
        _es8311_write(i2c, _ES8311_REG_ADC_MISC, 0x10)
        _es8311_write(i2c, _ES8311_REG_ADC_VOLUME, 0x1D)  # 0 dB adc volume
        _es8311_write(i2c, _ES8311_REG_ADC_DIGITAL, 0x80)

        # DAC volume ~0 dB + enable speaker path
        _es8311_write(i2c, _ES8311_REG_DAC_VOLUME, 0xE0)
        _es8311_write(i2c, _ES8311_REG_GPIO, 0x11)
    except OSError as e:
        return i2c, e
    return i2c, None


def _audio_in():
    return machine.I2S(
        1,
        sck=machine.Pin(I2S_PINS["sck"]),
        ws=machine.Pin(I2S_PINS["ws"]),
        sd=machine.Pin(I2S_PINS["sdin"]),
        mode=machine.I2S.RX,
        bits=16,
        format=machine.I2S.MONO,
        rate=SAMPLE_RATE,
        ibuf=4096,
    )


def _audio_out():
    return machine.I2S(
        0,
        sck=machine.Pin(I2S_PINS["sck"]),
        ws=machine.Pin(I2S_PINS["ws"]),
        sd=machine.Pin(I2S_PINS["sd"]),
        mode=machine.I2S.TX,
        bits=16,
        format=machine.I2S.MONO,
        rate=SAMPLE_RATE,
        ibuf=4096,
    )


def record_ms(duration_ms):
    """Record `duration_ms` of 8 kHz mono -> bytes (int16 LE). Blocking."""
    if codec2 is None:
        raise RuntimeError("codec2 module not present in firmware")
    n = SAMPLE_RATE * duration_ms // 1000
    a = _audio_in()
    buf = bytearray(n * 2)
    idx = 0
    while idx < len(buf):
        idx += a.readinto(memoryview(buf)[idx:])
    a.deinit()
    return bytes(buf)


def play(pcm_bytes):
    """Play int16 LE PCM on the speaker."""
    a = _audio_out()
    a.write(pcm_bytes)
    a.deinit()


def encode(pcm_bytes):
    """codec2-encode int16 LE PCM -> encoded bytes."""
    if codec2 is None:
        raise RuntimeError("codec2 module not present in firmware")
    return codec2.encode(CODEC2_MODE, pcm_bytes)


def decode(bits):
    """codec2-decode encoded bytes -> int16 LE PCM."""
    if codec2 is None:
        raise RuntimeError("codec2 module not present in firmware")
    return codec2.decode(CODEC2_MODE, bits)


def handle_inbound(audio_message):
    """Decode + play an inbound AudioMessage dict (see lma_encoder.decode_audio_message)."""
    data = audio_message.get("audio_data", b"")
    if not data:
        return False
    try:
        pcm = decode(data)
        play(pcm)
        return True
    except Exception:
        return False
