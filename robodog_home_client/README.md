# robodog_home_client

Host-side (laptop / Raspberry Pi) client that reads **air temperature and
humidity** off the LMAO server over the **Reticulum TCP interface** — no RNode,
no LoRa, no AutoInterface.

```
client ──RNS TCP (:4246)──▶ lmao-server
   LMAOEnvelope{ request.history, series = AIR_TEMP + AIR_HUMIDITY }
   ◀── LMAOEnvelope{ chart }  (ChartBundle: temp in tenths °C, hum in %)
```

It is the read side of the same protobuf contract the Cardputer and RoboDog
calendar clients speak: `Request.history` in, `ChartBundle` out.  The server
answers air-only requests from its all-node air buffer
(`lma_core/env_history.py`), which folds **every** node's `SensorReport` air
readings — Sprout SHT30 ambient temperature/humidity (`sensor_id` 3/2) and the
Cardputer's die temperature (`sensor_id` 1, used when no ambient reading is
present).  The newest reporting node wins, so the output tracks whichever
sensor last reported.

The soil chart path is untouched: `SproutHistory` still owns soil-bearing
reports, and a request that selects `SOIL` is answered from it exactly as
before.

## Run

```sh
# one fetch, human-readable
bazel run //robodog_home_client:home_client

# JSON, last 5 samples per series
bazel run //robodog_home_client:home_client -- --json --count 5

# poll every 5 minutes
bazel run //robodog_home_client:home_client -- --interval 300

# or straight from the tree
python3 robodog_home_client/client.py --verbose
```

Output (human-readable):

```
Client lxmf/delivery hash: 1a2b…  (must be in ALLOWED_CLIENTS)
LMAO env — node e824ad2d
  temperature: 22.6 °C (72.7 °F)
  humidity:    55 %
  temp series: 21.4, 22.6 °C
  hum series:  50, 55 %
```

JSON output:

```json
{"humidity_pct": [50, 55], "latest_humidity_pct": 55,
 "latest_temp_c": 22.6, "node_id": "e824ad2d",
 "period_ms": 300000, "start_ms": 1700000000000,
 "temps_c": [21.4, 22.6]}
```

Exit code `0` = a ChartBundle was received; `1` = no reply (timeout, unknown
server, unallow-listed client, or no samples yet).

## Firmware — the board that reports and charts

`firmware/` is the ESP-IDF firmware for the Waveshare ESP32-S3 RLCD-4.2 board
(same panel as `robodog_client`, ST7305): it reads its **SHTC3**
temperature/humidity sensor, reports it to the server over the Reticulum TCP
interface, and draws **its own history as a chart** on the reflective panel.

```
every RH_INTERVAL_SECONDS:
  SHTC3 ──▶ LMAOEnvelope{SensorReport (sensor_id 3 temp, 2 humidity)} ──▶ server
  LMAOEnvelope{HistoryRequest AIR_TEMP+AIR_HUMIDITY} ──▶ server
  ◀── LMAOEnvelope{ChartBundle} ──▶ chart (temp + humidity traces)
```

Panel layout (400×300 landscape): device name + latest `22.6C 55%` on the top
row, status/sample-count under it, a framed plot with the temperature trace
(solid, own min/max on the left in °C) and the humidity trace (dotted, own
min/max on the right in %), and `oldest … now` on the footer.  The chart is
drawn from `ChartBundle` values (`temp` tenths of °C, `hum` %); an empty reply
renders `waiting for data...` rather than a placeholder curve.

**Identity.** The board's Reticulum identity is pinned at build time
(`RH_NODE_IDENTITY_HEX`, from the per-device store
`~/.local/share/lmao_client/devs/<name>/identity`) and persisted in NVS, so
its `lxmf/delivery` hash never drifts across re-flashes.

**No credentials in source.** WiFi SSID/passphrase, server host/port and the
device name are **NVS-only** — `flash.sh` writes them to the 24 KB NVS
partition at flash time.  Nothing is compiled in and nothing is committed;
`build.sh` bakes only the server delivery hash, the pinned identity and the
cadence.

```sh
# build (Docker ESP-IDF, staged into the pinned RTReticulum checkout)
RH_DEST_HASH_HEX=<server delivery hash> \
RH_NODE_IDENTITY_HEX=<64-byte private key hex> \
RH_DEVICE_NAME="main bedroom" RH_INTERVAL_SECONDS=60 \
bazel run //robodog_home_client:build_firmware

# flash + provision NVS (credentials from the environment, never in the tree)
LMAO_WIFI_SSID=... LMAO_WIFI_PASS=... \
bazel run //robodog_home_client:flash_firmware -- \
    --device-name "main bedroom" --server-host tp4 --port /dev/ttyACM0
```

`flash.sh` refuses to run without `--wifi-ssid/--wifi-pass` (or
`LMAO_WIFI_SSID`/`LMAO_WIFI_PASS`), and requires `--device-name`.

Build-time variables (`build.sh`; nothing is written to the source tree):

