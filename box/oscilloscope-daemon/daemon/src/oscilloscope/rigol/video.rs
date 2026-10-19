// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! The scope's HDMI output, through a USB capture card.
//!
//! A thread dequeues every buffer the card delivers and keeps only the
//! newest, so the acquisition loop always draws the current screen rather
//! than working through a queue of old ones. Frames are decoded only when
//! the loop takes one.

use std::path::{Path, PathBuf};
use std::sync::{Condvar, Mutex};
use std::time::Duration;

use anyhow::{bail, Context, Result};

use super::frame::{decode_bmp, decode_mjpeg, Layout, VideoFrame};

/// A frame as the card delivered it, before decoding.
#[derive(Debug, Clone)]
pub struct RawFrame {
    pub encoding: Encoding,
    pub width: usize,
    pub height: usize,
    pub bytes: Vec<u8>,
    pub sequence: u64,
    pub captured_ns: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Encoding {
    Mjpeg,
    Yuyv,
    /// The scope's own screenshot, read over SCPI.
    Bmp,
}

impl RawFrame {
    pub fn decode(&self) -> Result<VideoFrame> {
        let (width, height, layout, data) = match self.encoding {
            Encoding::Yuyv => {
                let expected = self.width * self.height * 2;
                if self.bytes.len() < expected {
                    bail!(
                        "a YUYV frame held {} bytes where {}x{} needs {expected}",
                        self.bytes.len(),
                        self.width,
                        self.height
                    );
                }
                (self.width, self.height, Layout::Yuyv, self.bytes[..expected].to_vec())
            }
            Encoding::Mjpeg => {
                let (width, height, data) = decode_mjpeg(&self.bytes)?;
                (width, height, Layout::Ycc, data)
            }
            Encoding::Bmp => {
                let (width, height, data) = decode_bmp(&self.bytes)?;
                (width, height, Layout::Ycc, data)
            }
        };
        Ok(VideoFrame {
            width,
            height,
            layout,
            data,
            captured_ns: self.captured_ns,
            sequence: self.sequence,
        })
    }
}

/// Where frames come from.
pub trait FrameSource: Send + Sync {
    /// The sequence number of the newest frame, zero before the first.
    fn newest(&self) -> Result<u64>;

    /// The newest frame once it is newer than `after`, waiting up to
    /// `timeout` for one.
    fn next_after(&self, after: u64, timeout: Duration) -> Result<Option<RawFrame>>;

    /// What is being captured, for logs and errors.
    fn describe(&self) -> String;

    /// Frames to let pass after a change before trusting the picture: a
    /// card has a frame or two in flight and the scope takes a moment to
    /// redraw.
    fn settle_frames(&self) -> u64 {
        3
    }

    /// Longest to wait for one frame.
    fn frame_wait(&self) -> Duration {
        Duration::from_secs(3)
    }
}

/// The newest frame, shared between the capture thread and the scope.
#[derive(Default)]
pub struct Latest {
    slot: Mutex<Slot>,
    arrived: Condvar,
}

#[derive(Default)]
struct Slot {
    frame: Option<RawFrame>,
    /// Why the capture thread stopped, once it has.
    failed: Option<String>,
}

impl Latest {
    pub fn store(&self, frame: RawFrame) {
        let mut slot = self.slot.lock().unwrap_or_else(|p| p.into_inner());
        slot.frame = Some(frame);
        self.arrived.notify_all();
    }

    pub fn fail(&self, reason: String) {
        let mut slot = self.slot.lock().unwrap_or_else(|p| p.into_inner());
        slot.failed = Some(reason);
        self.arrived.notify_all();
    }

    pub fn newest(&self) -> Result<u64> {
        let slot = self.slot.lock().unwrap_or_else(|p| p.into_inner());
        if let Some(reason) = &slot.failed {
            return Err(crate::oscilloscope::UnitGone(reason.clone()).into());
        }
        Ok(slot.frame.as_ref().map_or(0, |f| f.sequence))
    }

