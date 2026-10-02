//! Cardputer ADV ST7789 panel + RGB565 framebuffer — the chart surface.
//!
//! Pins (M5Stack Cardputer ADV — the built-in panel config used by the no_std
//! Rust reference `BotEkrem/echoputer`, verified to drive THIS panel):
//!
//!   ST7789V2 240x135 on SPI2/FSPI: SCK=36 MOSI=35 CS=37 DC=34 RST=33 BL=38.
//!
//! The SX1262 radio (and SD/EXT) live on SPI3/HSPI (SCK=40/MOSI=14/MISO=39),
//! which is why the panel owns SPI2 here.  The panel is a 240×135 sub-window of
//! the ST7789's 240×320 GRAM at mipidsi `display_offset(52,40)` with a Deg90
//! rotation and inverted colours — all of which the mipidsi ST7789 model applies
//! from `init()`, so no raw CASET/RASET/MADCTL hand-rolling is needed here.
//!
//! The chart renders into [`FB`] through the two-primitive
//! `lma_chart::Display` contract (fill + pixel); `mipidsi` then blits the whole
//! 64 KiB frame to the panel in one windowed transfer ([`blit`]).

use embedded_graphics::pixelcolor::Rgb565;
use mipidsi::models::ST7789;

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

/// Draw target for `lma_chart::draw`.  Stateless: the frame lands in [`FB`].
pub struct Frame;

impl Frame {
    fn fb() -> &'static mut [u8; FB_LEN] {
        // Single-threaded boot; Frame is the only writer of FB.
        unsafe { &mut *core::ptr::addr_of_mut!(FB) }
    }
}

/// The chart draws through the two-primitive ``lma_chart::Display`` contract
/// (fill + pixel); lines, traces and the embedded 8x8 text are rendered by
/// the crate over `pixel`, so this adapter only touches the framebuffer.
impl lma_chart::Display for Frame {
    fn fill(&mut self, rgb: u32) {
        let c = rgb565(rgb);
        let fb = Self::fb();
        for pair in fb.chunks_exact_mut(2) {
            pair.copy_from_slice(&c);
        }
    }

    fn pixel(&mut self, x: i32, y: i32, rgb: u32) {
        if x < 0 || y < 0 || x >= PANEL_W as i32 || y >= PANEL_H as i32 {
            return;
        }
        let c = rgb565(rgb);
        let fb = Self::fb();
        let i = (y as usize * PANEL_W + x as usize) * 2;
        fb[i] = c[0];
        fb[i + 1] = c[1];
    }
}

/// Push [`FB`] to the panel in one windowed mipidsi transfer.  mipidsi already
/// rotated (Deg90) and offset (52,40) the ST7789 model to the 240×135 window,
/// so the logical blit is the full 240×135 frame at (0,0)-(239,134).
pub fn blit<DI, RST>(
    display: &mut mipidsi::Display<DI, ST7789, RST>,
) -> Result<(), DI::Error>
where
    DI: mipidsi::interface::Interface,
    RST: embedded_hal::digital::OutputPin,
    Rgb565: mipidsi::interface::InterfacePixelFormat<DI::Word>,
{
    // Feed the 64 KiB BE-RGB565 buffer straight through; mipidsi batches into
    // its interface staging buffer (a single long RAMWR window per chunk).
    let frame: &[u8] =
        unsafe { core::slice::from_raw_parts(core::ptr::addr_of!(FB) as *const u8, FB_LEN) };
    let colors = frame.chunks_exact(2).map(|c| {
        Rgb565::new(
            c[0] & 0xf8,                                   // R (5 → 8 bits)
            ((c[0] & 0x07) << 5) | (c[1] >> 3),            // G (6 → 8 bits)
            ((c[1] & 0x1f) << 3),                          // B (5 → 8 bits)
        )
    });
    display.set_pixels(0, 0, (PANEL_W - 1) as u16, (PANEL_H - 1) as u16, colors)
}
