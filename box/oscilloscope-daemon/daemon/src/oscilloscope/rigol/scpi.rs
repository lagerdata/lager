// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! SCPI to the instrument: USB-TMC through libusb, or a raw TCP socket.
//!
//! USB-TMC goes through libusb rather than `/dev/usbtmc*` because the box's
//! udev rules unbind the kernel driver from every SCPI instrument, so that
//! userspace can claim the interface. The protocol is small: each command is
//! a `DEV_DEP_MSG_OUT` on the bulk-out endpoint, and each reply is asked for
//! with a `REQUEST_DEV_DEP_MSG_IN` and read off bulk-in until a header says
//! end of message.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use anyhow::{bail, Context, Result};

use crate::oscilloscope::UnitGone;

/// Commands and queries, with the reply as text or as an IEEE 488.2 block.
pub trait Scpi: Send {
    fn write(&mut self, command: &str) -> Result<()>;
    /// One whole reply message, as sent.
    fn read_message(&mut self) -> Result<Vec<u8>>;

    /// How long a read may wait for the instrument.
    fn set_timeout(&mut self, _timeout: Duration) {}

    /// The most reply bytes to ask for in one transfer, where the link has
    /// transfers (USB-TMC); a reply longer than this arrives over several.
    fn set_max_transfer(&mut self, _bytes: u32) {}

    fn query(&mut self, command: &str) -> Result<String> {
        self.write(command)?;
        let reply = self.read_message()?;
        Ok(String::from_utf8_lossy(&reply).trim().to_string())
    }

    fn query_block(&mut self, command: &str) -> Result<Vec<u8>> {
        self.write(command)?;
        let reply = self.read_message()?;
        Ok(ieee_block(&reply)
            .with_context(|| format!("reading the reply to {command}"))?
            .to_vec())
    }
}

/// Where the instrument is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Address {
    Usb {
        vid: u16,
        pid: u16,
        serial: Option<String>,
    },
    Tcp {
        host: String,
        port: u16,
    },
}

impl Address {
    /// A VISA resource string, as nets store it
    /// (`USB0::0x1AB1::0x0515::MS5A0000000001::INSTR`,
    /// `TCPIP0::10.0.0.5::5555::SOCKET`), or the shorter `usb:1ab1:0515[:serial]`
    /// and `tcp:host[:port]`.
    pub fn parse(text: &str) -> Result<Address> {
        let text = text.trim();
        let lower = text.to_ascii_lowercase();
        if lower.starts_with("usb") && text.contains("::") {
            let parts: Vec<&str> = text.split("::").collect();
            if parts.len() < 3 {
                bail!("{text:?} is not a USB VISA address: it needs a vendor and a product ID");
            }
            let serial = parts
                .get(3)
                .filter(|s| !s.is_empty() && !s.eq_ignore_ascii_case("INSTR"))
                .map(|s| s.to_string());
            return Ok(Address::Usb {
                vid: hex_u16(parts[1])?,
                pid: hex_u16(parts[2])?,
                serial,
            });
        }
        if lower.starts_with("tcpip") && text.contains("::") {
            let parts: Vec<&str> = text.split("::").collect();
            let host = parts.get(1).filter(|h| !h.is_empty()).with_context(|| {
                format!("{text:?} is not a TCPIP VISA address: it names no host")
            })?;
            let port = match parts.get(2) {
                Some(p) if p.chars().all(|c| c.is_ascii_digit()) && !p.is_empty() => {
                    p.parse().with_context(|| format!("port {p:?} in {text:?}"))?
                }
                _ => RIGOL_SOCKET_PORT,
            };
            return Ok(Address::Tcp {
                host: host.to_string(),
                port,
            });
        }
        if let Some(rest) = lower.strip_prefix("usb:") {
            let rest = &text[text.len() - rest.len()..];
            let mut parts = rest.splitn(3, ':');
            let vid = hex_u16(parts.next().unwrap_or(""))?;
            let pid = hex_u16(parts.next().unwrap_or(""))?;
            let serial = parts.next().filter(|s| !s.is_empty()).map(str::to_string);
            return Ok(Address::Usb { vid, pid, serial });
        }
        if let Some(rest) = lower.strip_prefix("tcp:") {
            let rest = &text[text.len() - rest.len()..];
            let (host, port) = match rest.rsplit_once(':') {
                Some((host, port)) => (
                    host,
                    port.parse().with_context(|| format!("port {port:?} in {text:?}"))?,
                ),
                None => (rest, RIGOL_SOCKET_PORT),
            };
            if host.is_empty() {
                bail!("{text:?} names no host");
            }
            return Ok(Address::Tcp {
                host: host.to_string(),
                port,
            });
        }
        bail!(
            "{text:?} is not an instrument address; expected a VISA resource such as \
             USB0::0x1AB1::0x0515::<serial>::INSTR, or tcp:<host>:<port>"
        )
    }
}

/// The MSO5000's raw SCPI socket.
pub const RIGOL_SOCKET_PORT: u16 = 5555;

fn hex_u16(text: &str) -> Result<u16> {
    let digits = text
        .trim()
        .trim_start_matches("0x")
        .trim_start_matches("0X");
    u16::from_str_radix(digits, 16).with_context(|| format!("{text:?} is not a hexadecimal USB ID"))
}

