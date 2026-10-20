// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Where the waveform is on the captured screen, and which colour is which
//! channel.
//!
//! The map is measured, not looked up. At open the daemon shows the grid,
//! captures a frame, hides it and captures another; the grid lines are what
//! differ, and their spacing and centre give the waveform area in captured
//! pixels at whatever resolution and scaling the capture card delivers.
//! A frame that does not change when the grid does is a capture card that
//! is not showing this scope, which is how the pairing is confirmed.

use anyhow::{bail, Result};

use super::frame::{rgb_to_ycc, VideoFrame};

/// Divisions across and down the waveform area.
pub const HORIZONTAL_DIVISIONS: f64 = 10.0;
pub const VERTICAL_DIVISIONS: f64 = 8.0;

/// The waveform area in captured pixels.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ScreenMap {
    pub left: usize,
    pub top: usize,
    pub width: usize,
    pub height: usize,
    /// The graticule centre, where time zero and the channel offset sit.
    pub center_x: f64,
    pub center_y: f64,
    pub px_per_div_x: f64,
    pub px_per_div_y: f64,
}

impl ScreenMap {
    fn from_lattice(
        center_x: f64,
        center_y: f64,
        px_per_div_x: f64,
        px_per_div_y: f64,
        frame_width: usize,
        frame_height: usize,
    ) -> ScreenMap {
        let half_w = px_per_div_x * HORIZONTAL_DIVISIONS / 2.0;
        let half_h = px_per_div_y * VERTICAL_DIVISIONS / 2.0;
        // The border lines themselves are inside: a trace at full scale is
        // drawn on them.
        let left = (center_x - half_w).round().max(0.0) as usize;
        let right = ((center_x + half_w).round() as usize).min(frame_width - 1);
        let top = (center_y - half_h).round().max(0.0) as usize;
        let bottom = ((center_y + half_h).round() as usize).min(frame_height - 1);
        ScreenMap {
            left,
            top,
            width: right - left + 1,
            height: bottom - top + 1,
            center_x,
            center_y,
            px_per_div_x,
            px_per_div_y,
        }
    }
}

/// The capture card is not showing the scope: its HDMI output is off, the
/// cable is out, or the card is another scope's.
#[derive(Debug)]
pub struct NoPicture(pub String);

