# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the ``ble-sniffer`` net role and its nRF Sniffer driver.

The driver talks to ``FakeFirmware``, an in-process stand-in for the serial
port that decodes the host's requests and answers the way firmware 5.0.0 did
on a bench dongle: protocol-3 frames, a backlog of empty PING_RESP frames on
open, and advertising PDU events once scanning starts. A fake clock advances
on every read, so captures finish without waiting. The frame bytes come from
a real capture. The /net/command handler is exercised with ``Net`` mocked and
the driver replaced, as in test_dmm_siglent_sdm3065x.py.
"""

from __future__ import annotations

import os
import pathlib
import struct
import threading
import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import serial
from flask import Flask
from lager.exceptions import BleSnifferBackendError
from lager.http_handlers import net_command
from lager.http_handlers.usb_scanner import CHANNEL_MAPS, SUPPORTED_USB
from lager.nets.constants import NetType
from lager.protocols.ble_sniffer import ble_sniffer_net, capture_jobs, dispatcher
from lager.protocols.ble_sniffer import nrf_sniffer as nrf
from lager.protocols.ble_sniffer.ble_sniffer_net import (
    BleSnifferBusyError,
    BleSnifferNet,
    CaptureOptions,
)

_SERIAL = "C2E15977A9D11709"
_ADDRESS = f"USB0::0x1915::0x522A::{_SERIAL}::INSTR"
_SAVED = [{"name": "sniffer1", "role": "ble-sniffer", "instrument": "Nordic_nRF_Sniffer",
           "address": _ADDRESS}]

# An ADV_NONCONN_IND from 45:83:A7:51:5E:C9 as firmware 5.0.0 sent it (unescaped),
# and the same packet as nrfutil-ble-sniffer 0.22.0 wrote it to a pcap record
# after its board ID byte: the S1 byte after "42 17" gone and the length one less.
_REAL_FRAME = bytes.fromhex(
    "2b0003c109020a0125390000ccfa06a6d6be898e421700c95e51a78345"
    "02011a0dff4c00160800466b0b77cf79a60f6776")
_REAL_PCAP_FRAME = bytes.fromhex(
    "2a0003c109020a0125390000ccfa06a6d6be898e4217c95e51a78345"
    "02011a0dff4c00160800466b0b77cf79a60f6776")


def _pcap_bytes(records: int, truncated_tail: bool = False) -> bytes:
    """A little-endian classic pcap with ``records`` 4-byte records."""
    data = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 272)
    for i in range(records):
        data += struct.pack("<IIII", 1700000000 + i, 0, 4, 4) + b"\x01\x02\x03\x04"
    if truncated_tail:
        data += struct.pack("<IIII", 1700000099, 0, 8, 8) + b"\x01\x02"
    return data


def _frame(packet_id: int, payload: bytes, counter: int = 0, version: int = 3) -> bytes:
    """A protocol-2 or -3 firmware frame, unescaped."""
    return struct.pack("<HBHB", len(payload), version, counter, packet_id) + payload


def _meta(flags: int, rssi: int, ts_us: int) -> bytes:
    return bytes((10, flags, 37, -rssi, 0, 0)) + struct.pack("<I", ts_us)


def _adv_event(address: str, *, random: bool = True, name: str | None = None,
               rssi: int = -50, coded: bool = False, crc_ok: bool = True,
               ts_us: int = 0) -> bytes:
    """A protocol-3 EVENT_PACKET_ADV_PDU frame for an ADV_IND, with the S1 byte."""
    ad = bytes.fromhex("020106")
    if name is not None:
        encoded = name.encode()
        ad += bytes((len(encoded) + 1, 0x09)) + encoded
    adv_a = bytes.fromhex(address.replace(":", ""))[::-1]
    header = bytes((0x40 if random else 0x00, len(adv_a) + len(ad)))
    flags = (0x01 if crc_ok else 0x00) | ((2 if coded else 0) << 4)
    ble = bytes.fromhex("d6be898e") + (b"\x00" if coded else b"") + header + b"\x00"
    return _frame(0x02, _meta(flags, rssi, ts_us) + ble + adv_a + ad + bytes(3))


def _data_event(rssi: int = -80, ts_us: int = 0) -> bytes:
    """A protocol-3 EVENT_PACKET_DATA_PDU frame: an empty LL data PDU, with the S1 byte."""
    ble = bytes.fromhex("11223344") + bytes((0x01, 0x00)) + b"\x00"
    return _frame(0x06, _meta(0x01, rssi, ts_us) + ble + bytes(3))


def _parsed(raw: bytes) -> nrf.Frame:
    frame = nrf.parse_frame(raw)
    assert frame is not None
    return frame


class FakeFirmware:
    """A serial port with nRF Sniffer firmware 5.0.0 behind it.

    ``adverts`` are sent, two per read, in a loop once ``REQ_SCAN_CONT``
    arrives. ``silent`` firmware answers nothing; ``version_frame`` replaces
    the version answer. ``fail_after`` reads into a scan, reads raise
    ``SerialException`` as an unplugged dongle does; ``on_scan_read`` is
    called on every read while scanning. ``requests`` lists the decoded
    ``(packet_id, payload)`` the host sent.
    """

    def __init__(self, clock: list[float], adverts: list[bytes] | None = None,
                 silent: bool = False, version_frame: bytes | None = None,
                 fail_after: int | None = None, on_scan_read=None) -> None:
        self._clock = clock
        self._adverts = adverts or []
        self._silent = silent
        self._version_frame = version_frame or _frame(0x1C, b"5.0.0")
        self._fail_after = fail_after
        self._on_scan_read = on_scan_read
        self._decoder = nrf.SlipDecoder()
        self._scanning = False
        self._scan_reads = 0
        self._sent = 0
        # Queued while the port was closed. Reads return at most 40 bytes, so
        # advertising frames arrive split across reads.
        self._out = b"".join(nrf.slip_encode(_frame(0x0E, b"", i)) for i in range(5))
        self.requests: list[tuple[int, bytes]] = []
        self.closed = False

    def write(self, data: bytes) -> int:
        if self._fail_after is not None and self._scan_reads >= self._fail_after:
            raise serial.SerialException("write failed: [Errno 5] Input/output error")
        for frame in self._decoder.feed(data):
            assert frame[0] == 6 and frame[2] == 1, "host requests use protocol 1"
            packet_id, payload = frame[5], frame[6:6 + frame[1]]
            self.requests.append((packet_id, payload))
            if self._silent:
                continue
            if packet_id == 0x1B:
                self._out += nrf.slip_encode(self._version_frame)
            elif packet_id == 0x07:
                self._scanning = True
            elif packet_id == 0xFE:
                self._scanning = False
        return len(data)

    def read(self, size: int = 1) -> bytes:
        self._clock[0] += 0.1
        if self._scanning:
            self._scan_reads += 1
            if self._fail_after is not None and self._scan_reads > self._fail_after:
                raise serial.SerialException("read failed: [Errno 5] Input/output error")
            if self._on_scan_read is not None:
                self._on_scan_read(self._scan_reads)
            for _ in range(2 if self._adverts else 0):
                self._out += nrf.slip_encode(self._adverts[self._sent % len(self._adverts)])
                self._sent += 1
        chunk, self._out = self._out[:min(size, 40)], self._out[min(size, 40):]
        return chunk

    def close(self) -> None:
        self.closed = True

    def ids(self) -> list[int]:
        return [packet_id for packet_id, _ in self.requests]

    def payloads(self, packet_id: int) -> list[bytes]:
        return [payload for pid, payload in self.requests if pid == packet_id]


@pytest.fixture
def clock(monkeypatch):
    """A fake monotonic clock for the driver, advanced by ``FakeFirmware.read``."""
    now = [1000.0]
    fake = SimpleNamespace(monotonic=lambda: now[0], time=lambda: 1.7e9 + now[0],
                           sleep=time.sleep)
    monkeypatch.setattr(nrf, "time", fake)
    monkeypatch.setattr(ble_sniffer_net, "time", fake)  # the measured duration
    return now


@pytest.fixture
def firmware(clock, monkeypatch):
    """Install a ``FakeFirmware`` as the sniffer port and return it."""
    def install(**kwargs):
        fw = FakeFirmware(clock, **kwargs)
        monkeypatch.setattr(nrf, "open_serial", lambda port: fw)
        return fw
    return install


@pytest.fixture
def driver(tmp_path, monkeypatch):
    """A driver whose tty resolves and whose lock files live in tmp_path."""
    monkeypatch.setattr(ble_sniffer_net, "_LOCK_DIR", tmp_path)
    monkeypatch.setattr(nrf, "find_sniffer_tty", lambda serial: "/dev/ttyACM9")
    return nrf.NrfSniffer({"name": "sniffer1", "address": _ADDRESS})


def _records(path) -> list[tuple[float, bytes]]:
    """``(timestamp, record)`` for each record of a little-endian pcap."""
    data = path.read_bytes()
    assert struct.unpack("<IHHiIII", data[:24])[6] == 272
    records, offset = [], 24
    while offset < len(data):
        seconds, micros, length, _ = struct.unpack("<IIII", data[offset:offset + 16])
        records.append((seconds + micros / 1e6, data[offset + 16:offset + 16 + length]))
        offset += 16 + length
    return records


class StubDriver(ble_sniffer_net.BleSnifferBase):
    """The smallest driver on ``BleSnifferBase``: what a second sniffer model would write.

    It writes ``records`` one-byte records, then waits for the duration or a
    stop. ``fail_before`` raises before recording starts; ``lose`` raises
    ``DeviceLost`` after the records.
    """

    LINKTYPE = 147  # LINKTYPE_USER0

    def __init__(self, identity: str | None = None, port: str | None = "/dev/stub0",
                 records: int = 2, fail_before: str | None = None, lose: bool = False) -> None:
        self.identity = identity or uuid.uuid4().hex
        self._port = port
        self._records = records
        self._fail_before = fail_before
        self._lose = lose

    def port(self) -> str | None:
        return self._port

    def missing_message(self) -> str:
        return f"Stub {self.identity} is not connected"

    def _record(self, port, options, ctx):
        if self._fail_before:
            raise BleSnifferBackendError(self._fail_before)
        ctx.started()
        for i in range(self._records):
            ctx.writer.write(bytes((i,)), 1.7e9 + i)
        if self._lose:
            raise ble_sniffer_net.DeviceLost("read failed")
        ctx.stop.wait(options.duration)
        return {"extra": port}


@pytest.fixture
def locks(tmp_path, monkeypatch):
    """Lock files in tmp_path."""
    monkeypatch.setattr(ble_sniffer_net, "_LOCK_DIR", tmp_path)


class TestBaseDriver:

    def test_template_does_the_common_work(self, locks, tmp_path):
        drv = StubDriver(records=3)
        out = tmp_path / "c.pcap"
        stop_soon = threading.Timer(0.1, ble_sniffer_net.request_stop, (drv.identity,))
        stop_soon.start()
        result = drv.capture(CaptureOptions(duration=30), out)
        assert result["extra"] == "/dev/stub0" and result["port"] == "/dev/stub0"
        assert result["packets"] == 3 and result["stopped"] is True
        assert 0.05 < result["duration"] < 5
        assert struct.unpack("<IHHiIII", out.read_bytes()[:24])[6] == 147
        assert not out.with_name("c.pcap.part").exists()
        assert not ble_sniffer_net.is_locked(drv.identity)

    def test_device_lost_keeps_the_records(self, locks, tmp_path):
        drv = StubDriver(records=2, lose=True)
        out = tmp_path / "c.pcap"
        with pytest.raises(BleSnifferBackendError, match="The 2 packets recorded"):
            drv.capture(CaptureOptions(duration=1), out)
        assert ble_sniffer_net.count_pcap_packets(out) == 2

    def test_device_lost_before_any_record(self, locks, tmp_path):
        out = tmp_path / "c.pcap"
        with pytest.raises(BleSnifferBackendError, match="stopped answering \\(read failed\\)"):
            StubDriver(records=0, lose=True).capture(CaptureOptions(duration=1), out)
        assert not out.exists() and not out.with_name("c.pcap.part").exists()

    def test_missing_device_uses_the_driver_message(self, locks, tmp_path):
        drv = StubDriver(port=None)
        with pytest.raises(BleSnifferBackendError, match="Stub .* is not connected"):
            drv.capture(CaptureOptions(duration=1), tmp_path / "c.pcap")
        assert drv.status() == {"serial": drv.identity, "port": None, "present": False,
                                "busy": False}


class TestCaptureJobs:

    def test_job_runs_reports_progress_and_stops(self, locks, tmp_path):
        drv = StubDriver(records=4)
        job = capture_jobs.start_job("sniffer1", drv, CaptureOptions(duration=60),
                                     tmp_path / "j.pcap")
        info = job.to_dict()
        assert info["state"] == "running"  # start returns once recording begins
        assert info["file"] == str(tmp_path / "j.pcap") and info["duration"] == 60.0
        assert capture_jobs.get_job(job.id) is job
        assert capture_jobs.latest_job(drv.identity) is job
        assert ble_sniffer_net.request_stop(drv.identity) == tmp_path / "j.pcap"
        assert capture_jobs.wait_for(job, 5)
        info = job.to_dict()
        assert info["state"] == "done" and info["error"] is None
        assert info["result"]["stopped"] is True and info["result"]["packets"] == 4
        time.sleep(0.05)
        assert job.to_dict()["elapsed"] == info["elapsed"] == info["result"]["duration"]

    def test_busy_fails_the_start(self, locks, tmp_path):
        drv = StubDriver()
        with ble_sniffer_net.device_lock(drv.identity), \
                pytest.raises(BleSnifferBusyError) as info:
            capture_jobs.start_job("s", drv, CaptureOptions(duration=5), tmp_path / "j.pcap")
        assert str(info.value).count("[BleSniffer]") == 1
        assert capture_jobs.latest_job(drv.identity) is None

    @pytest.mark.parametrize("drv, match", [
        (StubDriver(port=None), "is not connected"),
        (StubDriver(fail_before="did not answer a version request"), "did not answer"),
    ])
    def test_a_sniffer_that_cannot_start_fails_the_start(self, locks, tmp_path, drv, match):
        with pytest.raises(BleSnifferBackendError, match=match) as info:
            capture_jobs.start_job("s", drv, CaptureOptions(duration=5), tmp_path / "j.pcap")
        assert str(info.value).count("[BleSniffer]") == 1
        assert not (tmp_path / "j.pcap").exists()

    def test_invalid_options_fail_the_start(self, locks, tmp_path):
        with pytest.raises(ValueError, match="only with follow"):
            capture_jobs.start_job("s", StubDriver(), CaptureOptions(only_advertising=True),
                                   tmp_path / "j.pcap")

    def test_a_device_lost_after_the_start_fails_the_job(self, locks, tmp_path):
        drv = StubDriver(records=3, lose=True)
        # The stub records and is lost before start_job looks, or just after.
        job = capture_jobs.start_job("s", drv, CaptureOptions(duration=5), tmp_path / "j.pcap")
        assert capture_jobs.wait_for(job, 5)
        info = job.to_dict()
        assert info["state"] == "failed" and info["error_kind"] == "backend"
        assert "The 3 packets recorded" in info["error"]
        assert info["file_saved"] is True and info["result"] is None

    def test_finished_jobs_are_trimmed(self, locks, tmp_path, monkeypatch):
        monkeypatch.setattr(capture_jobs, "_KEEP_JOBS", 2)
        drv = StubDriver(records=0)
        jobs = []
        for i in range(4):
            job = capture_jobs.start_job("s", drv, CaptureOptions(duration=30),
                                         tmp_path / f"j{i}.pcap")
            ble_sniffer_net.request_stop(drv.identity)
            assert capture_jobs.wait_for(job, 5)
            jobs.append(job)
        capture_jobs.start_job("s", StubDriver(records=0, lose=True), CaptureOptions(duration=1),
                               tmp_path / "trim.pcap")
        assert [capture_jobs.get_job(j.id) for j in jobs[:2]] == [None, None]
        assert capture_jobs.get_job(jobs[3].id) is jobs[3]


class TestCaptureOptions:

    def test_defaults_are_valid(self):
        CaptureOptions().validate()

    @pytest.mark.parametrize("kwargs, match", [
        ({"duration": 0.5}, "duration must be between 1 and 86400"),
        ({"duration": 86401}, "duration must be between 1 and 86400"),
        ({"duration": None}, "duration must be a number"),
        ({"duration": True}, "duration must be a number"),
        ({"follow": 5}, "follow must be a string"),
        ({"follow_by_name": ["x"]}, "follow_by_name must be a string"),
        ({"rssi_cut_off": "-60"}, "whole number"),
        ({"follow": "AA:BB:CC:DD:EE"}, "Invalid Bluetooth LE address"),
        ({"follow": "AA:BB:CC:DD:EE:FF", "address_type": "static"}, "public or random"),
        ({"address_type": "public"}, "applies only to follow"),
        ({"follow": "AA:BB:CC:DD:EE:FF", "follow_by_name": "dut"}, "not both"),
        ({"follow_by_name": "  "}, "must not be empty"),
        ({"only_advertising": True}, "only with follow"),
        ({"follow_by_name": "dut", "coded": True}, "Coded PHY"),
        ({"rssi_cut_off": -5}, "between -255 and -10"),
        ({"rssi_cut_off": -300}, "between -255 and -10"),
    ])
    def test_rejects(self, kwargs, match):
        with pytest.raises(ValueError, match=match):
            CaptureOptions(**kwargs).validate()


class TestProtocol:

    def test_slip_escapes_round_trip_across_split_feeds(self):
        frame = bytes((0x01, 0xAB, 0xBC, 0xCD, 0x02))
        encoded = nrf.slip_encode(frame)
        assert encoded == bytes.fromhex("ab01cdaccdbdcdce02bc")
        decoder = nrf.SlipDecoder()
        assert decoder.feed(b"\x55" + encoded[:4]) == []
        assert decoder.feed(encoded[4:]) == [frame]

    def test_slip_drops_a_frame_that_never_ends(self):
        decoder = nrf.SlipDecoder()
        assert decoder.feed(b"\xab" + b"\x01" * 2000 + b"\xbc") == []
        assert decoder.feed(nrf.slip_encode(b"ok")) == [b"ok"]

    def test_request_header_is_protocol_1(self):
        assert nrf.encode_request(0x1B, b"", 0x0102) == bytes.fromhex("ab0600010201" "1bbc")
        assert nrf.encode_request(0x07, b"\x03", 4) == bytes.fromhex("ab06010104000703bc")

    @pytest.mark.parametrize("raw, expected", [
        # Protocols 2 and 3 have a little-endian 2-byte payload length, as in
        # Wireshark's packet-nordic_ble.c; protocol 1 a header length byte.
        (bytes.fromhex("050003b9091c352e302e30"), (3, 0x1C, b"5.0.0")),
        (bytes.fromhex("050002b9091c352e302e30"), (2, 0x1C, b"5.0.0")),
        (bytes.fromhex("060501b9091c352e302e30"), (1, 0x1C, b"5.0.0")),
        (bytes.fromhex("050003b9091c352e"), None),  # payload cut short
        (bytes.fromhex("000009000002"), None),  # unknown protocol
        (b"\x00\x00", None),
    ])
    def test_parse_frame(self, raw, expected):
        frame = nrf.parse_frame(raw)
        assert (frame and (frame.version, frame.packet_id, frame.payload)) == expected

    def test_pcap_frame_matches_the_nordic_tool(self):
        assert nrf.pcap_frame(_parsed(_REAL_FRAME)) == _REAL_PCAP_FRAME

    def test_pcap_frame_on_coded_phy_keeps_the_coding_indicator(self):
        raw = _adv_event("11:22:33:44:55:66", coded=True)
        cut = nrf.pcap_frame(_parsed(raw))
        assert len(cut) == len(raw) - 1
        # Metadata (10), access address (4), CI (1), then the 2-byte header.
        assert cut[6 + 15:6 + 17] == raw[6 + 15:6 + 17]
        assert cut[6 + 17:] == raw[6 + 18:]

    def test_pcap_frame_leaves_other_frames_alone(self):
        version = bytes.fromhex("050003b9091c352e302e30")
        assert nrf.pcap_frame(_parsed(version)) == version

    def test_protocol_2_advertising_is_told_by_access_address(self):
        adv = _parsed(_adv_event("11:22:33:44:55:66")[:2] + b"\x02" + _adv_event(
            "11:22:33:44:55:66")[3:5] + b"\x06" + _adv_event("11:22:33:44:55:66")[6:])
        data = _parsed(_data_event())
        assert adv.version == 2 and adv.packet_id == 0x06
        assert nrf.is_advertising(adv)
        assert not nrf.is_advertising(data)
        assert nrf.firmware_timestamp_us(adv) is None

    def test_follow_payload_keeps_the_written_byte_order(self):
        # Checked on a bench dongle: the reversed order follows nothing.
        assert nrf.follow_payload("AA:BB:CC:DD:EE:FF", "public", 0x05) == \
            bytes.fromhex("aabbccddeeff000005")
        assert nrf.follow_payload("aa-bb-cc-dd-ee-ff", "random", 0) == \
            bytes.fromhex("aabbccddeeff010000")

    def test_adv_address_and_name(self):
        named = _parsed(_adv_event("11:22:33:44:55:66", name="DUT"))
        assert nrf.adv_address(named) == ("11:22:33:44:55:66", "random")
        assert nrf.adv_name(named) == "DUT"
        coded = _parsed(_adv_event("11:22:33:44:55:66", random=False, name="x", coded=True))
        assert nrf.adv_address(coded) == ("11:22:33:44:55:66", "public")
        assert nrf.adv_name(_parsed(_adv_event("11:22:33:44:55:66"))) is None
        bad_crc = _parsed(_adv_event("11:22:33:44:55:66", name="DUT", crc_ok=False))
        assert nrf.adv_name(bad_crc) is None and nrf.adv_address(bad_crc) is None
        assert nrf.adv_address(_parsed(_REAL_FRAME)) == ("45:83:A7:51:5E:C9", "random")
        assert nrf.adv_name(_parsed(_REAL_FRAME)) is None  # manufacturer data only
        assert nrf.adv_address(_parsed(_data_event())) is None

    def test_clock_follows_the_firmware_and_its_wrap(self, monkeypatch):
        monkeypatch.setattr(nrf, "time", SimpleNamespace(time=lambda: 100.0))
        clock = nrf._Clock()
        stamps = [clock.stamp(_parsed(_adv_event("11:22:33:44:55:66", ts_us=ts)))
                  for ts in (0xFFFFF000, 0xFFFFFFFF, 0x00000F00, 0x00000E00)]
        assert stamps[0] == 100.0
        assert stamps[1] == pytest.approx(100.0 + 0xFFF / 1e6)
        assert stamps[2] == pytest.approx(100.0 + (0x1000 + 0xF00) / 1e6)
        assert stamps[3] == pytest.approx(stamps[2] - 0x100 / 1e6)  # reordered, not a wrap

    def test_open_serial_failure_is_a_backend_error(self, tmp_path):
        with pytest.raises(BleSnifferBackendError, match="Cannot open sniffer port"):
            nrf.open_serial(str(tmp_path / "ttyACM0"))

    def test_open_serial_busy_names_another_program(self, monkeypatch):
        busy = serial.SerialException(16, "could not open port: Device or resource busy")
        monkeypatch.setattr(nrf.serial, "Serial", MagicMock(side_effect=busy))
        with pytest.raises(BleSnifferBackendError, match="in use by another program"):
            nrf.open_serial("/dev/ttyACM1")


class TestHelpers:

    def test_serial_from_address(self):
        assert nrf.serial_from_address(_ADDRESS) == _SERIAL

    def test_address_without_serial(self):
        with pytest.raises(BleSnifferBackendError, match="no USB serial number"):
            nrf.serial_from_address("USB0::0x1915::0x522A::::INSTR")

    @pytest.mark.parametrize("interfaces, expected", [
        ({"ttyACM0": 0, "ttyACM1": 2}, "/dev/ttyACM1"),  # firmware 5.x: if00 and if02
        ({"ttyACM3": 0}, "/dev/ttyACM3"),                 # firmware 4.x: one interface
    ])
    def test_find_sniffer_tty_takes_the_highest_interface(self, tmp_path, monkeypatch,
                                                          interfaces, expected):
        for name in [*interfaces, "ttyACM7"]:
            (tmp_path / name).mkdir()
        idents = {f"/dev/{name}": {"vid": "1915", "pid": "522a", "serial": _SERIAL,
                                   "interface": number}
                  for name, number in interfaces.items()}
        # Another device's tty is never picked.
        idents["/dev/ttyACM7"] = {"vid": "1915", "pid": "522a", "serial": "OTHER",
                                  "interface": 5}
        monkeypatch.setattr(nrf, "_SYS_TTY", tmp_path)
        monkeypatch.setattr(nrf.serial_id, "identity_for_tty", idents.get)
        assert nrf.find_sniffer_tty(_SERIAL) == expected

    def test_find_sniffer_tty_without_the_sniffer(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nrf, "_SYS_TTY", tmp_path)
        assert nrf.find_sniffer_tty(_SERIAL) is None

    def test_count_pcap_packets_ignores_a_cut_record(self, tmp_path):
        path = tmp_path / "c.pcap"
        path.write_bytes(_pcap_bytes(2, truncated_tail=True))
        assert ble_sniffer_net.count_pcap_packets(path) == 2

    def test_count_pcap_packets_rejects_other_files(self, tmp_path):
        path = tmp_path / "c.pcap"
        path.write_bytes(b"not a pcap at all, just text....")
        with pytest.raises(BleSnifferBackendError, match="not a pcap file"):
            ble_sniffer_net.count_pcap_packets(path)

    def test_default_capture_path(self, tmp_path):
        path = ble_sniffer_net.default_capture_path(tmp_path, "my net")
        assert path.parent == tmp_path
        assert path.name.startswith("my_net-") and path.suffix == ".pcap"

    def test_delete_capture_stays_under_the_output_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ble_sniffer_net, "OUTPUT_ROOT", tmp_path)
        capture = tmp_path / "a.pcap"
        capture.write_bytes(b"x")
        assert ble_sniffer_net.delete_capture(capture) == capture.resolve()
        assert not capture.exists()
        with pytest.raises(ValueError, match="Only .pcap captures"):
            ble_sniffer_net.delete_capture(tmp_path.parent / "other.pcap")
        with pytest.raises(ValueError, match="Only .pcap captures"):
            ble_sniffer_net.delete_capture(tmp_path / "notes.txt")

    def test_lock_is_exclusive(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ble_sniffer_net, "_LOCK_DIR", tmp_path)
        assert not ble_sniffer_net.is_locked(_SERIAL)
        with ble_sniffer_net.device_lock(_SERIAL):
            assert ble_sniffer_net.is_locked(_SERIAL)
            with pytest.raises(BleSnifferBusyError, match="busy"), \
                    ble_sniffer_net.device_lock(_SERIAL):
                pass
        assert not ble_sniffer_net.is_locked(_SERIAL)


class TestCapture:

    def test_capture_records_packet_events_and_idles(self, firmware, driver, tmp_path):
        fw = firmware(adverts=[_REAL_FRAME, _adv_event("11:22:33:44:55:66")])
        out = tmp_path / "out" / "cap.pcap"
        result = driver.capture(CaptureOptions(duration=1.0), out)
        assert fw.ids()[:5] == [0xFE, 0x1B, 0x17, 0x07, 0x0C] and fw.ids()[-1] == 0xFE
        assert fw.payloads(0x17) == [bytes((3, 37, 38, 39))]
        assert fw.payloads(0x07) == [b"\x03"]  # scan responses and aux packets
        assert fw.payloads(0x0C) == [bytes(16)]
        assert 0x00 not in fw.ids()
        assert fw.closed
        records = _records(out)
        assert records[0][1] == b"\x00" + _REAL_PCAP_FRAME
        assert len(records) == result["packets"] > 0
        assert not out.with_name("cap.pcap.part").exists()
        assert result["file"] == str(out)
        assert result["bytes"] == out.stat().st_size
        assert result["port"] == "/dev/ttyACM9"
        assert result["firmware"] == "5.0.0"
        assert result["followed"] is None and result["follow_seen"] is None
        assert result["stopped"] is False
        assert result["duration"] >= 1.0

    def test_record_times_come_from_the_firmware_clock(self, firmware, driver, tmp_path):
        firmware(adverts=[_adv_event("11:22:33:44:55:66", ts_us=5_000_000),
                          _adv_event("11:22:33:44:55:66", ts_us=5_000_625)])
        out = tmp_path / "cap.pcap"
        driver.capture(CaptureOptions(duration=1.0), out)
        (t0, _), (t1, _) = _records(out)[:2]
        assert t1 - t0 == pytest.approx(625e-6, abs=2e-6)

    def test_follow_with_a_given_type_and_coded(self, firmware, driver, tmp_path):
        fw = firmware(adverts=[_REAL_FRAME])
        result = driver.capture(
            CaptureOptions(duration=1.0, follow="aa-bb-cc-dd-ee-ff", address_type="public",
                           only_advertising=True, coded=True),
            tmp_path / "cap.pcap")
        assert fw.payloads(0x07) == [b"\x07"]
        assert fw.payloads(0x00) == [bytes.fromhex("aabbccddeeff000005")]
        assert result["followed"] == "AA:BB:CC:DD:EE:FF"
        assert result["follow_seen"] is False

    def test_follow_takes_the_address_type_from_advertising(self, firmware, driver, tmp_path):
        fw = firmware(adverts=[_REAL_FRAME, _adv_event("11:22:33:44:55:66", random=False)])
        result = driver.capture(CaptureOptions(duration=1.0, follow="11:22:33:44:55:66"),
                                tmp_path / "cap.pcap")
        assert fw.payloads(0x00) == [bytes.fromhex("112233445566000000")]
        assert result["followed"] == "11:22:33:44:55:66"
        assert result["follow_seen"] is True

    def test_follow_of_a_device_never_heard(self, firmware, driver, tmp_path):
        fw = firmware(adverts=[_REAL_FRAME])
        result = driver.capture(CaptureOptions(duration=1.0, follow="11:22:33:44:55:66"),
                                tmp_path / "cap.pcap")
        assert 0x00 not in fw.ids()
        assert result["followed"] is None and result["follow_seen"] is False

    def test_follow_by_name_follows_once_the_name_is_seen(self, firmware, driver, tmp_path):
        fw = firmware(adverts=[_REAL_FRAME,
                               _adv_event("99:22:33:44:55:66", name="DUT", crc_ok=False),
                               _adv_event("11:22:33:44:55:66", name="DUT")])
        result = driver.capture(CaptureOptions(duration=1.0, follow_by_name="DUT"),
                                tmp_path / "cap.pcap")
        assert fw.payloads(0x00) == [bytes.fromhex("112233445566010000")]
        assert result["followed"] == "11:22:33:44:55:66"
        assert result["follow_seen"] is True

    def test_follow_by_name_without_that_name(self, firmware, driver, tmp_path):
        fw = firmware(adverts=[_adv_event("11:22:33:44:55:66", name="other")])
        result = driver.capture(CaptureOptions(duration=1.0, follow_by_name="DUT"),
                                tmp_path / "cap.pcap")
        assert 0x00 not in fw.ids()
        assert result["followed"] is None and result["follow_seen"] is False
        assert result["packets"] > 0

    def test_data_pdus_are_recorded_and_count_as_seen(self, firmware, driver, tmp_path):
        firmware(adverts=[_adv_event("11:22:33:44:55:66", rssi=-40), _data_event(rssi=-90)])
        out = tmp_path / "cap.pcap"
        result = driver.capture(CaptureOptions(duration=1.0, follow="11:22:33:44:55:66",
                                               address_type="random", rssi_cut_off=-60), out)
        ids = {record[1 + 5] for _, record in _records(out)}
        assert ids == {0x02, 0x06}  # the cut-off drops advertising only
        assert result["follow_seen"] is True

    def test_rssi_cut_off(self, firmware, driver, tmp_path):
        near = _adv_event("11:22:33:44:55:66", rssi=-40)
        firmware(adverts=[near, _adv_event("66:55:44:33:22:11", rssi=-80)])
        out = tmp_path / "cap.pcap"
        driver.capture(CaptureOptions(duration=1.0, rssi_cut_off=-60), out)
        records = [record for _, record in _records(out)]
        assert records and all(r == b"\x00" + nrf.pcap_frame(_parsed(near)) for r in records)

    def test_request_stop_ends_the_capture(self, firmware, driver, tmp_path):
        out = tmp_path / "cap.pcap"

        def stop_on_third(reads):
            if reads == 3:
                assert ble_sniffer_net.request_stop(_SERIAL) == out

        firmware(adverts=[_REAL_FRAME], on_scan_read=stop_on_third)
        result = driver.capture(CaptureOptions(duration=60.0), out)
        assert result["stopped"] is True
        assert result["duration"] < 1.0
        assert result["packets"] > 0
        assert ble_sniffer_net.request_stop(_SERIAL) is None

    def test_unplug_mid_capture_keeps_what_was_recorded(self, firmware, driver, tmp_path):
        fw = firmware(adverts=[_REAL_FRAME], fail_after=4)
        out = tmp_path / "cap.pcap"
        with pytest.raises(BleSnifferBackendError,
                           match=r"stopped answering during the capture .*read failed.* "
                                 r"The \d+ packets recorded before that are in .*cap.pcap"):
            driver.capture(CaptureOptions(duration=10.0), out)
        assert ble_sniffer_net.count_pcap_packets(out) > 0
        assert not out.with_name("cap.pcap.part").exists()
        assert fw.closed
        assert not ble_sniffer_net.is_locked(_SERIAL)

    def test_unplug_before_any_packet_leaves_no_file(self, firmware, driver, tmp_path):
        firmware(adverts=[], fail_after=0)
        out = tmp_path / "cap.pcap"
        with pytest.raises(BleSnifferBackendError, match="stopped answering .*unplugged"):
            driver.capture(CaptureOptions(duration=10.0), out)
        assert list(tmp_path.iterdir()) == [tmp_path / "lager-ble-sniffer-C2E15977A9D11709.lock"]

    def test_unresponsive_sniffer_fails_and_leaves_out_alone(self, firmware, driver, tmp_path):
        fw = firmware(silent=True, adverts=[_REAL_FRAME])
        out = tmp_path / "cap.pcap"
        out.write_bytes(b"an earlier capture")
        with pytest.raises(BleSnifferBackendError, match="did not answer.*replug"):
            driver.capture(CaptureOptions(duration=1.0), out)
        assert fw.ids() == [0xFE, 0x1B]
        assert fw.closed
        assert out.read_bytes() == b"an earlier capture"
        assert not out.with_name("cap.pcap.part").exists()

    def test_unknown_protocol_is_reported(self, firmware, driver, tmp_path):
        firmware(version_frame=bytes.fromhex("0500040000" "1c352e302e30"))
        with pytest.raises(BleSnifferBackendError, match="speaks serial protocol 4"):
            driver.capture(CaptureOptions(duration=1.0), tmp_path / "cap.pcap")

    def test_missing_dongle(self, firmware, driver, tmp_path, monkeypatch):
        firmware()
        monkeypatch.setattr(nrf, "find_sniffer_tty", lambda serial: None)
        with pytest.raises(BleSnifferBackendError, match="is not on USB"):
            driver.capture(CaptureOptions(duration=1.0), tmp_path / "cap.pcap")

    def test_busy_dongle(self, firmware, driver, tmp_path):
        fw = firmware()
        with ble_sniffer_net.device_lock(_SERIAL), pytest.raises(BleSnifferBusyError):
            driver.capture(CaptureOptions(duration=1.0), tmp_path / "cap.pcap")
        assert fw.requests == []

    def test_status_does_not_open_the_port(self, driver, monkeypatch):
        monkeypatch.setattr(nrf, "open_serial", MagicMock(side_effect=AssertionError))
        assert driver.status() == {"serial": _SERIAL, "port": "/dev/ttyACM9",
                                   "present": True, "busy": False}


class TestNet:

    def test_role_and_routing(self):
        assert NetType.from_role("ble-sniffer") is NetType.BleSniffer
        net = dispatcher.make_net("sniffer1", _SAVED[0])
        assert isinstance(net, BleSnifferNet)

    def test_unsupported_instrument(self):
        with pytest.raises(BleSnifferBackendError, match="Unsupported"):
            dispatcher.make_net("x", {"instrument": "Other", "address": _ADDRESS})

    def test_record_without_address(self):
        with pytest.raises(BleSnifferBackendError, match="no device address"):
            dispatcher.make_net("x", {"instrument": "Nordic_nRF_Sniffer"})

    def test_capture_defaults_to_the_run_output_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LAGER_OUTPUT_DIR", str(tmp_path))
        drv = MagicMock()
        drv.capture.return_value = {"packets": 0}
        BleSnifferNet("sniffer1", drv).capture(duration=2, follow_by_name="DUT")
        options, out = drv.capture.call_args.args
        assert options == CaptureOptions(duration=2, follow_by_name="DUT")
        assert out.parent == tmp_path and out.suffix == ".pcap"

    def test_capture_validates_before_the_driver(self):
        drv = MagicMock()
        with pytest.raises(ValueError):
            BleSnifferNet("sniffer1", drv).capture(duration=0)
        drv.capture.assert_not_called()

    def test_catalog_entry(self):
        assert SUPPORTED_USB["Nordic_nRF_Sniffer"] == {
            "vid": "1915", "pid": "522a", "net_type": ["ble-sniffer"]}
        assert CHANNEL_MAPS["Nordic_nRF_Sniffer"] == {"ble-sniffer": ["0"]}


@pytest.fixture
def post():
    app = Flask(__name__)
    net_command.register_net_command_routes(app)
    client = app.test_client()

    def _post(action, params=None, drv=None):
        drv = MagicMock() if drv is None else drv
        body = {"netname": "sniffer1", "action": action, "params": params or {}}
        with patch("lager.http_handlers.net_command.Net") as net_cls, \
                patch.object(dispatcher, "make_driver", return_value=drv):
            net_cls.get_local_nets.return_value = _SAVED
            resp = client.post("/net/command", json=body)
        return resp, drv

    return _post


class TestNetCommand:

    def test_start_then_job_then_stop(self, post, locks, tmp_path, monkeypatch):
        monkeypatch.setattr(ble_sniffer_net, "OUTPUT_ROOT", tmp_path)
        drv = StubDriver(records=2)
        resp, _ = post("start", {"duration": 60, "follow": "AA:BB:CC:DD:EE:FF",
                                 "address_type": "public", "rssi_cut_off": -70}, drv)
        assert resp.status_code == 200, resp.get_json()
        started = resp.get_json()["value"]
        assert started["state"] == "running"
        assert started["duration"] == 60.0
        assert pathlib.Path(started["file"]).parent == tmp_path
        resp, _ = post("job", {"job": started["job"]})
        assert resp.get_json()["value"]["state"] == "running"
        resp, _ = post("stop", drv=drv)
        assert resp.get_json()["value"] == {"stopped": True, "file": started["file"],
                                            "job": started["job"]}
        assert capture_jobs.wait_for(capture_jobs.get_job(started["job"]), 5)
        resp, _ = post("job", {"job": started["job"]})
        done = resp.get_json()["value"]
        assert done["state"] == "done" and done["result"]["stopped"] is True
        assert resp.get_json()["message"].startswith(f"Capture {started['job']} done: 2 packet")

    @pytest.mark.parametrize("params, message", [
        ({}, "job (the ID 'start' returned) is required"),
        ({"job": "nope"}, "No capture job nope"),
    ])
    def test_job_errors_are_400(self, post, params, message):
        resp, _ = post("job", params)
        assert resp.status_code == 400
        assert message in resp.get_json()["error"]

    def test_bad_option_is_400_without_touching_the_sniffer(self, post):
        drv = MagicMock(spec=StubDriver)
        resp, _ = post("start", {"only_advertising": True}, drv)
        assert resp.status_code == 400
        assert "only with follow" in resp.get_json()["error"]
        drv.capture.assert_not_called()

    @pytest.mark.parametrize("params, message", [
        ({"duration": None}, "duration must be a number"),
        ({"duration": [5]}, "duration must be a number"),
        ({"duration": 86401}, "between 1 and 86400"),
        ({"rssi_cut_off": "-60"}, "whole number"),
        ({"follow": 12}, "follow must be a string"),
        ({"follow_by_name": {"a": 1}}, "follow_by_name must be a string"),
    ])
    def test_wrong_types_and_ranges_are_400(self, post, params, message):
        drv = MagicMock(spec=StubDriver)
        resp, _ = post("start", params, drv)
        assert resp.status_code == 400, resp.get_json()
        assert message in resp.get_json()["error"]
        drv.capture.assert_not_called()

    def test_start_prunes_old_files_first(self, post, locks, tmp_path, monkeypatch):
        monkeypatch.setattr(ble_sniffer_net, "OUTPUT_ROOT", tmp_path)
        old, new, other = tmp_path / "old.pcap", tmp_path / "new.pcap.part", tmp_path / "x.txt"
        for path in (old, new, other):
            path.write_bytes(b"x")
        day_ago = os.path.getmtime(old) - ble_sniffer_net.KEEP_CAPTURES_S - 1
        os.utime(old, (day_ago, day_ago))
        os.utime(other, (day_ago, day_ago))
        drv = StubDriver(records=0, lose=True)
        resp, _ = post("start", {"duration": 1}, drv)
        assert resp.status_code in (200, 502)
        assert not old.exists() and new.exists() and other.exists()

    def test_stop_without_a_capture(self, post):
        resp, _ = post("stop", drv=StubDriver())
        assert resp.status_code == 200
        assert resp.get_json()["value"] == {"stopped": False, "file": None, "job": None}

    def test_busy_is_409(self, post, locks, tmp_path, monkeypatch):
        monkeypatch.setattr(ble_sniffer_net, "OUTPUT_ROOT", tmp_path)
        drv = StubDriver()
        with ble_sniffer_net.device_lock(drv.identity):
            resp, _ = post("start", {"duration": 2}, drv)
        assert resp.status_code == 409
        assert "busy" in resp.get_json()["error"]

    def test_backend_failure_is_502(self, post, locks, tmp_path, monkeypatch):
        monkeypatch.setattr(ble_sniffer_net, "OUTPUT_ROOT", tmp_path)
        resp, _ = post("start", {"duration": 2}, StubDriver(port=None))
        assert resp.status_code == 502
        assert "is not connected" in resp.get_json()["error"]

    def test_status_reports_the_latest_job(self, post, locks, tmp_path, monkeypatch):
        monkeypatch.setattr(ble_sniffer_net, "OUTPUT_ROOT", tmp_path)
        drv = StubDriver(records=1)
        resp, _ = post("status", drv=drv)
        assert resp.get_json()["value"]["job"] is None
        started = post("start", {"duration": 30}, drv)[0].get_json()["value"]
        status = post("status", drv=drv)[0].get_json()
        assert status["value"]["job"]["job"] == started["job"]
        assert status["message"] == "Sniffer on /dev/stub0, busy with a capture"
        ble_sniffer_net.request_stop(drv.identity)
        assert capture_jobs.wait_for(capture_jobs.get_job(started["job"]), 5)

    @pytest.mark.parametrize("status, message", [
        ({"serial": "S", "port": None, "present": False, "busy": False},
         "Sniffer S not found on USB"),
        ({"serial": "S", "port": "/dev/ttyACM0", "present": True, "busy": True},
         "Sniffer on /dev/ttyACM0, busy with a capture"),
        ({"serial": "S", "port": "/dev/ttyACM0", "present": True, "busy": False},
         "Sniffer on /dev/ttyACM0, idle"),
    ])
    def test_status(self, post, status, message):
        drv = MagicMock()
        drv.status.return_value = dict(status)
        resp, _ = post("status", drv=drv)
        assert resp.status_code == 200
        assert resp.get_json()["message"] == message

    def test_delete(self, post, tmp_path, monkeypatch):
        monkeypatch.setattr(ble_sniffer_net, "OUTPUT_ROOT", tmp_path)
        capture = tmp_path / "s.pcap"
        capture.write_bytes(b"x")
        resp, _ = post("delete", {"file": str(capture)})
        assert resp.status_code == 200
        assert resp.get_json()["value"]["deleted"] is True
        resp, _ = post("delete", {"file": str(capture)})
        assert resp.get_json()["value"]["deleted"] is False

    def test_delete_outside_the_output_root_is_400(self, post, tmp_path, monkeypatch):
        monkeypatch.setattr(ble_sniffer_net, "OUTPUT_ROOT", tmp_path / "root")
        resp, _ = post("delete", {"file": os.fspath(tmp_path / "x.pcap")})
        assert resp.status_code == 400

    def test_unknown_action(self, post):
        resp, _ = post("stream")
        assert resp.status_code == 400
        assert "Unknown action 'stream'" in resp.get_json()["error"]
