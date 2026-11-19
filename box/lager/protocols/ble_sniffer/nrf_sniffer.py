# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
nRF Sniffer for Bluetooth LE driver: lager speaks the sniffer's serial protocol itself.

The sniffer is an nRF52840 Dongle (or another Nordic board) running the nRF
Sniffer firmware, which enumerates as USB CDC ACM ``1915:522a``. The host
sends SLIP-framed requests (start ``0xAB``, end ``0xBC``, escape ``0xCD``)
with a protocol-1 header ``[6, payload_len, 1, counter_lo, counter_hi, id]``.
The firmware answers with a protocol-2 or -3 header
``[len_lo, len_hi, version, counter_lo, counter_hi, id]`` (firmware 5.0.0
speaks 3; protocol 1 frames have a header-length byte and a one-byte length).
A capture is:

1. ``GO_IDLE``, then ``REQ_VERSION``, and wait for ``RESP_VERSION``. No
   answer fails the capture instead of recording nothing
2. Advertising channel hop sequence 37, 38, 39, ``REQ_SCAN_CONT`` with the
   scan flags, an all-zero temporary key, and ``REQ_FOLLOW`` once the device
   to follow is known: at once for an address with a given type, otherwise
   when its first good advertisement shows the address type, or the name
3. Read for the duration or until ``request_stop``, writing every
   advertising and data PDU event to the pcap
4. ``GO_IDLE``

Locking, the output file and the result are ``BleSnifferBase.capture``'s.

The request encodings are the ones Nordic's nrfutil-ble-sniffer 0.22.0 sends.
The pcap uses LINKTYPE_NORDIC_BLE (272), which Wireshark's ``nordic_ble``
dissector decodes: each record is a board ID byte and the unescaped frame,
less the radio's S1 padding byte that a packet event carries after the
2-byte PDU header (see ``pcap_frame``). Record times come from the
firmware's microsecond clock in protocol 3, anchored to the host clock at the
first packet; protocol 2 packets get the host time when they are read, which
is batched by the 50 ms serial read timeout.