impl std::fmt::Display for NoPicture {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for NoPicture {}

/// Luma a grid pixel gains when the grid is switched on.
const GRID_STEP: u8 = 12;
/// Fewest grid lines that pin down a lattice in each direction.
const MIN_VERTICAL_LINES: usize = 5;
const MIN_HORIZONTAL_LINES: usize = 4;

/// Measure the waveform area from a frame with the grid shown and one with
/// it hidden, everything else being equal.
pub fn measure(with_grid: &VideoFrame, without_grid: &VideoFrame) -> Result<ScreenMap> {
    if (with_grid.width, with_grid.height) != (without_grid.width, without_grid.height) {
        bail!("the capture card changed resolution between the two calibration frames");
    }
    let (width, height) = (with_grid.width, with_grid.height);
    let mut columns = vec![0u32; width];
    let mut rows = vec![0u32; height];
    let mut changed = 0u64;
    for y in 0..height {
        for x in 0..width {
            if with_grid.luma(x, y).saturating_sub(without_grid.luma(x, y)) >= GRID_STEP {
                columns[x] += 1;
                rows[y] += 1;
                changed += 1;
            }
        }
    }
    if changed == 0 {
        return Err(NoPicture(
            "the video did not change when the scope's grid was switched on, so the capture \
             card is not showing this scope; check the HDMI cable, that the scope's HDMI output \
             is switched on (Utility > IO > HDMI > Output), and that the net names the capture \
             card the scope is plugged into"
                .into(),
        )
        .into());
    }
    let vertical = lattice(&line_centres(&columns, MIN_VERTICAL_LINES), MIN_VERTICAL_LINES).ok_or_else(|| {
        anyhow::anyhow!(
            "the video changed with the grid, but not into evenly spaced vertical lines; \
             the capture card may be scaling or cropping the picture"
        )
    })?;
    let horizontal = lattice(&line_centres(&rows, MIN_HORIZONTAL_LINES), MIN_HORIZONTAL_LINES).ok_or_else(|| {
        anyhow::anyhow!(
            "the video changed with the grid, but not into evenly spaced horizontal lines; \
             the capture card may be scaling or cropping the picture"
        )
    })?;
    Ok(ScreenMap::from_lattice(
        vertical.0,
        horizontal.0,
        vertical.1,
        horizontal.1,
        width,
        height,
    ))
}

/// Whether something covers the right of the waveform area: the scope's
/// side menu, which hides a fifth of it. Taken from the same two frames as
/// `measure`: the grid line four divisions right of centre changes far less
/// than the one four divisions left of it.
pub fn right_side_covered(with_grid: &VideoFrame, without_grid: &VideoFrame, map: &ScreenMap) -> bool {
    let band = |divs: f64| -> u64 {
        let x = map.center_x + divs * map.px_per_div_x;
        let half = (map.px_per_div_x * 0.1).max(1.0);
        let from = (x - half).max(0.0) as usize;
        let to = ((x + half) as usize).min(with_grid.width.saturating_sub(1));
        let mut changed = 0u64;
        for y in map.top..(map.top + map.height).min(with_grid.height) {
            for x in from..=to {
                if with_grid.luma(x, y).saturating_sub(without_grid.luma(x, y)) >= GRID_STEP {
                    changed += 1;
                }
            }
        }
        changed
    };
    let (left, right) = (band(-4.0), band(4.0));
    left > 0 && right * 4 < left
}

/// The centres of the runs of positions that are much more lit than the
/// rest, which are the lines crossing this axis.
///
/// The bar is half the strength of the `min_lines`-th strongest peak, not of
/// the strongest: the centre axes carry tick marks as well as the grid's
/// dots, and on an MSO5000 are three times as strong as the other lines.
///
/// Each line comes with its strength, the count at its strongest position.
fn line_centres(profile: &[u32], min_lines: usize) -> Vec<(f64, u32)> {
    let mut peaks: Vec<u32> = (0..profile.len())
        .filter(|&i| {
            let here = profile[i];
            here > 0
                && (i == 0 || profile[i - 1] <= here)
                && (i + 1 == profile.len() || profile[i + 1] < here)
        })
        .map(|i| profile[i])
        .collect();
    peaks.sort_unstable_by(|a, b| b.cmp(a));
    let reference = peaks
        .get(min_lines.saturating_sub(1))
        .or(peaks.last())
        .copied()
        .unwrap_or(0);
    let threshold = reference / 2;
    let mut centres = Vec::new();
    let mut run: Option<(u32, u64, u64)> = None; // (strongest, weight, weighted sum)
    for (i, &count) in profile.iter().enumerate() {
        if count > threshold && count > 0 {
            let (peak, w, sum) = run.unwrap_or((0, 0, 0));
            run = Some((peak.max(count), w + u64::from(count), sum + u64::from(count) * i as u64));
        } else if let Some((peak, w, sum)) = run.take() {
            centres.push((sum as f64 / w as f64, peak));
        }
    }
    if let Some((peak, w, sum)) = run {
        centres.push((sum as f64 / w as f64, peak));
    }
    centres
}

/// How much stronger than the other lines the centre axis must be to be
/// taken for it.
const AXIS_STRENGTH: f64 = 1.5;

/// The centre and spacing of the evenly spaced lines among `lines`, or
/// `None` when fewer than `min_lines` fit one spacing.
///
/// The centre is the centre axis where one line stands out as it, since the
/// axis carries tick marks the rest of the grid lacks; a menu covering one
/// side of the screen then hides lines without moving the centre. Failing
/// that, it is the middle of the outermost lines that fit, which holds
/// whether or not the border lines changed with the grid, because the grid
/// is symmetric about its centre.
fn lattice(lines: &[(f64, u32)], min_lines: usize) -> Option<(f64, f64)> {
    let centres: Vec<f64> = lines.iter().map(|l| l.0).collect();
    if centres.len() < min_lines {
        return None;
    }
    let mut gaps: Vec<f64> = centres.windows(2).map(|w| w[1] - w[0]).collect();
    gaps.sort_by(|a, b| a.total_cmp(b));
    let spacing = gaps[gaps.len() / 2];
    if spacing < 4.0 {
        return None;
    }
    // The longest run of lines on that spacing.
    let mut best: Vec<f64> = Vec::new();
    for (i, &origin) in centres.iter().enumerate() {
        let fitting: Vec<f64> = centres[i..]
            .iter()
            .copied()
            .filter(|&c| {
                let steps = (c - origin) / spacing;
                (steps - steps.round()).abs() < 0.15
            })
            .collect();
        if fitting.len() > best.len() {
            best = fitting;
        }
    }
    if best.len() < min_lines {
        return None;
    }
    // Least squares over (step, position), for spacing finer than a pixel.
    let origin = best[0];
    let points: Vec<(f64, f64)> = best
        .iter()
        .map(|&c| (((c - origin) / spacing).round(), c))
        .collect();
    let n = points.len() as f64;
    let mean_k = points.iter().map(|p| p.0).sum::<f64>() / n;
    let mean_c = points.iter().map(|p| p.1).sum::<f64>() / n;
    let num: f64 = points.iter().map(|p| (p.0 - mean_k) * (p.1 - mean_c)).sum();
    let den: f64 = points.iter().map(|p| (p.0 - mean_k).powi(2)).sum();
    let spacing = if den > 0.0 { num / den } else { spacing };
    // The fitted position of step k.
    let at = |k: f64| mean_c + spacing * (k - mean_k);
    let strength = |c: f64| lines.iter().find(|l| l.0 == c).map_or(0, |l| l.1);
    let mut strengths: Vec<u32> = best.iter().map(|&c| strength(c)).collect();
    strengths.sort_unstable();
    let typical = f64::from(strengths[strengths.len() / 2]);
    let axes: Vec<f64> = points
        .iter()
        .filter(|p| f64::from(strength(p.1)) >= typical * AXIS_STRENGTH)
        .map(|p| p.0)
        .collect();
    if let [axis] = axes[..] {
        return Some((at(axis), spacing));
    }
    let first = points.first()?.0;
    let last = points.last()?.0;
    Some((at((first + last) / 2.0), spacing))
}

/// The scope's measurement bar: the readouts `:MEASure:<item>?` and the
/// front panel's Measure > Add put in a row of tiles inside the bottom of
/// the waveform area, newest on the left. Drawn over the trace, so what is
/// under it is not the signal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MeasurementBar {
    /// Readouts in the bar.
    pub tiles: usize,
    /// The bar's top border, as a row of the frame.
    pub top: usize,
    /// The bar's last column in the frame.
    pub right: usize,
}