    pub fn next_after(&self, after: u64, timeout: Duration) -> Result<Option<RawFrame>> {
        let slot = self.slot.lock().unwrap_or_else(|p| p.into_inner());
        let (slot, _) = self
            .arrived
            .wait_timeout_while(slot, timeout, |slot| {
                slot.failed.is_none() && slot.frame.as_ref().is_none_or(|f| f.sequence <= after)
            })
            .unwrap_or_else(|p| p.into_inner());
        if let Some(reason) = &slot.failed {
            return Err(crate::oscilloscope::UnitGone(reason.clone()).into());
        }
        Ok(slot.frame.as_ref().filter(|f| f.sequence > after).cloned())
    }
}

/// One format the card offers.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Mode {
    pub encoding: Encoding,
    pub width: u32,
    pub height: u32,
    pub fps: f64,
}

/// The mode to capture in: the fastest, then the largest, preferring MJPEG
/// on a tie, among those big enough to resolve the waveform.
///
/// Fast first, because a capture card on USB 2 offers its full resolution
/// in YUYV at a few frames a second and the live view is only as good as its
/// rate. Large second, because the picture is scaled from the scope's own
/// screen and every pixel lost is resolution lost.
pub fn choose_mode(modes: &[Mode]) -> Option<Mode> {
    let big_enough: Vec<Mode> = modes.iter().copied().filter(|m| m.height >= 600).collect();
    let pool = if big_enough.is_empty() { modes.to_vec() } else { big_enough };
    pool.into_iter().max_by(|a, b| {
        let fps = |m: &Mode| (m.fps.min(60.0) * 10.0).round() as u64;
        fps(a)
            .cmp(&fps(b))
            .then((u64::from(a.width) * u64::from(a.height)).cmp(&(u64::from(b.width) * u64::from(b.height))))
            .then((a.encoding == Encoding::Mjpeg).cmp(&(b.encoding == Encoding::Mjpeg)))
    })
}

/// The capture node of the video device on USB port `port` (for example
/// `1-1.3`, as sysfs names it), under `sysfs` (normally `/sys`).
///
/// A card registers several nodes, and only the lowest-numbered capture
/// node streams; the others carry metadata.
pub fn node_for_port(sysfs: &Path, port: &str) -> Result<PathBuf> {
    let class = sysfs.join("class/video4linux");
    let mut found: Vec<(u32, String)> = Vec::new();
    // Every port with a video device on it, to say where the card went.
    let mut ports: Vec<String> = Vec::new();
    for entry in std::fs::read_dir(&class).with_context(|| format!("reading {}", class.display()))? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().to_string();
        let Some(index) = name.strip_prefix("video").and_then(|n| n.parse::<u32>().ok()) else {
            continue;
        };
        let device = std::fs::canonicalize(entry.path().join("device")).unwrap_or_default();
        if let Some(usb_port) = usb_port_of(&device) {
            if !ports.contains(&usb_port) {
                ports.push(usb_port);
            }
        }
        // The interface directory is `<port>:<config>.<interface>`, under the
        // port's own directory.
        let on_port = device.components().any(|c| {
            let c = c.as_os_str().to_string_lossy();
            c == port || c.split(':').next() == Some(port)
        });
        if on_port {
            let index_file = entry.path().join("index");
            let node_index = std::fs::read_to_string(&index_file)
                .ok()
                .and_then(|t| t.trim().parse::<u32>().ok())
                .unwrap_or(0);
            if node_index == 0 {
                found.push((index, name));
            }
        }
    }
    found.sort();
    match found.first() {
        Some((_, name)) => Ok(PathBuf::from("/dev").join(name)),
        None => {
            ports.sort();
            let elsewhere = if ports.is_empty() {
                "no video device is on any USB port".to_string()
            } else {
                format!("video devices are on USB ports {}", ports.join(", "))
            };
            bail!(
                "no capture card is on USB port {port}; check that it is plugged into the port \
                 the net was created with ({elsewhere})"
            )
        }
    }
}

/// The USB port a device's sysfs path is under: the port of its interface
/// directory, `<port>:<config>.<interface>`, such as `3-2.4.2` from
/// `3-2.4.2:1.0`.
fn usb_port_of(device: &Path) -> Option<String> {
    device.components().find_map(|c| {
        let c = c.as_os_str().to_string_lossy();
        let (port, interface) = c.split_once(':')?;
        let is_port = port.split_once('-').is_some_and(|(bus, path)| {
            !bus.is_empty()
                && bus.bytes().all(|b| b.is_ascii_digit())
                && !path.is_empty()
                && path.bytes().all(|b| b.is_ascii_digit() || b == b'.')
        });
        (is_port && interface.contains('.')).then(|| port.to_string())
    })
}

