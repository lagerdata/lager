# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Bluetooth LE sniffer net interface.

A ble-sniffer net is one sniffer device, such as an nRF52840 Dongle running
Nordic's nRF Sniffer for Bluetooth LE firmware. A capture records the radio
traffic the sniffer hears for a fixed time and writes it to a pcap file.

A capture holds the device for its whole duration, so it does not run inside
hardware_service, whose calls stop after 30 seconds. It runs in the caller's
process: a background job of the box HTTP server for the CLI (see
``capture_jobs``), or the script under ``lager python``. An exclusive
``flock`` on a file in ``/tmp`` (shared by every process in the container)
keeps two captures off one device; the second one gets
``BleSnifferBusyError``. A capture registers itself with ``capture_control``
so ``request_stop`` from another thread of the same process can end it early.

``BleSnifferBase.capture`` does everything that is the same for every
sniffer: checking options, the lock, the ``.part`` file, stop registration,
a device lost mid-capture, and the common result keys. A driver supplies
``port()``, ``missing_message()``, ``LINKTYPE`` and ``_record()``.

Option names are the same in the driver, the box HTTP handler and the CLI.
A bad option raises ``ValueError`` before the device is touched, so the box
answers 400 with the message.
"""

from __future__ import annotations

import fcntl
import os
import re
import struct
import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from lager.exceptions import BleSnifferBackendError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

#: Capture length limits, in seconds. An attached ``lager python`` run still
#: ends at 300 s, start-up included.
MIN_DURATION_S = 1.0
MAX_DURATION_S = 86400.0

#: Where the box HTTP handler writes captures. It is under /tmp/lager-output
#: because GET /download-file serves only that tree.
OUTPUT_ROOT = Path("/tmp/lager-output/ble-sniffer")

#: How long the box keeps a capture under ``OUTPUT_ROOT`` that nobody deleted, in seconds.
KEEP_CAPTURES_S = 86400.0

#: RSSI cut-off limits in dBm.
RSSI_MIN_DBM = -255
RSSI_MAX_DBM = -10

ADDRESS_TYPES = ("public", "random")

_ADDRESS_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
_LOCK_DIR = Path("/tmp")
# A status check takes the lock for an instant; a capture starting at that
# moment retries for this long before calling the sniffer busy.
_LOCK_RETRY_S = 0.3
_LOCK_POLL_S = 0.02
_PCAP_HEADER_LEN = 24
_PCAP_RECORD_HEADER_LEN = 16
_PCAP_MAGIC = {b"\xd4\xc3\xb2\xa1": "<", b"\xa1\xb2\xc3\xd4": ">",
               b"\x4d\x3c\xb2\xa1": "<", b"\xa1\xb2\x3c\x4d": ">"}


class BleSnifferBusyError(BleSnifferBackendError):
    """Another capture holds the sniffer. The box answers 409."""


@dataclass(frozen=True)
class CaptureOptions:
    """One capture request, checked by ``validate()``.

    ``follow`` is a Bluetooth LE device address (``AA:BB:CC:DD:EE:FF``).
    ``address_type`` says whether it is a public or a random address; None
    takes the type from the device's first advertisement.
    ``follow_by_name`` follows the first device whose legacy advertising
    carries this exact name. ``only_advertising`` needs one of the two.
    ``rssi_cut_off`` drops advertising packets weaker than this many dBm.
    """

    duration: float = 10.0
    follow: str | None = None
    address_type: str | None = None
    follow_by_name: str | None = None
    only_advertising: bool = False
    coded: bool = False
    rssi_cut_off: int | None = None

    def validate(self) -> None:
        """Check the request without touching the device.

        Raises:
            ValueError: An option has the wrong type, is out of range, or
                conflicts with another.
        """
        # Wrong types raise ValueError like the other checks, so the box HTTP
        # handler answers 400 for all of them.
        if isinstance(self.duration, bool) or not isinstance(self.duration, (int, float)):
            raise ValueError("duration must be a number of seconds")  # noqa: TRY004
        if not MIN_DURATION_S <= self.duration <= MAX_DURATION_S:
            raise ValueError(
                f"duration must be between {MIN_DURATION_S:g} and {MAX_DURATION_S:g} seconds")
        for name in ("follow", "follow_by_name", "address_type"):
            if getattr(self, name) is not None and not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")
        if self.rssi_cut_off is not None and (isinstance(self.rssi_cut_off, bool)
                                              or not isinstance(self.rssi_cut_off, int)):
            raise ValueError("rssi_cut_off must be a whole number of dBm")
        if self.follow is not None and not _ADDRESS_RE.match(self.follow):
            raise ValueError(
                f"Invalid Bluetooth LE address '{self.follow}'. Use AA:BB:CC:DD:EE:FF")
        if self.address_type is not None and self.address_type not in ADDRESS_TYPES:
            raise ValueError("address_type must be public or random")
        if self.address_type is not None and self.follow is None:
            raise ValueError("address_type applies only to follow")
        if self.follow is not None and self.follow_by_name is not None:
            raise ValueError("Give follow or follow_by_name, not both")
        if self.follow_by_name is not None and not self.follow_by_name.strip():
            raise ValueError("follow_by_name must not be empty")
        if self.only_advertising and self.follow is None and self.follow_by_name is None:
            raise ValueError("only_advertising applies only with follow or follow_by_name")
        if self.coded and self.follow_by_name is not None:
            raise ValueError("follow_by_name cannot be used with coded: names are read from "
                             "legacy advertising, which is never sent on the LE Coded PHY")
        if self.rssi_cut_off is not None and not RSSI_MIN_DBM <= self.rssi_cut_off <= RSSI_MAX_DBM:
            raise ValueError(
                f"rssi_cut_off must be between {RSSI_MIN_DBM} and {RSSI_MAX_DBM} dBm")


def default_capture_path(directory: Path | str, net_name: str) -> Path:
    """A new capture file name in ``directory``: ``<net>-<UTC time>.pcap``."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", net_name) or "sniffer"
    return Path(directory) / f"{safe}-{stamp}.pcap"