/// Where the bar's top border is looked for, in divisions down from the top
/// of the area: it sits about 7.2 down, the bar filling the rest.
const BAR_ROWS_DIVS: (f64, f64) = (6.9, 7.5);
/// Rows above the border that must be clear of its grey: the screen behind
/// the bar is black, where a picture the card washed grey is grey all over.
const BAR_CLEAR_ABOVE_PX: usize = 4;
/// How far right of the area's left edge the border may start.
const BAR_START_DIVS: f64 = 0.25;
/// The border runs this far from the area's left edge, plus a tile's width
/// for every readout. Measured on an MSO5074 through an MS2109 at 1080p,
/// with 1, 2, 3, 6 and 10 readouts: 142.5 px a tile at 150 px a division.
const BAR_LEAD_DIVS: f64 = 0.476;
const BAR_TILE_DIVS: f64 = 0.949;
/// Pixels the border may break for, where a readout's text touches it.
const BAR_GAP_PX: usize = 3;

/// The grey of the bar's border, about RGB (52, 80, 84): too grey for the
/// palette to take for a channel.
fn bar_border((luma, cb, cr): (u8, u8, u8)) -> bool {
    (40..=140).contains(&luma) && cb.abs_diff(128) <= 14 && (104..=130).contains(&cr)
}

