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

    // RF-beacon leg: transmit a LoRa frame and confirm TX_DONE on-air.
    // Repeats every ~2 s in the heartbeat loop below so the event is visible
    // on any attached monitor (the one-shot boot log raced the serial attach).
    use sx126x::irq;
    let beacon: [u8; 7] = [0xcb, b'L', b'M', b'A', b'O', 1, 0];

    // Arm continuous RX — mirrors the reference `start_recv(continuous=True)`
    // at the end of interface init. The radio idles in RX between beacons;
    // this *is* the leaf's listen path.
    match radio.start_rx([0xFF, 0xFF, 0xFF]) {
        Ok(()) => println!("[t3] continuous RX armed"),
        Err(_) => println!("[t3] RX arm FAILED"),
    }

    // Periodic beacon: TX out of the RX idle with a bounded timeout, wait
    // TX_DONE on REAL millisecond timing (spin loops elide at opt-level "s" —
    // an elided poll window was the "endless TX" red herring), then re-arm
    // continuous RX and dump any received frame.
    let mut beat = 0u32;
    loop {
        beat = beat.wrapping_add(1);

        let loaded = (|| -> Result<(), ()> {
            radio.prepare_send(&beacon)?;
            // Bounded TX: airtime ~60 ms, timeout 200 ms (0x3200 * 15.625 us)
            // so a wedged sequencer is RTC-aborted instead of hanging.
            radio.start_tx_timeout([0x00, 0x32, 0x00])?;
            Ok(())
        })();
        if !loaded.is_ok() {
            println!("[t3] beacon #{beat} load FAILED");
        }

        let mut outcome = "timeout-poll";
        let mut polls = 0u32;
        for i in 0..100 {
            polls = i;
            match radio.get_irq_status() {
                Ok(v) => {
                    if v & irq::TX_DONE != 0 {
                        outcome = "TX_DONE";
                        break;
                    }
                    if v & irq::TIMEOUT != 0 {
                        outcome = "TX_TIMEOUT";
                        break;
                    }
                }
                Err(_) => outcome = "irq_err",
            }
            esp_hal::delay::Delay::new().delay_millis(5);
        }
        if outcome != "TX_DONE" {
            // Recover the radio before the next cycle.
            let _ = configure(&mut radio);
        }
        let _ = radio.clear_irq();
        println!("[t3] beacon #{beat} result={outcome} polls={polls}");

        // Back to continuous RX; listen through the idle window and dump any
        // received frame (bidirectional RNS path).
        let _ = radio.start_rx([0xFF, 0xFF, 0xFF]);
        for _ in 0..40 {
            if let Ok(v) = radio.get_irq_status() {
                if v & irq::RX_DONE != 0 {
                    let ok = irq::rx_success(v);
                    let (len, ptr) = radio.get_rx_buffer_status().unwrap_or((0, 0));
                    let (rssi, snr) = radio.get_packet_status().unwrap_or((0, 0.0));
                    let mut pkt = [0u8; 255];
                    let n = radio.read_buffer(ptr, &mut pkt[..len as usize]).unwrap_or(0);
                    println!(
                        "[t3] RX irq={v:#06x} ok={ok} len={len} rssi={rssi}dBm snr={snr}dB data={:02x?}",
                        &pkt[..n.min(32)]
                    );
                    let _ = radio.clear_irq();
                    let _ = radio.start_rx([0xFF, 0xFF, 0xFF]);
                }
            }
            esp_hal::delay::Delay::new().delay_millis(50);
        }
    }
}