def count_pcap_packets(path: Path | str) -> int:
    """Number of complete records in a classic pcap file.

    A record cut short at the end of the file is not counted: a capture
    process killed mid-write can leave one.

    Raises:
        BleSnifferBackendError: The file is not a classic pcap file.
    """
    with open(path, "rb") as f:
        header = f.read(_PCAP_HEADER_LEN)
        order = _PCAP_MAGIC.get(header[:4])
        if order is None or len(header) < _PCAP_HEADER_LEN:
            raise BleSnifferBackendError(f"{path} is not a pcap file")
        count = 0
        while True:
            record = f.read(_PCAP_RECORD_HEADER_LEN)
            if len(record) < _PCAP_RECORD_HEADER_LEN:
                return count
            incl_len = struct.unpack(order + "IIII", record)[2]
            if len(f.read(incl_len)) < incl_len:
                return count
            count += 1


def delete_capture(path: Path | str) -> Path:
    """Delete a capture file the box HTTP handler wrote.

    Only ``.pcap`` files under ``OUTPUT_ROOT`` can be deleted, so a request
    cannot remove anything else on the box.

    Raises:
        ValueError: The path is outside ``OUTPUT_ROOT`` or not a ``.pcap`` file.
        FileNotFoundError: The file does not exist.
    """
    target = Path(path).resolve()
    root = OUTPUT_ROOT.resolve()
    if not target.is_relative_to(root) or target.suffix != ".pcap":
        raise ValueError(f"Only .pcap captures under {OUTPUT_ROOT} can be deleted")
    target.unlink()
    return target


def prune_captures(max_age_s: float = KEEP_CAPTURES_S) -> list[Path]:
    """Delete ``.pcap`` and ``.pcap.part`` files under ``OUTPUT_ROOT`` older than ``max_age_s``.

    Returns the deleted paths. A file another process removes first is skipped.
    """
    if not OUTPUT_ROOT.is_dir():
        return []
    cutoff = time.time() - max_age_s
    deleted = []
    for path in OUTPUT_ROOT.iterdir():
        if not path.name.endswith((".pcap", ".pcap.part")):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted.append(path)
        except FileNotFoundError:
            continue
    return deleted