/// Find the measurement bar in `frame`, and count its readouts from the
/// length of its top border. None when the scope shows no readout.
pub fn measurement_bar(frame: &VideoFrame, map: &ScreenMap) -> Option<MeasurementBar> {
    let right_edge = (map.left + map.width).min(frame.width);
    let from = map.top + (BAR_ROWS_DIVS.0 * map.px_per_div_y) as usize;
    let to = (map.top + (BAR_ROWS_DIVS.1 * map.px_per_div_y) as usize).min(frame.height);
    // The area's own left edge is drawn in a grey like the border's.
    let starts = map.left + 4..(map.left + (BAR_START_DIVS * map.px_per_div_x) as usize).min(right_edge);
    let shortest = BAR_TILE_DIVS * 0.8 * map.px_per_div_x;
    let mut found: Option<(usize, usize)> = None;
    for y in from..to {
        let Some(start) = starts.clone().find(|&x| bar_border(frame.ycc(x, y))) else {
            continue;
        };
        let (mut end, mut gap) = (start, 0);
        for x in start..right_edge {
            if bar_border(frame.ycc(x, y)) {
                (end, gap) = (x, 0);
            } else {
                gap += 1;
                if gap > BAR_GAP_PX {
                    break;
                }
            }
        }
        let above = y.saturating_sub(BAR_CLEAR_ABOVE_PX);
        let grey_above = (start..=end).step_by(4).filter(|&x| bar_border(frame.ycc(x, above))).count();
        let clear_above = grey_above * 5 < (end - start) / 4 + 1;
        if (end - start) as f64 >= shortest && clear_above && found.is_none_or(|(_, right)| end > right) {
            found = Some((y, end));
        }
    }
    let (top, right) = found?;
    let divs = (right - map.left) as f64 / map.px_per_div_x;
    let tiles = ((divs - BAR_LEAD_DIVS) / BAR_TILE_DIVS).round().max(1.0) as usize;
    Some(MeasurementBar { tiles, top, right })
}

/// Which channel a pixel is drawn in, from its chroma.
pub struct Palette {
    /// Channel bit per (Cb / 4, Cr / 4).
    lut: Vec<u8>,
    min_luma: u8,
}

/// The MSO5000's channel colours: yellow, cyan, magenta, blue, as its own
/// screenshot draws them.
pub const MSO5000_COLOURS: [(u8, u8, u8); 4] = [
    (255, 255, 0),
    (0, 255, 255),
    (255, 0, 189),
    (0, 0, 255),
];

/// How far a pixel's hue may be from its channel's, in degrees of the
/// Cb/Cr plane.
const HUE_TOLERANCE_DEG: f64 = 25.0;
/// Below this distance from grey, a pixel has no colour to judge.
const MIN_CHROMA: f64 = 20.0;
/// Pure blue, channel 4, is luma 29, 22 once through an HDMI capture card
/// and half that where intensity grading dims it; black has no chroma, so
/// `MIN_CHROMA` is what keeps it out.
const MIN_LUMA: u8 = 8;

impl Palette {
    pub fn new(colours: &[(u8, u8, u8)]) -> Palette {
        let hues: Vec<f64> = colours
            .iter()
            .map(|&(r, g, b)| {
                let (_, cb, cr) = rgb_to_ycc(r, g, b);
                (f64::from(cr) - 128.0).atan2(f64::from(cb) - 128.0)
            })
            .collect();
        let mut lut = vec![0u8; 64 * 64];
        for cb4 in 0..64usize {
            for cr4 in 0..64usize {
                let cb = (cb4 * 4 + 2) as f64 - 128.0;
                let cr = (cr4 * 4 + 2) as f64 - 128.0;
                if cb.hypot(cr) < MIN_CHROMA {
                    continue;
                }
                let hue = cr.atan2(cb);
                let nearest = hues
                    .iter()
                    .enumerate()
                    .map(|(i, &h)| (i, angle_between(hue, h)))
                    .min_by(|a, b| a.1.total_cmp(&b.1));
                if let Some((channel, distance)) = nearest {
                    if distance <= HUE_TOLERANCE_DEG.to_radians() {
                        lut[cb4 * 64 + cr4] = 1 << channel;
                    }
                }
            }
        }
        Palette {
            lut,
            min_luma: MIN_LUMA,
        }
    }