/// Where the capture card is: a device node, or `usb:<port>`.
pub fn resolve(spec: &str) -> Result<PathBuf> {
    match spec.strip_prefix("usb:") {
        Some(port) => node_for_port(Path::new("/sys"), port),
        None => Ok(PathBuf::from(spec)),
    }
}

/// Frames held in memory, for tests and offline replay.
pub struct Replay {
    latest: Latest,
    frames: Mutex<std::collections::VecDeque<RawFrame>>,
}

impl Replay {
    pub fn new(frames: Vec<RawFrame>) -> Replay {
        Replay {
            latest: Latest::default(),
            frames: Mutex::new(frames.into()),
        }
    }

    /// Deliver the next queued frame, as the card would.
    pub fn advance(&self) -> bool {
        let next = self.frames.lock().unwrap_or_else(|p| p.into_inner()).pop_front();
        match next {
            Some(frame) => {
                self.latest.store(frame);
                true
            }
            None => false,
        }
    }

    pub fn push(&self, frame: RawFrame) {
        self.frames.lock().unwrap_or_else(|p| p.into_inner()).push_back(frame);
    }
}

impl FrameSource for Replay {
    fn newest(&self) -> Result<u64> {
        self.latest.newest()
    }

    fn next_after(&self, after: u64, timeout: Duration) -> Result<Option<RawFrame>> {
        if self.latest.newest()? <= after {
            self.advance();
        }
        self.latest.next_after(after, timeout)
    }

    fn describe(&self) -> String {
        "replayed frames".into()
    }
}

#[cfg(target_os = "linux")]
pub use self::v4l2::V4l2Source;

/// How long a change on the scope takes to reach a capture card's video:
/// 190 to 330 ms measured from `*OPC?` on an MSO5074 into an MS2109, with
/// margin. `*OPC?` itself returns in under 30 ms, long before the redraw.
const HDMI_SETTLE: Duration = Duration::from_millis(400);

/// Frames a second, over windows of a second from the first frame counted.
/// Starting at the first frame matters: a window opened when streaming began
/// would average in the warm-up, when nothing is published, and report a
/// tenth of the real rate.
#[derive(Debug, Default)]
pub struct RateMeter {
    window: Option<(std::time::Instant, u64)>,
}

impl RateMeter {
    /// Count a frame at `now`; the rate, once a window has closed.
    pub fn tick(&mut self, now: std::time::Instant) -> Option<f64> {
        let (start, frames) = self.window.get_or_insert((now, 0));
        *frames += 1;
        let elapsed = now.duration_since(*start);
        if elapsed < Duration::from_secs(1) {
            return None;
        }
        let rate = (*frames - 1) as f64 / elapsed.as_secs_f64();
        self.window = Some((now, 1));
        Some(rate)
    }
}

/// Frames covering `HDMI_SETTLE` at `fps` frames a second.
pub fn settle_frames_at(fps: f64) -> u64 {
    let frames = (fps * HDMI_SETTLE.as_secs_f64()).ceil();
    if frames.is_finite() { (frames as u64).max(3) } else { 3 }
}

#[cfg(target_os = "linux")]
mod v4l2 {
    use super::*;
    use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
    use std::sync::Arc;

    use v4l::buffer::Type;
    use v4l::frameinterval::FrameIntervalEnum;
    use v4l::framesize::FrameSizeEnum;
    use v4l::io::mmap::Stream;
    use v4l::io::traits::CaptureStream;
    use v4l::video::Capture;
    use v4l::{Device, FourCC};

    const MJPG: &[u8; 4] = b"MJPG";
    const YUYV: &[u8; 4] = b"YUYV";
    /// Few buffers, so that a slow consumer is never handed a stale one.
    const BUFFERS: u32 = 3;
    /// An MS2109 sends black for its first 18 to 35 frames after streaming
    /// starts, whatever is on its input; published, those would calibrate
    /// against a blank screen.
    const WARMUP: Duration = Duration::from_secs(2);
    /// The same card fills every other buffer with an image-less JPEG, just
    /// a start and an end marker, which no decoder accepts.
    const MIN_IMAGE_BYTES: usize = 64;