/// The data inside an IEEE 488.2 definite-length block, `#NDDD<data>`, or
/// the indefinite form `#0<data>` that runs to the end of the message.
pub fn ieee_block(reply: &[u8]) -> Result<&[u8]> {
    let start = reply
        .iter()
        .position(|&b| b == b'#')
        .context("the reply holds no IEEE 488.2 block")?;
    let digits = *reply.get(start + 1).context("the block header is cut short")?;
    if !digits.is_ascii_digit() {
        bail!("the block header's digit count is {:?}", digits as char);
    }
    let digits = usize::from(digits - b'0');
    let body = start + 2;
    if digits == 0 {
        let end = reply
            .iter()
            .rposition(|&b| b != b'\n')
            .map_or(body, |last| last + 1)
            .max(body);
        return Ok(&reply[body..end]);
    }
    let length_text = reply
        .get(body..body + digits)
        .context("the block header is cut short")?;
    let length: usize = std::str::from_utf8(length_text)
        .ok()
        .and_then(|t| t.parse().ok())
        .with_context(|| format!("the block length {length_text:?} is not a number"))?;
    let data = body + digits;
    reply.get(data..data + length).with_context(|| {
        format!(
            "the block says {length} bytes but the reply holds {}",
            reply.len().saturating_sub(data)
        )
    })
}

// USB-TMC, from the USBTMC 1.0 specification.
const DEV_DEP_MSG_OUT: u8 = 1;
const REQUEST_DEV_DEP_MSG_IN: u8 = 2;
const DEV_DEP_MSG_IN: u8 = 2;
const HEADER: usize = 12;
const EOM: u8 = 0x01;
const INITIATE_CLEAR: u8 = 5;
const CHECK_CLEAR_STATUS: u8 = 6;
const STATUS_SUCCESS: u8 = 0x01;
const STATUS_PENDING: u8 = 0x02;
/// Bytes asked for per `REQUEST_DEV_DEP_MSG_IN` unless `set_max_transfer`
/// lowers it. A screenshot runs to a few megabytes, so it arrives over
/// several.
const READ_CHUNK: u32 = 1 << 20;
/// The longest reply `read_message` takes before deciding the instrument
/// will never end it.
const MAX_MESSAGE_BYTES: usize = 64 << 20;
/// Reads `read_transfer` discards ahead of a reply before giving up: more
/// than the packets of one unread 500-byte DS1000Z transfer.
const MAX_DISCARDED_READS: usize = 32;

/// The `bTag` sequence: 1 to 255, never 0, wrapping.
#[derive(Debug, Clone, Copy)]
pub struct Tag(u8);

impl Default for Tag {
    fn default() -> Self {
        Tag(0)
    }
}

impl Tag {
    pub fn next(&mut self) -> u8 {
        self.0 = if self.0 == 255 { 1 } else { self.0 + 1 };
        self.0
    }
}

/// A command as one bulk-out transfer, padded to four bytes.
pub fn dev_dep_msg_out(tag: u8, payload: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(HEADER + payload.len() + 3);
    out.extend_from_slice(&[DEV_DEP_MSG_OUT, tag, !tag, 0]);
    out.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    out.extend_from_slice(&[EOM, 0, 0, 0]);
    out.extend_from_slice(payload);
    while out.len() % 4 != 0 {
        out.push(0);
    }
    out
}

/// Ask for up to `max` bytes of the reply.
pub fn request_dev_dep_msg_in(tag: u8, max: u32) -> [u8; HEADER] {
    let mut out = [0u8; HEADER];
    out[..4].copy_from_slice(&[REQUEST_DEV_DEP_MSG_IN, tag, !tag, 0]);
    out[4..8].copy_from_slice(&max.to_le_bytes());
    out
}

/// A reply transfer's header: how many data bytes follow, and whether they
/// end the message.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InHeader {
    pub length: usize,
    pub end_of_message: bool,
}

pub fn parse_in_header(tag: u8, header: &[u8]) -> Result<InHeader> {
    if header.len() < HEADER {
        bail!("a USB-TMC reply of {} bytes is shorter than its header", header.len());
    }
    if header[0] != DEV_DEP_MSG_IN {
        bail!("expected a DEV_DEP_MSG_IN reply, got message ID {}", header[0]);
    }
    if header[1] != tag || header[2] != !tag {
        bail!(
            "the reply is tagged {} where {tag} was asked for; the instrument is answering \
             an earlier request",
            header[1]
        );
    }
    Ok(InHeader {
        length: u32::from_le_bytes([header[4], header[5], header[6], header[7]]) as usize,
        end_of_message: header[8] & EOM != 0,
    })
}

/// The bulk endpoints, which is all the framing needs from USB.
pub trait Bulk: Send {
    fn bulk_out(&mut self, data: &[u8], timeout: Duration) -> Result<usize>;
    fn bulk_in(&mut self, buffer: &mut [u8], timeout: Duration) -> Result<usize>;
    fn max_packet(&self) -> usize;

    /// Bring the device back after a transfer that failed part way: an
    /// instrument left holding a request it will never answer answers
    /// nothing after it either.
    fn recover(&mut self) {}

    /// Reset the device's USB port and claim its interface again, for an
    /// instrument that answers nothing at all.
    fn reset(&mut self) -> Result<()> {
        Ok(())
    }
}

/// USB-TMC over any pair of bulk endpoints.
pub struct UsbTmc<B: Bulk> {
    bulk: B,
    tag: Tag,
    pub timeout: Duration,
    max_transfer: u32,
}

