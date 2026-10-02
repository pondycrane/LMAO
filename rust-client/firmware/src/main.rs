#![no_std]
#![no_main]

//! T1 — no_std boot test on the M5Stack Cardputer (ESP32-S3).
//!
//! Boots esp-hal, logs the chip revision + base MAC (the "logs chip/mac"
//! acceptance of design ticket T1), prints a heartbeat to the serial console
//! (USB-Serial-JTAG), then idles. No radio/display work yet (T3/T4/T6 follow);
//! this only proves the Rust no_std toolchain + esp-hal boots and runs on the
//! Cardputer and that `espflash` can load it.

extern crate alloc;
use alloc::format;
use esp_hal::{efuse::InterfaceMacAddress, main, Config};
use esp_println::println;

// espflash 4.x requires an ESP-IDF app descriptor in the ELF; the canonical
// esp-hal example wires it via this macro.
esp_bootloader_esp_idf::esp_app_desc!();

// ESP32-S3 vectored-interrupt dispatch table is bound to no-op stubs until a
// peripheral actually claims an interrupt (T3+). See interrupt_stubs.rs.
include!("interrupt_stubs.rs");

// esp-hal SPI RadioBus + firmware wiring for the Cardputer SX1262 (RF-leg step 3).
pub mod sx1262_radio;
// RNS link: SX1262 driver under the RadioInterface contract + RNode framing.
pub mod rns_link;
// Link initiator + Resource push to the server's lmao.data destination.
pub mod link_resource;
// Cardputer ST7789 panel driver + RGB565 framebuffer (the chart display).
pub mod display;

// no_std heap. rns-core packet building + the RNode split assembler allocate;
// back them with a static linked-list heap.
#[global_allocator]
static HEAP: linked_list_allocator::LockedHeap = linked_list_allocator::LockedHeap::empty();
/// Nominal Unix epoch base for LXMF message timestamps (the firmware has no
/// RTC; message ordering/freshness is not the point of this client probe).
const START_EPOCH: f64 = 1_788_000_000.0;

/// Static heap backing store. 128 KiB: the RNS identity build + per-beat
/// packet and per-announce Vec allocations churn a linked-list allocator hard;
/// 32 KiB fragmented to exhaustion after ~8 min on the earlier build
/// (allocation failure → halt). The ESP32-S3 has plenty of SRAM for this.
const HEAP_SIZE: usize = 131_072;
static mut HEAP_MEM: [u8; HEAP_SIZE] = [0u8; HEAP_SIZE];

/// Panic handler: log and halt. (esp-hal 1.x does not provide one; we bring
/// our own so a panic produces a visible line on the console instead of a
/// silent hang/reset.)
#[panic_handler]
fn panic(info: &core::panic::PanicInfo) -> ! {
    println!("[t1] panic: {:?}", info);
    loop {}
}