    pub struct V4l2Source {
        path: PathBuf,
        mode: Mode,
        latest: Arc<Latest>,
        /// Frames published in the last second, in thousandths. A card's
        /// advertised rate is not what it delivers: an MS2109 at 60 fps
        /// fills every other buffer with nothing.
        delivered_milli_fps: Arc<AtomicU64>,
        stop: Arc<AtomicBool>,
        thread: Option<std::thread::JoinHandle<()>>,
    }

    impl Drop for V4l2Source {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::Relaxed);
            if let Some(thread) = self.thread.take() {
                let _ = thread.join();
            }
        }
    }

    fn modes(device: &Device) -> Result<Vec<Mode>> {
        let mut modes = Vec::new();
        for description in device.enum_formats().context("listing the capture formats")? {
            let encoding = match &description.fourcc.repr {
                repr if repr == MJPG => Encoding::Mjpeg,
                repr if repr == YUYV => Encoding::Yuyv,
                _ => continue,
            };
            for size in device.enum_framesizes(description.fourcc).unwrap_or_default() {
                let sizes: Vec<(u32, u32)> = match size.size {
                    FrameSizeEnum::Discrete(d) => vec![(d.width, d.height)],
                    FrameSizeEnum::Stepwise(s) => vec![(s.max_width, s.max_height)],
                };
                for (width, height) in sizes {
                    let intervals = device
                        .enum_frameintervals(description.fourcc, width, height)
                        .unwrap_or_default();
                    for interval in intervals {
                        let fraction = match interval.interval {
                            FrameIntervalEnum::Discrete(f) => f,
                            FrameIntervalEnum::Stepwise(s) => s.min,
                        };
                        if fraction.numerator == 0 {
                            continue;
                        }
                        modes.push(Mode {
                            encoding,
                            width,
                            height,
                            fps: f64::from(fraction.denominator) / f64::from(fraction.numerator),
                        });
                    }
                }
            }
        }
        Ok(modes)
    }

    impl V4l2Source {
        pub fn open(path: &Path) -> Result<V4l2Source> {
            let device = Device::with_path(path)
                .with_context(|| format!("opening the capture card at {}", path.display()))?;
            let offered = modes(&device)?;
            let mode = choose_mode(&offered).with_context(|| {
                format!(
                    "the capture card at {} offers neither MJPEG nor YUYV",
                    path.display()
                )
            })?;
            let mut format = device.format().context("reading the capture format")?;
            format.width = mode.width;
            format.height = mode.height;
            format.fourcc = FourCC::new(match mode.encoding {
                Encoding::Mjpeg => MJPG,
                Encoding::Yuyv => YUYV,
                Encoding::Bmp => unreachable!("a capture card offers no BMP mode"),
            });
            let format = device.set_format(&format).context("setting the capture format")?;
            let _ = device.set_params(&v4l::video::capture::Parameters::with_fps(
                mode.fps.round() as u32,
            ));
            let mode = Mode {
                width: format.width,
                height: format.height,
                ..mode
            };
            tracing::info!(
                device = %path.display(),
                encoding = ?mode.encoding,
                width = mode.width,
                height = mode.height,
                fps = mode.fps,
                "capturing the scope's screen"
            );

            let latest = Arc::new(Latest::default());
            let delivered_milli_fps = Arc::new(AtomicU64::new(0));
            let stop = Arc::new(AtomicBool::new(false));
            let thread = {
                let latest = latest.clone();
                let delivered = delivered_milli_fps.clone();
                let stop = stop.clone();
                std::thread::Builder::new()
                    .name("scope-video".into())
                    .spawn(move || capture(device, mode, &latest, &delivered, &stop))
                    .context("starting the video thread")?
            };
            Ok(V4l2Source {
                path: path.to_path_buf(),
                mode,
                latest,
                delivered_milli_fps,
                stop,
                thread: Some(thread),
            })
        }
    }

    fn capture(device: Device, mode: Mode, latest: &Latest, delivered: &AtomicU64, stop: &AtomicBool) {
        let mut stream = match Stream::with_buffers(&device, Type::VideoCapture, BUFFERS) {
            Ok(stream) => stream,
            Err(e) => {
                latest.fail(format!("the capture card would not stream: {e}"));
                return;
            }
        };
        // Bounded so a stop request is seen even with no signal on HDMI.
        stream.set_timeout(Duration::from_millis(500));
        let mut sequence = 0u64;
        let mut timeouts = 0u32;
        let started = std::time::Instant::now();
        let mut meter = RateMeter::default();
        while !stop.load(Ordering::Relaxed) {
            match stream.next() {
                Ok((bytes, meta)) => {
                    timeouts = 0;
                    let used = (meta.bytesused as usize).min(bytes.len());
                    if used < MIN_IMAGE_BYTES || started.elapsed() < WARMUP {
                        continue;
                    }
                    sequence += 1;
                    if let Some(fps) = meter.tick(std::time::Instant::now()) {
                        delivered.store((fps * 1000.0) as u64, Ordering::Relaxed);
                    }
                    latest.store(RawFrame {
                        encoding: mode.encoding,
                        width: mode.width as usize,
                        height: mode.height as usize,
                        bytes: bytes[..used].to_vec(),
                        sequence,
                        captured_ns: crate::oscilloscope::monotonic_ns(),
                    });
                }
                Err(e) if e.kind() == std::io::ErrorKind::TimedOut => {
                    timeouts += 1;
                    // Ten seconds of nothing: the scope is off, or its HDMI
                    // cable is out. Said once, then kept trying.
                    if timeouts == 20 {
                        tracing::warn!("no video from the capture card for 10 s");
                    }
                }
                Err(e) => {
                    latest.fail(format!("the capture card stopped: {e}"));
                    return;
                }
            }
        }
    }

    impl FrameSource for V4l2Source {
        fn newest(&self) -> Result<u64> {
            self.latest.newest()
        }

        fn next_after(&self, after: u64, timeout: Duration) -> Result<Option<RawFrame>> {
            self.latest.next_after(after, timeout)
        }

        fn settle_frames(&self) -> u64 {
            let delivered = self.delivered_milli_fps.load(Ordering::Relaxed);
            let fps = if delivered > 0 { delivered as f64 / 1000.0 } else { self.mode.fps };
            settle_frames_at(fps)
        }

        fn describe(&self) -> String {
            format!(
                "{} ({:?} {}x{} at {:.0} fps)",
                self.path.display(),
                self.mode.encoding,
                self.mode.width,
                self.mode.height,
                self.mode.fps
            )
        }
    }
}

