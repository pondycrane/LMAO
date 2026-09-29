# RF-leg step 2 — the SX1262 driver core (no_std, bus-generic): evidence

The hardware path to on-device E2E. Step 1 (PR #185) cloned the leaf radio
`Interface` contract; **step 2 is the SX1262 driver core** the interface drives.

## `crates/sx126x` (no_std + alloc, bus-generic)
Faithful port of the **repo's MicroPython `cardputer_client/lib/lora/sx126x.py`**
— the exact driver that already RF-works on this Cardputer — so the fixed leaf
profile writes the same bytes the reference writes on-air:
- `cmd`/`reg`/`pkt`/`irq` opcode, register & flag maps (`0x86` freq, `0x8B`
  modulation, `0x8C` packet params, `0x8A` packet type (LoRa=0x01), `0x740/0x741`
  sync-word regs, RX/TX/CRC/timeout IRQ bits).
- The chip-register calculations verbatim:
  - **frequency**: `rffreq = (hz << 25) // 32e6` (868 MHz → `0x36400000`).
  - **syncword**: SX127x→SX126x nibble transform `0x0404+((sw&0xF)<<4)+((sw&0xF0)<<8)`
    for byte syncwords; the leaf's `0x1424` is already the 16-bit form (written verbatim).
  - **modulation**: `(sf, bw_code, cr-4, ldro)`; BW125→0x04, SF7, CR4:5→1.
- `RadioBus` trait (SPI exchange + RESET + BUSY) — bus-generic so the core is
  **host-testable** (a recording mock) and the esp-hal SPI binding is a thin impl.
- `Sx1262<B>`: reset/standby, packet-type, RF freq + syncword + modulation,
  DIO2 RF-switch + DIO3 TCXO (board config), PA/TX params, `prepare_send`/
  `start_tx`, `start_rx`, `get_irq_status`, `get_rx_buffer_status`, `get_packet_status`
  (**RSSI** — the sensor_id 9 `SensorReport` source), `read_buffer`.

## Host verification (via `//rust-client:test_sx126x`, 7 tests)
Golden byte locks against the reference calculations:
`rf_frequency_868mhz_golden`, `sync_word_0x1424_written_verbatim_to_reg` (+ byte
transform), `modulation_sf7_bw125_cr45_golden`, `fixed_profile_config_sequence`,
`irq_receive_success_judgement`, `rssi_and_packet_status_decode`,
`tx_path_loads_the_t3_framed_packet` (a T3-framed control frame into the TX FIFO,
then `SET_TX`).

## Grounded
Cardputer SX1262 pins (repo `cardputer_pins.h` / `lora_boards.py`): SCK40/MOSI14/
MISO39/CS5/BUSY6/DIO1=4/RST3 (HSPI); RF **868/BW125/SF7/CR4:5/pre24/syncword
0x1424**; DIO2=RF-SW; DIO3-TCXO.

## On-device (hardware, next)
The esp-hal SPI `RadioBus` impl + wiring `Sx1262` under the `RadioInterface`
into the firmware main loop, then a real LoRa TX/RX against the production
RNode. The encodings are locked here; RF behaviour is the flash-verified leg.
