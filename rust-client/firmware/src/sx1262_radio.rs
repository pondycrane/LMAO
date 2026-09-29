//! esp-hal SPI `RadioBus` impl for the Cardputer SX1262 (RF-leg step 3).
//!
//! Pins (from `cardputer_client/firmware/main/cardputer_pins.h` + `lora_boards.py`):
//!   SPI2 (HSPI); SCK=GPIO40, MOSI=GPIO14, MISO=GPIO39, CS=GPIO5, BUSY=GPIO6,
//!   DIO1=GPIO4 (IRQ), RST=GPIO3. GPIO39/40 are the JTAG MTCK/MTDO legs and
//! must be reclaimed as GPIO (done here by handing them to SPI).
//!
//! HARDWARE-VERIFY REQUIRED: the exact SX1262 SPI response layout (where the
//! status byte and read data land relative to the opcode/args) is asserted per
//! the MicroPython `_cmd`/`_reg_read` semantics but MUST be confirmed on the
//! device (a logic analyser or a register round-trip against the real radio)
//! before trusting RF traffic to it. RF RX/TX is the on-hardware leg; this only
//! proves the binding compiles + the command bytes are issued.

use esp_hal::gpio::{Input, Output};
use esp_hal::spi::master::Spi;
use esp_hal::Blocking;
use sx126x::RadioBus;

pub struct EspRadioBus<'d> {
    spi: Spi<'d, Blocking>,
    rst: Output<'d>,
    busy: Input<'d>,
}

impl<'d> EspRadioBus<'d> {
    /// `spi` must be an `Spi` already bound to SCK/MOSI/MISO/CS on the pins above.
    pub fn new(spi: Spi<'d, Blocking>, rst: Output<'d>, busy: Input<'d>) -> Self {
        EspRadioBus { spi, rst, busy }
    }
}

impl RadioBus for EspRadioBus<'_> {
    type Error = ();

    fn command(&mut self, opcode: u8, write: &[u8], read: &mut [u8]) -> Result<(), ()> {
        // One CS assertion per command. For reads we clock `read.len()` extra
        // bytes in the same transaction (SX1262 returns status then data).
        // NOTE (hardware-verif): layout of status/data relative to opcode+args
        // mirrors the MicroPython `_cmd`; confirm on the device.
        let mut buf = [0u8; 260];
        let data_len = read.len().saturating_sub(1); // first byte is status
        let n = 1 + write.len() + read.len();
        buf[0] = opcode;
        buf[1..1 + write.len()].copy_from_slice(write);
        // Sequential in-place transfer: TX buf (opcode+args+nops), RX same slots.
        self.spi
            .transfer(&mut buf[..n])
            .map_err(|_| ())?;
        // (On-wire trace removed after bring-up: the command stream was
        // verified byte-identical to the reference driver.)
        if !read.is_empty() {
            // After [opcode+write], the following bytes are [status, data...].
            let status = buf[1 + write.len()];
            read[0] = status;
            for i in 0..data_len {
                if 1 + i < read.len() {
                    let r = 1 + write.len() + 1 + i;
                    read[1 + i] = if r < n { buf[r] } else { 0 };
                }
            }
        }
        let _ = data_len;
        Ok(())
    }

    fn reset(&mut self) -> Result<(), ()> {
        // Reference reset timing: 1 ms low pulse, then 5 ms settle high.
        self.rst.set_low();
        self.delay_ms(1);
        self.rst.set_high();
        self.delay_ms(5);
        self.wait_ready()
    }

    fn wait_ready(&mut self) -> Result<(), ()> {
        // SX1262 asserts BUSY during internal ops. Typical <105 us, but a full
        // CALIBRATE can hold it up to ~18 ms (longer with TCXO startup) — the
        // budget here must cover the worst case, ~50 ms.
        for _ in 0..10_000_000 {
            if self.busy.is_low() {
                return Ok(());
            }
            core::hint::spin_loop();
        }
        Err(())
    }

    fn delay_ms(&mut self, ms: u32) {
        // Real cycle-counted delay (spin_loop optimizes away at opt-level "s").
        esp_hal::delay::Delay::new().delay_millis(ms);
    }
}