impl<B: Bulk> UsbTmc<B> {
    pub fn new(bulk: B) -> Self {
        UsbTmc {
            bulk,
            tag: Tag::default(),
            timeout: DEFAULT_TIMEOUT,
            max_transfer: READ_CHUNK,
        }
    }

    /// Get the instrument answering again after a process that died with
    /// a query sent and its reply never asked for. A DS1000Z then answers
    /// neither that query nor the next one: the first command after a
    /// restart timed out. A throwaway `*OPC?` takes that loss, and a failed
    /// read clears the instrument, so the second answers. Whether either did.
    fn resync(&mut self) -> bool {
        let timeout = self.timeout;
        self.timeout = RESYNC_TIMEOUT;
        let mut answered = false;
        for attempt in 1..=2 {
            match self.query("*OPC?") {
                Ok(_) => {
                    answered = true;
                    break;
                }
                Err(e) => tracing::debug!(attempt, error = %e, "resyncing the USB-TMC link"),
            }
        }
        self.timeout = timeout;
        answered
    }

    /// `resync`, then a USB port reset when the instrument still answers
    /// nothing. A DS1054Z plugged back in while the daemon kept trying to
    /// open it timed out on bulk-out as well as bulk-in, through clears and
    /// re-claims, for minutes; one port reset brought it back.
    fn settle(&mut self) -> Result<()> {
        if self.resync() {
            return Ok(());
        }
        tracing::warn!("the instrument answers nothing on USB; resetting its port");
        self.bulk.reset()?;
        self.resync();
        Ok(())
    }

    /// One reply transfer: what one `REQUEST_DEV_DEP_MSG_IN` returns.
    fn read_transfer(&mut self, into: &mut Vec<u8>) -> Result<bool> {
        let tag = self.tag.next();
        let request = request_dev_dep_msg_in(tag, self.max_transfer);
        self.bulk.bulk_out(&request, self.timeout)?;

        let packet = self.bulk.max_packet().max(64);
        let mut buffer = vec![0u8; HEADER + self.max_transfer as usize + packet];
        let mut got = self.bulk.bulk_in(&mut buffer, self.timeout)?;
        // What arrives ahead of this reply's header is not this reply. A
        // full-speed DS1000Z sends the zero-length packet that closes a
        // transfer ending on a packet boundary only after the transfer has
        // been read, so it turns up here; and it sends the unread rest of a
        // reply whose reader died ahead of the next reply, through
        // INITIATE_CLEAR and into the next process. Packets are discarded
        // until this reply's header, which carries its tag, comes.
        let mut discarded = 0;
        let header = loop {
            match parse_in_header(tag, &buffer[..got]) {
                Ok(header) => break header,
                Err(e) if discarded >= MAX_DISCARDED_READS => return Err(e),
                Err(e) => {
                    if got > 0 {
                        tracing::debug!(
                            bytes = got,
                            error = %e,
                            "discarding what came ahead of a reply"
                        );
                    }
                    discarded += 1;
                    got = self.bulk.bulk_in(&mut buffer, self.timeout)?;
                }
            }
        };
        let mut have = got - HEADER;
        // A transfer ends at a short packet; one that filled the buffer
        // exactly is followed by more.
        let mut last = got;
        while have < header.length && last > 0 && last % packet == 0 {
            let start = HEADER + have;
            last = self.bulk.bulk_in(&mut buffer[start..], self.timeout)?;
            have += last;
        }
        if have < header.length {
            bail!(
                "the instrument sent {have} of the {} bytes it announced",
                header.length
            );
        }
        into.extend_from_slice(&buffer[HEADER..HEADER + header.length]);
        Ok(header.end_of_message)
    }
}

/// Long enough for a screenshot or a full waveform record over USB 2.
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(5);
/// An idle instrument answers `*OPC?` within milliseconds.
const RESYNC_TIMEOUT: Duration = Duration::from_millis(500);

impl<B: Bulk> Scpi for UsbTmc<B> {
    fn set_timeout(&mut self, timeout: Duration) {
        self.timeout = timeout;
    }

    fn set_max_transfer(&mut self, bytes: u32) {
        self.max_transfer = bytes.clamp(1, READ_CHUNK);
    }

    fn write(&mut self, command: &str) -> Result<()> {
        let mut payload = command.as_bytes().to_vec();
        if !payload.ends_with(b"\n") {
            payload.push(b'\n');
        }
        let tag = self.tag.next();
        let transfer = dev_dep_msg_out(tag, &payload);
        self.bulk
            .bulk_out(&transfer, self.timeout)
            .with_context(|| format!("sending {command}"))?;
        Ok(())
    }

    fn read_message(&mut self) -> Result<Vec<u8>> {
        let mut message = Vec::new();
        // Bounded so a device that never says end of message cannot hold
        // the hardware thread forever.
        while message.len() < MAX_MESSAGE_BYTES {
            match self.read_transfer(&mut message) {
                Ok(true) => return Ok(message),
                Ok(false) => {}
                // A query the instrument does not answer (one it does not
                // know, say) times out with the request still outstanding.
                Err(e) if e.downcast_ref::<UnitGone>().is_none() => {
                    self.bulk.recover();
                    return Err(e);
                }
                Err(e) => return Err(e),
            }
        }
        self.bulk.recover();
        bail!("the reply ran past 64 MB without an end of message")
    }
}

/// SCPI over the instrument's raw socket. Replies end with a newline, and a
/// block reply is read to the length its header gives.
pub struct TcpScpi {
    stream: TcpStream,
}