/// Open the capture card `spec` names.
pub fn open(spec: &str) -> Result<Box<dyn FrameSource>> {
    let path = resolve(spec)?;
    #[cfg(target_os = "linux")]
    {
        Ok(Box::new(V4l2Source::open(&path)?))
    }
    #[cfg(not(target_os = "linux"))]
    {
        bail!(
            "capturing video needs V4L2, which only Linux has; cannot open {}",
            path.display()
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mode(encoding: Encoding, width: u32, height: u32, fps: f64) -> Mode {
        Mode {
            encoding,
            width,
            height,
            fps,
        }
    }

    #[test]
    fn the_rate_is_counted_from_the_first_frame_not_from_the_stream_start() {
        let mut meter = RateMeter::default();
        // The first frame arrives after a warm-up; the window starts there.
        let first = std::time::Instant::now() + Duration::from_secs(2);
        let mut rate = None;
        for i in 0..=31u32 {
            rate = meter.tick(first + Duration::from_micros(33_333) * i).or(rate);
        }
        let rate = rate.expect("a window closed after a second");
        assert!((rate - 30.0).abs() < 0.5, "{rate}");
    }

    #[test]
    fn settling_covers_the_hdmi_redraw_at_the_rate_actually_delivered() {
        // 30 frames a second: the MS2109's real rate at a nominal 60.
        assert_eq!(settle_frames_at(30.0), 12);
        assert_eq!(settle_frames_at(60.0), 24);
        assert_eq!(settle_frames_at(2.0), 3);
        assert_eq!(settle_frames_at(f64::NAN), 3);
    }

    #[test]
    fn the_fastest_large_mode_wins_over_a_slow_full_resolution_one() {
        let offered = [
            mode(Encoding::Yuyv, 1920, 1080, 5.0),
            mode(Encoding::Mjpeg, 1920, 1080, 30.0),
            mode(Encoding::Mjpeg, 1280, 720, 60.0),
            mode(Encoding::Yuyv, 640, 480, 60.0),
        ];
        assert_eq!(choose_mode(&offered), Some(offered[2]));
    }

    #[test]
    fn at_equal_rate_and_size_mjpeg_is_preferred() {
        let offered = [
            mode(Encoding::Yuyv, 1280, 720, 30.0),
            mode(Encoding::Mjpeg, 1280, 720, 30.0),
        ];
        assert_eq!(choose_mode(&offered), Some(offered[1]));
    }

    #[test]
    fn a_card_with_only_small_modes_still_gives_one() {
        let offered = [mode(Encoding::Yuyv, 640, 480, 30.0)];
        assert_eq!(choose_mode(&offered), Some(offered[0]));
        assert_eq!(choose_mode(&[]), None);
    }

    #[test]
    fn the_capture_node_on_a_port_is_found_and_its_metadata_node_skipped() {
        let root = std::env::temp_dir().join(format!("lager-v4l-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let devices = root.join("devices/pci0000:00/usb1/1-1/1-1.3");
        for (node, index, interface) in [("video2", "0", "1-1.3:1.0"), ("video3", "1", "1-1.3:1.0")] {
            let iface = devices.join(interface);
            std::fs::create_dir_all(&iface).unwrap();
            let class = root.join("class/video4linux").join(node);
            std::fs::create_dir_all(&class).unwrap();
            std::fs::write(class.join("index"), index).unwrap();
            std::os::unix::fs::symlink(&iface, class.join("device")).unwrap();
        }
        let other = root.join("devices/pci0000:00/usb1/1-1/1-1.4/1-1.4:1.0");
        std::fs::create_dir_all(&other).unwrap();
        let class = root.join("class/video4linux/video0");
        std::fs::create_dir_all(&class).unwrap();
        std::fs::write(class.join("index"), "0").unwrap();
        std::os::unix::fs::symlink(&other, class.join("device")).unwrap();

        assert_eq!(
            node_for_port(&root, "1-1.3").unwrap(),
            PathBuf::from("/dev/video2")
        );
        assert_eq!(
            node_for_port(&root, "1-1.4").unwrap(),
            PathBuf::from("/dev/video0")
        );
        let missing = format!("{:#}", node_for_port(&root, "1-1.5").unwrap_err());
        assert!(missing.contains("no capture card is on USB port 1-1.5"), "{missing}");
        assert!(missing.contains("video devices are on USB ports 1-1.3, 1-1.4"), "{missing}");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn the_newest_frame_is_handed_over_and_older_ones_are_skipped() {
        let raw = |sequence| RawFrame {
            encoding: Encoding::Yuyv,
            width: 2,
            height: 1,
            bytes: vec![16, 128, 16, 128],
            sequence,
            captured_ns: 0,
        };
        let latest = Latest::default();
        latest.store(raw(1));
        latest.store(raw(2));
        latest.store(raw(3));
        let got = latest.next_after(0, Duration::ZERO).unwrap().unwrap();
        assert_eq!(got.sequence, 3);
        assert!(latest.next_after(3, Duration::from_millis(5)).unwrap().is_none());
        latest.fail("unplugged".into());
        assert!(crate::oscilloscope::unit_is_gone(&latest.newest().unwrap_err()));
    }

    #[test]
    fn a_yuyv_frame_decodes_without_conversion() {
        let raw = RawFrame {
            encoding: Encoding::Yuyv,
            width: 2,
            height: 1,
            bytes: vec![200, 40, 210, 220],
            sequence: 1,
            captured_ns: 0,
        };
        let frame = raw.decode().unwrap();
        assert_eq!(frame.ycc(0, 0), (200, 40, 220));
        assert_eq!(frame.ycc(1, 0), (210, 40, 220));
    }
}
