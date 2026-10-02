//! Sprout sender — Atom Lite (ESP32) no_std edge sender for the Rust LMAO
//! stack: boots esp-hal, reads the soil/air sensors, and sends LXMF
//! SensorReports over the RAK3172 DTU (P2P LoRa) to the Rust LMAO server.
//!
//! This is the "rust on sprout" leg: data that used to ride the native-client
//! (ESP-IDF C++) now comes from this Rust sender on the same DTU radio, so the
//! server's `rf.rs` → `contacts.rs` → `sprout.rs` ingestion starts seeing
//! Sprout sensor data from a Rust node.
//!
//! Pins (hardware-verification.md / native-client headers):
//!   UART2  TX=22, RX=19 → RAK3172 DTU (P2P LoRa, AT 115200)
//!   ADC1_CH4 = GPIO32 (Watering Unit moisture probe, 12-bit/11 dB)
//!   I2C0   SCL=21, SDA=25 → ENV III SHT30 @0x44
//!   GPIO26 → pump enable (held LOW; THIS BUILD NEVER ARMS ACTUATION)

#![no_std]
#![no_main]

extern crate alloc;

use esp_hal::analog::adc::{Adc, AdcConfig, Attenuation};
use esp_hal::delay::Delay;
use esp_hal::gpio::{Input, InputConfig, Level, Output, OutputConfig, Pull};
use esp_hal::main;
use esp_hal::rmt::{PulseCode, Rmt, TxChannelConfig, TxChannelCreator};
use esp_hal::uart::{self, UartTx};
use esp_hal::Config;
use esp_println::println;

esp_bootloader_esp_idf::esp_app_desc!();

/// The Sprout's RNS identity + announce/LXMF message builder (this firmware).
mod link_resource;
mod sender;

#[global_allocator]
static HEAP: linked_list_allocator::LockedHeap = linked_list_allocator::LockedHeap::empty();
/// Static heap: RNS/LXMF packet building + the split-assembler allocate. The
/// Atom Lite has 520 KiB SRAM; 96 KiB is ample for the sender.
const HEAP_SIZE: usize = 98_304;
static mut HEAP_MEM: [u8; HEAP_SIZE] = [0u8; HEAP_SIZE];

/// Panic handler: log and halt (esp-hal 1.x brings none — the console line
/// beats a silent hang/reset).
#[panic_handler]
fn panic(info: &core::panic::PanicInfo) -> ! {
    println!("[sprout] panic: {:?}", info);
    loop {}
}

// ── hardware/pin + timing constants (native-client parity) ────────────────
const RAK_TX: u32 = 22; // UART2 TX → RAK3172 RX
const RAK_RX: u32 = 19; // UART2 RX ← RAK3172 TX
const DTU_BAUD: u32 = 115_200;

const SHT30_ADDR: u8 = 0x44;

/// Default plant band for sensors 10/11 (the native-client "kale" profile:
/// target 45 ± 8 → dry 37 / wet 53). The chart's band lines come from here.
const DRY_PCT: f32 = 37.0;
const WET_PCT: f32 = 53.0;

/// 5-min sensor cadence (matches the native-client SEND_INTERVAL_MS; first
/// report goes out immediately after boot).
const SEND_INTERVAL_MS: u32 = 300_000;
/// Asset announce cadence (matches the native-client ANNOUNCE_INTERVAL_MS).
const ANNOUNCE_INTERVAL_MS: u32 = 30_000;
/// Loop tick.
const TICK_MS: u32 = 100;
/// Cooldown before re-attempting a LINKREQUEST while the link is Idle/Failed.
const LINK_RETRY_MS: u32 = 5_000;
/// Backoff between Resource re-advertisements while a transfer is in flight
/// (a ~212 B LoRa frame has ~300 ms airtime — advertising every tick would
/// flood the half-duplex channel).
const RES_POLL_MS: u32 = 5_000;
/// Nominal Unix epoch base (no RTC on the Atom; ordering is not the point).
const START_EPOCH: f64 = 1_788_000_000.0;

/// Calibration span for the Watering Unit probe (12-bit/11 dB verified).
const MOISTURE_DRY_COUNT: f32 = 2068.0;
const MOISTURE_WET_COUNT: f32 = 1580.0;

/// Write one AT line (appending CRLF) + flush.
fn at_write(tx: &mut UartTx<'_, esp_hal::Blocking>, line: &str) {
    let _ = tx.write(line.as_bytes());
    let _ = tx.write(b"\r\n");
    let _ = tx.flush();
}