impl TcpScpi {
    pub fn connect(host: &str, port: u16) -> Result<TcpScpi> {
        let stream = TcpStream::connect((host, port))
            .with_context(|| format!("connecting to the instrument at {host}:{port}"))?;
        stream.set_read_timeout(Some(DEFAULT_TIMEOUT))?;
        stream.set_write_timeout(Some(DEFAULT_TIMEOUT))?;
        stream.set_nodelay(true)?;
        Ok(TcpScpi { stream })
    }
}

impl Scpi for TcpScpi {
    fn set_timeout(&mut self, timeout: Duration) {
        let _ = self.stream.set_read_timeout(Some(timeout));
    }

    fn write(&mut self, command: &str) -> Result<()> {
        let mut line = command.as_bytes().to_vec();
        if !line.ends_with(b"\n") {
            line.push(b'\n');
        }
        self.stream
            .write_all(&line)
            .map_err(socket_error)
            .with_context(|| format!("sending {command}"))
    }

    fn read_message(&mut self) -> Result<Vec<u8>> {
        let mut message = Vec::new();
        let mut byte = [0u8; 1];
        loop {
            let n = self.stream.read(&mut byte).map_err(socket_error)?;
            if n == 0 {
                return Err(UnitGone("the instrument closed its socket".into()).into());
            }
            message.push(byte[0]);
            if message.len() == 2 && message[0] == b'#' {
                return read_socket_block(&mut self.stream, message);
            }
            if byte[0] == b'\n' {
                return Ok(message);
            }
        }
    }
}

fn read_socket_block(stream: &mut TcpStream, mut message: Vec<u8>) -> Result<Vec<u8>> {
    let digits = usize::from(message[1].wrapping_sub(b'0'));
    if digits == 0 || digits > 9 {
        bail!("the socket returned an indefinite or malformed block");
    }
    let mut length_text = vec![0u8; digits];
    stream.read_exact(&mut length_text).map_err(socket_error)?;
    message.extend_from_slice(&length_text);
    let length: usize = std::str::from_utf8(&length_text)
        .ok()
        .and_then(|t| t.parse().ok())
        .context("the block length is not a number")?;
    let start = message.len();
    // The data, then the newline that ends the reply.
    message.resize(start + length + 1, 0);
    stream.read_exact(&mut message[start..]).map_err(socket_error)?;
    Ok(message)
}

fn socket_error(error: std::io::Error) -> anyhow::Error {
    use std::io::ErrorKind::*;
    match error.kind() {
        ConnectionReset | ConnectionAborted | BrokenPipe | UnexpectedEof => {
            UnitGone(format!("the instrument's socket failed: {error}")).into()
        }
        _ => error.into(),
    }
}

/// The bulk endpoints of a claimed USB-TMC interface.
pub struct RusbBulk {
    handle: rusb::DeviceHandle<rusb::Context>,
    interface: u8,
    ep_in: u8,
    ep_out: u8,
    max_packet: usize,
}

impl Drop for RusbBulk {
    fn drop(&mut self) {
        let _ = self.handle.release_interface(self.interface);
    }
}

fn usb_error(error: rusb::Error) -> anyhow::Error {
    match error {
        rusb::Error::NoDevice | rusb::Error::Io => {
            UnitGone(format!("the instrument stopped answering on USB: {error}")).into()
        }
        rusb::Error::Busy | rusb::Error::Access => anyhow::anyhow!(
            "{error}: another process holds the instrument's USB-TMC interface; only the \
             scope daemon may talk to it"
        ),
        other => other.into(),
    }
}

impl Bulk for RusbBulk {
    fn bulk_out(&mut self, data: &[u8], timeout: Duration) -> Result<usize> {
        let sent = self
            .handle
            .write_bulk(self.ep_out, data, timeout)
            .map_err(usb_error)?;
        if sent != data.len() {
            bail!("USB-TMC bulk-out sent {sent} of {} bytes", data.len());
        }
        Ok(sent)
    }

    fn bulk_in(&mut self, buffer: &mut [u8], timeout: Duration) -> Result<usize> {
        self.handle
            .read_bulk(self.ep_in, buffer, timeout)
            .map_err(usb_error)
    }

    fn max_packet(&self) -> usize {
        self.max_packet
    }

    fn recover(&mut self) {
        tracing::warn!("a USB-TMC read failed part way; clearing the instrument");
        self.initiate_clear();
        self.clear_in_halt();
        self.drain();
    }

    /// The reset returns both ends' data toggles to DATA0, so no clear is
    /// needed after it. A device that re-enumerates instead has gone, and
    /// the next open finds it again.
    fn reset(&mut self) -> Result<()> {
        let _ = self.handle.release_interface(self.interface);
        self.handle
            .reset()
            .map_err(usb_error)
            .context("resetting the instrument's USB port")?;
        self.handle
            .claim_interface(self.interface)
            .map_err(usb_error)
            .context("claiming the USB-TMC interface after a port reset")?;
        Ok(())
    }
}

const USBTMC_CLASS: u8 = 0xFE;
const USBTMC_SUBCLASS: u8 = 0x03;