While idle with the port open, firmware 5.0.0 sends empty ``PING_RESP``
frames about five times a second, and frames queued while the port was closed
arrive first after opening. Both are ignored.
"""

from __future__ import annotations

import enum
import errno
import logging
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import serial

from lager.devices import serial_id
from lager.exceptions import BleSnifferBackendError
from lager.protocols.ble_sniffer.ble_sniffer_net import (
    BleSnifferBase,
    CaptureContext,
    CaptureOptions,
    DeviceLost,
)

if TYPE_CHECKING:
    import threading
    from collections.abc import Iterator, Mapping

logger = logging.getLogger(__name__)

#: USB ID of the nRF Sniffer for Bluetooth LE firmware.
VID = "1915"
PID = "522a"
BAUDRATE = 1_000_000

#: Wireshark's link type for nRF Sniffer frames.
LINKTYPE_NORDIC_BLE = 272

_SLIP_START = 0xAB
_SLIP_END = 0xBC
_SLIP_ESC = 0xCD
_ESCAPES = {_SLIP_START: 0xAC, _SLIP_END: 0xBD, _SLIP_ESC: 0xCE}
_UNESCAPES = {v: k for k, v in _ESCAPES.items()}
# Longest frame kept by the decoder. Packet events are under 300 bytes; a
# frame that grows past this lost its end byte and is dropped.
_MAX_FRAME = 1024

_HOP_SEQUENCE = (37, 38, 39)
_BOARD_ID = 0
_ADV_ACCESS_ADDRESS = 0x8E89BED6
# Seconds to wait for RESP_VERSION before calling the sniffer unresponsive.
_VERSION_TIMEOUT_S = 2.0
_READ_TIMEOUT_S = 0.05
_WRITE_TIMEOUT_S = 1.0
_SYS_TTY = Path("/sys/class/tty")


class PacketId(enum.IntEnum):
    """Packet IDs of the nRF Sniffer serial protocol that lager uses."""

    REQ_FOLLOW = 0x00
    EVENT_PACKET_ADV_PDU = 0x02
    EVENT_PACKET_DATA_PDU = 0x06
    REQ_SCAN_CONT = 0x07
    SET_TEMPORARY_KEY = 0x0C
    PING_RESP = 0x0E
    SET_ADV_CHANNEL_HOP_SEQ = 0x17
    REQ_VERSION = 0x1B
    RESP_VERSION = 0x1C
    GO_IDLE = 0xFE


#: Packet events written to the pcap. Protocol 2 sends every packet as 0x06.
_RECORDED = (PacketId.EVENT_PACKET_ADV_PDU, PacketId.EVENT_PACKET_DATA_PDU)

# REQ_SCAN_CONT flag bits.
_SCAN_FOLLOW_RSP = 0x01
_SCAN_FOLLOW_AUX = 0x02
_SCAN_CODED = 0x04
# REQ_FOLLOW flag bits.
_FOLLOW_ONLY_ADVERTISING = 0x01
_FOLLOW_CODED = 0x04

# Legacy advertising PDU types that start with AdvA, and those of them that
# carry AD structures after it.
_ADV_TYPES_WITH_ADDRESS = (0x0, 0x1, 0x2, 0x4, 0x6)  # IND, DIRECT_IND, NONCONN, SCAN_RSP, SCAN_IND
_ADV_TYPES_WITH_DATA = (0x0, 0x2, 0x4, 0x6)
_AD_SHORT_NAME = 0x08
_AD_COMPLETE_NAME = 0x09
_PHY_CODED = 2
# Packet event flags bit: the packet passed its CRC.
_FLAG_CRC_OK = 0x01


class Transport(Protocol):
    """The subset of ``serial.Serial`` the driver uses."""

    def read(self, size: int = 1) -> bytes: ...
    def write(self, data: bytes) -> int | None: ...
    def close(self) -> None: ...


def open_serial(port: str) -> Transport:
    """Open the sniffer's tty with an exclusive lock.

    Raises:
        BleSnifferBackendError: The port cannot be opened, for example
            because another program holds it.
    """
    try:
        return serial.Serial(port, BAUDRATE, timeout=_READ_TIMEOUT_S,
                             write_timeout=_WRITE_TIMEOUT_S, exclusive=True)
    except (OSError, serial.SerialException) as e:
        if getattr(e, "errno", None) in (errno.EBUSY, errno.EAGAIN):
            raise BleSnifferBackendError(
                f"Sniffer port {port} is in use by another program. Stop it and retry.") from e
        raise BleSnifferBackendError(f"Cannot open sniffer port {port}: {e}") from e


def slip_encode(frame: bytes) -> bytes:
    """One SLIP-framed packet for ``frame``."""
    body = bytearray()
    for byte in frame:
        if byte in _ESCAPES:
            body += bytes((_SLIP_ESC, _ESCAPES[byte]))
        else:
            body.append(byte)
    return bytes((_SLIP_START, *body, _SLIP_END))


def encode_request(packet_id: int, payload: bytes, counter: int) -> bytes:
    """A SLIP-framed host request with the protocol-1 header the firmware accepts."""
    header = bytes((6, len(payload), 1, counter & 0xFF, (counter >> 8) & 0xFF, packet_id))
    return slip_encode(header + payload)


class SlipDecoder:
    """Splits a byte stream into unescaped frames, keeping a partial frame between feeds."""

    def __init__(self) -> None:
        self._frame: bytearray | None = None
        self._escaped = False

    def feed(self, data: bytes) -> list[bytes]:
        """Frames completed by ``data``. Bytes outside a frame, and overlong frames, are dropped."""
        frames = []
        for byte in data:
            if byte == _SLIP_START:
                self._frame = bytearray()
                self._escaped = False
            elif self._frame is None:
                continue
            elif byte == _SLIP_END:
                frames.append(bytes(self._frame))
                self._frame = None
            elif self._escaped:
                self._frame.append(_UNESCAPES.get(byte, byte))
                self._escaped = False
            elif byte == _SLIP_ESC:
                self._escaped = True
            else:
                self._frame.append(byte)
            if self._frame is not None and len(self._frame) > _MAX_FRAME:
                self._frame = None
        return frames


@dataclass(frozen=True)
class Frame:
    """One firmware frame: the unescaped bytes and its parsed header."""

    raw: bytes
    version: int
    packet_id: int
    payload: bytes


def parse_frame(raw: bytes) -> Frame | None:
    """The parsed firmware frame, or None if it is malformed or of an unknown protocol."""
    if len(raw) < 6:
        return None
    version = raw[2]
    if version == 1:
        length, offset = raw[1], raw[0]
    elif version in (2, 3):
        length, offset = raw[0] | raw[1] << 8, 6
    else:
        return None
    payload = raw[offset:offset + length]
    if offset < 6 or len(payload) != length:
        return None
    return Frame(raw, version, raw[5], payload)


def follow_payload(address: str, address_type: str, flags: int) -> bytes:
    """``REQ_FOLLOW`` payload: address bytes in the order written, type, 0, flags.

    The firmware matches the address most significant byte first, the order
    of ``AA:BB:CC:DD:EE:FF``, not the over-the-air order.
    """
    addr = bytes.fromhex(address.replace(":", "").replace("-", ""))
    return addr + bytes((1 if address_type == "random" else 0, 0, flags))


def _pdu_offset(payload: bytes) -> int | None:
    """Offset of the PDU header in a packet event payload, or None if too short.

    The payload is metadata (its first byte is its length), the access
    address, then on the coded PHY a coding indicator byte, then the PDU.
    """
    if not payload or len(payload) < payload[0] + 4 or payload[0] < 4:
        return None
    phy = (payload[1] >> 4) & 0x7
    return payload[0] + 4 + (1 if phy == _PHY_CODED else 0)


def is_packet_event(frame: Frame) -> bool:
    """Whether the frame is a radio packet, which goes in the pcap."""
    return frame.packet_id in _RECORDED and _pdu_offset(frame.payload) is not None


def is_advertising(frame: Frame) -> bool:
    """Whether a packet event was received on the advertising access address."""
    if frame.packet_id == PacketId.EVENT_PACKET_ADV_PDU:
        return True
    if frame.version >= 3 or not is_packet_event(frame):
        return False
    meta = frame.payload[0]
    return struct.unpack_from("<I", frame.payload, meta)[0] == _ADV_ACCESS_ADDRESS


def rssi(frame: Frame) -> int | None:
    """RSSI in dBm of a packet event, or None if too short."""
    return -frame.payload[3] if len(frame.payload) > 3 else None


def firmware_timestamp_us(frame: Frame) -> int | None:
    """The firmware's 32-bit microsecond receive time of a protocol-3 packet event."""
    if frame.version < 3 or not is_packet_event(frame) or frame.payload[0] < 10:
        return None
    return struct.unpack_from("<I", frame.payload, 6)[0]


