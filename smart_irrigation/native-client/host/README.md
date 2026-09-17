# Native (C++) Sprout client — host interop spike (issue #130, steps 1-2)

Proven on 2026-09-16:

1. **RNS core validated on host** — `RTReticulum` (0xSeren/RTReticulum, MIT, C++17),
   doctest suite: **61 cases / 1208 assertions pass**
   (identity/crypto/loopback/transport/link/resource).
2. **Bidirectional interop with real Python Reticulum (RNS 1.3.8)** over its
   TCP interface (HDLC framing):
   - RTReticulum announced → Python RNS logged
     `Valid announce … 1 hops away, received on TCPInterface[Client …:39588]`
   - Python RNS announced → RTReticulum `on_announce` fired **6×**
   => **wire-compatible RNS in native C++** — the mesh-framing risk for #130 is retired.

## Artifacts
- `interop.cpp` — host interop node (identity + `spike/interop` dest + announce + `on_announce`)
- `tcp_interface.{h,cpp}` — POSIX port of the firmware TCP interface (HDLC; no handshake)

## Build (aarch64 host)
- Deps: `cmake`, `libmbedtls-dev`, **Monocypher** (built single-file → `/usr/local`),
  `pip install rns lxmf` (the Python interop peer).
- Build footprint in `/tmp/RTReticulum` (kept for the board step):
  ```bash
  git clone https://github.com/0xSeren/RTReticulum && cd RTReticulum
  cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
  g++ -std=c++17 -O2 -Iinclude -Ihost -Ithird_party/tlsf \
      host/interop.cpp host/tcp_interface.cpp \
      build/librtreticulum.a build/librtreticulum_hal.a \
      -lpthread -lmbedcrypto -lmonocypher -lm -o build/interop
  ```

## Run
Terminal 1 (Python RNS server — TCPServerInterface on 127.0.0.1:42424):
```bash
python3 rns_spike.py
```
Terminal 2:
```bash
./build/interop 127.0.0.1 42424 30     # expect  INTEROP=OK (saw Python RNS announce)
```

## Next (issue #130 steps 3-4)
- **ESP-IDF build for ESP32-PICO-D4** + a **DTU-AT (RAK3172 P2P) interface**
  mirroring `firmware/lib/dtu/dtu_at.py` -> announce seen on the production RNode.
- **Send-only LXMF** SensorReport (sensor_id 3 / 2) -> server -> DuckDB.
