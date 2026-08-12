# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
AcronameUSBNet – driver for USBHub2x4 / USBHub3p / USBHub3c.

Implements: enable / disable / toggle / state
Lazy-imports BrainStem to minimise start-up cost.
"""

from __future__ import annotations

import logging
import time

from lager.util.usb_sysfs import enumerate_usb_devices

from .usb_net import (
    HUB_ABSENT,
    HUB_OPEN_FAILED,
    HUB_OP_TIMEOUT_S,
    HUB_SERIAL_MISMATCH,
    HUB_UNREACHABLE,
    USBNet,
    LibraryMissingError,
    DeviceNotFoundError,
    PortStateError,
    hub_access,
    run_hub_op,
)

# BrainStem/USB access to a hub is EXCLUSIVE, and the hub is driven from several
# box processes — box_http_server (the `lager usb` path), the MCP server, and
# each `lager python` test (its own subprocess). Serialise their access with the
# shared cross-process device lock (fcntl.flock, shared /tmp) and NEVER hold a
# hub connected between operations, so no process pins it open and blocks the
# others. Mirrors the YKUSH driver; see ykush.py.
_LOCK_TIMEOUT_S = 10.0

# Acroname Inc. One physical hub enumerates under MORE THAN ONE product id (the
# control endpoint and the hub silicon are separate USB devices), and the hub
# component's iSerial descriptor may be empty even when the control endpoint's
# carries a serial. So the sysfs cross-check below filters on VENDOR only: a pid
# filter taken from the net's address would hide half of what is on the bus,
# which is the half that tells you the hub is physically there.
_ACRONAME_VID = "24ff"

# The open-failure summary is appended to a DeviceNotFoundError message, which
# reaches a user as a `lager nets state` footnote. Bounded so a bench with many
# hubs cannot turn one line into a screenful; the box log gets it uncapped.
_OPEN_FAILURE_DETAIL_MAX = 240
_OPEN_FAILURE_MAX_SERIALS = 6

# How long to let the bus settle before the one retry in ``_with_hub``. Sized
# for a re-enumeration, which the kernel completes in well under half a second
# (the control endpoint reappears ~0.4s after its hub component); long enough to
# clear that race, short enough that a user-typed command does not feel hung.
_OPEN_RETRY_SETTLE_S = 0.5

# What to DO about each classification. The remedies are deliberately different
# from each other -- that difference is the entire point of classifying, and a
# reason that does not change what the reader does next is not worth printing.
_REMEDIES = {
    HUB_UNREACHABLE: ("the hub is on the USB bus but is not answering "
                      "BrainStem; check hub power and the upstream cable"),
    HUB_ABSENT: ("no Acroname device is on the USB bus; the hub is unplugged "
                 "or its upstream port is off"),
    HUB_SERIAL_MISMATCH: ("Acroname devices are on the bus but none has this "
                          "serial; check the net's address"),
    HUB_OPEN_FAILED: "the hub would not open; see `lager diagnose <net>`",
}

logger = logging.getLogger(__name__)


def _remedy_for(code):
    """The one-sentence "what to do next" for a classification."""
    return _REMEDIES.get(code) or _REMEDIES[HUB_OPEN_FAILED]


def _open_failure_detail(diag, sysfs):
    """One-line summary of why a hub would not open, for the exception message.

    Pure and deterministic: same diag in, same string out, no clock and no
    dict-iteration order. That matters because every net on a failed hub gets
    this same string as its reason, and the CLI groups identical reasons into a
    single footnote line rather than repeating one per net.
    """
    head = []

    cache = diag.get("cache")
    if cache and cache != "miss":
        head.append(f"cached spec: {cache}")

    discovery = diag.get("discovery") or "not attempted"
    serials = list(diag.get("spec_serials") or [])
    if serials:
        shown = serials[:_OPEN_FAILURE_MAX_SERIALS]
        extra = len(serials) - len(shown)
        listed = ", ".join(shown) + (f", +{extra} more" if extra else "")
        discovery = f"{discovery} [{listed}]"
    match = diag.get("spec_match")
    if match is False:
        discovery += ", no serial match"
    elif isinstance(match, str):
        discovery += f", {match}"
    head.append(f"discovery: {discovery}")

    # Every hub class is tried in turn and they usually fail identically, so
    # collapse runs of the same result rather than spending the budget saying
    # rc=7 three times.
    attempts = []
    for item in (diag.get("attempts") or []):
        if attempts and attempts[-1][0] == item:
            attempts[-1][1] += 1
        else:
            attempts.append([item, 1])
    attempts_s = "; ".join(
        f"{text} (x{n})" if n > 1 else text for text, n in attempts
    )

    # The bus verdict is the most valuable line here -- it is what separates a
    # vendor-library fault from an unplugged cable -- so it is reserved before
    # the attempt list gets any budget, and the attempt list is what gets cut.
    # Nothing is lost: the log line above carries all of it uncapped.
    tail = f"sysfs: {sysfs or 'unavailable'}"
    head_s = "; ".join(head)
    if not attempts_s:
        return f"{head_s}; {tail}"

    room = _OPEN_FAILURE_DETAIL_MAX - len(head_s) - len(tail) - len("; attempts: ; ")
    if room < 12:
        # No room to say anything useful about the attempts; drop them whole.
        return f"{head_s}; {tail}"
    if len(attempts_s) > room:
        attempts_s = attempts_s[:room - 3].rstrip() + "..."
    return f"{head_s}; attempts: {attempts_s}; {tail}"


class AcronameUSBNet(USBNet):
    """USBNet driver for Acroname STEM hubs (0-based port numbers).

    Each net binds the *specific* hub named by its address serial, so a box
    with more than one Acroname hub routes every net to the right hardware. A
    fresh connection is opened per operation and disconnected immediately after
    (under a cross-process lock), so the hub is never left claimed — which would
    otherwise block another process (e.g. a `lager python` test) from opening it.

    To keep each open cheap, DISCOVERY metadata (the hub's link Spec and the
    hub class that bound it) is cached per physical hub — never the live
    connection. A cached open is a direct ``connectFromSpec`` with no USB
    discovery scan; a stale cache entry (hub re-enumerated, unplugged) fails
    the connect, is invalidated, and the full discovery path runs again.
    """

    # This driver holds no USB handle between calls: `_with_hub` opens,
    # operates and disconnects every time, and `_conn_cache` stores discovery
    # metadata rather than a connection. So a re-enumeration has nothing here to
    # orphan, and the service self-restart that repairs an orphaned context
    # cannot repair anything on this path — it would only drop every other
    # in-flight box operation. Measured on a two-hub bench: a hub that would not
    # open triggered a restart, and the respawned process, with a brand-new
    # libusb context, failed identically 37s later.
    holds_usb_context_between_ops = False

    _brainstem = None         # cached vendor MODULE (an import, not a handle)
    _Result = None            # brainstem.result.Result alias
    # Per-hub discovery cache: lock key -> {"cls": hub class, "spec": link
    # Spec or None}. Metadata only — caching a live handle would pin the
    # hub's exclusive USB claim and block other processes (the bug fixed by
    # open/operate/close per op).
    _conn_cache: dict = {}

    # ------------------------------------------------------------------ #
    # helper: import BrainStem only when needed
    # ------------------------------------------------------------------ #
    def _require_library(self):
        if AcronameUSBNet._brainstem is not None:
            return  # already imported

        try:
            import brainstem  # pylint: disable=import-error
            from brainstem.result import Result
        except ModuleNotFoundError as exc:
            raise LibraryMissingError(
                "BrainStem Python SDK not installed inside the box "
                "(pip install brainstem)."
            ) from exc

        AcronameUSBNet._brainstem = brainstem
        AcronameUSBNet._Result = Result

    # ------------------------------------------------------------------ #
    # address parsing
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_address(address):
        """Pull (serial, pid) out of a VISA-style address.

        e.g. 'USB0::0x24FF::0x0013::BFABDDC4::INSTR' -> (0xBFABDDC4, 0x0013).
        Returns (None, None) for anything that doesn't match.
        """
        if not address:
            return (None, None)
        parts = str(address).split("::")
        serial = pid = None
        if len(parts) >= 4:
            try:
                serial = int(parts[3], 16)
            except ValueError:
                serial = None
        if len(parts) >= 3:
            try:
                pid = int(parts[2], 16)
            except ValueError:
                pid = None
        return (serial, pid)

    # ------------------------------------------------------------------ #
    # constructor — remembers which physical hub this net belongs to
    # ------------------------------------------------------------------ #
    def __init__(self, net_info: dict | None = None) -> None:
        net_info = net_info or {}
        self.address = net_info.get("address")
        self._serial, self._pid = self._parse_address(self.address)

    # ------------------------------------------------------------------ #
    # cross-process lock + open/operate/close  (never cache a live hub)
    # ------------------------------------------------------------------ #
    def _lock_key(self) -> str:
        """Lock key identifying the *physical* hub, so all nets on one hub
        serialise but different hubs don't block each other."""
        return self.address or f"acroname::{self._serial}"

    def _transport(self):
        return self._brainstem.link.Spec.USB

    def _ordered_classes(self):
        """Hub classes to try, port-capable class first: binding an 8-port
        hub with the 4-port class would truncate ports 4-7. The PID from the
        address picks the best starting class; the rest are fallbacks."""
        stem = self._brainstem.stem
        hub3 = (stem.USBHub3p, stem.USBHub3c)   # 8-port families
        hub2 = (stem.USBHub2x4,)                # 4-port family
        return (hub2 + hub3) if self._pid == 0x0011 else (hub3 + hub2)

    @staticmethod
    def _fmt_serial(value):
        """Render a BrainStem serial the way an address writes it."""
        if value is None:
            return "(none)"
        if isinstance(value, int):
            return f"0x{value:08X}"
        return repr(value)

    def _discover_spec(self, diag):
        """One USB discovery scan → THIS hub's link Spec (or None).

        Best-effort: older BrainStem SDKs without ``discover.findAllModules``
        just return None and the driver falls back to per-class
        ``discoverAndConnect`` exactly as before.

        Records what the scan did into ``diag`` for the open-failure message.
        A bare ``None`` return cannot distinguish "the scan threw", "the scan
        found nothing" and "the scan found hubs but not this serial" — and on a
        multi-hub bench that difference is the whole diagnosis, because the
        serials the scan DID return say whether discovery is skipping one hub
        while finding its neighbour.

        Also sets ``diag["context_healthy"]``: whether this PROCESS could reach
        the USB bus at all, as distinct from whether it found our hub. Only a
        scan that came back with at least one device proves it — see the
        ``specs`` case below for why "the scan completed" is not enough.
        """
        discover = getattr(self._brainstem, "discover", None)
        find_all = getattr(discover, "findAllModules", None) if discover else None
        if find_all is None:
            # Never ran, so nothing was observed either way. Leave unknown.
            diag["discovery"] = "findAllModules unavailable (old SDK)"
            return None
        try:
            specs = find_all(self._transport()) or []
        except Exception as e:
            diag["discovery"] = f"findAllModules raised {type(e).__name__}: {e}"
            diag["context_healthy"] = False
            logger.debug("Acroname %s: findAllModules raised",
                         self._lock_key(), exc_info=True)
            return None
        # A scan returning at least one device is positive evidence that this
        # process walked the bus and got answers, so a hub it did not return is
        # the hub's problem and not this process's USB context.
        #
        # An EMPTY scan is deliberately NOT that evidence, and must stay
        # unknown: on a bench whose only hub is wedged, "my USB stack is fine
        # and the hub is silent" and "my USB stack is broken" both return zero
        # specs. Calling that healthy would suppress the one recovery that does
        # work in the second case.
        diag["context_healthy"] = True if specs else None
        diag["discovery"] = f"findAllModules ok, {len(specs)} spec(s)"
        diag["spec_serials"] = [
            self._fmt_serial(getattr(s, "serial_number", None)) for s in specs
        ]
        if self._serial is None:
            # No address to match against: only safe when exactly one hub.
            diag["spec_match"] = "no address serial to match"
            return specs[0] if len(specs) == 1 else None
        for spec in specs:
            if getattr(spec, "serial_number", None) == self._serial:
                diag["spec_match"] = True
                return spec
        diag["spec_match"] = False
        return None

    def _try_connect(self, candidate, spec_obj):
        """Connect one candidate hub object, preferring the scan-free
        ``connectFromSpec`` path.

        Returns ``(ok, detail)``. The detail carries the vendor return code or
        the exception text so a failed open can say WHICH step refused and with
        what code; previously both were discarded and every failure mode
        collapsed into the same bare "no hub detected".
        """
        if spec_obj is not None and hasattr(candidate, "connectFromSpec"):
            try:
                rc = candidate.connectFromSpec(spec_obj)
            except Exception as e:
                # A spec that no longer connects is stale (re-enumeration);
                # let the caller fall back to full discovery.
                return False, f"connectFromSpec {type(e).__name__}: {e}"
            return rc == self._Result.NO_ERROR, f"connectFromSpec rc={rc}"
        # Deliberately NOT wrapped: a raising discoverAndConnect already
        # propagates with its own type and message, which the dispatcher logs.
        # Catching it here would convert it into a DeviceNotFoundError and
        # change the exception type (and HTTP status) callers see.
        if self._serial is not None:
            rc = candidate.discoverAndConnect(self._transport(), self._serial)
        else:
            rc = candidate.discoverAndConnect(self._transport())
        return rc == self._Result.NO_ERROR, f"discoverAndConnect rc={rc}"

    def _open_hub(self):
        """Connect THIS net's hub. Never caches the connection — the caller
        disconnects it via ``_close_hub`` as soon as the operation completes.

        Fast path: reuse the cached discovery metadata (hub class + link
        Spec) so the open is a direct connect with no USB discovery scan.
        Slow path (first call, or stale cache): one discovery scan for the
        Spec, then the class-ordered connect loop; the winner is cached.
        """
        self._require_library()
        key = self._lock_key()
        diag = {
            "cache": "miss",
            "discovery": "not attempted",
            "spec_serials": [],
            "spec_match": False,
            "attempts": [],
            # None until a scan observes something either way; see _discover_spec.
            "context_healthy": None,
        }

        cached = AcronameUSBNet._conn_cache.get(key)
        if cached is not None:
            candidate = cached["cls"]()
            ok, detail = self._try_connect(candidate, cached["spec"])
            if ok:
                return candidate
            diag["cache"] = f"hit ({cached['cls'].__name__}), {detail}"
            # Failed connectFromSpec can still leave a partial USB claim on
            # some BrainStem builds — always release before rediscovering.
            self._close_hub(candidate)
            AcronameUSBNet._conn_cache.pop(key, None)

        spec_obj = self._discover_spec(diag)
        # Try with the spec first (scan-free connects); if that yields
        # nothing (e.g. an SDK whose connectFromSpec misbehaves), fall back
        # to the original per-class discoverAndConnect loop.
        attempts = [spec_obj, None] if spec_obj is not None else [None]
        for attempt_spec in attempts:
            how = "spec" if attempt_spec is not None else "discover"
            for cls in self._ordered_classes():
                candidate = cls()
                ok, detail = self._try_connect(candidate, attempt_spec)
                if ok:
                    AcronameUSBNet._conn_cache[key] = {
                        "cls": cls, "spec": attempt_spec,
                    }
                    return candidate
                diag["attempts"].append(f"{cls.__name__}/{how} {detail}")
                self._close_hub(candidate)

        # Everything refused. Say what was tried, and cross-check the bus: the
        # vendor library reporting "no hub" while the kernel plainly has one
        # enumerated is a different fault from an unplugged cable, and needs a
        # different remedy. Full detail to the log (uncapped); the remedy onto
        # the message, where a user will actually read it.
        code, sysfs = self._sysfs_classify()
        code = code or HUB_OPEN_FAILED

        # A hub the kernel has enumerated but that will not answer is ALWAYS
        # hardware and always worth acting on. A hub that is simply not on the
        # bus is a normal bench state — someone unplugged it — and logging both
        # at the same level is how a real fault sits unnoticed among routine
        # ones. Level is the only thing that differs; the text is identical.
        log = logger.error if code == HUB_UNREACHABLE else logger.warning
        log(
            "Acroname %s: hub would not open (%s). cache: %s; discovery: %s; "
            "scan serials: %s; serial match: %s; attempts: %s; sysfs: %s",
            key, code, diag["cache"], diag["discovery"],
            ", ".join(diag["spec_serials"]) or "(none)", diag["spec_match"],
            "; ".join(diag["attempts"]) or "(none)", sysfs or "unavailable",
        )

        serial = self._serial
        where = f" with serial 0x{serial:08X}" if serial is not None else ""
        # The message carries the REMEDY, not the return codes. Both used to
        # share it, and the remedy is what the reader needs: a wall of
        # `USBHub2x4/spec rc=7` buries the one sentence that says what to do.
        # The breakdown moves to `.detail`, which --json and diagnostics render.
        raise DeviceNotFoundError(
            f"No Acroname hub detected on USB{where}: {_remedy_for(code)}",
            classification=code,
            usb_context_healthy=diag.get("context_healthy"),
            detail=_open_failure_detail(diag, sysfs),
        )

    def _sysfs_acroname_report(self):
        """What the kernel sees on the bus, as a short phrase (or None)."""
        return self._sysfs_classify()[1]

    def _sysfs_classify(self):
        """Cross-check the bus: ``(classification, phrase)``.

        Independent of BrainStem: sysfs stays truthful even when the vendor
        SDK's discovery does not return a device, which is exactly the case
        this is here to name (issue #196).

        The classification is what a caller acts on; the phrase is what a human
        reads. They are produced together because they answer the same question
        and must never disagree — a phrase saying the serial is on the bus while
        the code says the hub is absent would be worse than either alone.

        Returns ``(None, None)`` when sysfs itself could not be read, which is
        "we could not tell", not "nothing is there". Best-effort by
        construction — the whole body is guarded, because this runs on the way
        to raising DeviceNotFoundError and must never be able to replace that
        with an error from the diagnostic itself.
        """
        try:
            devices = enumerate_usb_devices(vid=_ACRONAME_VID)
            if not devices:
                return (HUB_ABSENT,
                        f"no Acroname ({_ACRONAME_VID}) device on the bus")

            def _ids(dev):
                return f"{dev.get('vid') or '????'}:{dev.get('pid') or '????'}"

            listed = ", ".join(_ids(d) for d in devices)
            # A device with no iSerial descriptor can never match, so count it
            # separately rather than let it read as "the hub is not there".
            no_serial = sum(1 for d in devices if not (d.get("serial") or "").strip())
            unnamed = f", {no_serial} with no serial descriptor" if no_serial else ""

            if self._serial is None:
                # Hubs are present but the net names no serial to pick among
                # them. Classified with the mismatch case because the remedy is
                # the same one: the net's address is what needs fixing.
                return (HUB_SERIAL_MISMATCH,
                        f"{len(devices)} Acroname ({_ACRONAME_VID}) device(s) on the "
                        f"bus ({listed}){unnamed}; net address names no serial")

            want = self._norm_serial(self._serial)
            for dev in devices:
                if self._norm_serial(dev.get("serial")) == want:
                    return (HUB_UNREACHABLE,
                            f"serial present on the bus ({listed}) -- discovery "
                            f"did not return an enumerated hub")
            return (HUB_SERIAL_MISMATCH,
                    f"{len(devices)} Acroname ({_ACRONAME_VID}) device(s) on the bus "
                    f"({listed}), none with serial 0x{self._serial:08X}{unnamed}")
        except Exception:
            return (None, None)

    @staticmethod
    def _norm_serial(value):
        """Comparable form of a serial from either side of the fence.

        The net address parses to an int; sysfs hands back a hex string. Strip
        case, any 0x, and leading zeros so 0xBFABDDC4, "BFABDDC4" and
        "0bfabddc4" all compare equal.
        """
        if value is None:
            return None
        if isinstance(value, int):
            value = f"{value:x}"
        raw = str(value).strip().lower().removeprefix("0x").lstrip("0")
        return (raw or "0") if str(value).strip() else None

    @staticmethod
    def _close_hub(hub) -> None:
        """Disconnect the hub, releasing the USB claim. Best-effort."""
        if hub is None:
            return
        try:
            hub.disconnect()
        except Exception:
            pass

    def _with_hub(self, fn, *, retry=True, timeout=None):
        """Serialise across threads and processes, open a fresh hub connection,
        run ``fn(hub)``, and always disconnect so the hub is never left claimed.

        The whole cycle runs under a deadline, not just ``fn``: the open is the
        part most likely to hang. ``discoverAndConnect``/``connectFromSpec``
        are native BrainStem calls that block indefinitely against a hub whose
        USB link is wedged, and one of those used to take out every later USB
        command in the process.

        Retries the OPEN once when — and only when — the bus says our serial is
        there but discovery did not return it. That is the shape of a hub caught
        mid-re-enumeration, and the YKUSH driver has always self-healed it
        (``ykush.py`` ``_with_device``); this driver never did.

        Three things keep the retry from becoming a cost:

        * It is gated on evidence, not on hope. If the serial is NOT on the bus,
          a second attempt provably cannot connect either, so ``hub-absent`` and
          ``hub-serial-mismatch`` still cost exactly one attempt.
        * It is bounded: one retry and one short settle, never a loop.
        * ``states()`` passes ``retry=False``. That is the polling path — the
          state sweep and the TUI call it every few seconds against a budget
          shared with every other instrument on the bench, so a transient race
          is gone by the next poll anyway and a retry would only spend a budget
          that is not this hub's to spend. The one-shot user paths have their
          own timeout and are where a lost race actually costs a person a
          re-typed command.

        BOTH attempts run inside the ONE ``run_hub_op`` deadline, not around it.
        Retrying outside would hand each attempt its own ``HUB_OP_TIMEOUT_S``,
        so a hub that hangs on open could hold a caller for twice the bound the
        CLI is waiting on — turning the box's structured 504 into a transport
        timeout, which is the failure this deadline exists to prevent.

        The retry also stays inside ``hub_access``: dropping the lock between
        attempts would let another process open the hub in the gap, and the
        second attempt would then fail for a different reason than the first.
        """
        def _session():
            hub = None
            try:
                try:
                    hub = self._open_hub()
                except DeviceNotFoundError as first:
                    if not (retry and first.classification == HUB_UNREACHABLE):
                        raise
                    # The cached spec names an enumeration that is gone; drop it
                    # so the retry pays for a full rediscovery rather than
                    # repeating the same stale connect.
                    AcronameUSBNet._conn_cache.pop(self._lock_key(), None)
                    logger.info(
                        "Acroname %s: serial is on the bus but discovery missed "
                        "it; retrying the open once after %.1fs",
                        self._lock_key(), _OPEN_RETRY_SETTLE_S,
                    )
                    time.sleep(_OPEN_RETRY_SETTLE_S)
                    hub = self._open_hub()  # a second failure propagates
                return fn(hub)
            finally:
                self._close_hub(hub)

        key = self._lock_key()
        if timeout is None:
            with hub_access(key, timeout=_LOCK_TIMEOUT_S):
                return run_hub_op(key, _session, timeout=HUB_OP_TIMEOUT_S)
        # A caller-supplied budget bounds the WHOLE cycle: the lock wait and
        # the native open/read/close both count against it, so a contended
        # lock cannot silently double the caller's bound (issue #205).
        budget = min(timeout, HUB_OP_TIMEOUT_S)
        start = time.monotonic()
        with hub_access(key, timeout=min(_LOCK_TIMEOUT_S, budget)):
            remaining = max(0.5, budget - (time.monotonic() - start))
            return run_hub_op(key, _session, timeout=remaining)

    # ------------------------------------------------------------------ #
    # internal – decode enable+power bits
    # ------------------------------------------------------------------ #
    @staticmethod
    def _port_enabled(raw_state: int) -> bool:
        return (raw_state & 0b11) == 0b11

    def _read_enabled(self, hub, port: int) -> bool:
        """Read the live enabled/disabled state of a port from the hub."""
        res = hub.usb.getPortState(port)
        if res.error != self._Result.NO_ERROR:
            raise PortStateError(f"Acroname error code {res.error}")
        return self._port_enabled(res.value)

    # ------------------------------------------------------------------ #
    # USBNet interface
    # ------------------------------------------------------------------ #
    def enable(self, net_name: str, port: int) -> None:  # type: ignore[override]
        self._with_hub(lambda hub: hub.usb.setPortEnable(port))

    def disable(self, net_name: str, port: int) -> None:  # type: ignore[override]
        self._with_hub(lambda hub: hub.usb.setPortDisable(port))

    def state(self, net_name: str, port: int) -> bool:  # type: ignore[override]
        return self._with_hub(lambda hub: self._read_enabled(hub, port))

    def states(self, ports, *, timeout=None) -> dict:  # type: ignore[override]
        """Read every requested port inside ONE hub session.

        ``state()`` costs a full discoverAndConnect/read/disconnect cycle, all
        of it under this hub's lock, so reading an 8-port hub one net at a time
        pays that eight times over for eight register reads. Here the session is
        opened once and each read is just ``getPortState``.

        A port that will not read is recorded as None so it does not lose the
        other seven, but the failure is logged with the hub's own error code.
        It used to be swallowed entirely, which left a partial result with
        nothing anywhere saying which ports failed or why -- and a partial
        result is the one shape a request-deadline miss cannot produce, so it
        is the evidence that separates the two causes (issue #196).
        """
        def _read_all(hub):
            out = {}
            for port in ports:
                try:
                    out[port] = self._read_enabled(hub, port)
                except Exception as e:
                    # One unreadable port must not lose the other seven.
                    logger.warning(
                        "Acroname %s: port %s unreadable: %s: %s",
                        self._lock_key(), port, type(e).__name__, e,
                    )
                    out[port] = None
            return out

        # No open-retry here. This is the polling path: the whole-bench state
        # sweep budgets one deadline across EVERY instrument on the box, so a
        # retry would spend other instruments' time to win a race the next poll
        # wins for free. The one-shot paths retry; see _with_hub.
        return self._with_hub(_read_all, retry=False, timeout=timeout)

    def toggle(self, net_name: str, port: int) -> bool:  # type: ignore[override]
        def _do(hub):
            currently_on = self._read_enabled(hub, port)
            if currently_on:
                hub.usb.setPortDisable(port)
            else:
                hub.usb.setPortEnable(port)
            return not currently_on

        return self._with_hub(_do)
