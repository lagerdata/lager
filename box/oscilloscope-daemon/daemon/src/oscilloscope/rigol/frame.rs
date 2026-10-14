// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! A decoded video frame, as luma and chroma.
//!
//! Both formats a capture card offers come down to Y, Cb and Cr per pixel
//! without converting to RGB: YUYV carries them directly, and an MJPEG frame
//! decodes to them. Channel colours are told apart by chroma alone, so
//! nothing downstream needs RGB.

/// How the bytes of a frame are laid out.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Layout {
    /// Y0 U Y1 V for each pair of pixels.
    Yuyv,
    /// Y Cb Cr for each pixel.
    Ycc,
}

#[derive(Debug, Clone)]
pub struct VideoFrame {
    pub width: usize,
    pub height: usize,
    pub layout: Layout,
    pub data: Vec<u8>,
    /// Monotonic nanoseconds when the frame came off the card.
    pub captured_ns: u64,
    /// Increments with every frame the source delivers.
    pub sequence: u64,
}

impl VideoFrame {
    pub fn blank(width: usize, height: usize, layout: Layout) -> VideoFrame {
        let mut frame = VideoFrame {
            width,
            height,
            layout,
            data: Vec::new(),
            captured_ns: 0,
            sequence: 0,
        };
        frame.data = match layout {
            Layout::Yuyv => [16, 128].repeat(width * height),
            Layout::Ycc => [16, 128, 128].repeat(width * height),
        };
        frame
    }

    /// Luma, Cb and Cr at one pixel.
    #[inline]
    pub fn ycc(&self, x: usize, y: usize) -> (u8, u8, u8) {
        match self.layout {
            Layout::Yuyv => {
                let row = y * self.width * 2;
                let pair = row + (x & !1) * 2;
                (self.data[row + x * 2], self.data[pair + 1], self.data[pair + 3])
            }
            Layout::Ycc => {
                let at = (y * self.width + x) * 3;
                (self.data[at], self.data[at + 1], self.data[at + 2])
            }
        }
    }

    #[inline]
    pub fn luma(&self, x: usize, y: usize) -> u8 {
        match self.layout {
            Layout::Yuyv => self.data[(y * self.width + x) * 2],
            Layout::Ycc => self.data[(y * self.width + x) * 3],
        }
    }

    /// Set one pixel, for building test frames. In YUYV the chroma is shared
    /// with the neighbouring pixel of the pair.
    pub fn put(&mut self, x: usize, y: usize, (luma, cb, cr): (u8, u8, u8)) {
        match self.layout {
            Layout::Yuyv => {
                let row = y * self.width * 2;
                let pair = row + (x & !1) * 2;
                self.data[row + x * 2] = luma;
                self.data[pair + 1] = cb;
                self.data[pair + 3] = cr;
            }
            Layout::Ycc => {
                let at = (y * self.width + x) * 3;
                self.data[at..at + 3].copy_from_slice(&[luma, cb, cr]);
            }
        }
    }
}

/// BT.601 full-range RGB to Y, Cb, Cr, which is what JPEG uses.
pub fn rgb_to_ycc(r: u8, g: u8, b: u8) -> (u8, u8, u8) {
    let (r, g, b) = (f32::from(r), f32::from(g), f32::from(b));
    let y = 0.299 * r + 0.587 * g + 0.114 * b;
    let cb = 128.0 - 0.168_736 * r - 0.331_264 * g + 0.5 * b;
    let cr = 128.0 + 0.5 * r - 0.418_688 * g - 0.081_312 * b;
    let clamp = |v: f32| v.round().clamp(0.0, 255.0) as u8;
    (clamp(y), clamp(cb), clamp(cr))
}

/// Decode one MJPEG frame to Y, Cb, Cr.
pub fn decode_mjpeg(jpeg: &[u8]) -> anyhow::Result<(usize, usize, Vec<u8>)> {
    use zune_jpeg::zune_core::bytestream::ZCursor;
    use zune_jpeg::zune_core::colorspace::ColorSpace;
    use zune_jpeg::zune_core::options::DecoderOptions;

    let options = DecoderOptions::default().jpeg_set_out_colorspace(ColorSpace::YCbCr);
    let mut decoder = zune_jpeg::JpegDecoder::new_with_options(ZCursor::new(jpeg), options);
    let pixels = decoder
        .decode()
        .map_err(|e| anyhow::anyhow!("decoding an MJPEG frame: {e:?}"))?;
    let info = decoder
        .info()
        .ok_or_else(|| anyhow::anyhow!("an MJPEG frame decoded with no header"))?;
    Ok((usize::from(info.width), usize::from(info.height), pixels))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn both_layouts_read_back_what_was_put() {
        for layout in [Layout::Yuyv, Layout::Ycc] {
            let mut frame = VideoFrame::blank(8, 4, layout);
            frame.put(3, 2, (200, 40, 220));
            assert_eq!(frame.ycc(3, 2), (200, 40, 220), "{layout:?}");
            assert_eq!(frame.luma(4, 2), 16, "{layout:?}");
        }
    }

    #[test]
    fn an_mjpeg_frame_decodes_to_luma_and_chroma() {
        // A yellow bar over x 20..=27, y 10..=37, on black.
        let jpeg = include_bytes!("../../../tests/fixtures/yellow_bar_64x48.jpg");
        let (width, height, data) = decode_mjpeg(jpeg).unwrap();
        assert_eq!((width, height), (64, 48));
        let frame = VideoFrame {
            width,
            height,
            layout: Layout::Ycc,
            data,
            captured_ns: 0,
            sequence: 1,
        };
        let (luma, cb, cr) = frame.ycc(23, 20);
        let (want_luma, want_cb, want_cr) = rgb_to_ycc(255, 255, 0);
        assert!(luma.abs_diff(want_luma) < 8, "luma {luma}");
        assert!(cb.abs_diff(want_cb) < 8, "cb {cb}");
        assert!(cr.abs_diff(want_cr) < 8, "cr {cr}");
        assert!(frame.luma(5, 5) < 8, "the background is black");
    }

    #[test]
    fn grey_has_no_chroma_and_yellow_has_plenty() {
        let (_, cb, cr) = rgb_to_ycc(128, 128, 128);
        assert_eq!((cb, cr), (128, 128));
        let (_, cb, _) = rgb_to_ycc(255, 255, 0);
        assert!(cb < 10);
    }
}