    /// The bit of the channel this pixel belongs to, or zero.
    #[inline]
    pub fn classify(&self, (luma, cb, cr): (u8, u8, u8)) -> u8 {
        if luma < self.min_luma {
            return 0;
        }
        self.lut[usize::from(cb >> 2) * 64 + usize::from(cr >> 2)]
    }
}

fn angle_between(a: f64, b: f64) -> f64 {
    let d = (a - b).rem_euclid(std::f64::consts::TAU);
    d.min(std::f64::consts::TAU - d)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::oscilloscope::rigol::frame::Layout;

    /// A dotted grid at `spacing` about (`cx`, `cy`), with `with_border`
    /// deciding whether the outer lines are part of it.
    pub(crate) fn grid_frame(
        width: usize,
        height: usize,
        cx: usize,
        cy: usize,
        spacing_x: usize,
        spacing_y: usize,
        with_border: bool,
    ) -> VideoFrame {
        let mut frame = VideoFrame::blank(width, height, Layout::Ycc);
        let grey = (90, 128, 128);
        let reach_x = if with_border { 5 } else { 4 };
        let reach_y = if with_border { 4 } else { 3 };
        let top = cy - 4 * spacing_y;
        let bottom = cy + 4 * spacing_y;
        let left = cx - 5 * spacing_x;
        let right = cx + 5 * spacing_x;
        for k in -reach_x..=reach_x {
            let x = (cx as i64 + k * spacing_x as i64) as usize;
            for y in (top..=bottom).step_by(2) {
                frame.put(x, y, grey);
            }
        }
        for k in -reach_y..=reach_y {
            let y = (cy as i64 + k * spacing_y as i64) as usize;
            for x in (left..=right).step_by(2) {
                frame.put(x, y, grey);
            }
        }
        frame
    }

    #[test]
    fn the_waveform_area_is_measured_from_the_grid() {
        let without = VideoFrame::blank(1280, 720, Layout::Ycc);
        let with = grid_frame(1280, 720, 640, 360, 100, 75, false);
        let map = measure(&with, &without).unwrap();
        assert!((map.center_x - 640.0).abs() < 0.5, "{map:?}");
        assert!((map.center_y - 360.0).abs() < 0.5, "{map:?}");
        assert!((map.px_per_div_x - 100.0).abs() < 0.5, "{map:?}");
        assert!((map.px_per_div_y - 75.0).abs() < 0.5, "{map:?}");
        assert_eq!((map.left, map.width), (140, 1001));
        assert_eq!((map.top, map.height), (60, 601));
    }

    #[test]
    fn border_lines_that_change_too_give_the_same_area() {
        let without = VideoFrame::blank(1280, 720, Layout::Ycc);
        let with = grid_frame(1280, 720, 630, 350, 100, 75, true);
        let map = measure(&with, &without).unwrap();
        assert!((map.center_x - 630.0).abs() < 0.5, "{map:?}");
        assert!((map.center_y - 350.0).abs() < 0.5, "{map:?}");
    }

    #[test]
    fn centre_axes_drawn_stronger_than_the_grid_do_not_hide_it() {
        // The MSO5000 draws its centre axes solid, where the grid is dotted.
        let without = VideoFrame::blank(1024, 600, Layout::Ycc);
        let mut with = grid_frame(1024, 600, 522, 296, 100, 60, false);
        for y in 56..=536 {
            with.put(522, y, (90, 128, 128));
        }
        for x in 22..=1022 {
            with.put(x, 296, (90, 128, 128));
        }
        let map = measure(&with, &without).unwrap();
        assert!((map.center_x - 522.0).abs() < 0.5, "{map:?}");
        assert!((map.center_y - 296.0).abs() < 0.5, "{map:?}");
        assert!((map.px_per_div_x - 100.0).abs() < 0.5, "{map:?}");
        assert!((map.px_per_div_y - 60.0).abs() < 0.5, "{map:?}");
    }

    #[test]
    fn a_menu_over_the_right_of_the_grid_does_not_move_the_centre() {
        let without = VideoFrame::blank(1024, 600, Layout::Ycc);
        let mut with = grid_frame(1024, 600, 522, 296, 100, 60, false);
        for y in 56..=536 {
            with.put(522, y, (90, 128, 128));
        }
        // The menu hides everything right of x 825 in both frames.
        for y in 0..600 {
            for x in 826..1024 {
                with.put(x, y, (16, 128, 128));
            }
        }
        let map = measure(&with, &without).unwrap();
        assert!((map.center_x - 522.0).abs() < 0.5, "{map:?}");
        assert!((map.px_per_div_x - 100.0).abs() < 0.5, "{map:?}");
    }

    #[test]
    fn video_that_ignores_the_grid_is_not_this_scope() {
        let frame = grid_frame(1280, 720, 640, 360, 100, 75, false);
        let message = measure(&frame, &frame).unwrap_err().to_string();
        assert!(message.contains("not showing this scope"), "{message}");
    }

    #[test]
    fn video_that_changes_into_something_else_is_refused() {
        let without = VideoFrame::blank(320, 240, Layout::Ycc);
        let mut with = without.clone();
        for x in 0..320 {
            with.put(x, 100, (200, 128, 128));
        }
        assert!(measure(&with, &without).is_err());
    }

    #[test]
    fn each_channel_colour_is_its_own_channel_and_grey_is_none() {
        let palette = Palette::new(&MSO5000_COLOURS);
        for (channel, &(r, g, b)) in MSO5000_COLOURS.iter().enumerate() {
            assert_eq!(palette.classify(rgb_to_ycc(r, g, b)), 1 << channel, "channel {channel}");
            // Dimmer, as intensity grading draws rarely visited pixels.
            let dim = rgb_to_ycc(r / 2, g / 2, b / 2);
            assert_eq!(palette.classify(dim), 1 << channel, "dim channel {channel}");
        }
        assert_eq!(palette.classify(rgb_to_ycc(200, 200, 200)), 0);
        assert_eq!(palette.classify(rgb_to_ycc(10, 10, 0)), 0, "too dark to judge");
    }

    /// Frames from an MSO5074 through an MS2109 card at 1920x1080: the
    /// scope's 1280x720 output scaled up. Channels 1 to 4 at 1 V/div and
    /// offsets +3, +1, -1 and -3 V with no signal, so each trace lies on a
    /// grid line. The grid frames were taken after `:DISPlay:CLEar`, as
    /// calibration takes them; the `menu` pair with the side menu shown.
    fn hdmi_frame(jpeg: &[u8]) -> VideoFrame {
        let (width, height, data) = crate::oscilloscope::rigol::frame::decode_mjpeg(jpeg).unwrap();
        VideoFrame { width, height, layout: Layout::Ycc, data, captured_ns: 0, sequence: 1 }
    }

    fn hdmi_pair(menu: bool) -> (VideoFrame, VideoFrame) {
        if menu {
            (
                hdmi_frame(include_bytes!("../../../tests/fixtures/mso5074_ms2109_1080p_menu_grid.jpg")),
                hdmi_frame(include_bytes!("../../../tests/fixtures/mso5074_ms2109_1080p_menu_blank.jpg")),
            )
        } else {
            (
                hdmi_frame(include_bytes!("../../../tests/fixtures/mso5074_ms2109_1080p_grid.jpg")),
                hdmi_frame(include_bytes!("../../../tests/fixtures/mso5074_ms2109_1080p_blank.jpg")),
            )
        }
    }

    #[test]
    fn a_real_hdmi_frame_gives_the_grid_with_or_without_the_menu() {
        for menu in [false, true] {
            let (grid, blank) = hdmi_pair(menu);
            let map = measure(&grid, &blank).unwrap();
            // 100 x 60 px a division on the scope's own screen, scaled 1.5 times.
            assert!((map.px_per_div_x - 150.0).abs() < 1.0, "menu {menu}: {map:?}");
            assert!((map.px_per_div_y - 90.0).abs() < 1.0, "menu {menu}: {map:?}");
            assert!((map.center_x - 977.0).abs() < 2.0, "menu {menu}: {map:?}");
            assert!((map.center_y - 534.0).abs() < 2.0, "menu {menu}: {map:?}");
            assert_eq!(right_side_covered(&grid, &blank, &map), menu);
        }
    }

    #[test]
    fn a_real_hdmi_frame_puts_each_channel_at_its_offset() {
        let (grid, blank) = hdmi_pair(false);
        let map = measure(&grid, &blank).unwrap();
        let traces = hdmi_frame(include_bytes!("../../../tests/fixtures/mso5074_ms2109_1080p_traces.jpg"));
        let mut env = crate::oscilloscope::rigol::extract::Envelopes::default();
        env.extract(&traces, &map, &Palette::new(&MSO5000_COLOURS), 0b1111, None);
        assert_eq!(env.seen, 0b1111);
        for (channel, divs) in [3.0, 1.0, -1.0, -3.0].into_iter().enumerate() {
            let mut mids: Vec<f64> = env.samples[channel]
                .chunks_exact(2)
                .filter(|p| p[0] != i16::MIN)
                .map(|p| (f64::from(p[0]) + f64::from(p[1])) / 2.0)
                .collect();
            assert!(mids.len() * 100 >= map.width * 98, "channel {} drawn in {} of {} columns", channel + 1, mids.len(), map.width);
            mids.sort_by(f64::total_cmp);
            let median = mids[mids.len() / 2] / crate::oscilloscope::rigol::extract::COUNTS_PER_DIV;
            assert!((median - divs).abs() < 0.1, "channel {} at {median} div, not {divs}", channel + 1);
        }
    }

    /// The trigger-position marker inside the top of the area, at the
    /// trigger column: orange on a black screen (`traces`), and yellower
    /// on a 335 Hz 0-3.3 V square at 1 V/div whose picture the card washed
    /// with grey (`square`). Neither may read as CH1 clipping.
    #[test]
    fn the_trigger_marker_in_a_real_frame_is_not_clipping() {
        let (grid, blank) = hdmi_pair(false);
        let map = measure(&grid, &blank).unwrap();
        let trigger_column = map.center_x - map.left as f64;
        for (name, jpeg) in [
            ("traces", &include_bytes!("../../../tests/fixtures/mso5074_ms2109_1080p_traces.jpg")[..]),
            ("square", &include_bytes!("../../../tests/fixtures/mso5074_ms2109_1080p_square.jpg")[..]),
        ] {
            let frame = hdmi_frame(jpeg);
            let mut env = crate::oscilloscope::rigol::extract::Envelopes::default();
            let palette = Palette::new(&MSO5000_COLOURS);
            env.extract(&frame, &map, &palette, 0b0001, None);
            assert_eq!(env.clipped, 0b0001, "{name}: the marker should read as clipping unmasked");
            env.extract(&frame, &map, &palette, 0b0001, Some(trigger_column));
            assert_eq!(env.clipped, 0, "{name}: the marker read as clipping");
            let tops = env.samples[0]
                .chunks_exact(2)
                .filter(|p| p[1] != i16::MIN)
                .map(|p| f64::from(p[1]) / crate::oscilloscope::rigol::extract::COUNTS_PER_DIV)
                .fold(f64::MIN, f64::max);
            assert!(tops < 3.6, "{name}: CH1 reaches {tops} div, the marker's height");
        }
    }

    /// Three readouts (VPP, VMAX and FREQuency on CH1) in the measurement
    /// bar over a 335 Hz square, as `:MEASure:ITEM` leaves them.
    #[test]
    fn a_real_measurement_bar_is_found_and_its_readouts_counted() {
        let (grid, blank) = hdmi_pair(false);
        let map = measure(&grid, &blank).unwrap();
        let frame = hdmi_frame(include_bytes!("../../../tests/fixtures/mso5074_ms2109_1080p_three_measurements.jpg"));
        let bar = measurement_bar(&frame, &map).expect("the bar is found");
        assert_eq!(bar.tiles, 3, "{bar:?}");
        // Its top border is 7.2 divisions down, and it ends 3.3 across.
        let down = (bar.top - map.top) as f64 / map.px_per_div_y;
        assert!((down - 7.2).abs() < 0.1, "top border {down} div down");
        let across = (bar.right - map.left) as f64 / map.px_per_div_x;
        assert!((across - 3.32).abs() < 0.1, "right end {across} div across");
    }

    /// The readouts' CH1 labels are yellow: read as trace, they put CH1
    /// three divisions below a square that never goes under 0 V.
    #[test]
    fn the_measurement_bar_in_a_real_frame_is_not_read_as_trace() {
        let (grid, blank) = hdmi_pair(false);
        let map = measure(&grid, &blank).unwrap();
        let frame = hdmi_frame(include_bytes!("../../../tests/fixtures/mso5074_ms2109_1080p_three_measurements.jpg"));
        let bar = measurement_bar(&frame, &map);
        let palette = Palette::new(&MSO5000_COLOURS);
        let lowest = |bar| {
            let mut env = crate::oscilloscope::rigol::extract::Envelopes::default();
            env.extract_masked(&frame, &map, &palette, 0b0001, Some(map.center_x - map.left as f64), bar);
            env.samples[0]
                .chunks_exact(2)
                .filter(|p| p[0] != i16::MIN)
                .map(|p| f64::from(p[0]) / crate::oscilloscope::rigol::extract::COUNTS_PER_DIV)
                .fold(f64::MAX, f64::min)
        };
        assert!(lowest(None) < -2.0, "the labels should read as trace unmasked");
        let masked = lowest(bar);
        assert!(masked > -0.3, "CH1 reaches {masked} div with the bar left out");
    }

    #[test]
    fn a_real_frame_with_no_readout_has_no_measurement_bar() {
        let (grid, blank) = hdmi_pair(false);
        let map = measure(&grid, &blank).unwrap();
        for jpeg in [
            &include_bytes!("../../../tests/fixtures/mso5074_ms2109_1080p_square.jpg")[..],
            &include_bytes!("../../../tests/fixtures/mso5074_ms2109_1080p_traces.jpg")[..],
            &include_bytes!("../../../tests/fixtures/mso5074_ms2109_1080p_menu_blank.jpg")[..],
        ] {
            assert_eq!(measurement_bar(&hdmi_frame(jpeg), &map), None);
        }
    }

    #[test]
    fn a_real_square_wave_reads_its_levels() {
        let (grid, blank) = hdmi_pair(false);
        let map = measure(&grid, &blank).unwrap();
        let frame = hdmi_frame(include_bytes!("../../../tests/fixtures/mso5074_ms2109_1080p_square.jpg"));
        let mut env = crate::oscilloscope::rigol::extract::Envelopes::default();
        env.extract(&frame, &map, &Palette::new(&MSO5000_COLOURS), 0b0001, Some(map.center_x - map.left as f64));
        let mut mids: Vec<f64> = env.samples[0]
            .chunks_exact(2)
            .filter(|p| p[0] != i16::MIN && p[1] - p[0] < 1024)
            .map(|p| (f64::from(p[0]) + f64::from(p[1])) / 2.0 / crate::oscilloscope::rigol::extract::COUNTS_PER_DIV)
            .collect();
        mids.sort_by(f64::total_cmp);
        // Half the flat columns low, half high, at 1 V/div with offset 0.
        let (low, high) = (mids[mids.len() / 4], mids[mids.len() * 3 / 4]);
        assert!(low.abs() < 0.15, "low level at {low} div");
        assert!((high - 3.1).abs() < 0.2, "high level at {high} div");
    }
}

