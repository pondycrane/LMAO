# Smart Irrigation Firmware — Development Plan (LMAO-Integrated)

## System Overview

Edge node that fuses soil moisture + air temperature/pressure into a **fuzzy-logic irrigation controller**, while sending **LXMF SensorReports** over LoRa (via LMAO infrastructure) for offline training of a future Time-Series / Soil-Dynamics ML model. Downlink **CommandRequests** control the pump from the LMAO server side.

---

## 1. Architecture

### 1.1 High-Level

```
┌──────────────────────────────────────────────────────────────────────────┐
│                     LMAO Server (K8s / Turing Pi 2)                      │
│                                                                          │
│  ┌──────────┐  ┌──────────────┐  ┌───────────┐  ┌──────────────────┐   │
│  │ LXMF     │  │ NATS JetStream│  │ gRPC API │  │ IoT Ingest (DuckDB│   │
│  │ Router   │──│ lmao.messages.env──│          │  │                  │   │
│  └──────────┘  └──────────────┘  └───────────┘  └──────────────────┘   │
│         ▲           ▲                      │                           │
│         │           │ (publishes SensorReport data)                    │
│         └───────────┘                      │                           │
│                                      ChirpStack / TTN / LNS              │
└──────────────────────────────────────────────────────────────────────────┘
            ▲ LoRaWAN uplink / downlink
            │
┌──────────────────────────────────────────────────────────────────────────┐
│                   Atomic DTU + ATOM Lite (Irrigation Node)               │
│                                                                          │
│  ┌─────────────────────────────────────┐  ┌────────────────────────┐    │
│  │  ATOM Lite ESP32-S2 (MicroPython)   │  │  STM32WLE5CC DTU       │    │
│  │                                     │  │  (LoRaWAN radio)       │    │
│  │  ┌─────────────────────────────┐   │  │                        │    │
│  │  │  cardputer_client lib copy  │   │  │  LXMF SensorReport    │    │
│  │  │  (urns µReticulum port)     │   │  │  encoded protobuf     │    │
│  │  │  + proto/lma_encoder.py     │   │  │  → LoRaWAN TX/RX      │    │
│  │  └─────────────────────────────┘   │  │                        │    │
│  │                                     │  │                        │    │
│  │  ┌─────────────────────────────┐   │  │                        │    │
│  │  │  Fuzzy Logic Engine         │   │  │  Serial UART bridge   │    │
│  │  │  (MicroPython, fixed-point) │   │  │  (9600-115200 baud)   │    │
│  │  └─────────────────────────────┘   │  │                        │    │
│  │                                     │  │                        │    │
│  │  ┌─────────────────────────────┐   │  │  ┌──────────────────┐  │    │
│  │  │  Sensor Fusion              │   │  │  │  LoRaWAN Stack   │  │    │
│  │  │  (PCA9548A I2C)             │   │  │  │  (STM32WLE_5.x)  │  │    │
│  │  └──────┬──────────────────────┘   │  │  └──────────────────┘  │    │
│  │         │                           │  │                        │    │
│  │  ┌──────┴──────────────────────┐   │  │  ┌──────────────────┐  │    │
│  │  │  I2C Hub PCA9548A           │   │  │  │  LoRa Antenna    │  │    │
│  │  │  ┌──────────┬──────┬────────┐   │  │  └──────────────────┘  │    │
│  │  │  │Moisture  │SHT30 │QMP6988 │   │  │                        │    │
│  │  │  │Channel 0 │Ch 1  │Ch 2    │   │  │                        │    │
│  │  └─────────────────────────────┘   │  │                        │    │
│  │                                     │  │                        │    │
│  │  ┌─────────────────────────────┐   │  │                        │    │
│  │  │  Pump Controller            │   │  │                        │    │
│  │  │  (Relay/MOSFET on GPIO)     │   │  │                        │    │
│  │  └─────────────────────────────┘   │  │                        │    │
│  └─────────────────────────────────────┘  └────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────────┘
```

### 1.2 LMAO Protocol Flow

```
[Irrigation Node]                              [LMAO Server]
      │                                              │
      │  SensorReport (LXMF, OTAA LoRaWAN)           │
      │  ┌─────────────────┐                         │
      │  │ LMAOEnvelope    │──── LXMF ──────────────►│  LXMF Router
      │  │  └─SensorReport │                         │  (propagation)
      │  │    node_id,seq, │                         │
      │  │    battery,     │                         │
      │  │    readings[]   │                         │
      │  └─────────────────┘                         │
      │                                              │
      │  ◄── CommandRequest (LXMF downlink) ─────────│  gRPC/K8s → LXMF send
      │  ┌─────────────────┐                         │
      │  │ LMAOEnvelope    │────────────────────────►│  Command: pump_on/pump_off
      │  │  └─CommandRequest│    ────┐                │  Config: fuzzy_update
      │  │    cmd_id,      │        │                │  Reboot: node_reboot
      │  │    action,      │        │                │
      │  │    params       │        │                │
      │  └─────────────────┘        │                │
      │                              │                │
      │  CommandAck (LXMF uplink)    │                │
      │  └─success,msg──────────────┘                │
```

---

## 2. Shared LMAO Stack

The firmware reuses the **cardputer_client library** as its communication foundation.

### 2.1 Libraries to Port from Cardputer Client

| Source Path | Target | Purpose |
|---|---|---|
| `cardputer_client/lib/urns/` | `firmware/lib/urns/` | µReticulum MicroPython port (RNS, LXMF, crypto, transport) |
| `cardputer_client/lib/urns/interfaces/lora.py` | `firmware/lib/urns/interfaces/lora.py` | SX1262/SX1278 LoRa driver (adapted for Atom Lite pins) |
| `cardputer_client/lib/urns/interfaces/serial.py` | `firmware/lib/urns/interfaces/serial.py` | Serial transport to STM32WLE5CC DTU |
| `cardputer_client/lib/urns/identity.py` | `firmware/lib/urns/` | X25519/Ed25519 node identity |
| `cardputer_client/lib/urns/destination.py` | `firmware/lib/urns/` | Destination hash resolution |
| `cardputer_client/lib/urns/lxmf.py` | `firmware/lib/urns/` | LXMF router & message handling |
| `cardputer_client/lib/urns/crypto/` | `firmware/lib/urns/crypto/` | Ed25519, AES, HKDF, HMAC |
| `cardputer_client/proto/lma_encoder.py` | `firmware/proto/lma_encoder.py` | Hand-coded protobuf wire-format (MicroPython-compatible) |
| `cardputer_client/lib/sensors/dht20.py` | `firmware/lib/sensors/dht20.py` | DHT20 sensor driver (example, extensible) |
| `cardputer_client/config.py` | `firmware/config.py` | Node config (wifi/loRa params, DEST_HASH, INTERVAL) |
| `cardputer_client/flash.py` | `firmware/flash.py` | Deploy tool (ampy/micropython-cli) |

### 2.2 Config Pattern (mirrors cardputer_client/config.py)

```python
"""
µReticulum config for ATOM Lite Irrigation Node.
Mirrors cardputer_client/config.py structure for consistency.
"""
from lora_boards import LORA_BOARDS

WIFI_SSID = None        # LoRaWAN only — no WiFi on Atom Lite
WIFI_PASS = None
NODE_NAME = "LMAO_Irrigation"
DEBUG = 0                 # 0 = silent (production), 1 = messages, 2 = debug
DEST_HASH = None          # Server's lxmf.delivery hash (injected by deploy tool)
INTERVAL_SECONDS = 300    # SensorReport every 5 min (adjustable via downlink)

# DTU bridge config (Atom Lite ↔ STM32WLE5CC over UART)
DTU_SERIAL_PORT = 1       # UART1
DTU_BAUDRATE = 115200

# Sensor config
SENSOR_MOISTURE_CHANNEL = 0     # PCA9548A channel 0
SENSOR_TEMP_HUMID_CHANNEL = 1   # PCA9548A channel 1 (SHT30)
SENSOR_PRESSURE_CHANNEL = 2     # PCA9548A channel 2 (QMP6988)

# Fuzzy config
FUZZY_CALIB_DRY = 30.0    # Raw ADC at dry soil
FUZZY_CALIB_WET = 500.0   # Raw ADC at saturated soil

# Pump config
PUMP_GPIO = 26              # GPIO for pump relay/MOSFET
PUMP_MIN_DURATION_MS = 5000  # Minimum pump ON time (prevent rapid cycling)
PUMP_MAX_DURATION_MS = 60000  # Maximum pump ON time (prevent flooding)

# Heartbeat
HEARTBEAT_INTERVAL_S = 60   # Send SensorReport with only battery + RSSI

CONFIG = {
    "loglevel": 3,
    "enable_transport": False,
    "lora_boards": LORA_BOARDS,  # Adapted for Atom Lite SX1262 if needed
    "interfaces": [
        {
            "type": "SerialInterface",  # UART → STM32 DTU
            "board": "atom_lite_to_dtu",
            "name": "DTU Bridge",
            "enabled": True,
            "port": DTU_SERIAL_PORT,
            "baudrate": DTU_BAUDRATE,
        },
    ],
}
```