#[main]
fn main() -> ! {
    // RF-leg step 3: boot esp-hal, then bring up the Cardputer SX1262 over SPI.
    let peripherals = esp_hal::init(Config::default());

    // Bring up the no_std heap before anything allocates (raw mut pointer into
    // the static backing store is safe here — single-threaded boot).
    unsafe {
        HEAP.lock().init(core::ptr::addr_of_mut!(HEAP_MEM) as *mut u8, HEAP_SIZE);
    }

    let mac = esp_hal::efuse::interface_mac_address(InterfaceMacAddress::Station);
    let rev = esp_hal::efuse::chip_revision();
    println!("[t1] lmao-firmware-t3 booted");
    println!("[t1] chip revision: {}.{}", rev.major, rev.minor);
    println!("[t1] mac: {}", mac);

    // SX1262 radio over SPI3 (HSPI) — Cardputer pins (see sx1262_radio.rs).
    // The radio/EXT bus is HSPI/SPI3_HOST (per cardputer_pins.h + the ADV
    // reference), leaving SPI2/FSPI free for the ST7789 panel.
    use esp_hal::gpio::{Input, InputConfig, Level, Output, OutputConfig};
    use esp_hal::spi::master::{Config as SpiCfg, Spi};
    use sx126x::Sx1262;

    // NOTE: CS is NOT bound to the SPI peripheral — the SX1262 needs CS held
    // LOW for the whole command stream (opcode + up-to-256-byte payload), but
    // esp-hal's non-DMA blocking transfer would otherwise toggle CS per 64-byte
    // hardware-FIFO chunk (corrupting >64-byte transfers). Drive CS manually.
    let spi = Spi::new(
        peripherals.SPI3,
        SpiCfg::default(),
    )
    .expect("SPI config")
    .with_sck(peripherals.GPIO40)
    .with_mosi(peripherals.GPIO14)
    .with_miso(peripherals.GPIO39);

    let cs = Output::new(peripherals.GPIO5, Level::High, OutputConfig::default());
    let rst = Output::new(peripherals.GPIO3, Level::High, OutputConfig::default());
    let busy = Input::new(peripherals.GPIO6, InputConfig::default());

    let bus = crate::sx1262_radio::EspRadioBus::new(spi, cs, rst, busy);
    let mut radio = Sx1262::new(bus);

    // Fixed leaf RF profile: 868 / BW125 / SF7 / CR4:5 / pre24 / syncword 0x1424.
    // Init order is byte-exact with the proven MicroPython `sx126x.py` bring-up.
    // Extracted so a latched stuck-TX (see below) can hard-reset + reconfigure.
    fn configure<B: sx126x::RadioBus>(radio: &mut Sx1262<B>) -> Result<(), B::Error> {
        radio.reset()?;
        radio.set_dio2_as_rf_switch()?;
        // TCXO is REQUIRED: the no-TCXO experiment showed the XOSC never runs
        // without DIO3 power (SET_STANDBY_XOSC no-op, SET_TX EXEC_FAIL), i.e.
        // the Cap LoRa-1262's clock is a DIO3-powered TCXO.
        radio.set_dio3_as_tcxo(1800, 5000)?;
        radio.set_dio_irq_masks()?;
        radio.clear_irq()?;
        radio.set_packet_type_lora()?;
        radio.set_rf_frequency(868_000_000)?;
        radio.set_sync_word(0x1424)?;
        radio.set_pa_config(14, 0x02)?; // 14 dBm optimal PA, 40 us ramp (reference default)
        radio.set_modulation_params(7, 0x04, 1, 0)?;
        // NOTE: the working MicroPython path never calibrates on this board
        // (calibrate/calibrate_image only run under use_dcdc, which is false).
        radio.antenna_mismatch_workaround()?;
        Ok(())
    }

    match configure(&mut radio) {
        Ok(()) => println!("[t3] sx1262 configured 868/BW125/SF7/CR4:5 pre24 syncword=0x1424"),
        Err(_) => println!("[t3] sx1262 configure FAILED (SPI/hardware)"),
    }

    // SPI command-channel round-trip: we wrote syncword 0x1424 → LSYNCRH (0x740);
    // read it back. A match proves the SPI channel genuinely reaches the radio
    // (config writes otherwise give no acknowledgment), which decides whether
    // IRQ=0 is a TX problem or a silent no-op channel.
    match radio.read_register(sx126x::reg::LSYNCRH) {
        Ok(v) => println!("[t3] reg740(readback LSYNCRH)={:#06x}", v),
        Err(_) => println!("[t3] reg740=ERR(busy/timeout)"),
    }
    match radio.read_register(sx126x::reg::LSYNCRL) {
        Ok(v) => println!("[t3] reg741(readback LSYNCRL)={:#06x}", v),
        Err(_) => println!("[t3] reg741=ERR(busy/timeout)"),
    }
    // calibrate/calibrate_image now run inside the config block above; report
    // post-calibration device errors.
    match radio.get_device_errors() {
        Ok(v) => println!("[t3] dev_errors(after calib)={:#06x}", v),
        Err(_) => println!("[t3] dev_errors=ERR"),
    }

    // DEBUG (temporary): SX1262 FIFO loopback — write a known 168-byte pattern
    // (announce-sized) into the TX FIFO and read it back. Corruption past the
    // 64-byte ESP32-S3 SPI FIFO boundary would show up here.
    {
        let mut pat = [0u8; 168];
        for (i, b) in pat.iter_mut().enumerate() {
            *b = i as u8;
        }
        let _ = radio.prepare_send(&pat);
        let mut rb = [0u8; 168];
        let n = radio.read_buffer(0, &mut rb).unwrap_or(0);
        let first_diff = pat.iter().zip(rb.iter()).position(|(a, b)| a != b);
        println!(
            "[dbg] fifo loopback n={n} match={} first_diff={first_diff:?}",
            rb[..168.min(n)] == pat[..168.min(n)]
        );
    }

    // RNS link: wire the driver under `RadioInterface` + the RNode RF framing
    // (the MicroPython `lora.py` transport path, now in Rust). The radio idles
    // in continuous RX (the leaf's listen path); each beat TXs a valid RNS
    // DATA packet whose payload is arbitrary bytes ("send any data"), then
    // listens and decodes any whole packet that reassembles.
    let mut link = crate::rns_link::RnsLink::new(radio, "LoRa SX1262");

    use leaf_rns::{LinkWindowConfig, LinkWindowScheduler};
    use rns_core::constants::{DESTINATION_SINGLE, HEADER_1, PACKET_TYPE_DATA};
    use rns_core::packet::{PacketFlags, RawPacket};

    // Duty-cycle link windows (the leaf design's core power feature): the radio
    // RX is awake only 200 ms of every 1000 ms; in the 800 ms dormant gap the
    // radio is stood down and any produced frames wait in the resume queue to
    // flush at the next window open.
    // TEMP (issue #197): keep RX awake continuously so a one-shot broadcast
    // LRPROOF isn't lost in a dormant RX gap (the earlier 200ms/800ms duty
    // cycle missed it; sprout heartbeats overwrite the radio FIFO while
    // dormant). Restore duty cycling after the link handshake is proven.
    let mut sched = LinkWindowScheduler::new(LinkWindowConfig::new(1000, 0));
    // The announced-only leaf holds an established link to its gateway across
    // dormant gaps, so keepalive pacing + the resume queue apply.
    sched.mark_link_established(0);

    // Real RNS identity: provision the node identity and derive its announce
    // destination hash (persistent across boots from the fixed seed).
    let (node_dest, node_identity) = crate::rns_link::node_destination_hash();
    let lxmf_del = crate::rns_link::lxmf_delivery_hash(&node_identity);
    let mut lxmf_buf = [0u8; 32];
    for (i, b) in lxmf_del.iter().enumerate() {
        lxmf_buf[i * 2] = HEX[(b >> 4) as usize];
        lxmf_buf[i * 2 + 1] = HEX[(b & 0x0f) as usize];
    }
    println!(
        "[rns] identity pk={:02x?}… lmao.leaf dest={:02x?} lxmf.delivery={}",
        node_identity.get_public_key().map(|k| k[..8].to_vec()).unwrap_or_default(),
        node_dest,
        core::str::from_utf8(&lxmf_buf).unwrap_or("?")
    );
    // DEBUG (temporary): full public key so we can compare the on-device
    // identity bytes to what goes on-air in the announce payload.
    if let Some(pk) = node_identity.get_public_key() {
        let mut hexs = alloc::string::String::new();
        for b in &pk {
            hexs.push_str(&format!("{b:02x}"));
        }
        println!("[dbg] identity pkfull={hexs}");
    }

    // LMAO node_id = hex of the identity hash (what the server keys sensor
    // reports by — matches the stable leaf's identity_hex).
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut node_id_buf = [0u8; 32];
    for (i, b) in node_identity.hash().iter().enumerate() {
        node_id_buf[i * 2] = HEX[(b >> 4) as usize];
        node_id_buf[i * 2 + 1] = HEX[(b & 0x0f) as usize];
    }
    let node_id = core::str::from_utf8(&node_id_buf).expect("ascii hex");

    let mut beat = 0u32;
    let mut now_ms = 0u32;

    // Cardputer → server Link + Resource push (issue #197). All packets ≤254 B.
    let mut link_res = crate::link_resource::LinkResource::new();
    let mut resource_started = false;

    // The chart panel: ST7789V2 240x135 on SPI2/FSPI via the open-source
    // mipidsi driver — SCK=36, MOSI=35, CS=37, DC=34, RST=33, BL=38 (the
    // Cardputer-ADV panel config, same as the working no_std Rust reference).
    // mipidsi's ST7789 model applies the 240×135 window (offset 52,40, Deg90,
    // inverted colours) + reset during init, so no raw CASET/MADCTL hand-rolling.
    use embedded_hal_bus::spi::ExclusiveDevice;
    use mipidsi::interface::SpiInterface;
    use mipidsi::models::ST7789;
    use mipidsi::options::{ColorInversion, Orientation, Rotation};
    use mipidsi::Builder;
    let mut delay = esp_hal::delay::Delay::new();
    let lcd_spi = Spi::new(
        peripherals.SPI2,
        SpiCfg::default().with_frequency(esp_hal::time::Rate::from_mhz(40)),
    )
    .expect("lcd SPI config")
    .with_sck(peripherals.GPIO36)
    .with_mosi(peripherals.GPIO35);
    let dc = Output::new(peripherals.GPIO34, Level::Low, OutputConfig::default());
    let cs = Output::new(peripherals.GPIO37, Level::High, OutputConfig::default());
    let rst = Output::new(peripherals.GPIO33, Level::High, OutputConfig::default());
    let mut bl = Output::new(peripherals.GPIO38, Level::Low, OutputConfig::default());
    let mut spi_buf = [0u8; 4096];
    let lcd_dev = ExclusiveDevice::new(lcd_spi, cs, delay).expect("excl device");
    let di = SpiInterface::new(lcd_dev, dc, &mut spi_buf[..]);
    let mut display = Builder::new(ST7789, di)
        .display_size(135, 240)
        .display_offset(52, 40)
        .invert_colors(ColorInversion::Inverted)
        .orientation(Orientation::new().rotate(Rotation::Deg90))
        .reset_pin(rst)
        .init(&mut delay)
        .expect("st7789 init");
    // Boot render: `lma_chart::draw` with no data paints the panel header +
    // "waiting for samples" — drawn while the backlight is still OFF, then the
    // rail comes up, so the very first thing the user sees is the ready chart
    // (never a garbled GRAM flash). This is the visible confirmation the 240×135
    // panel now drives via mipidsi (RST=33 + FSPI + correct window).
    let boot_c = lma_chart::ChartRecord {
        node: alloc::string::String::new(),
        dry: -1,
        wet: -1,
        temp: alloc::vec::Vec::new(),
        humidity: alloc::vec::Vec::new(),
        samples: alloc::vec::Vec::new(),
        water_mask: 0,
    };
    let mut boot_frame = crate::display::Frame;
    lma_chart::draw(&mut boot_frame, &boot_c);
    let _ = crate::display::blit(&mut display);
    bl.set_high();
    println!("[t6] st7789 init (240x135) — display ready for the chart");

    loop {
        beat = beat.wrapping_add(1);
        esp_hal::delay::Delay::new().delay_millis(100); // real 100 ms tick
        now_ms = now_ms.wrapping_add(100);

        // Produce a valid RNS DATA packet of arbitrary bytes every 5th beat.
        if false { // TEMP (issue #197): 31B arbitrary-data heartbeat test ping disabled to free the half-duplex RF channel
            let payload: [u8; 12] = [
                b'L', b'M', b'A', b'O', (beat >> 24) as u8, (beat >> 16) as u8,
                (beat >> 8) as u8, beat as u8, 0x21, 0x7d, 0x00, 0x01,
            ];
            let flags = PacketFlags {
                header_type: HEADER_1,
                context_flag: 0,
                transport_type: 0,
                destination_type: DESTINATION_SINGLE,
                packet_type: PACKET_TYPE_DATA,
            };
            if let Ok(p) = RawPacket::pack(flags, 0, &node_dest, None, 0, &payload) {
                let n = p.raw.len();
                sched.queue_tx(p.raw);
                println!(
                    "[rns] beat #{beat} queued {n} B (queue={})",
                    sched.queued_len()
                );
            }
        }

        let rng = esp_hal::rng::Rng::new();
        if sched.is_rx_awake(now_ms) {
            // Link window open: listen + flush queued frames.
            let now_f = START_EPOCH + f64::from(now_ms) / 1000.0;
            let mut esp_rng = crate::rns_link::EspRng(rng);

            // ── Cardputer → server Link + Resource driver (issue #197) ───────
            // Every ~10 s, ensure a Link handshake with `lmao.data` is in
            // flight; once established, push one big message as a Resource.
            let mut to_tx: alloc::vec::Vec<alloc::vec::Vec<u8>> = alloc::vec::Vec::new();
            if beat % 100 == 10 {
                // (Re)initiate the Link if it isn't already handshaking.
                if link_res.phase == crate::link_resource::LinkPhase::Idle
                    || link_res.phase == crate::link_resource::LinkPhase::Failed
                {
                    if let Some(pkt) = link_res.begin_link(now_f, &mut esp_rng) {
                        to_tx.push(pkt);
                        println!(
                            "[link] beat #{beat} LINKREQUEST to lmao.data link_id={:02x?}",
                            link_res.link_id
                        );
                    }
                }
            }
            // Route inbound link handshake / resource packets + collect replies.
            {
                let lr = &mut link_res;
                link.pump_rx(now_ms, &node_identity, &mut |pkt: &rns_core::packet::RawPacket| {
                    let replies = lr.pump_inbound(pkt, now_f, &mut esp_rng);
                    to_tx.extend(replies);
                }, &mut |c: &lma_chart::ChartRecord| {
                    // The server's reply carried a fresh DATA line — paint the
                    // Sprout chart into the framebuffer and blit the panel
                    // (the cardputer display, now in Rust + mipidsi).
                    let mut frame = crate::display::Frame;
                    let points = lma_chart::draw(&mut frame, c);
                    let _ = crate::display::blit(&mut display);
                    println!("[t6] chart drawn: {points} trace segments");
                });
            }
            // Once the link is up, push the big message as a Resource (once).
            if !resource_started && link_res.established() {
                let payload: alloc::vec::Vec<u8> = (0..500u16).map(|i| (i % 251) as u8).collect();
                let pkts = link_res.start_resource(&payload, now_f);
                to_tx.extend(pkts);
                resource_started = true;
                println!(
                    "[link] beat #{beat} link up ({}), pushing {}-B resource ({}-B parts)",
                    link_res.phase == crate::link_resource::LinkPhase::Complete,
                    payload.len(),
                    crate::link_resource::RESOURCE_SDU
                );
            }
            // Re-advertise on a long backoff (every ~5 s), NOT every tick:
            // a 212 B LoRa frame has ~300 ms of airtime, so advertising every
            // 100 ms floods the half-duplex channel and the server never
            // cleanly receives an advertisement.
            if resource_started && beat % 50 == 0 {
                let adv = link_res.poll_resource(now_f);
                to_tx.extend(adv);
            }
            match link_res.phase {
                crate::link_resource::LinkPhase::Complete => println!(
                    "[link] beat #{beat} RESOURCE COMPLETE ({}/{})",
                    link_res.sent_parts, link_res.total_parts
                ),
                crate::link_resource::LinkPhase::Failed => println!("[link] beat #{beat} FAILED"),
                _ => {}
            }
            for p in to_tx {
                match link.send(&p, now_ms) {
                    Ok(()) => println!("[link] beat #{beat} TX {} B", p.len()),
                    Err(()) => println!("[link] beat #{beat} TX FAILED"),
                }
            }
            // Periodic signed RNS announces. Alternate every ~10 s between the
            // `lmao.leaf` presence destination and the `lxmf.delivery`
            // destination (so the server can address replies back to this
            // leaf).
            if beat % 100 == 0 {
                // µReticulum reference random_hash: urandom(5) ‖ unix_time(5, BE)
                // so the server's path-table timebase is real (not garbage).
                let mut random5 = [0u8; 5];
                let a = rng.random();
                random5[..4].copy_from_slice(&a.to_le_bytes());
                random5[4] = (rng.random() & 0xff) as u8;
                let unix_time = (START_EPOCH + f64::from(now_ms) / 1000.0) as u64;
                let rh = crate::rns_link::random_hash(random5, unix_time);
                let (app, aspect, label) = if beat % 200 == 0 {
                    ("lmao", "leaf", "lmao.leaf")
                } else {
                    ("lxmf", "delivery", "lxmf.delivery")
                };
                let a = crate::rns_link::announce_for(&node_identity, app, aspect, rh);
                match a {
                    Some(pkt) => {
                        let n = pkt.len();
                        // DEBUG (temporary): dump the exact bytes RF sends, so
                        // we can compare in-firmware vs on-air announce fields.
                        let mut hexs = alloc::string::String::new();
                        for b in &pkt {
                            hexs.push_str(&format!("{b:02x}"));
                        }
                        println!("[dbg] beat #{beat} {label} n={n} hex={hexs}");
                        match link.send(&pkt, now_ms) {
                            Ok(()) => println!(
                                "[rns] beat #{beat} ANNOUNCE {n} B ({label} dest) src={node_dest:02x?}"
                            ),
                            Err(()) => {
                                println!("[rns] beat #{beat} ANNOUNCE TX FAILED — recovering radio");
                                let _ = configure(&mut link.radio_mut());
                            }
                        }
                    }
                    None => println!("[rns] beat #{beat} ANNOUNCE build failed (skip)"),
                }
            }
            // Periodic text message to the server (~30 s) — the production
            // LMAO POC envelope (LMAOEnvelope{text}, p:Envelope title) exactly
            // as the stable Cardputer leaf's "Hello from Cardputer" send.
            if false { // TEMP (issue #197): 291B LXMF "Hello" test message disabled to free channel + avoid ratchet-decrypt spam
                let hello = format!("Hello from Rust Cardputer leaf (seq {beat})");
                let ts_ms = (START_EPOCH * 1000.0) as u64 + beat as u64 * 100;
                let envelope =
                    crate::rns_link::build_text_envelope(node_id, &hello, ts_ms);
                let sent = crate::rns_link::build_message_to_server(
                    &node_identity,
                    &mut crate::rns_link::EspRng(rng),
                    &envelope,
                    START_EPOCH + beat as f64 / 10.0,
                )
                .map(|pkt| (pkt.len(), link.send(&pkt, now_ms)));
                match sent {
                    Some((n, Ok(()))) => println!(
                        "[rns] beat #{beat} Hello msg {n} B txt=`{hello}` sid={node_id} link_tx={}",
                        link.interface().stats.tx_frames
                    ),
                    Some((_, Err(()))) => {
                        println!("[rns] beat #{beat} LXMF send FAILED — recovering radio");
                        let _ = configure(&mut link.radio_mut());
                    }
                    None => println!("[rns] beat #{beat} LXMF build/encrypt failed (skip)"),
                }
            }
            for f in sched.drain_tx(now_ms) {
                match link.send(&f, now_ms) {
                    Ok(()) => println!(
                        "[rns] beat #{beat} window TX {} B link_tx={}",
                        f.len(),
                        link.interface().stats.tx_frames
                    ),
                    Err(()) => {
                        println!("[rns] beat #{beat} window TX FAILED — recovering radio");
                        let _ = configure(&mut link.radio_mut());
                    }
                }
            }
            // Keepalive pacing (no link traffic for a full period while awake).
            if sched.keepalive_due(now_ms) {
                println!("[rns] beat #{beat} keepalive due");
                sched.note_link_activity(now_ms);
            }
        } else {
            // Dormant gap: radio stood down (power). Queued frames flush at the
            // next window open (resume-across-gap).
            let _ = link.enter_standby();
            if beat % 50 == 0 && sched.queued_len() > 0 {
                println!(
                    "[rns] beat #{beat} dormant holding {} queued frame(s)",
                    sched.queued_len()
                );
            }
        }
    }
}