/// Open the USB-TMC interface of the instrument matching `vid`, `pid` and,
/// when given, `serial`, and bring it back from wherever a crashed process
/// left it.
pub fn open_usb(vid: u16, pid: u16, serial: Option<&str>) -> Result<UsbTmc<RusbBulk>> {
    use rusb::UsbContext;

    let context = rusb::Context::new().context("starting libusb")?;
    let mut seen = Vec::new();
    for device in context.devices().context("listing USB devices")?.iter() {
        let Ok(descriptor) = device.device_descriptor() else {
            continue;
        };
        if descriptor.vendor_id() != vid || descriptor.product_id() != pid {
            continue;
        }
        let handle = device.open().map_err(usb_error).with_context(|| {
            format!("opening USB device {vid:04x}:{pid:04x}")
        })?;
        let found_serial = handle
            .read_serial_number_string_ascii(&descriptor)
            .unwrap_or_default();
        if let Some(wanted) = serial {
            if !found_serial.eq_ignore_ascii_case(wanted) {
                seen.push(found_serial);
                continue;
            }
        }
        return claim(device, handle, &descriptor);
    }
    bail!(not_attached(vid, pid, serial, &seen))
}

/// Why no instrument matched, naming what was found instead. A serial that
/// could not be read is counted rather than listed: as an empty string it
/// ended the message on "found serials " and nothing after.
fn not_attached(vid: u16, pid: u16, serial: Option<&str>, seen: &[String]) -> String {
    let which = match serial {
        Some(serial) => format!("{vid:04x}:{pid:04x} with serial {serial}"),
        None => format!("{vid:04x}:{pid:04x}"),
    };
    if seen.is_empty() {
        return format!("no USB instrument {which} is attached; check its USB cable to the box");
    }
    let readable: Vec<&str> = seen.iter().map(String::as_str).filter(|s| !s.is_empty()).collect();
    let unreadable = seen.len() - readable.len();
    let mut found = Vec::new();
    if !readable.is_empty() {
        found.push(format!("serials {}", readable.join(", ")));
    }
    if unreadable > 0 {
        found.push(format!("{unreadable} whose serial could not be read"));
    }
    format!("no USB instrument {which} is attached; found {}", found.join(" and "))
}

fn claim(
    device: rusb::Device<rusb::Context>,
    handle: rusb::DeviceHandle<rusb::Context>,
    descriptor: &rusb::DeviceDescriptor,
) -> Result<UsbTmc<RusbBulk>> {
    for index in 0..descriptor.num_configurations() {
        let Ok(config) = device.config_descriptor(index) else {
            continue;
        };
        for interface in config.interfaces() {
            for setting in interface.descriptors() {
                if setting.class_code() != USBTMC_CLASS || setting.sub_class_code() != USBTMC_SUBCLASS
                {
                    continue;
                }
                let mut ep_in = None;
                let mut ep_out = None;
                let mut max_packet = 64;
                for endpoint in setting.endpoint_descriptors() {
                    if endpoint.transfer_type() != rusb::TransferType::Bulk {
                        continue;
                    }
                    match endpoint.direction() {
                        rusb::Direction::In => {
                            ep_in = Some(endpoint.address());
                            max_packet = usize::from(endpoint.max_packet_size());
                        }
                        rusb::Direction::Out => ep_out = Some(endpoint.address()),
                    }
                }
                let (Some(ep_in), Some(ep_out)) = (ep_in, ep_out) else {
                    continue;
                };
                let number = setting.interface_number();
                // Not supported on every platform, and needless where udev
                // already unbound usbtmc.
                let _ = handle.set_auto_detach_kernel_driver(true);
                handle
                    .claim_interface(number)
                    .map_err(usb_error)
                    .context("claiming the USB-TMC interface")?;
                let _ = handle.clear_halt(ep_out);
                let mut bulk = RusbBulk {
                    handle,
                    interface: number,
                    ep_in,
                    ep_out,
                    max_packet,
                };
                bulk.clear_in_halt();
                bulk.initiate_clear();
                bulk.drain();
                let mut tmc = UsbTmc::new(bulk);
                tmc.settle()?;
                return Ok(tmc);
            }
        }
    }
    bail!("the instrument has no USB-TMC interface")
}