@contextmanager
def device_lock(identity: str, retry_s: float = _LOCK_RETRY_S) -> Iterator[None]:
    """Hold the exclusive capture lock for one sniffer, or fail within ``retry_s``.

    ``identity`` is the sniffer's USB serial number. ``flock`` locks are per
    open file and drop when the process exits, so a crashed capture never
    leaves the device locked.

    Raises:
        BleSnifferBusyError: Another capture holds the lock.
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", identity) or "unknown"
    path = _LOCK_DIR / f"lager-ble-sniffer-{safe}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        _flock(fd, identity, retry_s)
        yield
    finally:
        os.close(fd)


def _flock(fd: int, identity: str, retry_s: float) -> None:
    attempts = 1 + int(retry_s / _LOCK_POLL_S)
    for attempt in range(attempts):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as e:
            if attempt == attempts - 1:
                raise BleSnifferBusyError(
                    f"Sniffer {identity} is busy with another capture") from e
        time.sleep(_LOCK_POLL_S)


def is_locked(identity: str) -> bool:
    """Whether a capture holds the lock for ``identity`` right now."""
    try:
        with device_lock(identity, retry_s=0.0):
            return False
    except BleSnifferBusyError:
        return True


# Captures running in this process, by sniffer identity: the stop flag and the
# output file. Module-level because the box HTTP handler's stop request
# arrives on another request thread than the capture it ends.
_running: dict[str, tuple[threading.Event, Path]] = {}
_running_lock = threading.Lock()


@contextmanager
def capture_control(identity: str, out: Path) -> Iterator[threading.Event]:
    """Register a capture in this process; yields the event that asks it to stop."""
    stop = threading.Event()
    with _running_lock:
        _running[identity] = (stop, out)
    try:
        yield stop
    finally:
        with _running_lock:
            _running.pop(identity, None)


def request_stop(identity: str) -> Path | None:
    """Ask this process's capture on ``identity`` to stop; its file, or None if none runs."""
    with _running_lock:
        entry = _running.get(identity)
    if entry is None:
        return None
    entry[0].set()
    return entry[1]


class DeviceLost(Exception):
    """Raised by a driver's ``_record`` when the device stops answering mid-capture."""


class PcapWriter:
    """Classic little-endian pcap of one link type, flushed after every record."""

    def __init__(self, f: BinaryIO, linktype: int) -> None:
        self._f = f
        self.packets = 0
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, linktype))
        f.flush()

    def write(self, record: bytes, timestamp: float) -> None:
        """Append one record stamped with ``timestamp`` (seconds since the epoch)."""
        seconds = int(timestamp)
        micros = min(int((timestamp - seconds) * 1_000_000), 999_999)
        self._f.write(struct.pack("<IIII", seconds, micros, len(record), len(record)) + record)
        self._f.flush()
        self.packets += 1


@dataclass
class CaptureContext:
    """What ``BleSnifferBase.capture`` hands a driver's ``_record``.

    The driver writes records to ``writer``, calls ``started()`` once the
    device is recording (the measured duration starts there), and returns
    when ``options.duration`` has passed since then or ``stop`` is set.
    """

    writer: PcapWriter
    stop: threading.Event
    on_start: Callable[[CaptureContext], None] | None = None
    started_at: float | None = None

    def started(self) -> None:
        """Mark the start of recording and tell ``on_start``."""
        self.started_at = time.monotonic()
        if self.on_start is not None:
            self.on_start(self)

    @property
    def elapsed(self) -> float:
        """Seconds since ``started()``, or 0 before it."""
        return 0.0 if self.started_at is None else time.monotonic() - self.started_at


