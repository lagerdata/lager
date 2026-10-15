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
        bail!(
            "the video did not change when the scope's grid was switched on, so the capture \
             card is not showing this scope; check the HDMI cable, that the scope's HDMI output \
             is switched on (Utility > IO > HDMI > Output), and that the net names the capture \
             card the scope is plugged into"
        );
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

/// Which channel a pixel is drawn in, from its chroma.
pub struct Palette {
    /// Channel bit per (Cb / 4, Cr / 4).
    lut: Vec<u8>,
    min_luma: u8,
}

/// The MSO5000's channel colours: yellow, cyan, magenta, blue.
pub const MSO5000_COLOURS: [(u8, u8, u8); 4] = [
    (255, 255, 0),
    (0, 255, 255),
    (255, 0, 255),
    (0, 112, 255),
];

/// How far a pixel's hue may be from its channel's, in degrees of the
/// Cb/Cr plane.
const HUE_TOLERANCE_DEG: f64 = 25.0;
/// Below this distance from grey, a pixel has no colour to judge.
const MIN_CHROMA: f64 = 20.0;
const MIN_LUMA: u8 = 32;

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
}