def _legacy_adv_pdu(frame: Frame) -> bytes | None:
    # The PDU of a good legacy advertisement that starts with AdvA. A packet
    # that failed its CRC is skipped: on the bench those carried names and
    # addresses with flipped bits.
    if not is_advertising(frame) or not frame.payload[1] & _FLAG_CRC_OK:
        return None
    offset = _pdu_offset(frame.payload)
    pdu = frame.payload[offset:] if offset is not None else b""
    if len(pdu) < 9 or pdu[0] & 0x0F not in _ADV_TYPES_WITH_ADDRESS or pdu[1] < 6:
        return None
    return pdu


def adv_address(frame: Frame) -> tuple[str, str] | None:
    """``(address, address_type)`` of a good legacy advertising PDU that has AdvA.

    The PDU is the 2-byte header, the S1 padding byte, then AdvA.
    """
    pdu = _legacy_adv_pdu(frame)
    if pdu is None:
        return None
    return pdu[3:9][::-1].hex(":").upper(), "random" if pdu[0] & 0x40 else "public"


def adv_name(frame: Frame) -> str | None:
    """The local name in a good legacy advertisement or scan response, if it has one."""
    pdu = _legacy_adv_pdu(frame)
    if pdu is None or pdu[0] & 0x0F not in _ADV_TYPES_WITH_DATA:
        return None
    data = pdu[9:3 + pdu[1]]
    i = 0
    while i + 1 < len(data) and data[i]:
        ad_len, ad_type = data[i], data[i + 1]
        if ad_type in (_AD_SHORT_NAME, _AD_COMPLETE_NAME):
            return data[i + 2:i + 1 + ad_len].decode("utf-8", errors="replace")
        i += 1 + ad_len
    return None


def pcap_frame(frame: Frame) -> bytes:
    """A packet event as Wireshark expects it: without the S1 byte.

    The firmware passes on the S1 byte the nRF radio puts after the PDU
    header and length; the ``nordic_ble`` dissector has no field for it, so
    leaving it in shifts every following byte. Checked against the pcap
    nrfutil-ble-sniffer writes for protocol 3; protocol 1 frames are returned
    unchanged.
    """
    offset = _pdu_offset(frame.payload) if frame.version >= 2 else None
    if not is_packet_event(frame) or offset is None or len(frame.payload) < offset + 3:
        return frame.raw
    payload = frame.payload[:offset + 2] + frame.payload[offset + 3:]
    return struct.pack("<H", len(payload)) + frame.raw[2:6] + payload