| Var | Default | Meaning |
|---|---|---|
| `RH_DEST_HASH_HEX` | unset | Server `lxmf/delivery` hash the board addresses |
| `RH_NODE_IDENTITY_HEX` | unset | Pinned 64-byte private key (per-device identity) |
| `RH_INTERVAL_SECONDS` | `60` | Report + history-refresh cadence |
| `RH_SERVER_HOST` / `RH_SERVER_PORT` | `tp4` / `4246` | Defaults for the NVS server target |
| `RH_DEVICE_NAME` | unset | Default device name (NVS wins at runtime) |

NVS keys (`robodog_home` namespace, written only by `flash.sh`):
`device_name`, `wifi_ssid`, `wifi_pass`, `server_host`, `server_port`.
**WiFi credentials are never compiled in** — a flashed image contains no
SSID/passphrase (verified by scanning the `.bin`).

The device announces its delivery destination with the LXMF display name set
to the device name, and the server learns it in the contact book on its first
report — named from `SensorReport.node_id` (so it is named even if the
announce was missed).  Set/retype it explicitly with
`POST :8081/contacts`:

```sh
curl -X POST http://tp4:8081/contacts \
  -d '{"name":"main bedroom","type":"robodog_home","delivery_hash":"983c0411…"}'
```

The server answers an air-history request from the **requester's own** series
when it knows that device's name, so two boards on one mesh each chart their
own history rather than whichever node reported last.

Capture/verify the panel exactly like the calendar firmware:

```sh
python3 robodog_client/capture.py --port /dev/ttyACM0 --reset --n 2 --describe
```

## Configuration

| Flag | Env | Default | Meaning |
|---|---|---|---|
| `--server-host` | `LMAO_SERVER_HOST` | `tp4` | LMAO server LAN hostname (never an IP — tp4's wlan0 is DHCP) |
| `--server-port` | `LMAO_SERVER_PORT` | `4246` | Server's RNS `TCPServerInterface` port (`LMAO_TCP_PORT` on the server) |
| `--dest-hash` | `LMAO_DEST_HASH` | unset | Pin the server's `lxmf/delivery` hash instead of matching its announce display name (`lmao-server`) |
| `--identity-dir` | — | `~/.local/share/lmao_home_client/lxmf` | Client identity (persisted; the delivery hash is stable across runs) |
| `--count` | — | `0` | Samples per series (`0` = the server's full ring, 30) |
| `--since-ms` | — | `0` | Delta watermark (`0` = full fetch) |
| `--timeout` | — | `30` | Seconds to wait for the announce and then for the reply |
| `--interval` | — | `0` | Poll period (`0` = fetch once and exit) |
| `--json` / `--verbose` | — | off | Machine-readable output / INFO logging |

`LMAO_HOME_CLIENT_STATE_PATH` makes the Reticulum state dir persistent
(known destinations survive restarts); unset keeps a throwaway temp dir.

A request is only sent once the Reticulum interface reports `online` — the
`TCPClientInterface` connects on its own thread and a packet handed to
`Transport` before that point is dropped silently (no error, no retry).  The
client waits for the announce and for the interface, so a request sent right
after startup lands.

The interface set comes from one definition,
`RnsConfig(role="tcp-client")` in `lma_core/config_utils.py`: a single
`TCPClientInterface` and nothing else.  There is no RNode/AutoInterface
fallback by design — multicast is unreliable on the home AP, and this client
has no LoRa hardware.

## Allow-list

The server drops LXMF from senders that are not in `ALLOWED_CLIENTS`
(`LMAO_ALLOWED_CLIENTS` in `k8s/lmao-server.yaml`) — a dropped request gets no
reply, and the client reports a timeout.  The client prints its
`lxmf/delivery` hash at startup; add it to the list and redeploy the server:

```sh
bazel run //tools:install_all -- --setup-registry --include-services --skip-rnode
```

## Data availability

The server only answers once some node has reported air readings.  The
RoboDog home board reports every `RH_INTERVAL_SECONDS` (default 60 s), so the
env buffer fills from the board itself; the Cardputer client ships
`SEND_SENSOR = False` (its own temperature is not considered useful
server-side) and does not contribute.

## Tests

```sh
bazel test //tests:test_env_history //tests:test_robodog_home_client \
          //tests:test_server_env_reply \
          //robodog_home_client:rd_chart_test \
          //robodog_home_client:lma_chart_test --test_output=all
```

- `test_env_history` — per-node rings, tenths/percent wire units, count/since
  windows, series selection.
- `test_robodog_home_client` — TCP-only config, request encoding, reply
  decoding/summary, announce matching, and a full request/reply round trip
  against a fake RNS/LXMF.
- `test_server_env_reply` — the real server module routing air-only
  `HistoryRequest`s to `EnvHistory`, the Sprout soil path staying unchanged,
  and sensor reports reaching the buffer.
- `rd_chart_test` — chart geometry (extents, scaling, clamping, flat series,
  newest-sample overflow) compiled as plain C11.
- `lma_chart_test` — the firmware's `HistoryRequest` encoder and `ChartBundle`
  decoder against golden bytes generated from the Python protobuf stubs.
