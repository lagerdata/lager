// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! A captured screen to per-channel envelopes: for every pixel column of the
//! waveform area, the highest and lowest pixel drawn in each channel's
//! colour, as a (minimum, maximum) pair of counts.

use protocol::lscp::NO_SAMPLE;

use super::frame::VideoFrame;
use super::screen::{MeasurementBar, Palette, ScreenMap};

/// Counts per vertical division. A division is tens of pixels, so this
/// keeps a pixel worth many counts and a full screen far inside an i16.
pub const COUNTS_PER_DIV: f64 = 1024.0;

/// The most channels a screen can show.
pub const MAX_CHANNELS: usize = 4;

/// The trigger-position marker the scope draws inside the top of the area,
/// in divisions: about 25 x 35 pixels at 150 x 90 a division. Its orange
/// passes for CH1's yellow, so it is left out rather than told apart.
const MARKER_HALF_WIDTH_DIVS: f64 = 0.1;
const MARKER_HEIGHT_DIVS: f64 = 0.45;

/// Columns a channel must touch the top or bottom edge in before it is
/// called clipped: a trace past full scale runs along the edge, where a
/// stray pixel or two does not.
const CLIPPED_COLUMNS: usize = 3;

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
    /// Find the traces of the channels in `wanted` (bit n for channel n),
    /// leaving out the trigger marker drawn at `trigger_column` of the area.
    pub fn extract(
        &mut self,
        frame: &VideoFrame,
        map: &ScreenMap,
        palette: &Palette,
        wanted: u8,
        trigger_column: Option<f64>,
    ) {
        self.extract_masked(frame, map, palette, wanted, trigger_column, None);
    }

    /// As `extract`, also leaving out the scope's measurement bar: its
    /// readouts are drawn over the trace in the channels' colours.
    pub fn extract_masked(
        &mut self,
        frame: &VideoFrame,
        map: &ScreenMap,
        palette: &Palette,
        wanted: u8,
        trigger_column: Option<f64>,
        bar: Option<MeasurementBar>,
    ) {
        let columns = map.width.min(frame.width.saturating_sub(map.left));
        let rows = map.height.min(frame.height.saturating_sub(map.top));
        self.columns = columns;
        self.clipped = 0;
        self.seen = 0;
        self.top.clear();
        self.top.resize(columns * MAX_CHANNELS, UNSET);
        self.bottom.clear();
        self.bottom.resize(columns * MAX_CHANNELS, UNSET);

        // The marker sits at the trigger column, or against the side of the
        // area it is off.
        let marker = trigger_column.map(|column| {
            let centre = column.clamp(0.0, columns.saturating_sub(1) as f64);
            let half = MARKER_HALF_WIDTH_DIVS * map.px_per_div_x;
            let from = (centre - half).floor().max(0.0) as usize;
            let to = ((centre + half).ceil() as usize).min(columns.saturating_sub(1));
            let below = (MARKER_HEIGHT_DIVS * map.px_per_div_y).ceil() as usize;
            (from..=to, below)
        });

        // The bar, from its top border down and from the left of the area to
        // its end.
        let under_bar = bar.map(|bar| {
            (bar.top.saturating_sub(map.top), bar.right.saturating_sub(map.left))
        });

        // Row by row, for the cache: the frame is stored that way.
        for row in 0..rows {
            let y = map.top + row;
            for column in 0..columns {
                if let Some((span, below)) = &marker {
                    if row < *below && span.contains(&column) {
                        continue;
                    }
                }
                if let Some((from_row, to_column)) = under_bar {
                    if row >= from_row && column <= to_column {
                        continue;
                    }
                }
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
            let mut on_edge = 0;
            for column in 0..columns {
                let at = column * MAX_CHANNELS + channel;
                let (top, bottom) = (self.top[at], self.bottom[at]);
                if top == UNSET {
                    samples.extend_from_slice(&[NO_SAMPLE, NO_SAMPLE]);
                    continue;
                }
                self.seen |= 1 << channel;
                if top == 0 || bottom == last_row {
                    on_edge += 1;
                }
                // Screen rows grow downwards, so the lowest pixel is the
                // minimum.
                samples.extend_from_slice(&[to_counts(bottom), to_counts(top)]);
            }
            if on_edge >= CLIPPED_COLUMNS {
                self.clipped |= 1 << channel;
            }
        }
    }

    /// Whether every channel in `wanted` that shows anywhere is missing from
    /// the right of the area while one of them runs across the rest: what
    /// the scope's side menu, open over a fifth of the grid, looks like.
    pub fn right_side_bare(&self, wanted: u8) -> bool {
        let shown = self.seen & wanted;
        if shown == 0 || self.columns == 0 {
            return false;
        }
        let edge = self.columns * (100 - BARE_RIGHT_PERCENT) / 100;
        let drawn = |channel: usize, column: usize| self.top[column * MAX_CHANNELS + channel] != UNSET;
        let channels = (0..MAX_CHANNELS).filter(|&c| shown & (1 << c) != 0);
        let right_bare = channels
            .clone()
            .all(|c| (edge..self.columns).all(|column| !drawn(c, column)));
        right_bare
            && channels.into_iter().any(|c| {
                let drawn_left = (0..edge).filter(|&column| drawn(c, column)).count();
                drawn_left * 100 >= edge * TRACED_LEFT_PERCENT
            })
    }
}

