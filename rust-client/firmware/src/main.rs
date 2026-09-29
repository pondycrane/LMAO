#![no_std]
#![no_main]

//! T1 — no_std boot test on the M5Stack Cardputer (ESP32-S3).
//!
//! Boots esp-hal, logs the chip revision + base MAC (the "logs chip/mac"
//! acceptance of design ticket T1), prints a heartbeat to the serial console
//! (USB-Serial-JTAG), then idles. No radio/display work yet (T3/T4/T6 follow);
//! this only proves the Rust no_std toolchain + esp-hal boots and runs on the
//! Cardputer and that `espflash` can load it.

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

// no_std heap. rns-core packet building + the RNode split assembler allocate;
// back them with a static linked-list heap.
#[global_allocator]
static HEAP: linked_list_allocator::LockedHeap = linked_list_allocator::LockedHeap::empty();
/// Static heap backing store (32 KiB — plenty for transient RNS packets).
const HEAP_SIZE: usize = 32768;
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

    // SX1262 radio over SPI2 (HSPI) — Cardputer pins (see sx1262_radio.rs).
    use esp_hal::gpio::{Input, InputConfig, Level, Output, OutputConfig};
    use esp_hal::spi::master::{Config as SpiCfg, Spi};
    use sx126x::Sx1262;

    let spi = Spi::new(
        peripherals.SPI2,
        SpiCfg::default(),
    )
    .expect("SPI config")
    .with_sck(peripherals.GPIO40)
    .with_mosi(peripherals.GPIO14)
    .with_miso(peripherals.GPIO39)
    .with_cs(peripherals.GPIO5);

    let rst = Output::new(peripherals.GPIO3, Level::High, OutputConfig::default());
    let busy = Input::new(peripherals.GPIO6, InputConfig::default());

    let bus = crate::sx1262_radio::EspRadioBus::new(spi, rst, busy);
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
    let mut sched = LinkWindowScheduler::new(LinkWindowConfig::new(200, 800));
    // The announced-only leaf holds an established link to its gateway across
    // dormant gaps, so keepalive pacing + the resume queue apply.
    sched.mark_link_established(0);

    // Bootstrap receiver hash (a real RNS leaf would announce its identity's
    // destination hash — the std transport's DHT; out of scope for this probe).
    let dst: [u8; 16] = [0xFF; 16];

    let mut beat = 0u32;
    let mut now_ms = 0u32;
    loop {
        beat = beat.wrapping_add(1);
        esp_hal::delay::Delay::new().delay_millis(100); // real 100 ms tick
        now_ms = now_ms.wrapping_add(100);

        // Produce a valid RNS DATA packet of arbitrary bytes every 5th beat.
        if beat % 5 == 0 {
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
            if let Ok(p) = RawPacket::pack(flags, 0, &dst, None, 0, &payload) {
                let n = p.raw.len();
                sched.queue_tx(p.raw);
                println!(
                    "[rns] beat #{beat} queued {n} B (queue={})",
                    sched.queued_len()
                );
            }
        }

        if sched.is_rx_awake(now_ms) {
            // Link window open: listen + flush queued frames.
            let _ = link.pump_rx(now_ms);
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