/// Drain/read whatever the modem replies during `ms` (so config/PSEND
/// responses don't leak into the RX line parser).
fn at_drain(rx2: &mut esp_hal::uart::UartRx<'_, esp_hal::Blocking>, delay: &mut Delay, ms: u32) {
    let mut scratch = [0u8; 128];
    let mut budget = ms;
    while budget > 0 {
        let step = core::cmp::min(budget, 10);
        budget -= step;
        let _ = rx2.read_buffered(&mut scratch);
        delay.delay_millis(step);
    }
}

/// Drive the SK6812 front LED (G27, single GRB pixel, 10 MHz RMT ticks) to a
/// colour. Red = actuation armed, black = dry-run (matches the native-client's
/// `button_led`). Consumes + returns the RMT channel (transmit() consumes it).
fn led_set(
    ch: esp_hal::rmt::Channel<'_, esp_hal::Blocking, esp_hal::rmt::Tx>,
    r: u8,
    g: u8,
    b: u8,
) -> esp_hal::rmt::Channel<'_, esp_hal::Blocking, esp_hal::rmt::Tx> {
    // SK6812 at 10 MHz (RMT 80 MHz / clk_div 8 = 0.1 us/tick): 1-bit = 0.9us H
    // /0.3us L, 0-bit = 0.3us H /0.9us L (GRB byte order, MSB-first); a ≥80us
    // low reset terminator latches the frame.
    let mut data = [PulseCode::new(Level::High, 1, Level::Low, 1); 26];
    for i in 0..24 {
        let byte = match i / 8 {
            0 => g,
            1 => r,
            _ => b,
        };
        let bit = (byte >> (7 - (i % 8))) & 1;
        data[i] = if bit == 1 {
            PulseCode::new(Level::High, 9, Level::Low, 3)
        } else {
            PulseCode::new(Level::High, 3, Level::Low, 9)
        };
    }
    data[24] = PulseCode::new(Level::Low, 1000, Level::Low, 1000); // 200 us reset
    data[25] = PulseCode::end_marker();
    let tx = ch.transmit(&data).expect("led transmit");
    tx.wait().expect("led done")
}


fn tx_packet(
    tx: &mut UartTx<'_, esp_hal::Blocking>,
    rx2: &mut esp_hal::uart::UartRx<'_, esp_hal::Blocking>,
    delay: &mut Delay,
    packet: &[u8],
    seq: u8,
) {
    for (line, wait_ms) in lma_dtu::at_dtu::tx_lines(packet, seq) {
        println!("[dtu] -> {line}");
        at_write(tx, &line);
        at_drain(rx2, delay, wait_ms);
    }
}