/// The share of the area, from the right, that the side menu always covers:
/// it hides about a fifth.
const BARE_RIGHT_PERCENT: usize = 15;
/// How much of the rest a trace has to cross for the empty right to look
/// like something covering it rather than a trace that ends there.
const TRACED_LEFT_PERCENT: usize = 75;

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
            env.extract(&frame, &map(), &Palette::new(&MSO5000_COLOURS), 0b0011, None);
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
        env.extract(&frame, &map(), &Palette::new(&MSO5000_COLOURS), 0b1111, None);
        let column = 150;
        let a = &env.samples[0][column * 2..column * 2 + 2];
        let d = &env.samples[3][column * 2..column * 2 + 2];
        assert!(a[0] > 0 && a[1] > a[0], "A is above centre: {a:?}");
        assert!(d[1] < 0 && d[1] > d[0], "D is below centre: {d:?}");
    }

    #[test]
    fn a_trace_along_the_edge_of_the_area_is_clipped() {
        let mut frame = VideoFrame::blank(640, 480, Layout::Ycc);
        for x in 300..310 {
            draw(&mut frame, 2, x, 20, 40);
        }
        let mut env = Envelopes::default();
        env.extract(&frame, &map(), &Palette::new(&MSO5000_COLOURS), 0b0100, None);
        assert_eq!(env.clipped, 0b0100);
    }

    #[test]
    fn a_pixel_or_two_on_the_edge_is_not_clipping() {
        let mut frame = VideoFrame::blank(640, 480, Layout::Ycc);
        draw(&mut frame, 2, 300, 20, 40);
        draw(&mut frame, 2, 301, 20, 40);
        let mut env = Envelopes::default();
        env.extract(&frame, &map(), &Palette::new(&MSO5000_COLOURS), 0b0100, None);
        assert_eq!(env.clipped, 0);
        assert_eq!(env.seen, 0b0100, "still drawn");
    }

    #[test]
    fn the_trigger_marker_is_not_read_as_a_trace() {
        let mut frame = VideoFrame::blank(640, 480, Layout::Ycc);
        // The marker: orange, 0.4 div down from the top, at the trigger
        // column (area column 250, x = 300).
        for x in 296..=304 {
            for y in 20..40 {
                frame.put(x, y, rgb_to_ycc(255, 165, 0));
            }
        }
        // CH1 trace at +1 div across the same columns.
        for x in 290..=310 {
            draw(&mut frame, 0, x, 170, 172);
        }
        let mut env = Envelopes::default();
        let palette = Palette::new(&MSO5000_COLOURS);
        env.extract(&frame, &map(), &palette, 0b0001, None);
        assert_eq!(env.clipped, 0b0001, "without the mask the marker reads as clipping CH1");
        env.extract(&frame, &map(), &palette, 0b0001, Some(250.0));
        assert_eq!(env.clipped, 0, "the marker is left out");
        let a = &env.samples[0][250 * 2..250 * 2 + 2];
        assert!(a.iter().all(|&c| (c - 1024).abs() < 60), "the trace under it is kept: {a:?}");
    }

    #[test]
    fn a_trigger_off_the_side_puts_the_marker_at_that_edge() {
        let mut frame = VideoFrame::blank(640, 480, Layout::Ycc);
        for x in 50..=54 {
            for y in 20..40 {
                frame.put(x, y, rgb_to_ycc(255, 165, 0));
            }
        }
        let mut env = Envelopes::default();
        env.extract(&frame, &map(), &Palette::new(&MSO5000_COLOURS), 0b0001, Some(-400.0));
        assert_eq!(env.seen, 0, "{:?}", &env.samples[0][..10]);
    }

    #[test]
    fn a_trace_cut_off_at_the_right_looks_covered() {
        let palette = Palette::new(&MSO5000_COLOURS);
        let mut env = Envelopes::default();
        let traced = |to: usize| {
            let mut frame = VideoFrame::blank(640, 480, Layout::Ycc);
            for x in 50..to {
                draw(&mut frame, 0, x, 200, 202);
            }
            frame
        };
        // The menu's edge: four fifths of the 501 columns.
        env.extract(&traced(50 + 400), &map(), &palette, 0b0001, None);
        assert!(env.right_side_bare(0b0001));
        env.extract(&traced(50 + 501), &map(), &palette, 0b0001, None);
        assert!(!env.right_side_bare(0b0001), "the whole width is traced");
        // A trace that is only a short burst on the left is not cut off.
        env.extract(&traced(50 + 200), &map(), &palette, 0b0001, None);
        assert!(!env.right_side_bare(0b0001));
        env.extract(&VideoFrame::blank(640, 480, Layout::Ycc), &map(), &palette, 0b0001, None);
        assert!(!env.right_side_bare(0b0001), "no trace at all says nothing");
    }

    #[test]
    fn one_channel_on_the_right_means_nothing_covers_it() {
        let palette = Palette::new(&MSO5000_COLOURS);
        let mut frame = VideoFrame::blank(640, 480, Layout::Ycc);
        for x in 50..450 {
            draw(&mut frame, 0, x, 200, 202);
        }
        draw(&mut frame, 1, 540, 300, 310);
        let mut env = Envelopes::default();
        env.extract(&frame, &map(), &palette, 0b0011, None);
        assert!(!env.right_side_bare(0b0011));
        assert!(env.right_side_bare(0b0001), "B is not asked about");
    }

    #[test]
    fn every_column_of_the_area_has_a_pair() {
        let frame = VideoFrame::blank(640, 480, Layout::Ycc);
        let mut env = Envelopes::default();
        env.extract(&frame, &map(), &Palette::new(&MSO5000_COLOURS), 0b0001, None);
        assert_eq!(env.columns, 501);
        assert_eq!(env.samples[0].len(), 1002);
    }
}