impl RusbBulk {
    /// Discard anything a previous process left half-sent or half-read.
    /// Best effort: an instrument that refuses still works if nothing was
    /// left in flight.
    fn initiate_clear(&mut self) {
        let request_type = rusb::request_type(
            rusb::Direction::In,
            rusb::RequestType::Class,
            rusb::Recipient::Interface,
        );
        let timeout = Duration::from_secs(1);
        let mut status = [0u8; 1];
        let started = self.handle.read_control(
            request_type,
            INITIATE_CLEAR,
            0,
            u16::from(self.interface),
            &mut status,
            timeout,
        );
        if !matches!(started, Ok(1)) || status[0] != STATUS_SUCCESS {
            tracing::debug!(?started, status = status[0], "USB-TMC INITIATE_CLEAR not accepted");
            return;
        }
        for _ in 0..50 {
            let mut reply = [0u8; 2];
            let checked = self.handle.read_control(
                request_type,
                CHECK_CLEAR_STATUS,
                0,
                u16::from(self.interface),
                &mut reply,
                timeout,
            );
            if !matches!(checked, Ok(n) if n >= 1) || reply[0] != STATUS_PENDING {
                break;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        let _ = self.handle.clear_halt(self.ep_out);
    }

    /// Clear bulk-in only if it is halted. Clearing resets the host's data
    /// toggle, and a DS1000Z does not reset its own: the host then takes the
    /// scope's next packet for a repeat and drops it, so the next reply
    /// loses its first 64 bytes, header and all.
    fn clear_in_halt(&mut self) {
        const GET_STATUS: u8 = 0x00;
        const HALTED: u8 = 0x01;
        let request_type = rusb::request_type(
            rusb::Direction::In,
            rusb::RequestType::Standard,
            rusb::Recipient::Endpoint,
        );
        let mut status = [0u8; 2];
        let read = self.handle.read_control(
            request_type,
            GET_STATUS,
            0,
            u16::from(self.ep_in),
            &mut status,
            Duration::from_secs(1),
        );
        // An endpoint whose status cannot be read is cleared as before.
        if !matches!(read, Ok(2)) || status[0] & HALTED != 0 {
            let _ = self.handle.clear_halt(self.ep_in);
        }
    }

    /// Discard reply data still queued on bulk-in. A DS1000Z keeps the rest
    /// of a reply nobody finished reading through INITIATE_CLEAR, and even
    /// into the next process to open it, which then reads that data as the
    /// header of its own first reply.
    fn drain(&mut self) {
        // A queued packet is there at once; 50 ms only bounds the wait when
        // the pipe is already empty. 64 packets is more than a stalled
        // transfer leaves behind.
        let timeout = Duration::from_millis(50);
        let mut buffer = vec![0u8; self.max_packet.max(64)];
        let mut discarded = 0;
        for _ in 0..64 {
            match self.handle.read_bulk(self.ep_in, &mut buffer, timeout) {
                // A zero-length packet may sit between two stale transfers.
                Ok(n) => discarded += n,
                Err(_) => break,
            }
        }
        if discarded > 0 {
            tracing::debug!(bytes = discarded, "discarded stale USB-TMC reply data");
        }
    }
}

/// Open whatever `address` names.
pub fn open(address: &Address) -> Result<Box<dyn Scpi>> {
    Ok(match address {
        Address::Usb { vid, pid, serial } => Box::new(open_usb(*vid, *pid, serial.as_deref())?),
        Address::Tcp { host, port } => Box::new(TcpScpi::connect(host, *port)?),
    })
}

/// The cross-process lock the box's Python drivers take before opening an
/// instrument (`lager.util.device_lock`), held here for as long as the
/// daemon has the instrument: an ad-hoc script must not claim the interface
/// out from under it.
pub struct DeviceLock {
    _file: std::fs::File,
}

impl DeviceLock {
    pub fn path(address: &str) -> std::path::PathBuf {
        let safe: String = address
            .chars()
            .map(|c| {
                if c.is_ascii_alphanumeric() || c == '_' || c == '-' {
                    c
                } else {
                    '_'
                }
            })
            .collect();
        std::env::temp_dir()
            .join("lager_device_locks")
            .join(format!("device_{safe}.lock"))
    }

    pub fn acquire(address: &str, timeout: Duration) -> Result<DeviceLock> {
        use std::os::unix::io::AsRawFd;

        let path = Self::path(address);
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)
                .with_context(|| format!("creating {}", dir.display()))?;
        }
        let mut file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(&path)
            .with_context(|| format!("opening {}", path.display()))?;
        let deadline = std::time::Instant::now() + timeout;
        loop {
            // SAFETY: flock on a descriptor this function owns.
            let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
            if rc == 0 {
                break;
            }
            if std::time::Instant::now() >= deadline {
                let mut holder = String::new();
                let _ = file.read_to_string(&mut holder);
                bail!(
                    "another process (pid {}) holds the lock on {address}; stop it so the \
                     scope daemon can open the instrument",
                    holder.trim()
                );
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        // Recorded as the Python lock does, so its refusal can name us.
        let _ = file.set_len(0);
        let _ = write!(file, "{}", std::process::id());
        let _ = file.flush();
        Ok(DeviceLock { _file: file })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;

    #[test]
    fn a_missing_instrument_names_what_was_found_and_counts_unreadable_serials() {
        let wanted = Some("MS5A1");
        let found = |seen: &[&str]| {
            let seen: Vec<String> = seen.iter().map(|s| s.to_string()).collect();
            not_attached(0x1ab1, 0x0515, wanted, &seen)
        };
        assert!(found(&[]).ends_with("check its USB cable to the box"));
        assert!(found(&["MS5A2"]).ends_with("found serials MS5A2"));
        let unread = found(&[""]);
        assert!(unread.ends_with("found 1 whose serial could not be read"), "{unread}");
        assert!(
            found(&["MS5A2", ""]).ends_with("found serials MS5A2 and 1 whose serial could not be read")
        );
    }

    #[test]
    fn visa_usb_addresses_parse_with_and_without_a_serial() {
        assert_eq!(
            Address::parse("USB0::0x1AB1::0x0515::MS5A123456789::INSTR").unwrap(),
            Address::Usb {
                vid: 0x1ab1,
                pid: 0x0515,
                serial: Some("MS5A123456789".into())
            }
        );
        assert_eq!(
            Address::parse("USB0::0x1AB1::0x0515::INSTR").unwrap(),
            Address::Usb {
                vid: 0x1ab1,
                pid: 0x0515,
                serial: None
            }
        );
        assert_eq!(
            Address::parse("usb:1ab1:0515").unwrap(),
            Address::Usb {
                vid: 0x1ab1,
                pid: 0x0515,
                serial: None
            }
        );
    }

    #[test]
    fn tcp_addresses_default_to_the_raw_socket() {
        assert_eq!(
            Address::parse("TCPIP0::10.0.0.5::INSTR").unwrap(),
            Address::Tcp {
                host: "10.0.0.5".into(),
                port: 5555
            }
        );
        assert_eq!(
            Address::parse("tcp:scope.local:5025").unwrap(),
            Address::Tcp {
                host: "scope.local".into(),
                port: 5025
            }
        );
        assert!(Address::parse("/dev/usbtmc0").is_err());
    }

    #[test]
    fn a_definite_block_is_cut_to_its_length() {
        assert_eq!(ieee_block(b"#15hello\n").unwrap(), b"hello");
        assert_eq!(ieee_block(b"#210abcdefghij").unwrap(), b"abcdefghij");
        assert!(ieee_block(b"#19short").is_err());
        assert!(ieee_block(b"1.5e-3\n").is_err());
    }

    #[test]
    fn an_indefinite_block_runs_to_the_end_of_the_message() {
        assert_eq!(ieee_block(b"#0abc\n").unwrap(), b"abc");
    }

    #[test]
    fn tags_skip_zero_when_they_wrap() {
        let mut tag = Tag(254);
        assert_eq!(tag.next(), 255);
        assert_eq!(tag.next(), 1);
    }

    #[test]
    fn a_command_is_framed_and_padded() {
        let out = dev_dep_msg_out(7, b"*IDN?\n");
        assert_eq!(&out[..4], &[1, 7, !7, 0]);
        assert_eq!(&out[4..8], &6u32.to_le_bytes());
        assert_eq!(out[8], EOM);
        assert_eq!(&out[12..18], b"*IDN?\n");
        assert_eq!(out.len() % 4, 0);
    }

    #[test]
    fn a_reply_from_an_earlier_request_is_refused() {
        let mut header = [0u8; 12];
        header[..4].copy_from_slice(&[2, 3, !3, 0]);
        assert!(parse_in_header(4, &header).is_err());
        assert!(parse_in_header(3, &header).is_ok());
    }

    /// Bulk endpoints that answer each request with canned transfers.
    struct FakeBulk {
        sent: Vec<Vec<u8>>,
        /// Every `REQUEST_DEV_DEP_MSG_IN`, header only.
        requests: Vec<[u8; HEADER]>,
        /// Reply bodies and their end-of-message bit, one per request.
        replies: VecDeque<(Vec<u8>, bool)>,
        /// Pending bulk-in data for the current transfer.
        pending: VecDeque<u8>,
        packet: usize,
        /// A request is outstanding that the device will not answer.
        stuck: bool,
        recovered: usize,
        /// Close a transfer that ends on a packet boundary with a
        /// zero-length packet, read after the transfer, as a DS1000Z does.
        zlp_on_boundary: bool,
        zlp_due: bool,
        /// Packets a dead reader left unread, sent ahead of anything else.
        stale: VecDeque<Vec<u8>>,
        /// Neither endpoint moves until a port reset.
        dead: bool,
        resets: usize,
    }

    impl Bulk for FakeBulk {
        fn bulk_out(&mut self, data: &[u8], _timeout: Duration) -> Result<usize> {
            if self.dead {
                bail!("Operation timed out");
            }
            if data[0] == REQUEST_DEV_DEP_MSG_IN {
                self.requests.push(data[..HEADER].try_into().unwrap());
                let tag = data[1];
                let Some((body, eom)) = self.replies.pop_front() else {
                    // Asked for a reply that never comes.
                    self.stuck = true;
                    return Ok(data.len());
                };
                let mut transfer = vec![DEV_DEP_MSG_IN, tag, !tag, 0];
                transfer.extend_from_slice(&(body.len() as u32).to_le_bytes());
                transfer.extend_from_slice(&[u8::from(eom), 0, 0, 0]);
                transfer.extend_from_slice(&body);
                while transfer.len() % 4 != 0 {
                    transfer.push(0);
                }
                self.pending = transfer.into();
            } else {
                self.sent.push(data.to_vec());
            }
            Ok(data.len())
        }

        /// One packet at a time, as a device with a small endpoint sends.
        fn bulk_in(&mut self, buffer: &mut [u8], _timeout: Duration) -> Result<usize> {
            if self.stuck {
                bail!("Operation timed out");
            }
            if self.zlp_due {
                self.zlp_due = false;
                return Ok(0);
            }
            if let Some(packet) = self.stale.pop_front() {
                buffer[..packet.len()].copy_from_slice(&packet);
                return Ok(packet.len());
            }
            let n = self.pending.len().min(self.packet).min(buffer.len());
            for slot in buffer.iter_mut().take(n) {
                *slot = self.pending.pop_front().unwrap();
            }
            self.zlp_due = self.zlp_on_boundary && n == self.packet && self.pending.is_empty();
            Ok(n)
        }

        fn max_packet(&self) -> usize {
            self.packet
        }

        fn recover(&mut self) {
            self.stuck = false;
            self.recovered += 1;
        }

        fn reset(&mut self) -> Result<()> {
            self.dead = false;
            self.stuck = false;
            self.resets += 1;
            Ok(())
        }
    }

    fn tmc(replies: Vec<(Vec<u8>, bool)>) -> UsbTmc<FakeBulk> {
        UsbTmc::new(FakeBulk {
            sent: Vec::new(),
            requests: Vec::new(),
            replies: replies.into(),
            pending: VecDeque::new(),
            packet: 64,
            stuck: false,
            recovered: 0,
            zlp_on_boundary: false,
            zlp_due: false,
            stale: VecDeque::new(),
            dead: false,
            resets: 0,
        })
    }

    #[test]
    fn a_query_reads_its_reply() {
        let mut scpi = tmc(vec![(b"RIGOL TECHNOLOGIES,MSO5204,MS5A1,00.01.03\n".to_vec(), true)]);
        assert_eq!(
            scpi.query("*IDN?").unwrap(),
            "RIGOL TECHNOLOGIES,MSO5204,MS5A1,00.01.03"
        );
        assert_eq!(&scpi.bulk.sent[0][12..18], b"*IDN?\n");
    }

    #[test]
    fn a_reply_longer_than_a_packet_is_read_across_packets() {
        let mut body = b"#9000000300".to_vec();
        body.extend((0..300).map(|i| i as u8));
        body.push(b'\n');
        let mut scpi = tmc(vec![(body, true)]);
        let block = scpi.query_block(":WAV:DATA?").unwrap();
        assert_eq!(block.len(), 300);
        assert_eq!(block[299], 299u16 as u8);
    }

    #[test]
    fn a_zero_length_packet_after_a_full_transfer_is_skipped() {
        // 12 header bytes and 116 of body: exactly two 64-byte packets.
        let mut first = vec![b'x'; 115];
        first.push(b'\n');
        let mut scpi = tmc(vec![(first, true), (b"1\n".to_vec(), true)]);
        scpi.bulk.zlp_on_boundary = true;
        assert_eq!(scpi.query(":A?").unwrap().len(), 115);
        assert_eq!(scpi.query(":B?").unwrap(), "1");
        assert_eq!(scpi.bulk.recovered, 0);
    }

    #[test]
    fn what_a_dead_reader_left_is_discarded_ahead_of_the_reply() {
        let mut scpi = tmc(vec![(
            b"RIGOL TECHNOLOGIES,DS1054Z,DS1ZA1,00.04.05\n".to_vec(),
            true,
        )]);
        // The tail of an earlier reply, then a whole one tagged for a
        // request this process never made.
        scpi.bulk.stale.push_back(b"P2\n\0".to_vec());
        let mut earlier = vec![DEV_DEP_MSG_IN, 200, !200, 0, 2, 0, 0, 0, EOM, 0, 0, 0];
        earlier.extend_from_slice(b"1\n\0\0");
        scpi.bulk.stale.push_back(earlier);
        assert_eq!(
            scpi.query("*IDN?").unwrap(),
            "RIGOL TECHNOLOGIES,DS1054Z,DS1ZA1,00.04.05"
        );
        assert_eq!(scpi.bulk.recovered, 0);
    }

    #[test]
    fn a_capped_link_asks_for_capped_transfers() {
        let mut scpi = tmc(vec![(b"1\n".to_vec(), true)]);
        scpi.set_max_transfer(500);
        assert_eq!(scpi.query(":A?").unwrap(), "1");
        let request = scpi.bulk.requests[0];
        assert_eq!(u32::from_le_bytes(request[4..8].try_into().unwrap()), 500);
    }

    #[test]
    fn an_instrument_that_answers_nothing_has_its_port_reset() {
        let mut scpi = tmc(vec![(b"1\n".to_vec(), true), (b"1\n".to_vec(), true)]);
        scpi.bulk.dead = true;
        scpi.settle().unwrap();
        assert_eq!(scpi.bulk.resets, 1);
        assert_eq!(scpi.query("*OPC?").unwrap(), "1");
    }

    #[test]
    fn an_instrument_that_answers_is_not_reset() {
        let mut scpi = tmc(vec![(b"1\n".to_vec(), true)]);
        scpi.settle().unwrap();
        assert_eq!(scpi.bulk.resets, 0);
    }

    #[test]
    fn a_reply_without_end_of_message_is_asked_for_again() {
        let mut scpi = tmc(vec![(b"#15he".to_vec(), false), (b"llo\n".to_vec(), true)]);
        assert_eq!(scpi.query_block(":DISP:DATA?").unwrap(), b"hello");
    }

    #[test]
    fn an_unanswered_query_clears_the_instrument_so_the_next_one_works() {
        let mut scpi = tmc(vec![]);
        assert!(scpi.query(":LA:DIGital1:DISPlay?").is_err());
        assert_eq!(scpi.bulk.recovered, 1);
        scpi.bulk.replies.push_back((b"1\n".to_vec(), true));
        assert_eq!(scpi.query(":LA:STATe?").unwrap(), "1");
    }

    #[test]
    fn the_lock_path_matches_the_python_drivers() {
        let path = DeviceLock::path("USB0::0x1AB1::0x0515::MS5A1::INSTR");
        assert!(
            path.ends_with("lager_device_locks/device_USB0__0x1AB1__0x0515__MS5A1__INSTR.lock"),
            "{}",
            path.display()
        );
    }

    #[test]
    fn a_held_lock_is_refused_until_released() {
        let address = format!("TEST::lock::{}", std::process::id());
        let held = DeviceLock::acquire(&address, Duration::ZERO).unwrap();
        // flock is per open file description, so a second open in the same
        // process contends just as another process would.
        let refused = DeviceLock::acquire(&address, Duration::from_millis(60));
        assert!(refused.is_err());
        drop(held);
        assert!(DeviceLock::acquire(&address, Duration::ZERO).is_ok());
        let _ = std::fs::remove_file(DeviceLock::path(&address));
    }
}