class BleSnifferBase(ABC):
    """A sniffer driver. Subclasses set ``identity`` (the device's unique ID) and ``LINKTYPE``.

    The device stays unopened until ``capture``; ``status`` never opens it.
    """

    #: pcap link type of the records ``_record`` writes.
    LINKTYPE: int
    identity: str

    @abstractmethod
    def port(self) -> str | None:
        """The device's port now, or None when it is not connected."""

    @abstractmethod
    def missing_message(self) -> str:
        """The error text when ``port()`` finds no device."""

    @abstractmethod
    def _record(self, port: str, options: CaptureOptions, ctx: CaptureContext) -> dict:
        """Open ``port``, record to ``ctx.writer`` and close it; called with the lock held.

        Returns driver-specific result keys. Raises ``DeviceLost`` when the
        device stops answering mid-capture, ``BleSnifferBackendError`` for
        any other device failure.
        """

    def capture(self, options: CaptureOptions, out: Path | str,
                on_start: Callable[[CaptureContext], None] | None = None) -> dict:
        """Capture for ``options.duration`` seconds and write a pcap to ``out``.

        The pcap is written to ``<out>.part`` and renamed to ``out`` when the
        capture ends, so ``out`` is never left half-written or truncated by a
        device that fails at the start. ``on_start`` is called with the
        context once the device records.

        Returns ``{"file", "bytes", "packets", "duration", "port", "stopped"}``
        and the driver's own keys: ``duration`` is the measured capture time
        in seconds and ``stopped`` whether ``request_stop`` ended it early.

        Raises:
            ValueError: An option is invalid.
            BleSnifferBusyError: Another capture holds the device.
            BleSnifferBackendError: The device is missing or the capture
                failed. When it stops answering mid-capture, the packets
                recorded until then are kept in ``out`` and the message says
                how many.
        """
        options.validate()
        out = Path(out)
        part = out.with_name(out.name + ".part")
        out.parent.mkdir(parents=True, exist_ok=True)
        with device_lock(self.identity):
            port = self.port()
            if port is None:
                raise BleSnifferBackendError(self.missing_message())
            try:
                ctx, extra = self._record_to(port, options, part, out, on_start)
            except BaseException:
                part.unlink(missing_ok=True)
                raise
        os.replace(part, out)
        return {**extra, "file": str(out), "bytes": out.stat().st_size,
                "packets": count_pcap_packets(out), "port": port,
                "duration": round(ctx.elapsed, 3), "stopped": ctx.stop.is_set()}

    def _record_to(self, port: str, options: CaptureOptions, part: Path, out: Path,
                   on_start: Callable[[CaptureContext], None] | None,
                   ) -> tuple[CaptureContext, dict]:
        with open(part, "wb") as f, capture_control(self.identity, out) as stop:
            ctx = CaptureContext(PcapWriter(f, self.LINKTYPE), stop, on_start)
            try:
                return ctx, self._record(port, options, ctx)
            except DeviceLost as e:
                lost = e
        if ctx.writer.packets == 0:
            raise BleSnifferBackendError(
                f"Sniffer {self.identity} stopped answering ({lost}). It may have been "
                f"unplugged.") from lost
        os.replace(part, out)
        raise BleSnifferBackendError(
            f"Sniffer {self.identity} stopped answering during the capture ({lost}). It may "
            f"have been unplugged. The {ctx.writer.packets} packets recorded before that are "
            f"in {out}.") from lost

    def status(self) -> dict:
        """Report ``{"serial", "port", "present", "busy"}`` without opening the device.

        ``serial`` is the device's ``identity``.
        """
        port = self.port()
        return {"serial": self.identity, "port": port, "present": port is not None,
                "busy": is_locked(self.identity)}


class BleSnifferNet:
    """The object ``Net.get(name, NetType.BleSniffer)`` returns.

    Calls the driver in this process. A capture blocks for its duration.
    """

    def __init__(self, name: str, driver: BleSnifferBase) -> None:
        self.name = name
        self._driver = driver

    def capture(
        self,
        duration: float = 10.0,
        follow: str | None = None,
        address_type: str | None = None,
        follow_by_name: str | None = None,
        only_advertising: bool = False,
        coded: bool = False,
        rssi_cut_off: int | None = None,
        out: Path | str | None = None,
    ) -> dict:
        """Capture Bluetooth LE traffic to a pcap file; see ``BleSnifferBase.capture``.

        ``out`` defaults to a new file in ``lager.output_dir()``, which is the
        run's folder under ``lager python``.
        """
        options = CaptureOptions(
            duration=duration, follow=follow, address_type=address_type,
            follow_by_name=follow_by_name, only_advertising=bool(only_advertising),
            coded=bool(coded), rssi_cut_off=rssi_cut_off,
        )
        options.validate()
        if out is None:
            from lager.run_assertions import output_dir

            out = default_capture_path(output_dir(), self.name)
        return self._driver.capture(options, out)

    def status(self) -> dict:
        """Report the device port and lock state."""
        return self._driver.status()

    def __str__(self) -> str:
        return f"<BleSnifferNet {self.name}>"