#[main]
fn main() -> ! {
    let peripherals = esp_hal::init(Config::default());
    unsafe {
        HEAP.lock().init(core::ptr::addr_of_mut!(HEAP_MEM) as *mut u8, HEAP_SIZE);
    }

    let mut delay = Delay::new();

    // ── SAFETY: pump enable LOW (OFF) FIRST — never leave the control line
    // floating. This build boots dry-run; the G39 button arms actuation (the
    // control engine — not part of the send leg — is what would energise it).
    let mut pump = Output::new(peripherals.GPIO26, Level::Low, OutputConfig::default());
    pump.set_low();
    println!("[sprout] rust sender boot — pump drive LOW (dry-run)");

    // ── front controls: G39 button (external pull-up ⇒ LOW when pressed) + the
    // single SK6812 red LED on G27. Long-press (2 s) arms actuation; quick tap
    // disarms; the armed state is NOT persisted (reset → dry-run), matching the
    // native-client `button_led`.
    let mut btn = Input::new(peripherals.GPIO39, InputConfig::default().with_pull(Pull::Up));
    let rmt = Rmt::new(peripherals.RMT, esp_hal::time::Rate::from_mhz(80)).expect("rmt");
    let mut led_channel = rmt
        .channel0
        .configure_tx(&TxChannelConfig::default().with_clk_divider(8))
        .expect("rmt tx channel")
        .with_pin(peripherals.GPIO27);
    led_channel = led_set(led_channel, 0, 0, 0); // LED off (dry-run)
    println!("[sprout] front controls: G39 button + SK6812 LED ready (dry-run)");

    // ── identity ────────────────────────────────────────────────────────────
    let keys = sender::provision();
    println!("[sprout] lxmf/delivery hash: {}", hex16(&keys.delivery_hash));
    println!("[sprout] node_id: {}", keys.node_id);

    // ── RAK3172 DTU over UART2 (P2P LoRa) ──────────────────────────────────
    let uart_full = uart::Uart::new(
        peripherals.UART2,
        uart::Config::default(),
    )
    .expect("uart2 config")
    .with_tx(peripherals.GPIO22)
    .with_rx(peripherals.GPIO19);
    let (mut rx2, mut tx) = uart_full.split();

    // Configure the RAK3172 P2P mesh parameters (AT+PRECV=0 first, the set
    // cmds, then continuous RX on).
    for line in lma_dtu::at_dtu::boot_lines() {
        println!("[dtu] cfg: {line}");
        at_write(&mut tx, &line);
        at_drain(&mut rx2, &mut delay, 120);
    }
    println!("[dtu] RAK3172 P2P on-mesh, listening");

    // ── sensors: moisture (ADC1_CH4) + ENV III SHT30 (I2C0) ────────────────
    let mut adc_cfg = AdcConfig::new();
    let mut moisture_pin = adc_cfg.enable_pin(peripherals.GPIO32, Attenuation::_11dB);
    let mut adc = Adc::new(peripherals.ADC1, adc_cfg);
    let mut i2c = esp_hal::i2c::master::I2c::new(
        peripherals.I2C0,
        esp_hal::i2c::master::Config::default(),
    )
    .expect("i2c0")
    .with_sda(peripherals.GPIO25)
    .with_scl(peripherals.GPIO21);

    let mut hw_rng = esp_hal::rng::Rng::new();
    let mut esp_rng = sender::EspRng(esp_hal::rng::Rng::new());
    let mut at_rx = lma_dtu::at_dtu::AtRx::new();

    // ── loop state ──────────────────────────────────────────────────────────
    let mut now_ms: u32 = 0;
    let mut last_send_ms: u32 = 0; // 0 → first sample goes out immediately
    let mut last_announce_ms: u32 = 0;
    // Link+Resource state (server-facing): the SensorReport envelope is pushed
    // as an RNS Resource to the server's lmao.data link destination (the
    // server's on_resource_received folds it into the Sprout chart).
    let mut link_res = crate::link_resource::LinkResource::new();
    let mut pending_payload: Option<alloc::vec::Vec<u8>> = None;
    let mut resource_started = false;
    let mut last_res_poll_ms: u32 = 0;
    let mut last_link_attempt_ms: u32 = 0;
    let mut bf = [0u8; 512]; // UART RX scratch
    let mut tick = 0u32;
    // Front-control (arming) state.
    let mut actuation_armed = false;
    let mut btn_last = true; // G39 released (external pull-up ⇒ HIGH)
    let mut press_start_ms: Option<u32> = None;

    loop {
        delay.delay_millis(TICK_MS);
        now_ms = now_ms.wrapping_add(TICK_MS);
        tick = tick.wrapping_add(1);

        // Poll DTU RX: reassemble server frames (ACK/DATA addressed back to
        // this node). The send-only leg logs them; retransmit/link handling is
        // a follow-up once the node is on the whitelist.
        if let Ok(n) = rx2.read_buffered(&mut bf) {
            if n > 0 {
                for pkt in at_rx.feed(&bf[..n], now_ms) {
                    println!("[dtu] RX complete packet {}B", pkt.len());
                    // Feed the Link+Resource inbound (LRPROOF / part-requests /
                    // proof) so the SensorReport Resource progresses.
                    if let Ok(rp) = rns_core::packet::RawPacket::unpack(&pkt) {
                        let now_f = START_EPOCH + now_ms as f64 / 1000.0;
                        let replies = link_res.pump_inbound(&rp, now_f, &mut esp_rng);
                        for r in replies {
                            let seq = (hw_rng.random() & 0xf0) as u8;
                            tx_packet(&mut tx, &mut rx2, &mut delay, &r, seq);
                        }
                    }
                }
            }
        }

        // Front control: long-press (≥2 s) arms actuation → red LED; quick tap
        // disarms. Per-session only (never persisted ⇒ reset drops to dry-run).
        let pressed = btn.is_low();
        match (pressed, btn_last) {
            (true, false) => press_start_ms = Some(now_ms),
            (false, true) => {
                if let Some(start) = press_start_ms {
                    let dur = now_ms.wrapping_sub(start);
                    if dur >= 2000 && !actuation_armed {
                        actuation_armed = true;
                        led_channel = led_set(led_channel, 255, 0, 0);
                        println!("[sprout] actuation ARMED (button long-press, red LED on)");
                    } else if dur <= 1000 && actuation_armed {
                        actuation_armed = false;
                        led_channel = led_set(led_channel, 0, 0, 0);
                        println!("[sprout] actuation dry-run (button tap, LED off)");
                    }
                }
                press_start_ms = None;
            }
            _ => {}
        }
        btn_last = pressed;

        // Periodic signed announces (lmao.sprout + lxmf.delivery).
        if now_ms - last_announce_ms >= ANNOUNCE_INTERVAL_MS || last_announce_ms == 0 {
            last_announce_ms = now_ms;
            let unix = (START_EPOCH + now_ms as f64 / 1000.0) as u64;
            let rand_seq = (hw_rng.random() & 0xff) as u8;
            let mut random5 = [0u8; 5];
            for b in random5.iter_mut() {
                *b = (hw_rng.random() & 0xff) as u8;
            }
            let rh = sender::random_hash(random5, unix);
            if let Some(f) = sender::announce_for(&keys.identity, "lmao", "sprout", rh) {
                println!("[sprout] announce lmao/sprout ({}B)", f.len());
                tx_packet(&mut tx, &mut rx2, &mut delay, &f, rand_seq);
            }
            if let Some(f) = sender::announce_for(&keys.identity, "lxmf", "delivery", rh) {
                println!("[sprout] announce lxmf/delivery ({}B)", f.len());
                tx_packet(&mut tx, &mut rx2, &mut delay, &f, rand_seq);
            }
        }

        // ── Link + Resource pump: push the queued SensorReport protobuf to the
        //    server's `lmao.data` link destination as an RNS Resource (the
        //    server's `on_resource_received` folds it into the Sprout chart).
        let now_f = START_EPOCH + now_ms as f64 / 1000.0;
        if pending_payload.is_some() {
            // A lost LRPROOF strands the half-duplex link in `Linking`, so a
            // timed-out Linking phase is reset to Idle to issue a fresh
            // LINKREQUEST (while still cooldown-gated).
            if link_res.phase == crate::link_resource::LinkPhase::Linking
                && now_ms.wrapping_sub(last_link_attempt_ms) >= LINK_RETRY_MS
            {
                link_res.reset();
                println!("[link] LRPROOF timeout — resetting to re-link");
            }
            // (Re)initiate the LINKREQUEST if not already handshaking.
            if (link_res.phase == crate::link_resource::LinkPhase::Idle
                || link_res.phase == crate::link_resource::LinkPhase::Failed)
                && now_ms.wrapping_sub(last_link_attempt_ms) >= LINK_RETRY_MS
            {
                last_link_attempt_ms = now_ms;
                if let Some(pkt) = link_res.begin_link(now_f, &mut esp_rng) {
                    println!("[link] LINKREQUEST to lmao.data (link_id={:02x?})", link_res.link_id);
                    let seq = (hw_rng.random() & 0xf0) as u8;
                    tx_packet(&mut tx, &mut rx2, &mut delay, &pkt, seq);
                }
            }
            // Once the link is up, push the SensorReport envelope as a Resource.
            if !resource_started && link_res.established() {
                if let Some(env) = pending_payload.as_ref() {
                    let pkts = link_res.start_resource(env, now_f);
                    let seq = (hw_rng.random() & 0xf0) as u8;
                    for p in pkts {
                        tx_packet(&mut tx, &mut rx2, &mut delay, &p, seq);
                    }
                    resource_started = true;
                    println!("[link] link up — pushing SensorReport Resource ({}B)", env.len());
                }
            }
        }
        // Re-advertise on a backoff while a transfer is in flight.
        if resource_started && now_ms.wrapping_sub(last_res_poll_ms) >= RES_POLL_MS {
            last_res_poll_ms = now_ms;
            let adv = link_res.poll_resource(now_f);
            let seq = (hw_rng.random() & 0xf0) as u8;
            for p in adv {
                tx_packet(&mut tx, &mut rx2, &mut delay, &p, seq);
            }
            match link_res.phase {
                crate::link_resource::LinkPhase::Complete => {
                    println!(
                        "[link] RESOURCE COMPLETE ({}/{})",
                        link_res.sent_parts, link_res.total_parts
                    );
                    resource_started = false;
                    pending_payload = None;
                }
                crate::link_resource::LinkPhase::Failed => {
                    println!("[link] FAILED — retrying next cooldown");
                    resource_started = false;
                    pending_payload = None;
                }
                _ => {}
            }
        }

        // 5-min sensor bundle → LXMF SensorReport → server.
        if now_ms - last_send_ms >= SEND_INTERVAL_MS || last_send_ms == 0 {
            last_send_ms = now_ms;

            // Moisture % (2-point calibration; lower counts == wetter).
            // Bounded poll: on ESP32 rev1 the one-shot SAR can wedge in
            // `WouldBlock` (never reports done), which `nb::block!` would spin
            // on forever — so give up after a bounded number of polls and send
            // the report without moisture (air + band still ride along).
            let mut raw = 0u16;
            for _ in 0..100_000u32 {
                match adc.read_oneshot(&mut moisture_pin) {
                    Ok(v) => {
                        raw = v;
                        break;
                    }
                    Err(nb::Error::WouldBlock) => {}
                    Err(_) => break,
                }
            }
            let mut moisture: Option<f32> = None;
            if raw > 0 {
                let span = MOISTURE_DRY_COUNT - MOISTURE_WET_COUNT;
                let mut pct = (MOISTURE_DRY_COUNT - raw as f32) / span * 100.0;
                if pct < 0.0 {
                    pct = 0.0;
                }
                if pct > 100.0 {
                    pct = 100.0;
                }
                moisture = Some(pct);
                println!("[sprout] moisture raw={raw} -> {pct:.1}%");
            } else {
                println!("[sprout] ADC unavailable (moisture skipped)");
            }

            // Air T/H from the ENV III SHT30 (fail-safe: the report continues
            // without air when the bus is missing — same as the native-client).
            let mut air: Option<(f32, f32)> = None;
            {
                let mut buf = [0u8; 6];
                if i2c.write(SHT30_ADDR, &[0x2c, 0x06]).is_ok() {
                    delay.delay_millis(60); // SHT30 high-rep conversion time
                    if i2c.read(SHT30_ADDR, &mut buf).is_ok() {
                        let raw_t = ((buf[0] as u16) << 8) | buf[1] as u16;
                        let raw_h = ((buf[3] as u16) << 8) | buf[4] as u16;
                        air = Some((
                            -45.0f32 + 175.0 * (raw_t as f32) / 65535.0,
                            100.0f32 * (raw_h as f32) / 65535.0,
                        ));
                    }
                }
            }

            // Build the SensorReport envelope + LXMF message.
            let timestamp_ms = now_ms as u64;
            let mut readings: alloc::vec::Vec<alloc::vec::Vec<u8>> = alloc::vec::Vec::new();
            if let Some((temp, hum)) = air {
                println!("[sprout] air {temp:.1}C / {hum:.0}%");
                readings.push(lma_dtu::envelope::encode_reading(3, temp, "C", timestamp_ms));
                readings.push(lma_dtu::envelope::encode_reading(2, hum, "%", timestamp_ms));
            }
            if let Some(m) = moisture {
                readings.push(lma_dtu::envelope::encode_reading(4, m, "%", timestamp_ms));
            }
            readings.push(lma_dtu::envelope::encode_reading(10, DRY_PCT, "%", timestamp_ms));
            readings.push(lma_dtu::envelope::encode_reading(11, WET_PCT, "%", timestamp_ms));
            if actuation_armed {
                // ML watering-event tags once the actuator is armed. The pump
                // itself is driven by the control engine (not in this send
                // leg), so duration/active are 0 here.
                readings.push(lma_dtu::envelope::encode_reading(6, 0.0, "s", timestamp_ms));
                readings.push(lma_dtu::envelope::encode_reading(7, 0.0, "bool", timestamp_ms));
            }

            if !readings.is_empty() {
                let seq = (now_ms % 100_000) as u32;
                let report = lma_dtu::envelope::encode_sensor_report(&keys.node_id, seq, 0.0, &readings);
                let env = lma_dtu::envelope::encode_envelope(&report);
                println!(
                    "[sprout] SensorReport queued (env {}B) — will push as link Resource",
                    env.len()
                );
                // The server's RF leg folds LMAOEnvelope{Sensor} Resources into
                // the chart, so the SensorReport now rides as a Resource to the
                // server's `lmao.data` link destination (Link+Resource pump above),
                // replacing the old opportunistic single-packet send the server's
                // `on_resource_received` path never folded.
                pending_payload = Some(env);
            }
        }

        let _ = tick;
    }
}

fn hex16(b: &[u8]) -> alloc::string::String {
    let mut s = alloc::string::String::with_capacity(b.len() * 2);
    for &x in b {
        s.push(char::from_digit((x >> 4) as u32, 16).unwrap_or('0'));
        s.push(char::from_digit((x & 0x0f) as u32, 16).unwrap_or('0'));
    }
    s
}
