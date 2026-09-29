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
    match (|| -> Result<(), (/* esp-hal error */)> {
        radio.reset()?;
        radio.set_packet_type_lora()?;
        radio.set_rf_frequency(868_000_000)?;
        radio.set_sync_word(0x1424)?;
        radio.set_modulation_params(7, 0x04, 1, 0)?;
        radio.set_dio2_as_rf_switch()?;
        radio.set_dio3_as_tcxo()?;
        radio.set_pa_config(14, 0x06)?;
 
        Ok(())
    })() {
        Ok(()) => println!("[t3] sx1262 configured 868/BW125/SF7/CR4:5 syncword=0x1424"),
        Err(_) => println!("[t3] sx1262 configure FAILED (SPI/hardware)"),
    }

    // RF-beacon leg: transmit one LoRa frame and confirm TX_DONE on-air.
    // DIAGNOSTIC build: log the raw IRQ status over several polls to diagnose
    // the SX1262 read path (TX_DONE=false on the merged flash is under test).
    use sx126x::irq;
    let beacon: [u8; 7] = [0xcb, b'L', b'M', b'A', b'O', 1, 0];
    let loaded = (|| -> Result<(), ()> {
        radio.prepare_send(&beacon)?;
        radio.start_tx()?;
        Ok(())
    })();
    println!("[t3] beacon loaded={} 7B, polling irq...", loaded.is_ok());
    let mut saw_tx_done = false;
    for i in 0..8 {
        match radio.get_irq_status() {
            Ok(v) => {
                println!("[t3] irq[{i}]={:#06x}{}", v, if v & irq::TX_DONE != 0 { " TX_DONE" } else { "" });
                if v & irq::TX_DONE != 0 {
                    saw_tx_done = true;
                }
            }
            Err(_) => println!("[t3] irq[{i}]=ERR(busy/timeout)"),
        }
        for _ in 0..400_000 {
            core::hint::spin_loop();
        }
    }
    match radio.get_rx_buffer_status() {
        Ok((len, ptr)) => println!("[t3] rxbuf len={len} ptr={ptr}"),
        Err(_) => println!("[t3] rxbuf=ERR"),
    }
    println!("[t3] beacon TX_DONE={}", saw_tx_done);

    let mut beat = 0u32;
    loop {
        beat = beat.wrapping_add(1);
        println!("[t1] heartbeat {}", beat);
        for _ in 0..5_000_000 {
            core::hint::spin_loop();
        }
    }
}