class _Clock:
    """Record times: the firmware's microsecond clock anchored to host time at the first packet.

    The firmware clock is 32 bits and wraps every 71.6 minutes. The firmware
    clock gives the time within a wrap, and the host clock picks the wrap:
    the one that puts the packet nearest the host's elapsed time. So a quiet
    stretch of any length, with no packet between wraps, keeps its true
    length, and a packet a little out of order stays a small step back.
    """

    _WRAP_US = 0x100000000

    def __init__(self) -> None:
        self._host: float | None = None
        self._first = 0

    def stamp(self, frame: Frame) -> float:
        ts = firmware_timestamp_us(frame)
        now = time.time()
        if ts is None:
            return now
        if self._host is None:
            self._host, self._first = now, ts
            return self._host
        within = (ts - self._first) & (self._WRAP_US - 1)
        host_us = (now - self._host) * 1_000_000
        wraps = round((host_us - within) / self._WRAP_US)
        return self._host + (within + wraps * self._WRAP_US) / 1_000_000


def find_sniffer_tty(serial: str) -> str | None:
    """The sniffer's data tty, or None when the sniffer is not plugged in.

    nRF Sniffer firmware 4.x exposes one CDC ACM interface. Firmware 5.x
    exposes two, and the sniffer protocol runs on the second (``if02``): the
    first does not read what the host writes, so commands sent there time out.
    Taking the tty on the highest interface number is right for both.
    """
    candidates = []
    for dev in sorted(_SYS_TTY.glob("ttyACM*")):
        ident = serial_id.identity_for_tty(f"/dev/{dev.name}")
        if (ident and ident["vid"] == VID and ident["pid"] == PID
                and ident["serial"] == serial):
            interface = ident.get("interface")
            candidates.append((-1 if interface is None else interface, f"/dev/{dev.name}"))
    return max(candidates)[1] if candidates else None


def serial_from_address(address: str) -> str:
    """The USB serial number in a ``USB0::0x1915::0x522A::<serial>::INSTR`` address.

    Raises:
        BleSnifferBackendError: The address has no serial number field.
    """
    parts = (address or "").split("::")
    if len(parts) < 4 or not parts[3]:
        raise BleSnifferBackendError(
            f"Sniffer address '{address}' has no USB serial number. "
            f"Re-add the net from 'lager instruments'.")
    return parts[3]


class _Session:
    """One open sniffer port: framing, request counter and frame reads."""

    def __init__(self, transport: Transport) -> None:
        self._t = transport
        self._decoder = SlipDecoder()
        self._counter = 0
        self.unknown_version: int | None = None

    def send(self, packet_id: int, payload: bytes = b"") -> None:
        self._t.write(encode_request(packet_id, payload, self._counter))
        self._counter = (self._counter + 1) & 0xFFFF

    def frames(self, deadline: float, stop: threading.Event | None = None) -> Iterator[Frame]:
        """Each frame read before ``deadline`` or until ``stop`` is set."""
        while time.monotonic() < deadline and not (stop is not None and stop.is_set()):
            for raw in self._decoder.feed(self._t.read(4096)):
                frame = parse_frame(raw)
                if frame is not None:
                    yield frame
                elif len(raw) >= 6 and raw[2] not in (1, 2, 3):
                    self.unknown_version = raw[2]

    def version(self) -> str:
        """Idle the sniffer, then ask for the firmware version.

        Raises:
            BleSnifferBackendError: No ``RESP_VERSION`` within the timeout.
        """
        self.send(PacketId.GO_IDLE)
        self.send(PacketId.REQ_VERSION)
        for frame in self.frames(time.monotonic() + _VERSION_TIMEOUT_S):
            if frame.packet_id == PacketId.RESP_VERSION:
                return frame.payload.decode("ascii", errors="replace").strip("\x00 ")
        if self.unknown_version is not None:
            raise BleSnifferBackendError(
                f"The sniffer speaks serial protocol {self.unknown_version}, which lager does "
                f"not read (it reads 1 to 3). Flash the nRF Sniffer for Bluetooth LE firmware "
                f"from Nordic's nrfutil-ble-sniffer package.")
        raise BleSnifferBackendError(
            f"The sniffer did not answer a version request within {_VERSION_TIMEOUT_S:g} s. "
            f"Unplug and replug it, and check that it runs the nRF Sniffer for Bluetooth LE "
            f"firmware.")