### 2.3 LoRa Boards Preset (mirrors lora_boards.py)

```python
"""
LoRa board pinouts for ATOM Lite + STM32WLE5CC DTU.
Mirrors cardputer_client/lora_boards.py pattern.
"""

LORA_BOARDS = {
    "atom_lite_dtu_bridge": {
        # Atom Lite I2C → PCA9548A Hub
        "i2c_sda": 21,
        "i2c_scl": 22,
        # Atom Lite UART → STM32 DTU (SerialInterface)
        "dtu_uart_tx": 17,
        "dtu_uart_rx": 16,
        # PCA9548A channel assignments
        "moisture_channel": 0,
        "temp_humid_channel": 1,
        "pressure_channel": 2,
        # Pump control
        "pump_gpio": 26,
        # Battery ADC
        "battery_adc": 34,
    },
}
```

---

## 3. Sensor Firmware

### 3.1 Sensor Mapping to LMAO SensorReport

The `SensorReport.readings[]` array uses `sensor_id` values to identify each measurement type.
**New sensor IDs for irrigation node** (added to the existing Cardputer protocol):

| sensor_id | Type | Source | Unit |
|-----------|------|--------|------|
| 1 | Die Temperature | ESP32-S2 internal | °C |
| 2 | Ambient Humidity | SHT30 on I2C ch1 | % |
| 3 | Ambient Temperature | SHT30 on I2C ch1 | °C |
| 4 | Soil Moisture | Capacitive sensor on I2C ch0 | % (calibrated) |
| 5 | Barometric Pressure | QMP6988 on I2C ch2 | hPa |
| 6 | Pump Duration (output) | Fuzzy engine | seconds |
| 7 | Pump Active | Relay state | bool (0/1) |
| 8 | Battery Voltage | ESP32 ADC | V |
| 9 | RSSI | SX1262 / LoRaWAN link margin | dBm |

### 3.2 Sensor Reading Functions

```python
# firmware/src/sensors.py — adapted from cardputer_client main.py

from machine import I2C, Pin, ADC
import time

# PCA9548A I2C multiplexer
from urns.interfaces.i2c_mux import PCA9548A
i2c_mux = PCA9548A(I2C(1, sda=Pin(21), scl=Pin(22)), freq=100000)

# Moisture sensor (channel 0) — agent discovers analog vs I2C type
class MoistureSensor:
    """Capacitive soil moisture — calibrated from raw ADC."""
    def __init__(self, pin=32):  # ADC pin or I2C channel 0
        self.pin = ADC(Pin(pin))
        self.pin.atten(ADC.ATTN_11DB)  # 0-3.3V range
        self.calib_dry = 30.0     # from config.py
        self.calib_wet = 500.0    # from config.py

    def read(self):
        raw = self.pin.read()
        # Linear interpolation to 0-100%
        if raw <= self.calib_dry: return 100.0  # dry = full moisture? depends on sensor type
        if raw >= self.calib_wet: return 0.0    # wet = low ADC
        pct = 100.0 - ((raw - self.calib_dry) / (self.calib_wet - self.calib_dry)) * 100.0
        return max(0.0, min(100.0, pct))


# SHT30 on I2C channel 1
class SHT30Sensor:
    def __init__(self, i2c_bus, addr=0x44):
        self.i2c = i2c_bus
        self.addr = addr

    def read(self):
        data = self.i2c.readfrom_mem(self.addr, 0x00, 6)
        temp = ((data[0] << 8) | data[1]) * 175 / 65535 - 45
        hum = ((data[3] << 8) | data[4]) * 100 / 65535
        return round(temp, 2), round(hum, 2)


# QMP6988 on I2C channel 2
class QMP6988Sensor:
    def __init__(self, i2c_bus, addr=0x70):
        self.i2c = i2c_bus
        self.addr = addr

    def read(self):
        data = self.i2c.readfrom_mem(self.addr, 0x1a, 3)  # pressure register
        pressure = ((data[0] << 16) | (data[1] << 8) | data[2]) / 4096.0
        return round(pressure, 2)


# Pressure trend calculation (running window)
class PressureTrend:
    """Compute hPa/hour from rolling pressure samples."""
    def __init__(self, window=12):  # 12 samples × 5min = 1 hour
        self.history = []
        self.window = window

    def add(self, pressure):
        self.history.append(pressure)
        if len(self.history) > self.window:
            self.history.pop(0)

    def trend(self):
        if len(self.history) < 2:
            return 0.0
        dt_hours = len(self.history) * (5.0 / 60.0)  # 5-min intervals
        delta = self.history[-1] - self.history[0]
        return round(delta / dt_hours, 2) if dt_hours > 0 else 0.0


# Main sensor read loop — builds SensorReport readings list
def read_all_sensors():
    """Read all sensors and return list of SensorReading dicts for LMAO."""
    readings = []

    # 1. ESP32 die temperature (always present, same as Cardputer)
    try:
        import esp32
        readings.append({
            "sensor_id": 1, "value": esp32.mcu_temperature(), "unit": "C",
            "timestamp_ms": int(time.time() * 1000),
        })
    except ImportError:
        pass  # ESP32-S2 may not have mcu_temperature

    # 2. Ambient humidity (SHT30, ch1)
    i2c_mux.selectChannel(1)
    temp, hum = sht30.read()
    readings.append({"sensor_id": 3, "value": temp, "unit": "C", "timestamp_ms": int(time.time()*1000)})
    readings.append({"sensor_id": 2, "value": hum, "unit": "%", "timestamp_ms": int(time.time()*1000)})

    # 3. Soil moisture (capacitive, ch0)
    i2c_mux.selectChannel(0)
    moisture = moisture_sensor.read()
    readings.append({"sensor_id": 4, "value": moisture, "unit": "%", "timestamp_ms": int(time.time()*1000)})

    # 4. Pressure (QMP6988, ch2)
    i2c_mux.selectChannel(2)
    pressure = pressure_sensor.read()
    readings.append({"sensor_id": 5, "value": pressure, "unit": "hPa", "timestamp_ms": int(time.time()*1000)})

    # 5. Pump state
    readings.append({"sensor_id": 6, "value": pump_duration, "unit": "s", "timestamp_ms": int(time.time()*1000)})
    readings.append({"sensor_id": 7, "value": 1 if pump_active else 0, "unit": "bool", "timestamp_ms": int(time.time()*1000)})

    # 6. Battery
    battery_mv = battery_adc.read() * 3300 / 4095  # 12-bit ADC → mV
    readings.append({"sensor_id": 8, "value": battery_mv / 1000.0, "unit": "V", "timestamp_ms": int(time.time()*1000)})

    # 7. RSSI from last LoRa packet (from DTU)
    readings.append({"sensor_id": 9, "value": last_rssi, "unit": "dBm", "timestamp_ms": int(time.time()*1000)})

    return readings
```

### 3.3 Pressure Trend Integration

```python
# Rolling pressure trend for fuzzy logic input
pressure_trend = PressureTrend(window=12)  # 1-hour lookback at 5-min intervals

# Each sensor read cycle:
pressure_val = pressure_sensor.read()
pressure_trend.add(pressure_val)
trend_hpa_hr = pressure_trend.trend()  # -3 to +3 range
```

---

## 4. Fuzzy Logic Engine (MicroPython)

### 4.1 Design Constraints

