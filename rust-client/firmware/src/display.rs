//! Cardputer ST7789 display driver + RGB565 framebuffer — the chart panel.
//!
//! Pins (M5Stack Cardputer ADV, per Meshtastic `variants/esp32s3/
//! m5stack_cardputer_adv/variant.h` — the same authority `cardputer_client/
//! firmware/main/cardputer_pins.h` cites for the radio):
//!
//!   SCK=GPIO36, MOSI=GPIO35, CS=GPIO37, DC=GPIO34, backlight=GPIO38.
//!
//! The SX1262 radio owns SPI2 (HSPI, SCK=40/MOSI=14/MISO=39/CS=5) in main, so
//! the panel takes SPI3 (HSPI) with the CSI-free 3-wire bus (SCK/MOSI/CS; DC
//! is a plain GPIO).  Panel is a 240×135 ST7789 in RGB565.
//!
//! HARDWARE-VERIFY REQUIRED (mirrors sx1262_radio.rs): the init order +
//! frequencies below are the vendor-standard ST7789 sequence and the offset/
//! MADCTL default matches the 240×135 Meshtastic config, but the exact panel
//! offset/backlight polarity must be confirmed on the device before trusting
//! pixels.  This proves the driver compiles + the framebuffer math; the LCD is
//! the on-hardware leg.

use esp_hal::gpio::Output;
use esp_hal::spi::master::Spi;
use esp_hal::Blocking;

pub const PANEL_W: usize = 240;
pub const PANEL_H: usize = 135;
const FB_LEN: usize = PANEL_W * PANEL_H * 2; // 64800 RGB565 bytes

/// Framebuffer (not on the 128 KiB heap — its own static).
static mut FB: [u8; FB_LEN] = [0u8; FB_LEN];

/// RGB888 → big-endian RGB565 bytes (the ST7789 RAMWR stream order).
fn rgb565(rgb: u32) -> [u8; 2] {
    let r = (rgb >> 16) & 0xff;
    let g = (rgb >> 8) & 0xff;
    let b = rgb & 0xff;
    let v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3);
    [(v >> 8) as u8, v as u8]
}

/// ST7789 on SPI3 (HSPI): SCK=36, MOSI=35, CS=37 (output), DC=34 (output),
/// backlight=38 (output).  The chart frame is drawn into [`FB`] and pushed
/// with [`Lcd::present`], so a frame costs one 64 KiB SPI burst per redraw.
pub struct Lcd<'d> {
    spi: Spi<'d, Blocking>,
    dc: Output<'d>,
    cs: Output<'d>,
    bl: Output<'d>,
}

impl<'d> Lcd<'d> {
    /// Wire + reset + bring the panel out of sleep (SWRESET → SLPOUT →
    /// RGB565 → native 240×135 orientation → NORON → DISPON).  CS idle high;
    /// `dc` only toggles per command.
    pub fn new(
        spi: Spi<'d, Blocking>,
        dc: Output<'d>,
        cs: Output<'d>,
        bl: Output<'d>,
    ) -> Self {
        let mut lcd = Lcd { spi, dc, cs, bl };
        lcd.init();
        lcd
    }

    fn init(&mut self) {
        let d = esp_hal::delay::Delay::new();
        self.bl.set_high(); // backlight rail on
        self.cmd(0x01); // SWRESET
        d.delay_millis(120);
        self.cmd(0x11); // SLPOUT
        d.delay_millis(120);
        self.cmd(0x3a);
        self.data(&[0x55]); // 16-bit RGB565
        self.cmd(0x36);
        self.data(&[0x00]); // MADCTL — 240×135, no flip, no offset
        self.cmd(0x13); // NORON
        d.delay_millis(10);
        self.cmd(0x29); // DISPON
        d.delay_millis(10);
    }

    fn cmd(&mut self, byte: u8) {
        self.cs.set_low();
        self.dc.set_low();
        self.spi.write(&[byte]).ok();
        self.cs.set_high();
    }

    fn data(&mut self, bytes: &[u8]) {
        self.cs.set_low();
        self.dc.set_high();
        self.spi.write(bytes).ok();
        self.cs.set_high();
    }

    fn fb_mut(&mut self) -> &mut [u8; FB_LEN] {
        // Single-threaded boot; Lcd is the only writer of FB.
        unsafe { &mut *core::ptr::addr_of_mut!(FB) }
    }

    /// Blit the framebuffer to the panel: CASET 0..239, RASET 0..134, RAMWR.
    pub fn present(&mut self) {
        self.cmd(0x2a); // CASET
        self.data(&[0x00, 0x00, 0x00, 239]);
        self.cmd(0x2b); // RASET
        self.data(&[0x00, 0x00, 0x00, 134]);
        self.cmd(0x2c); // RAMWR
        self.cs.set_low();
        self.dc.set_high();
        // Whole 64 KiB payload with CS held — one transaction so the panel
        // does not see command boundaries inside the frame image.
        unsafe {
            let fb: &[u8] =
                core::slice::from_raw_parts(core::ptr::addr_of!(FB) as *const u8, FB_LEN);
            self.spi.write(fb).ok();
        }
        self.cs.set_high();
    }
}

/// The chart draws through the two-primitive ``lma_chart::Display`` contract
/// (fill + pixel); lines, traces and the embedded 8x8 text are rendered by
/// the crate over `pixel`, so this driver only touches the framebuffer.
impl lma_chart::Display for Lcd<'_> {
    fn fill(&mut self, rgb: u32) {
        let c = rgb565(rgb);
        let fb = self.fb_mut();
        for pair in fb.chunks_exact_mut(2) {
            pair.copy_from_slice(&c);
        }
    }

    fn pixel(&mut self, x: i32, y: i32, rgb: u32) {
        if x < 0 || y < 0 || x >= PANEL_W as i32 || y >= PANEL_H as i32 {
            return;
        }
        let c = rgb565(rgb);
        let fb = self.fb_mut();
        let i = (y as usize * PANEL_W + x as usize) * 2;
        fb[i] = c[0];
        fb[i + 1] = c[1];
    }
}