@dataclass
class _Follow:
    """Follow state for one capture: what was asked for and what was sent."""

    address: str | None
    address_type: str | None
    name: str | None
    flags: int
    sent: tuple[str, str] | None = None
    seen: bool = False

    def wanted(self, frame: Frame) -> tuple[str, str] | None:
        """``(address, type)`` to send ``REQ_FOLLOW`` for, if this frame settles it."""
        if self.sent is not None or (self.address is None and self.name is None):
            return None
        found = adv_address(frame)
        if found is None:
            return None
        if self.address is not None and found[0] == self.address:
            return found
        if self.name is not None and adv_name(frame) == self.name:
            return found
        return None


class NrfSniffer(BleSnifferBase):
    """nRF Sniffer firmware on a Nordic board, driven over its serial protocol.

    The result of ``capture`` also has ``firmware``, the version the sniffer
    reported; ``followed``, the address a follow request was sent for (None
    when none was); and ``follow_seen``, whether a packet from the followed
    device was recorded (None without a follow).
    """

    LINKTYPE = LINKTYPE_NORDIC_BLE

    def __init__(self, net_info: Mapping[str, Any]) -> None:
        self.name = net_info.get("name") or "sniffer"
        self.identity = serial_from_address(net_info.get("address") or "")

    def port(self) -> str | None:
        """The sniffer's data tty now, or None when it is not plugged in."""
        return find_sniffer_tty(self.identity)

    def missing_message(self) -> str:
        """Text for a sniffer that is not on USB."""
        return (f"Sniffer {self.identity} is not on USB. Check that it is plugged in and runs "
                f"the nRF Sniffer for Bluetooth LE firmware (USB ID {VID}:{PID}).")

    def _record(self, port: str, options: CaptureOptions, ctx: CaptureContext) -> dict:
        transport = open_serial(port)
        try:
            return self._run(_Session(transport), options, ctx)
        except serial.SerialException as e:
            raise DeviceLost(str(e)) from e
        finally:
            _close(transport)

    @staticmethod
    def _run(session: _Session, options: CaptureOptions, ctx: CaptureContext) -> dict:
        firmware = session.version()
        scan_flags = _SCAN_FOLLOW_RSP | _SCAN_FOLLOW_AUX | (_SCAN_CODED if options.coded else 0)
        follow = _Follow(
            address=options.follow.replace("-", ":").upper() if options.follow else None,
            address_type=options.address_type, name=options.follow_by_name,
            flags=((_FOLLOW_ONLY_ADVERTISING if options.only_advertising else 0)
                   | (_FOLLOW_CODED if options.coded else 0)))
        session.send(PacketId.SET_ADV_CHANNEL_HOP_SEQ,
                     bytes((len(_HOP_SEQUENCE), *_HOP_SEQUENCE)))
        session.send(PacketId.REQ_SCAN_CONT, bytes((scan_flags,)))
        session.send(PacketId.SET_TEMPORARY_KEY, bytes(16))
        if follow.address is not None and follow.address_type is not None:
            follow.sent = (follow.address, follow.address_type)
            session.send(PacketId.REQ_FOLLOW,
                         follow_payload(follow.address, follow.address_type, follow.flags))
        clock = _Clock()
        ctx.started()
        try:
            for frame in session.frames(time.monotonic() + options.duration, ctx.stop):
                if not is_packet_event(frame):
                    continue
                if is_advertising(frame):
                    level = rssi(frame)
                    if (options.rssi_cut_off is not None and level is not None
                            and level < options.rssi_cut_off):
                        continue
                    wanted = follow.wanted(frame)
                    if wanted is not None:
                        follow.sent = wanted
                        logger.info("ble-sniffer: following %s (%s)", *wanted)
                        session.send(PacketId.REQ_FOLLOW,
                                     follow_payload(wanted[0], wanted[1], follow.flags))
                    if follow.sent is not None and adv_address(frame) == follow.sent:
                        follow.seen = True
                elif follow.sent is not None:
                    follow.seen = True
                ctx.writer.write(bytes((_BOARD_ID,)) + pcap_frame(frame), clock.stamp(frame))
        finally:
            try:
                session.send(PacketId.GO_IDLE)
            except serial.SerialException as e:
                logger.warning("ble-sniffer: go-idle failed: %s", e)
        asked = follow.address is not None or follow.name is not None
        return {"firmware": firmware,
                "followed": follow.sent[0] if follow.sent else None,
                "follow_seen": follow.seen if asked else None}


def _close(transport: Transport) -> None:
    try:
        transport.close()
    except (OSError, serial.SerialException) as e:
        logger.warning("ble-sniffer: closing the port failed: %s", e)