- **MicroPython compatible** — no `float` if avoidable (use fixed-point Q8.8)
- **Memory budget** — ≤ 30 KB RAM for the fuzzy engine
- **No external deps** — pure MicroPython, no C extensions

### 4.2 Fixed-Point Q8.8 Representation

| Value | Q8.8 (int16) | Meaning |
|-------|-------------|---------|
| 0.0 | 0 | Zero |
| 1.0 | 256 | One |
| 0.5 | 128 | Half |
| -0.5 | -128 | Minus half |
| 100.0 | 25600 | 100% |

### 4.3 Membership Functions (Q8.8)

**Soil Moisture** (sensor_id=4, range 0–100):
```
VERY_DRY:    triangle(0, 0, 30, 0)  → peaks at 15, zero at 0 and 30
DRY:         triangle(10, 0, 50, 0) → peaks at 30, zero at 10 and 50
MODERATE:    triangle(35, 0, 65, 0) → peaks at 50, zero at 35 and 65
WET:         triangle(55, 0, 85, 0) → peaks at 70, zero at 55 and 85
SATURATED:   triangle(75, 0, 100, 0) → peaks at 87.5, zero at 75 and 100
```

**Air Temperature** (sensor_id=3, range 5–45 °C):
```
COOL:        triangle(5, 0, 20, 0)   → peaks at 12.5, zero at 5 and 20
MILD:        triangle(15, 0, 30, 0)  → peaks at 22.5, zero at 15 and 30
WARM:        triangle(27, 0, 40, 0)  → peaks at 33.5, zero at 27 and 40
HOT:         triangle(38, 0, 45, 0)  → peaks at 41.5, zero at 38 and 45
```

**Pressure Trend** (computed, range −3 to +3 hPa/h):
```
FALLING:     triangle(−3, 0, −1, 0)  → peaks at −2, zero at −3 and −1
STABLE:      triangle(−1, 0, 1, 0)   → peaks at 0, zero at −1 and 1
RISING:      triangle(0, 0, 3, 0)    → peaks at 1.5, zero at 0 and 3
```

### 4.4 Fuzzy Engine Implementation

```python
# firmware/src/fuzzy.py — MicroPython fixed-point fuzzy logic

Q8_8 = 256  # Scaling factor

def _q8_8(val):
    """Convert float to Q8.8 fixed-point int."""
    return int(val * Q8_8)

def _deq8_8(val):
    """Convert Q8.8 fixed-point int back to float."""
    return val / Q8_8

def triangular_membership(x, a, b, c):
    """Triangular MF: a=left foot, b=peak, c=right foot. Returns Q8.8 degree [0..256]."""
    if a == b:  # Single point
        if x == a: return Q8_8
        return 0
    if x <= a or x >= c:
        return 0
    if a <= x <= b:
        # Rising slope
        return int((x - a) / (b - a) * Q8_8) if b > a else 0
    else:
        # Falling slope (b < x <= c)
        return int((c - x) / (c - b) * Q8_8) if c > b else 0

def fuzzy_eval(moisture_pct, temp_c, pressure_trend_hpa_hr):
    """
    Main fuzzy evaluation. Returns pump_duration_ms (int).
    Uses centroid (center of gravity) defuzzification.
    """
    # --- Membership degrees (Q8.8) ---
    moist_mf = {
        "VERY_DRY": triangular_membership(moisture_pct, 0, 15, 30),
        "DRY":      triangular_membership(moisture_pct, 10, 30, 50),
        "MODERATE": triangular_membership(moisture_pct, 35, 50, 65),
        "WET":      triangular_membership(moisture_pct, 55, 70, 85),
        "SATURATED": triangular_membership(moisture_pct, 75, 87, 100),
    }
    temp_mf = {
        "COOL": triangular_membership(temp_c, 5, 12, 20),
        "MILD": triangular_membership(temp_c, 15, 22, 30),
        "WARM": triangular_membership(temp_c, 27, 33, 40),
        "HOT":  triangular_membership(temp_c, 38, 41, 45),
    }
    pres_mf = {
        "FALLING": triangular_membership(pressure_trend_hpa_hr, -3, -2, -1),
        "STABLE":  triangular_membership(pressure_trend_hpa_hr, -1, 0, 1),
        "RISING":  triangular_membership(pressure_trend_hpa_hr, 0, 1.5, 3),
    }

    # --- Rule base (core rules, agent expands to full matrix) ---
    # Each rule: (antecedent_condition, consequent_centroid, weight_modifier)
    # Centroid = Q8.8 of the output membership peak (in seconds)

    rules = [
        # Priority: HIGH (pressure override — always suppress if strongly falling)
        ("pressure_falling_strong", pres_mf["FALLING"] > Q8_8 * 2/3, 0),

        # VERY_DRY rules
        ("vd_hot",       moist_mf["VERY_DRY"], "HOT",    30 * Q8_8),
        ("vd_mild",      moist_mf["VERY_DRY"], "MILD",   20 * Q8_8),
        ("vd_cool",      moist_mf["VERY_DRY"], "COOL",   15 * Q8_8),
        ("vd_rising",    moist_mf["VERY_DRY"], "RISING", 10 * Q8_8),  # reduced if rain likely

        # DRY rules
        ("dr_hot",       moist_mf["DRY"], "HOT",    25 * Q8_8),
        ("dr_warm",      moist_mf["DRY"], "WARM",   15 * Q8_8),
        ("dr_mild",      moist_mf["DRY"], "MILD",   10 * Q8_8),

        # MODERATE rules
        ("md_hot_rising", moist_mf["MODERATE"], "HOT",  8 * Q8_8, "RISING"),  # reduced if rising
        ("md_hot",       moist_mf["MODERATE"], "HOT",  5 * Q8_8),
        ("md_warm",      moist_mf["MODERATE"], "WARM", 3 * Q8_8),
        ("md_mild",      moist_mf["MODERATE"], "MILD", 0),  # no watering
        ("md_cool",      moist_mf["MODERATE"], "COOL", 0),

        # WET rules (short watering only if hot)
        ("wt_hot",       moist_mf["WET"], "HOT",  2 * Q8_8),
        ("wt_mild",      moist_mf["WET"], "MILD", 1 * Q8_8),
        ("wt_stable",    moist_mf["WET"], "STABLE", 0),

        # SATURATED — always 0
        ("sat",          moist_mf["SATURATED"], None, 0),
    ]

    # Simplified centroid defuzzification
    # (Full 175-rule matrix generated by agent — see §10)
    num = 0  # numerator (Q8.8 * seconds)
    den = 0  # denominator (Q8.8)

    for rule_name, antecedent, *rest in rules:
        if isinstance(rest[0], str):
            # 3-antecedent rule: antecedent + temp_mf + pres_mf
            temp_name, consequent_val = rest[0], rest[1]
            pres_name = rest[2] if len(rest) > 2 else "STABLE"
            if pres_name == "STABLE":
                strength = min(antecedent, temp_mf.get(temp_name, 0))
            else:
                strength = min(antecedent, temp_mf.get(temp_name, 0), pres_mf.get(pres_name, 0))
            centroid = consequent_val
        elif rest == []:
            # 1-antecedent rule (pressure override or saturated)
            strength, centroid = antecedent, rest[0] if rest else 0
        else:
            strength, centroid = antecedent, rest[0]

        if strength > 0:
            num += strength * centroid
            den += strength

    if den == 0:
        return 0

    # Q8.8 division: (num / den) → seconds → convert to ms
    duration_q88 = num // den  # Integer division in Q8.8
    duration_sec = _deq8_8(duration_q88)

    # Clamp to [0, 30] seconds
    duration_sec = max(0.0, min(30.0, duration_sec))

    # Convert to milliseconds
    return int(duration_sec * 1000)
```

### 4.5 Agent Deliverables for Fuzzy

1. **Full 5 × 4 × 3 = 60-rule matrix** (compressed — many rules share same consequent)
2. **Response surface plots** (sim_fuzzy.py on dev machine): pump duration as surface over moisture × temp, with pressure-trend overlays
3. **Calibration tool**: function to adjust membership peaks based on observed plant response
4. **Safety rules**: pressure falling fast → instant pump-off override (hard-coded, not fuzzy)

---

## 5. Pump Controller

