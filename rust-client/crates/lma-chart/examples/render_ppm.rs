//! Host preview: parse a `DATA …` line and render the Cardputer chart to a
//! PPM image, so the panel output can be eyeballed on the dev box before
//! (or without) touching the LCD.
//!
//! Usage (from the host workspace):
//!   cargo run -p lma-chart --example render_ppm -- [line] [out.ppm]
//! No args: renders a representative server-format line to `chart.ppm`.

use lma_chart::{self as chart};
use lma_chart::{draw, parse_data_line, Display, H, W};

/// RGB888 framebuffer sized to the panel.
struct Ppm {
    px: Vec<u8>, // (W*H*3) bytes
}

impl Default for Ppm {
    fn default() -> Self {
        Ppm { px: vec![0u8; (W as usize) * (H as usize) * 3] }
    }
}

impl Display for Ppm {
    fn fill(&mut self, rgb: u32) {
        let r = (rgb >> 16) as u8;
        let g = (rgb >> 8) as u8;
        let b = rgb as u8;
        self.px.fill(r);
        for p in self.px.chunks_exact_mut(3) {
            p[0] = r;
            p[1] = g;
            p[2] = b;
        }
    }
    fn pixel(&mut self, x: i32, y: i32, rgb: u32) {
        if x < 0 || y < 0 || x >= W || y >= H {
            return;
        }
        let i = ((y as usize) * (W as usize) + x as usize) * 3;
        self.px[i] = (rgb >> 16) as u8;
        self.px[i + 1] = (rgb >> 8) as u8;
        self.px[i + 2] = rgb as u8;
    }
}

const DEFAULT_LINE: &str = "DATA e824ad2d 37 53 8 21.8 22.1 21.5 22.4 22.0 21.7 22.3 22.1 \
    6 55 54 57 56 55 58 12 46 45 44 47 46 45 47 46 46 47 45 44 4";

fn main() {
    let line = std::env::args().nth(1).unwrap_or_else(|| DEFAULT_LINE.to_string());
    let out = std::env::args().nth(2).unwrap_or_else(|| "chart.ppm".to_string());
    let rec = parse_data_line(&line).expect("this DATA line should parse");
    let mut fb = Ppm::default();
    let points = draw(&mut fb, &rec);
    let mut f = std::fs::File::create(&out).expect("create output");
    use std::io::Write;
    writeln!(f, "P6 {} {} 255", W, H).unwrap();
    f.write_all(&fb.px).unwrap();
    eprintln!("rendered {} trace segments to {out}", points);
}
