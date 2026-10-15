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

/// Decode an uncompressed 24- or 32-bit BMP, as the scope's own screenshot
/// comes, to Y, Cb, Cr.
pub fn decode_bmp(bmp: &[u8]) -> anyhow::Result<(usize, usize, Vec<u8>)> {
    use anyhow::{bail, Context};

    let u16_at = |at: usize| bmp.get(at..at + 2).map(|b| u16::from_le_bytes([b[0], b[1]]));
    let u32_at = |at: usize| {
        bmp.get(at..at + 4)
            .map(|b| u32::from_le_bytes([b[0], b[1], b[2], b[3]]))
    };
    if bmp.get(..2) != Some(b"BM".as_slice()) {
        bail!(
            "the screenshot is not a BMP; it starts {:02x?}",
            &bmp[..bmp.len().min(8)]
        );
    }
    let short = || "the BMP header is cut short";
    let pixels_at = u32_at(10).context(short())? as usize;
    let width = u32_at(18).context(short())? as i32;
    let height = u32_at(22).context(short())? as i32;
    let bits = u16_at(28).context(short())?;
    let compression = u32_at(30).context(short())?;
    // BI_BITFIELDS (3) at 32 bits is still plain BGRA.
    if !(bits == 24 && compression == 0 || bits == 32 && matches!(compression, 0 | 3)) {
        bail!("the screenshot is a {bits}-bit BMP with compression {compression}; only 24- and 32-bit are read");
    }
    if width <= 0 || height == 0 {
        bail!("the screenshot is {width} x {height}");
    }
    let (width, rows) = (width as usize, height.unsigned_abs() as usize);
    // Rows run bottom up unless the height is negative.
    let bottom_up = height > 0;
    let bytes_per_pixel = usize::from(bits / 8);
    let stride = (width * bytes_per_pixel).div_ceil(4) * 4;
    let needed = pixels_at + stride * rows;
    if bmp.len() < needed {
        bail!("the screenshot holds {} bytes where its header needs {needed}", bmp.len());
    }
    let mut out = Vec::with_capacity(width * rows * 3);
    for y in 0..rows {
        let row = if bottom_up { rows - 1 - y } else { y };
        let start = pixels_at + row * stride;
        for pixel in bmp[start..start + width * bytes_per_pixel].chunks_exact(bytes_per_pixel) {
            let (luma, cb, cr) = rgb_to_ycc(pixel[2], pixel[1], pixel[0]);
            out.extend_from_slice(&[luma, cb, cr]);
        }
    }
    Ok((width, rows, out))
}

/// A 24-bit bottom-up BMP of `frame`, as the scope's screenshot is laid out.
#[cfg(test)]
pub fn encode_bmp(frame: &VideoFrame) -> Vec<u8> {
    let stride = (frame.width * 3).div_ceil(4) * 4;
    let size = 54 + stride * frame.height;
    let mut out = Vec::with_capacity(size);
    out.extend_from_slice(b"BM");
    out.extend_from_slice(&(size as u32).to_le_bytes());
    out.extend_from_slice(&[0; 4]);
    out.extend_from_slice(&54u32.to_le_bytes());
    out.extend_from_slice(&40u32.to_le_bytes());
    out.extend_from_slice(&(frame.width as i32).to_le_bytes());
    out.extend_from_slice(&(frame.height as i32).to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes());
    out.extend_from_slice(&24u16.to_le_bytes());
    out.extend_from_slice(&[0; 24]);
    for y in (0..frame.height).rev() {
        let start = out.len();
        for x in 0..frame.width {
            let (r, g, b) = ycc_to_rgb(frame.ycc(x, y));
            out.extend_from_slice(&[b, g, r]);
        }
        out.resize(start + stride, 0);
    }
    out
}

#[cfg(test)]
fn ycc_to_rgb((y, cb, cr): (u8, u8, u8)) -> (u8, u8, u8) {
    let (y, cb, cr) = (f32::from(y), f32::from(cb) - 128.0, f32::from(cr) - 128.0);
    let clamp = |v: f32| v.round().clamp(0.0, 255.0) as u8;
    (
        clamp(y + 1.402 * cr),
        clamp(y - 0.344_136 * cb - 0.714_136 * cr),
        clamp(y + 1.772 * cb),
    )
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
    fn a_bmp_screenshot_decodes_top_row_first() {
        let mut frame = VideoFrame::blank(5, 3, Layout::Ycc);
        let yellow = rgb_to_ycc(255, 255, 0);
        frame.put(1, 0, yellow);
        let (width, height, data) = decode_bmp(&encode_bmp(&frame)).unwrap();
        assert_eq!((width, height), (5, 3));
        let decoded = VideoFrame { width, height, layout: Layout::Ycc, data, captured_ns: 0, sequence: 1 };
        let (luma, cb, cr) = decoded.ycc(1, 0);
        assert!(luma.abs_diff(yellow.0) < 3 && cb.abs_diff(yellow.1) < 3 && cr.abs_diff(yellow.2) < 3);
        assert!(decoded.luma(1, 2) < 20, "the bottom row stays black");
    }

    #[test]
    fn a_short_or_foreign_screenshot_is_refused() {
        let bmp = encode_bmp(&VideoFrame::blank(4, 4, Layout::Ycc));
        assert!(decode_bmp(&bmp[..bmp.len() - 1]).is_err());
        assert!(decode_bmp(b"\x89PNG\r\n").is_err());
    }

    #[test]
    fn grey_has_no_chroma_and_yellow_has_plenty() {
        let (_, cb, cr) = rgb_to_ycc(128, 128, 128);
        assert_eq!((cb, cr), (128, 128));
        let (_, cb, _) = rgb_to_ycc(255, 255, 0);
        assert!(cb < 10);
    }
}