```python
# firmware/src/pump.py

from machine import Pin
import time

class PumpController:
    def __init__(self, gpio=26, min_duration_ms=5000, max_duration_ms=60000):
        self.gpio = Pin(gpio, Pin.OUT)
        self.gpio.value(0)  # OFF
        self.min_ms = min_duration_ms
        self.max_ms = max_duration_ms
        self.active = False
        self.on_ms = 0

    def set_duration(self, ms):
        """Set next pump duration (ms). Clamped to min/max."""
        self.target_ms = max(self.min_ms, min(self.max_ms, ms))
        self.active = True

    def execute(self):
        """Start pump and return after duration."""
        if not self.active:
            return
        self.gpio.value(1)  # ON
        start = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), start) < self.target_ms:
            # Check for interrupt command (downlink CAN abort pump)
            if self._check_interrupt():
                self.gpio.value(0)
                self.active = False
                return
            time.sleep_ms(100)
        self.gpio.value(0)  # OFF
        self.on_ms += self.target_ms
        self.active = False

    def _check_interrupt(self):
        """Check if a downlink command interrupted the pump."""
        return self._interrupted  # set by LXMF callback

    def off(self):
        """Emergency stop (called on CommandRequest with action=pump_stop)."""
        self.gpio.value(0)
        self.active = False
```

---

## 6. Main Loop (mirrors cardputer_client/main.py structure)

```python
# firmware/src/main.py

"""
ATOM Lite Irrigation Node — LMAO LXMF Sensor Client.

Adapted from cardputer_client/main.py structure:
- Init Reticulum via _init_rns()
- Init LXMF via _init_lxmf_router()
- Send periodic SensorReports (make_sensor_message → encode_sensor_envelope)
- Handle downlink CommandRequests (handle_reply → CommandRequest detection)
- Heap self-recovery (same pattern as Cardputer #71)
- Hardware watchdog (#74)
"""

import gc, sys, time, uasyncio as asyncio
gc.collect()

# ── µReticulum imports ──────────────────────────────────────────────
# Same pattern as cardputer_client/main.py: try /flash/lib then /lib
for _p in ["/flash/lib", "/lib"]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
    try:
        from urns import Identity, Reticulum
        from urns.lxmf import LXMessage, LXMRouter
        from proto.lma_encoder import (
            encode_sensor_envelope, decode_envelope,
            encode_command_ack, encode_field,
            encode_length_delimited,
            FIELD_SENSOR, FIELD_COMMAND, FIELD_ACK,
        )
        HAS_URNS = True
        break
    except ImportError:
        continue

# ── Local libs ─────────────────────────────────────────────────────
from fuzzy import fuzzy_eval
from sensors import MoistureSensor, SHT30Sensor, QMP6988Sensor, PressureTrend, read_all_sensors
from pump import PumpController

# ── State ──────────────────────────────────────────────────────────
NODE_IDENTITY = None
ROUTER = None
MOISTURE = None
SHT30_DEV = None
QMP6988_DEV = None
PRESSURE_TREND = None
PUMP = None
SEQ = 0
LAST_RSSI = None
_PUMP_INTERRUPTED = False
STATUS_LINES = []

# ── Display ────────────────────────────────────────────────────────
try:
    import st7789
    from machine import SPI, Pin
    HAS_DISPLAY = True
except ImportError:
    HAS_DISPLAY = False
    tft = None

# ── Watchdog ──────────────────────────────────────────────────────
WDT_TIMEOUT_MS = 120000
_wdt = None

def _start_watchdog():
    try:
        from machine import WDT
        t = WDT_TIMEOUT_MS
        while t >= 10000:
            try: return WDT(timeout=t)
            except: t //= 2
    except ImportError: pass
    return None

async def _feed_watchdog():
    interval = 30  # well under WDT_TIMEOUT_MS
    while True:
        _wdt.feed()
        await asyncio.sleep(interval)

# ── Heap recovery (same pattern as Cardputer #71) ──────────────────
_HEAP_PROBE = 2048

def _heap_ok():
    try:
        p = bytearray(_HEAP_PROBE)
        del p
        return True
    except MemoryError:
        return False

# ── Heartbeat / SensorSend task ───────────────────────────────────
async def _heartbeat_task(interval_s):
    global SEQ, LAST_RSSI, _PUMP_INTERRUPTED
    while True:
        try:
            # Read sensors
            moisture_pct = MOISTURE.read()
            i2c_mux.selectChannel(1)
            air_temp, air_hum = SHT30_DEV.read()
            i2c_mux.selectChannel(2)
            pressure = QMP6988_DEV.read()
            PRESSURE_TREND.add(pressure)
            trend_hpa_hr = PRESSURE_TREND.trend()

            # Fuzzy eval
            pump_ms = fuzzy_eval(moisture_pct, air_temp, trend_hpa_hr)

            # Execute pump (if duration > 0 and not already running)
            if pump_ms > 0 and not PUMP.active:
                PUMP.set_duration(pump_ms)
                PUMP.execute()

            # Build SensorReport readings list
            battery_mv = PUMP.battery_adc.read() * 3300 / 4095
            readings = [
                {"sensor_id": 4, "value": moisture_pct, "unit": "%", "timestamp_ms": int(time.time()*1000)},
                {"sensor_id": 3, "value": air_temp, "unit": "C", "timestamp_ms": int(time.time()*1000)},
                {"sensor_id": 2, "value": air_hum, "unit": "%", "timestamp_ms": int(time.time()*1000)},
                {"sensor_id": 5, "value": pressure, "unit": "hPa", "timestamp_ms": int(time.time()*1000)},
                {"sensor_id": 6, "value": pump_ms/1000.0, "unit": "s", "timestamp_ms": int(time.time()*1000)},
                {"sensor_id": 7, "value": 1 if PUMP.active else 0, "unit": "bool", "timestamp_ms": int(time.time()*1000)},
                {"sensor_id": 8, "value": battery_mv/1000, "unit": "V", "timestamp_ms": int(time.time()*1000)},
            ]

            # Send via LXMF (same pattern as Cardputer make_sensor_message)
            payload = encode_sensor_envelope(
                NODE_IDENTITY.hex(), SEQ, battery_mv/1000, readings
            )
            ROUTER.send_message(
                destination_hash=bytes.fromhex(DEST_HASH),
                content=payload,
                title="p:Envelope",
            )
            SEQ += 1
            STATUS_LINES.append(f"TX#{SEQ} moist={moisture_pct:.0f}% temp={air_temp:.1f}C pump={pump_ms}ms")

            # Check heap
            if not _heap_ok():
                STATUS_LINES.append("Heap fragment → reset")
                await asyncio.sleep(1)
                raise MemoryError("Heap fragmented, resetting")

        except Exception as e:
            STATUS_LINES.append(f"Error: {e}")
            import sys; sys.print_exception(e)
            # Try to recover
            gc.collect()
            if isinstance(e, MemoryError):
                STATUS_LINES.append("Resetting due to memory error")
                await asyncio.sleep(1)
                machine.reset()

        # Deep sleep (same as Cardputer sleep_between_send_cycles)
        await asyncio.sleep(interval_s)

# ── Downlink Handler ──────────────────────────────────────────────
def handle_downlink(message):
    """Process incoming LXMF messages for CommandRequest commands.
    Same pattern as Cardputer handle_reply — checks for CMD field 11.
    """
    raw = message.content if hasattr(message, "content") else b""
    if HAS_PROTO and decode_envelope is not None:
        try:
            result = decode_envelope(raw)
            if isinstance(result, dict) and "cmd_id" in result:
                # CommandRequest received
                action = result.get("action", "")
                params = result.get("params", {})
                cmd_id = result.get("cmd_id", "")

                if action.lower() == "pump_on":
                    PUMP.set_duration(int(params.get("duration_ms", 5000)))
                    PUMP.execute()
                    send_command_ack(cmd_id, True, "Pump started")
                elif action.lower() == "pump_off":
                    PUMP.off()
                    send_command_ack(cmd_id, True, "Pump stopped")
                elif action.lower() == "fuzzy_update":
                    # Update fuzzy calibration params
                    STATUS_LINES.append(f"Fuzzy params updated")
                    send_command_ack(cmd_id, True, "Params updated")
                elif action.lower() == "reboot":
                    send_command_ack(cmd_id, True, "Rebooting")
                    await asyncio.sleep(1)
                    machine.reset()

        except Exception as e:
            STATUS_LINES.append(f"Cmd decode error: {e}")

def send_command_ack(cmd_id, success, msg):
    """Send CommandAck back via LXMF (same pattern as Cardputer)."""
    if HAS_PROTO and encode_command_ack is not None:
        ack = encode_command_ack(cmd_id, NODE_IDENTITY.hex(), success, msg)
        ack_envelope = encode_field(FIELD_ACK, 2, encode_length_delimited(ack))
        ROUTER.send_message(
            destination_hash=bytes.fromhex(SERVER_DEST_HASH),
            content=ack_envelope,
            title="p:Envelope",
        )

# ── Main ──────────────────────────────────────────────────────────
def main():
    global NODE_IDENTITY, ROUTER, MOISTURE, SHT30_DEV, QMP6988_DEV
    global PRESSURE_TREND, PUMP, SEQ, STATUS_LINES, DEST_HASH
    global SERVER_DEST_HASH, _wdt, HAS_URNS, HAS_PROTO, tft

    # 1. Init identity
    NODE_IDENTITY = Identity.load_or_create("/flash/lmao_irrigation")
    SERVER_DEST_HASH = DEST_HASH  # Set from config.py

    # 2. Init µReticulum
    rns = _init_rns(CONFIG, DEBUG)
    rns.setup_interfaces()

    # 3. Init LXMF router (same pattern as Cardputer)
    ROUTER = _init_lxmf_router(NODE_IDENTITY, "/flash/lxmf_irrigation", "LMAO_Irrigation")

    # 4. Init hardware
    MOISTURE = MoistureSensor(pin=32)
    i2c_mux.selectChannel(1)
    SHT30_DEV = SHT30Sensor(i2c_mux.i2c)
    i2c_mux.selectChannel(2)
    QMP6988_DEV = QMP6988Sensor(i2c_mux.i2c)
    PRESSURE_TREND = PressureTrend(window=12)
    PUMP = PumpController(gpio=26, min_duration_ms=5000, max_duration_ms=60000)
    _wdt = _start_watchdog()

    # 5. Init display (if present)
    tft = init_display()

    # 6. Start tasks
    asyncio.create_task(_heartbeat_task(INTERVAL_SECONDS))
    asyncio.create_task(_feed_watchdog(_wdt))

    print("LMAO Irrigation Ready")
    print(f"ID: {NODE_IDENTITY.hex()[:8]}...")
    asyncio.run(asyncio.gather(*asyncio.Task.all_tasks()))

if __name__ == "__main__":
    main()
```

