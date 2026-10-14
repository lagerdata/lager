// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! A captured screen to per-channel envelopes: for every pixel column of the
//! waveform area, the highest and lowest pixel drawn in each channel's
//! colour, as a (minimum, maximum) pair of counts.

use protocol::lscp::NO_SAMPLE;

use super::frame::VideoFrame;
use super::screen::{Palette, ScreenMap};

/// Counts per vertical division. A division is tens of pixels, so this
/// keeps a pixel worth many counts and a full screen far inside an i16.
pub const COUNTS_PER_DIV: f64 = 1024.0;

/// The most channels a screen can show.
pub const MAX_CHANNELS: usize = 4;

/// Envelopes from one frame, reused frame after frame.
#[derive(Debug, Default)]
pub struct Envelopes {
    pub columns: usize,
    /// Per channel, `columns` pairs flattened as [min, max, min, max, ...].
    pub samples: [Vec<i16>; MAX_CHANNELS],
    /// Channels with a pixel on the top or bottom edge of the area, which
    /// is where a trace past full scale is drawn.
    pub clipped: u8,
    /// Channels with at least one pixel anywhere.
    pub seen: u8,
    top: Vec<u16>,
    bottom: Vec<u16>,
}

const UNSET: u16 = u16::MAX;

impl Envelopes {
    /// Find the traces of the channels in `wanted` (bit n for channel n).
    pub fn extract(&mut self, frame: &VideoFrame, map: &ScreenMap, palette: &Palette, wanted: u8) {
        let columns = map.width.min(frame.width.saturating_sub(map.left));
        let rows = map.height.min(frame.height.saturating_sub(map.top));
        self.columns = columns;
        self.clipped = 0;
        self.seen = 0;
        self.top.clear();
        self.top.resize(columns * MAX_CHANNELS, UNSET);
        self.bottom.clear();
        self.bottom.resize(columns * MAX_CHANNELS, UNSET);

        // Row by row, for the cache: the frame is stored that way.
        for row in 0..rows {
            let y = map.top + row;
            for column in 0..columns {
                let bits = palette.classify(frame.ycc(map.left + column, y)) & wanted;
                if bits == 0 {
                    continue;
                }
                for channel in 0..MAX_CHANNELS {
                    if bits & (1 << channel) == 0 {
                        continue;
                    }
                    let at = column * MAX_CHANNELS + channel;
                    if self.top[at] == UNSET {
                        self.top[at] = row as u16;
                    }
                    self.bottom[at] = row as u16;
                }
            }
        }

        let last_row = rows.saturating_sub(1) as u16;
        let to_counts = |row: u16| -> i16 {
            let y = (map.top + usize::from(row)) as f64;
            let divs = (map.center_y - y) / map.px_per_div_y;
            (divs * COUNTS_PER_DIV)
                .round()
                .clamp(f64::from(i16::MIN + 1), f64::from(i16::MAX)) as i16
        };
        for channel in 0..MAX_CHANNELS {
            let samples = &mut self.samples[channel];
            samples.clear();
            if wanted & (1 << channel) == 0 {
                continue;
            }
            samples.reserve(columns * 2);
            for column in 0..columns {
                let at = column * MAX_CHANNELS + channel;
                let (top, bottom) = (self.top[at], self.bottom[at]);
                if top == UNSET {
                    samples.extend_from_slice(&[NO_SAMPLE, NO_SAMPLE]);
                    continue;
                }
                self.seen |= 1 << channel;
                if top == 0 || bottom == last_row {
                    self.clipped |= 1 << channel;
                }
                // Screen rows grow downwards, so the lowest pixel is the
                // minimum.
                samples.extend_from_slice(&[to_counts(bottom), to_counts(top)]);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::oscilloscope::rigol::frame::{rgb_to_ycc, Layout};
    use crate::oscilloscope::rigol::screen::MSO5000_COLOURS;

    fn map() -> ScreenMap {
        // 10 x 8 divisions of 50 pixels, at (300, 220) in a 640 x 480 frame.
        ScreenMap {
            left: 50,
            top: 20,
            width: 501,
            height: 401,
            center_x: 300.0,
            center_y: 220.0,
            px_per_div_x: 50.0,
            px_per_div_y: 50.0,
        }
    }

    fn draw(frame: &mut VideoFrame, channel: usize, x: usize, from_y: usize, to_y: usize) {
        let (r, g, b) = MSO5000_COLOURS[channel];
        for y in from_y..=to_y {
            frame.put(x, y, rgb_to_ycc(r, g, b));
        }
    }

    #[test]
    fn a_column_becomes_the_pair_of_its_extremes_in_counts() {
        for layout in [Layout::Ycc, Layout::Yuyv] {
            let mut frame = VideoFrame::blank(640, 480, layout);
            // Channel B, one division above centre down to one below.
            draw(&mut frame, 1, 100, 170, 270);
            let mut env = Envelopes::default();
            env.extract(&frame, &map(), &Palette::new(&MSO5000_COLOURS), 0b0011);
            let column = 100 - 50;
            let b = &env.samples[1];
            assert_eq!(&b[column * 2..column * 2 + 2], &[-1024, 1024], "{layout:?}");
            assert_eq!(env.seen, 0b0010, "{layout:?}");
            assert!(env.samples[0].iter().all(|&s| s == NO_SAMPLE), "{layout:?}");
            assert!(env.samples[2].is_empty(), "not asked for");
        }
    }

    #[test]
    fn channels_drawn_in_the_same_column_stay_apart() {
        let mut frame = VideoFrame::blank(640, 480, Layout::Ycc);
        draw(&mut frame, 0, 200, 100, 110);
        draw(&mut frame, 3, 200, 300, 305);
        let mut env = Envelopes::default();
        env.extract(&frame, &map(), &Palette::new(&MSO5000_COLOURS), 0b1111);
        let column = 150;
        let a = &env.samples[0][column * 2..column * 2 + 2];
        let d = &env.samples[3][column * 2..column * 2 + 2];
        assert!(a[0] > 0 && a[1] > a[0], "A is above centre: {a:?}");
        assert!(d[1] < 0 && d[1] > d[0], "D is below centre: {d:?}");
    }

    #[test]
    fn a_trace_on_the_edge_of_the_area_is_clipped() {
        let mut frame = VideoFrame::blank(640, 480, Layout::Ycc);
        draw(&mut frame, 2, 300, 20, 40);
        let mut env = Envelopes::default();
        env.extract(&frame, &map(), &Palette::new(&MSO5000_COLOURS), 0b0100);
        assert_eq!(env.clipped, 0b0100);
    }

    #[test]
    fn every_column_of_the_area_has_a_pair() {
        let frame = VideoFrame::blank(640, 480, Layout::Ycc);
        let mut env = Envelopes::default();
        env.extract(&frame, &map(), &Palette::new(&MSO5000_COLOURS), 0b0001);
        assert_eq!(env.columns, 501);
        assert_eq!(env.samples[0].len(), 1002);
    }
}