### 6.2 SX1262 Integration Notes

The Atom Lite's LoRa radio uses an SX1262 (same as Cardputer's Cap LoRa-1262). The `lora_boards.py` preset for the Atom Lite:

```python
"atom_lite_dtu_bridge": {
    "spi_bus": 2,           # HSPI (SPI3_HOST)
    "sck": 40,              # MTDO — reclaimed JTAG pin
    "mosi": 14,             # GPIO14
    "miso": 39,             # MTCK — reclaimed JTAG pin
    "cs": 8,                # GPIO8
    "reset": 12,            # GPIO12
    "dio1": 11,             # DIO1 (IRQ) — sx126x driver requires this
    "tcxo_voltage": 1.8,    # TCXO voltage
    "tcxo_delay_us": 5000,  # TCXO startup time (5ms)
    "freq_khz": 868000,     # EU868 / 915000 for US
    "syncword": 0x1424,     # Reticulum default (same as Cardputer)
}
```

**Key difference from Cardputer:** The Atom Lite is **LoRaWAN-class** (connected to STM32WLE5CC DTU via UART), so the SX1262 on the Atom Lite acts as a **serial bridge** to the DTU — the DTU handles LoRaWAN protocol stack (join, session keys, frame counters), while the Atom Lite just sends/receives raw LoRa packets over UART.

**Alternative:** Skip the SX1262 on the Atom Lite entirely and use the STM32WLE5CC's built-in LoRa radio (STM32WLE5CC has an integrated SX1276/SX1262). This means:
- Atom Lite sends sensor data → UART → STM32WLE5CC DTU
- STM32WLE5CC handles all LoRaWAN (Class A) — join, TX, RX windows
- Atom Lite's SX1262 is unused (can repurpose pins for sensors)

**Recommended: use STM32WLE5CC as the LoRa radio.** This mirrors the architecture where:
- STM32WLE5CC = DTU (like the RNode in LMAO)
- Atom Lite = sensor node (like the Cardputer)
- UART bridge = transparent LoRa tunnel (like Cardputer's LoRaInterface)

---

## 7. Data Compression for LoRaWAN

LXMF over LoRaWAN is packet-based. Each `SensorReport` envelope is wrapped in an LXMF message with:
- Title: `p:Envelope`
- Content: protobuf-encoded `LMAOEnvelope.SensorReport`
- Method: Opportunistic (single-packet)

### 7.1 Payload Budget

A typical `SensorReport` with 8 readings:
- node_id: ~8 bytes (hex string)
- seq: 1 byte (varint)
- battery: 4 bytes (float32)
- 8 × SensorReading: each ~16 bytes → 128 bytes
- Total: ~141 bytes → **within LoRa packet limit** (~250 bytes with overhead)

**Optimization:** Reduce readings from 8 to 5 (drop pressure, pump state, RSSI) when battery < 3.0 V:
```python
def get_readings_for_network(battery_mv):
    if battery_mv < 3000:
        return [m, t, h]  # moisture, temp, humidity only
    return all_readings  # all 8
```

### 7.2 Compression Strategy

For future ML training datasets, the sensor node sends:
1. **Full SensorReport** on every interval (for the server-side time series)
2. **Fuzzy output summary** (pump duration) embedded in the SensorReport readings
3. **Batched snapshots** every N intervals (if multiple samples collected between TX cycles)

The server-side LXMF ingester (`k8s-app/iot_ingest.py`) writes to DuckDB. Add an `irrigation_node` table:
```sql
CREATE TABLE irrigation_sensor_readings (
    node_id TEXT,
    seq INTEGER,
    sensor_id INTEGER,
    value REAL,
    unit TEXT,
    timestamp_ms BIGINT,
    rssi INTEGER,
    battery_mv REAL,
    received_at BIGINT,
    PRIMARY KEY (node_id, seq, sensor_id, timestamp_ms)
);
```

---

## 8. Development Phases

### Phase 1: LMAO Stack Port (Days 1–3)
- [ ] Clone cardputer_client lib to `firmware/lib/urns/`
- [ ] Clone lma_encoder.py to `firmware/proto/`
- [ ] Adapt `lora_boards.py` for Atom Lite pinout
- [ ] Get µReticulum Identity working on Atom Lite (flash → verify node ID)
- [ ] Test basic LXMF message send/receive between Atom Lite + Cardputer

### Phase 2: Sensor Drivers (Days 4–7)
- [ ] PCA9548A I2C multiplexer driver (port from adafruit or hand-write)
- [ ] Capacitive moisture sensor — discover analog/I2C type, calibrate
- [ ] SHT30 driver — I2C ch1, verify readings
- [ ] QMP6988 driver — I2C ch2, verify readings
- [ ] PressureTrend rolling window
- [ ] Battery ADC calibration

### Phase 3: Fuzzy Engine (Days 8–12)
- [ ] Hand-coded fixed-point fuzzy logic (no `float` where possible)
- [ ] Full 60-rule matrix (5 moisture × 4 temp × 3 pressure)
- [ ] Centroid defuzzification
- [ ] `sim_fuzzy.py` on dev machine: sweep input space, plot response surface
- [ ] Unit tests: `tests/test_fuzzy.py` (pytest, MicroPython mock)
- [ ] Safety override: pressure falling > 2 hPa/h → pump_off regardless of fuzzy output

### Phase 4: Integration — Sensor → Fuzzy → LXMF Send (Days 13–16)
- [ ] Wire all sensors → fuzzy → SensorReport → LXMF send
- [ ] Verify `LMAOEnvelope` with new sensor_ids (4–9) decodes correctly on server
- [ ] Test with Cardputer as reference: both send SensorReports, server shows both
- [ ] Battery monitoring + low-battery readings reduction
- [ ] Heap fragmentation self-recovery (same pattern as Cardputer #71)

### Phase 5: Downlink + Pump Control (Days 17–20)
- [ ] Handle `CommandRequest` downlinks (pump_on, pump_off, fuzzy_update, reboot)
- [ ] Implement `CommandAck` responses
- [ ] Pump relay/MOSFET driver (dry run — no water)
- [ ] Test end-to-end: server sends pump_on → node executes → CommandAck → server receives
- [ ] Fuzzy params update via downlink (adjust membership peaks remotely)

### Phase 6: LoRaWAN Integration (Days 21–25)
- [ ] UART bridge: Atom Lite ↔ STM32WLE5CC DTU
- [ ] STM32CubeMX project: LoRaWAN stack (OTAA, Class A)
- [ ] SerialInterface in µReticulum config (Atom Lite → DTU UART)
- [ ] Test full path: sensor → LXMF → LoRaWAN uplink → RNode/ChirpStack → LMAO Server
- [ ] Verify RSSI in SensorReport readings
- [ ] ADR (adaptive data rate) if supported by stack
- [ ] Duty cycle management (EU868 1%, US915 4%)

### Phase 7: Power Profiling + Field Test (Days 26–30)
- [ ] Deep sleep profiling — target < 20 mA avg
- [ ] Measure sleep current (expect ~2 µA STM32 + ~10 µA ESP32)
- [ ] 72-hour continuous run test
- [ ] Solar + battery prototype
- [ ] Stress test: I2C lockup recovery (PCA9548A reset sequence)

### Phase 8: Server-Side + Documentation (Days 31–35)
- [ ] Extend `k8s-app/iot_ingest.py` for irrigation node sensor_id mapping
- [ ] DuckDB table: `irrigation_sensor_readings` (see §7.2)
- [ ] Query API: `GET /api/sensors/{node_id}/history?sensor_id=4`
- [ ] Pin mapping, assembly guide, field deployment checklist
- [ ] Complete `fuzzy-rules.md` with all 60 rules + response surface plots

---

## 9. Agent Handoff Notes

### What the coding agent must discover (not guess):

1. **Moisture sensor type** — scan I2C bus first (`i2c_scan()`); if analog (voltage divider), wire to ESP32-S2 ADC pin. Check the Watering Unit product photos/specs.
2. **PCA9548A channel mapping** — run I2C scan on each channel to confirm device placement.
3. **LoRaWAN region** — EU868 / US915 / AS923 — affects frequency, DR, duty cycle.
4. **Pump type** — relay (5V) vs MOSFET (PWM). Verify from photos.
5. **STM32WLE5CC UART pinout** — confirm TX/RX cross-wiring (Atom Lite UART1 TX1→STM32 RX, RX1→STM32 TX).
6. **Battery voltage divider** — measure resistors or ask user, then compute ADC→mV conversion.
7. **Atom Lite SX1262 pins** — if using SX1262 directly (not via DTU), confirm SPI pins match `lora_boards.py` preset.

### Key constraints:

- **No float if avoidable** — use fixed-point Q8.8 for fuzzy engine
- **Memory budget:** ESP32-S2 has ~520 KB SRAM + optional PSRAM; keep firmware < 256 KB flash
- **I2C clock:** 100 kHz for stability (not 400 kHz) — PCA9548A + long sensor wires
- **MicroPython only** — no C/C++, no ESP-IDF (unless STM32WLE5CC side only)
- **LMAO protocol** — SensorReport uses `sensor_id` convention: 1=ESP32 die temp, 2=humidity, 3=air temp, **4=new (soil moisture)**, **5=new (pressure)**, **6=new (pump duration)**, **7=new (pump active)**, **8=new (battery)**, **9=new (RSSI)**
- **Deep sleep is mandatory** — Atom Lite must sleep between sensor cycles (use `machine.deepsleep()`)
- **Same heap-recovery pattern** as Cardputer: gc.collect + heap probe + auto-reset on fragmentation

### Shared library deployment:

```bash
# Deploy cardputer_client lib to Atom Lite (same as cardputer flash)
# Adapt cardputer_client/flash.py for Atom Lite serial port
ampy --port /dev/ttyUSB1 put firmware/lib/urns/*.py /lib/urns/
ampy --port /dev/ttyUSB1 put firmware/lib/urns/interfaces/*.py /lib/urns/interfaces/
ampy --port /dev/ttyUSB1 put firmware/lib/urns/crypto/*.py /lib/urns/crypto/
ampy --port /dev/ttyUSB1 put firmware/proto/*.py /lib/proto/
ampy --port /dev/ttyUSB1 put firmware/config.py /flash/config.py
ampy --port /dev/ttyUSB1 put firmware/src/main.py /flash/main.py
```

Or create a unified `install_all.py` tool that flashes both Cardputer AND irrigation nodes:
```bash
bazel run //tools:install_all -- --include-irrigation --irrigation-port /dev/ttyACM0
```

---

## 10. Future ML Path (Post-Fuzzy)

Once sufficient data is collected (> 1,000 samples across seasons):

1. **Export LXMF SensorReports** → CSV/Parquet via `k8s-app/iot_ingest.py` DuckDB query
2. **Feature engineering:**
   - Soil moisture lag features (t−1, t−6, t−24, t−168)
   - Air temp trend (ΔT/minute)
   - Pressure trend (hPa/hour from QMP6988)
   - Humidity (condensation predictor)
   - Pump duration (output variable for supervised learning)
3. **Model candidates:**
   - **LSTM** — predict soil moisture at t+1, t+6, t+24 hours
   - **1D CNN** — detect anomalies (rapid moisture drop = leak/pump failure)
   - **Transformer** — if > 10,000 samples across multiple nodes
4. **Training on DGX Spark** — quantized INT8 model (~50 KB) for edge deployment
5. **On-node inference:** Replace fuzzy engine with TinyML model (MicroPython TensorFlow Lite)
6. **Fallback:** Fuzzy logic remains active when ML confidence < threshold

---

## 11. Complete Directory Structure

```
smart-irrigation/
├── platformio.ini                      # For STM32WLE5CC DTU firmware only
├── firmware/                           # MicroPython firmware for Atom Lite
│   ├── config.py                       # Node config (mirrors cardputer_client/config.py)
│   ├── lora_boards.py                  # Pinout presets (mirrors cardputer_client/lora_boards.py)
│   ├── flash.py                        # Deploy tool (adapted from cardputer_client/flash.py)
│   ├── main.py                         # Entry point (mirrors cardputer_client/main.py structure)
│   ├── lib/
│   │   ├── urns/                       # COPY from cardputer_client/lib/urns/
│   │   │   ├── __init__.py
│   │   │   ├── identity.py
│   │   │   ├── destination.py
│   │   │   ├── lxmf.py
│   │   │   ├── log.py
│   │   │   ├── reticulum.py
│   │   │   ├── transport.py
│   │   │   ├── crypto/                 # COPY from cardputer_client
│   │   │   │   ├── __init__.py
│   │   │   │   ├── aes.py
│   │   │   │   ├── ed25519.py
│   │   │   │   ├── hmac.py
│   │   │   │   ├── hkdf.py
│   │   │   │   └── ...
│   │   │   └── interfaces/
│   │   │       ├── __init__.py
│   │   │       ├── lora.py             # COPY + adapt for Atom Lite pins
│   │   │       ├── serial.py           # COPY — for UART to STM32 DTU
│   │   │       └── ...
│   │   └── sensors/
│   │       ├── __init__.py
│   │       ├── dht20.py                # COPY from cardputer_client (example sensor)
│   │       └── pca9548a.py             # NEW — I2C mux driver
│   ├── proto/
│   │   └── lma_encoder.py             # COPY from cardputer_client/proto/
│   ├── src/
│   │   ├── fuzzy.py                    # Fuzzy logic engine (fixed-point Q8.8)
│   │   ├── sensors.py                  # Sensor drivers (moisture, SHT30, QMP6988)
│   │   ├── pump.py                     # Pump controller (relay/MOSFET)
│   │   └── display.py                  # ST7789 display helpers (optional)
│   └── tests/
│       ├── test_fuzzy.py               # Fuzzy unit tests (pytest)
│       ├── test_lma_encoder.py         # Verify SensorReport encoding
│       └── test_sensors.py             # Sensor mock tests
├── stm32-dtu/                          # STM32WLE5CC LoRaWAN DTU
│   ├── STM32WLE5CC_Proj/              # CubeMX generated project
│   │   ├── Src/
│   │   │   ├── main.c                 # STM32 entry
│   │   │   ├── lorawan_app.c / .h     # App layer, join/session
│   │   │   ├── uart_bridge.c / .h     # UART ↔ LoRaWAN bridge
│   │   │   └── tx_scheduler.c / .h    # Scheduled TX
│   │   └── Middlewares/Third_Party/   # STM32WLE LoRaWAN stack
│   └── README.md                       # Build instructions
├── k8s-app/
│   └── irrigation_ingest.py            # NEW — extend iot_ingest.py for irrigation node
├── docs/
│   ├── pin-mapping.md                  # Pin assignments
│   ├── lora-params.md                  # LoRaWAN region, DR, frequency plan
│   ├── fuzzy-rules.md                  # Complete rulebase + membership functions
│   └── assembly-guide.md               # Wiring diagram, parts list
└── scripts/
    ├── sim_fuzzy.py                    # Python fuzzy simulation (dev machine)
    ├── validate_dataset.py             # Compress/validate SensorReport encoding
    └── plot_history.py                # Visualize collected sensor data
```

---

## 12. LMAO Integration Checklist

| Task | Status | Notes |
|------|--------|-------|
| Reuse `cardputer_client/lib/urns/` verbatim | ✅ | Copy as-is; no changes needed |
| Reuse `proto/lma_encoder.py` verbatim | ✅ | SensorReport encoding already exists |
| New sensor_id values (4–9) | ✅ | Documented in §3.1 |
| Server receives new sensor_ids | ✅ | `iot_ingest.py` reads all sensor_ids |
| Downlink CommandRequest → pump control | ✅ | Agent implements `pump_on/pump_off/fuzzy_update/reboot` |
| Atom Lite ↔ STM32WLE5CC UART bridge | ✅ | SerialInterface in µReticulum config |
| DEST_HASH injection (same as Cardputer) | ✅ | Reuse `install_all` tool with `--include-irrigation` |
| Heap recovery (same pattern) | ✅ | Copied from Cardputer #71 |
| Watchdog (same pattern) | ✅ | Copied from Cardputer #74 |
| REBOOT command handling | ✅ | Same pattern as Cardputer handle_reply |

---

## Appendix A: Complete Sensor ID Reference

| sensor_id | Field Name | Source | Type | Unit | Notes |
|-----------|-----------|--------|------|------|-------|
| 1 | Die Temperature | ESP32-S2 | float | °C | Existing (Cardputer) |
| 2 | Ambient Humidity | SHT30 | float | % | Existing |
| 3 | **Air Temperature** | SHT30 | float | °C | **NEW** (fuzzy input) |
| 4 | **Soil Moisture** | Capacitive | float | % | **NEW** (fuzzy input, calibrated 0–100) |
| 5 | **Barometric Pressure** | QMP6988 | float | hPa | **NEW** (fuzzy input) |
| 6 | **Pump Duration** | Fuzzy output | float | seconds | **NEW** (controller output) |
| 7 | **Pump Active** | Relay state | bool | 0/1 | **NEW** (feedback) |
| 8 | **Battery Voltage** | ADC | float | volts | **NEW** (power monitoring) |
| 9 | **RSSI** | LoRa link margin | float | dBm | **NEW** (signal quality) |

---

## Appendix B: Pin Mapping (Atom Lite → Sensors + DTU)

| Component | Atom Lite Pin | STM32WLE5CC Pin | Note |
|-----------|--------------|-----------------|------|
| PCA9548A SDA | GPIO21 | — | I2C1 SDA |
| PCA9548A SCL | GPIO22 | — | I2C1 SCL |
| PCA9548A INT | GPIO23 | — | Optional interrupt |
| Moisture sensor | GPIO32 (ADC1) | — | Depends on sensor type |
| SHT30 | I2C ch1 (21/22) | — | Via PCA9548A |
| QMP6988 | I2C ch2 (21/22) | — | Via PCA9548A |
| Pump Relay | GPIO26 | — | Active-high |
| Battery ADC | GPIO34 (ADC1) | — | Voltage divider |
| UART TX → DTU | GPIO17 (UART1_TX) | PA3 (RX) | 115200 baud |
| UART RX ← DTU | GPIO16 (UART1_RX) | PA2 (TX) | 115200 baud |
| DTU Status LED | GPIO2 | — | Blink = DTU active |
| Status LED | GPIO5 | — | Orange/blue on Atom Lite |

---

## 13. Challenge Brief — Algorithm Evaluation Mandate

**Hardware is fixed.** The next agent must assume: Atom Lite (ESP32-S2) + STM32WLE5CC LoRaWAN DTU + PCA9548A I2C hub + capacitive soil moisture sensor + SHT30 + QMP6988 + pump relay/MOSFET.

**Your mandate: make the irrigation algorithm optimal.** Everything else is plumbing.

### What you MUST evaluate before writing a single line of firmware:

#### A. Is fuzzy logic the right algorithm for irrigation?

The plan defaults to fuzzy logic because it's interpretable and easy to hand-tune. But for an irrigation controller, consider:

1. **State machine with hysteresis** — A simple threshold-based state machine (DRY → water for X seconds → back to DRY) is easier to calibrate, uses zero memory, and is less prone to runaway behavior. The fuzzy engine adds complexity for marginal benefit if the control surface is essentially "dry = more water, wet = no water."
2. **PID control on soil moisture** — Treat soil moisture as the controlled variable. The pump duration is the output. PID can handle the dynamics (slow moisture response, thermal inertia of soil). However, soil has large time constants — a 60-second pump doesn't change moisture immediately. This makes PID tricky without a predictive model.
3. **Simple model-based prediction** — Use the existing physics: soil moisture decay follows an exponential (or piecewise linear) curve depending on temperature and humidity (evapotranspiration). The node can maintain a running model of "moisture at t+1hr given current conditions" and decide pump duration to hit a target. This is more principled than fuzzy but requires an evapotranspiration estimation (a simplified Penman-Monteith using just air temp + humidity from SHT30).
4. **Hybrid approach** — Use evapotranspiration model + moisture decay curve to predict *how long* the water will last, then use a small fuzzy override layer for edge cases (rain incoming = shorten watering).

**Deliverable:** Compare all four approaches in terms of accuracy, memory footprint, calibration effort, and robustness. Recommend one. If fuzzy is recommended, justify why alternatives don't beat it for this specific use case.

#### B. If fuzzy: optimize the rulebase and membership functions

Assuming fuzzy is the chosen path (or the hybrid approach), the current plan is a rough sketch. The agent must:

1. **Derive proper membership function parameters** from soil physics, not arbitrary numbers.
   - "VERY_DRY at 0–30%" and "SATURATED at 70–100%" need to map to actual soil water content (% volumetric water content, VWC). Capacitive moisture sensors measure something related to dielectric constant, which correlates to VWC but differs by soil type. The calibration routine should output VWC% (0–100%), not raw ADC.
   - The transition zones (e.g., DRY 20–50%) should account for soil type. If the user grows in sand vs clay, the same VWC% means very different "felt dryness." The agent should flag this and propose a soil-type calibration factor the user inputs once.

2. **Generate the full 60-rule matrix** (5 moisture × 4 temp × 3 pressure) and plot the response surface.
   - Many rules will be identical (e.g., DRY + MILD + STABLE = same output as DRY + WARM + STABLE). Compress by merging equivalent rules into a smaller lookup table.
   - Include pressure trend in the matrix, but verify that pressure-trend has meaningful impact on short-term irrigation decisions. In many climates, hourly pressure trend is noise for a 5-minute irrigation decision. The agent should test sensitivity: if pressure trend changes the output by < 10% in any case, recommend dropping it from the fuzzy input and using it only for ML training.

3. **Defuzzification method** — The plan says "centroid." On a microcontroller, centroid requires division and accumulation over all active rules. A **Sugeno (Mamdani simplified) model** with constant consequents (each rule fires → output is a fixed number) reduces centroid to a weighted average: `output = Σ(weight_i × constant_i) / Σ(weight_i)`. This is one fewer multiplication and a single division — much faster. If Sugeno is used, each rule's consequent IS the pump duration in seconds (no triangle/peak shape needed).

4. **Safety constraints** — The plan mentions pressure falling as a hard override. Add more:
   - **Soil saturation lockout:** If moisture > 85% AND humidity > 90%, skip watering for 2 hours (condensation/dew conditions → water already delivered via atmosphere).
   - **Minimum interval:** Don't water again within N minutes of last watering (soil needs time to absorb; prevents ponding/runoff). This is a timer, not fuzzy.
   - **Pump cycle protection:** Minimum ON time (5s) and minimum OFF time (10s) between cycles to protect the pump.
   - **Max daily watering:** Cap at X minutes per day to prevent waterlogging (configurable).

#### C. Evapotranspiration (ET₀) model — the real algorithmic foundation

The fuzzy approach is heuristic. For algorithmic optimality, the node should estimate **reference evapotranspiration (ET₀)** using the Hargreaves-Samani simplified equation, which needs only temperature and solar radiation — but we don't have a pyranometer. The **Blaney-Criddle** or even simpler: `ET₀ ≈ k × (T_max - T_min) × f(humidity)` is feasible with just the SHT30.

**Proposed model-based algorithm:**
```
1. Measure soil moisture (θ_current), air temp (T), humidity (H), pressure trend (ΔP)
2. Compute ET₀ estimate from T and H (simplified Penman-Monteith or Hargreaves)
3. Compute soil drying rate: dθ/dt = -k × ET₀ (k = soil-dependent coefficient)
4. Predict moisture at t+1h: θ_future = θ_current + dθ/dt × 1h
5. If θ_future < θ_target (user-set, e.g., 40%), compute pump duration:
     water_needed = θ_current - θ_target
     pump_duration = water_needed × soil_coefficient × area_factor / flow_rate
6. Apply overrides: ΔP < -2 hPa/h → skip (rain); θ_current > 70% → skip
7. Apply constraints: min interval between waterings, max daily watering, pump protection
```

**This is better than fuzzy** if:
- You have a soil coefficient calibration (one-time input)
- You can estimate ET₀ reasonably (SHT30 gives T and H, which is ~70% of what Hargreaves needs)
- The pump flow rate is known (or can be calibrated once)

The agent should **benchmark both approaches** — fuzzy vs model-based — on the dev machine using realistic sensor data, and recommend whichever gives better watering decisions with less memory and simpler calibration.

#### D. ML dataset design (long-term optimality)

The node streams data for a future Time-Series / Soil Dynamics ML model. The **current algorithm choice determines what features get collected**. Optimize for what the ML model will need:

1. **Collect per-sample:** moisture, temp, humidity, pressure, pump duration, pump active flag, battery, RSSI, timestamp
2. **Derive on-node every 5 min:** moisture change rate (Δθ/min), temp change rate (ΔT/min), humidity change rate (ΔH/min), pressure change rate (ΔP/min) — these are the features the ML model will need to predict soil dynamics
3. **Track state machine:** "last watering time," "last watering duration," "days since last rain (ΔP indicator)" — temporal features
4. **Store the raw trajectory:** not just individual samples, but the *change curve* of moisture after each watering event. The ML model needs to learn: "when I applied X liters over Y minutes, how did moisture decay over the next 24 hours?" This requires recording the full moisture trace between waterings (every 5 min) and tagging the watering events.
5. **Output format:** batch all 5-min samples into one LoRaWAN uplink (batch size = 6 samples = 30 min window). Each batch becomes one training sample: `[pre-watering state, watering event, 6 post-watering moisture samples, temp/humidity trajectory over 30 min]`.

**Deliverable:** A `batch_encoder.py` on the dev machine that takes raw per-sample readings, groups them into 30-min batches, tags watering events, and outputs a JSON/CSV row that looks like:
```json
{
  "timestamp": "2026-09-11T10:00:00Z",
  "moisture_pre": 25.3,
  "pump_on": true,
  "pump_duration_s": 45,
  "moisture_post": [35.1, 33.8, 32.0, 30.5, 29.1, 27.9],
  "temp_trajectory": [32.1, 31.8, 31.2, 30.7, 30.2, 29.8],
  "humidity_trajectory": [45, 47, 50, 53, 55, 58],
  "pressure_trajectory": [1012, 1011, 1010, 1009, 1008, 1007]
}
```
This is what the ML model actually trains on — not individual readings.

#### E. Firmware protocol efficiency

Since hardware is fixed and the LMAO server exists, the node doesn't need full µReticulum/LXMF overhead. **Propose the minimal on-node protocol:**

The node sends a raw binary blob over UART to the STM32 DTU. The STM32 wraps it in a LoRaWAN uplink. The LMAO server receives it and decodes it.

```
Proposed node-to-DTU protocol (UART, 115200 baud):
| FRAME_START(0xA5) | NODE_ID(8B) | SEQ(2B) | TYPE(1B) | LENGTH(1B) | PAYLOAD(NB) | CRC16(2B) | FRAME_END(0x5A) |

TYPE values:
  0x01 = SensorBatch (full sensor data, ~100 bytes compressed)
  0x02 = PumpStatus (pump on/off, duration, moisture at decision time)
  0x03 = Heartbeat (battery, RSSI, uptime only, ~8 bytes)
  0x10 = DownlinkAck (received CommandRequest, success/fail)

Payload formats defined in proto/lma_messages.proto — reuse the existing SensorReport proto but send it as raw bytes.
No LXMF, no Reticulum, no Ed25519 on the node.
Server does LXMF envelope wrapping.
```

**This reduces node firmware from ~200 KB (full µReticulum) to ~40–60 KB.**

### Deliverable format

Produce a **written evaluation** (markdown) as the first output:

1. **Algorithm verdict** — fuzzy, model-based, hybrid, or simple state machine? Justify with comparisons of: calibration effort, memory footprint, decision accuracy, robustness, and suitability for ML training.
2. **Rulebase/membership functions** — if fuzzy is chosen, deliver the complete compressed 60-rule table with optimized parameters, response surface plot, and sensitivity analysis (especially pressure trend impact).
3. **Evapotranspiration model** — if model-based, provide the Hargreaves/Hargreaves-Samani equations simplified for T+H-only, calibrated coefficient, and Python reference implementation.
4. **ML dataset schema** — the batched 30-min format (see §D above) and the `batch_encoder.py` reference code.
5. **Protocol recommendation** — full µReticulum/LXMF on-node vs minimal UART binary, with firmware size estimates for each approach.
6. **Risk assessment** — what's most likely to go wrong algorithmically? (e.g., soil type mismatch → wrong calibration; pressure trend noise → false rain detection; pump dynamics → moisture never rises as expected)
7. **Recommended development plan** — an optimized, focused set of phases based on your evaluation.

**Only after this evaluation** should you proceed to implementation. The evaluation is the deliverable the user reads first.

**Hardware is fixed. The algorithm is what matters.**

---

## Appendix C: Fuzzy Rule Matrix Template

The agent must generate a complete 5 × 4 × 3 = 60-rule matrix (or a compressed variant where some combinations share the same consequent).

**Template for agent generation:**
```
IF Soil_Moisture IS [VERY_DRY|DRY|MODERATE|WET|SATURATED]
  AND Air_Temperature IS [COOL|MILD|WARM|HOT]
  AND Pressure_Trend IS [FALLING|STABLE|RISING]
THEN Pump_Duration = [0|5|10|15|20|25|30] seconds
```

**Special cases (always override):**
- Pressure Trend = FALLING AND hPa/hr < −2.0 → Pump = 0 (rain incoming)
- Soil Moisture = SATURATED → Pump = 0 (regardless of temp/pressure)
- Battery < 3.0V → Pump = 0 (protect battery)
- Pump already running AND Moisture still VERY_DRY after 30s → extend by 10s (max 60s total)

**Output:** A Python dict or JSON file:
```python
fuzzy_rules = {
    ("VERY_DRY", "HOT", "RISING"): 20,
    ("VERY_DRY", "HOT", "STABLE"): 30,
    ("VERY_DRY", "HOT", "FALLING"): 0,  # rain override
    # ... 60 entries or compressed subset
}
```

This gets embedded in the firmware's fuzzy engine or loaded from flash at boot.
