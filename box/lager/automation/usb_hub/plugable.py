# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""PlugableUSBNet — switchable per-port USB power for Plugable RTS5411 docks.

Developed and hardware-validated against a Plugable UD-CAM. Matches VID:PID
2230:5411 — 0x2230 is Plugable's OWN vendor ID, and 0x5411 tracks the Realtek
RTS5411 hub silicon Plugable reuses across its dock line. The driver therefore
works on any Plugable RTS5411 hub that genuinely implements per-port power
switching, which is why it is named for the family and not for one SKU.

WHY THIS DRIVER LOOKS DIFFERENT FROM ITS PEERS
----------------------------------------------
Acroname ships the BrainStem SDK; Yepkit ships pykush. Plugable ships NOTHING —
no SDK, no firmware protocol, no control endpoint; the product is marketed as
driverless. The only control channel that exists is the STANDARD USB hub class
per-port power switching (PPPS) defined in USB 2.0 §11.24 — the same mechanism
uhubctl drives — issued here as pyusb control transfers.

pyusb is already a box dependency (docker/box.Dockerfile). uhubctl is not in the
box image, and adding it would gate every bench on a new image release, so the
control transfers are issued in-process instead of shelling out.

DOCK TOPOLOGY (confirmed on hardware)
-------------------------------------
The dock enumerates as TWO cascaded 4-port USB 2.0 hubs, not as a USB2/USB3
companion pair::

    HUB A (root tier, e.g. 1-1.4)      <- ALL FOUR PORTS ARE INTERNAL
      port 1: DP-AltMode Billboard
      port 2: USB audio codec
      port 3: the dock's own gigabit NIC
      port 4: HUB B                    <- the inter-hub link
        HUB B (user tier, e.g. 1-1.4.4)
          ports 1-4: the dock's four external USB-A sockets

So Hub A is never exposed: a lager port N maps to (Hub B, port N). That also
keeps the dock's NIC and the inter-hub link off the switchable surface entirely.

Hub B's four ports switch REAL VBUS -- proven by writing a volatile baud rate to
the CP2102N on port 4, cycling PORT_POWER, and reading the power-on default back
while the same marker survives a plain USBDEVFS_RESET. Hub A's internal ports do
not appear to switch: after ten seconds "off", CONNECT reasserts 0.3 ms after
SET_FEATURE, so the device never lost its D+ pull-up. That second finding is
inferred from timing, not a register witness. Both hubs advertise per-port
switching identically (wHubCharacteristics=0x00a9, PortPwrCtrlMask=0xff), so the
descriptor CANNOT be used to tell them apart -- only the topology can.

A PORT IS NOT OBSERVABLE WHILE IT IS OFF
---------------------------------------
While PORT_POWER is clear the hub raises no change bit, so the kernel never
polls the port and never processes the disconnect. The device keeps its sysfs
node, its `lsusb` line, and its /dev/ttyUSB*, for as long as the port stays off
(verified at 30 s; the kernel never re-powers it on its own). "USB disconnect"
is logged only when power RETURNS. Any check of the form "is the device gone
yet?" is therefore wrong in both directions -- see ``_verify_off``.

USB 3 -- IMPLEMENTED, NOT VALIDATED
-----------------------------------
A SuperSpeed-linked hub enumerates as two virtual hubs and VBUS drops only when
PORT_POWER is cleared on BOTH halves (this is what uhubctl does by default).
That pairing is implemented here, but the bench dock links at USB 2.0 only, so
none of it has been exercised against hardware. Anything ambiguous is refused
rather than guessed at.

NEVER DO THESE THREE THINGS TO A HUB
------------------------------------
* ``set_configuration()`` — resets the hub and drops every downstream device,
  potentially including the box's own network uplink.
* ``detach_kernel_driver()`` — unbinds the kernel ``hub`` driver and tears down
  the whole downstream tree.
* cache a live ``usb.core.Device`` — always ``dispose_resources()`` in a
  ``finally``. This is the pyusb analogue of ``ykush._release``.

None of the four hub-class requests used here needs an interface claim (their
recipient is the device or a port), which is what makes the above avoidable.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

from .usb_net import (
    HUB_ABSENT,
    HUB_OPEN_FAILED,
    HUB_OP_TIMEOUT_S,
    DeviceNotFoundError,
    LibraryMissingError,
    PortStateError,
    USBNet,
    hub_access,
    run_hub_op,
    validate_off_time,
)

logger = logging.getLogger(__name__)

VID = 0x2230
PID = 0x5411

# Sysfs root, module-level so unit tests can point it at a fixture tree.
_SYS_USB = Path("/sys/bus/usb/devices")

# See ykush._LOCK_TIMEOUT_S — same reasoning, same value.
_LOCK_TIMEOUT_S = 10.0

# ─────────────  USB 2.0 §11.24 hub class  ─────────────
_REQ_GET_STATUS = 0x00
_REQ_CLEAR_FEATURE = 0x01
_REQ_SET_FEATURE = 0x03
_REQ_GET_DESCRIPTOR = 0x06

_FEAT_PORT_POWER = 8
_DESC_HUB = 0x29                 # USB 2.0 hub descriptor type

_RT_DEV_IN = 0xA0                # device-to-host | class | device
_RT_PORT_OUT = 0x23              # host-to-device | class | other
_RT_PORT_IN = 0xA3               # device-to-host | class | other

_PORT_STAT_CONNECT = 0x0001
_PORT_STAT_POWER = 0x0100        # USB2 wPortStatus bit 8
_PORT_CHANGE_CONNECT = 0x0001    # wPortChange bit 0 - C_PORT_CONNECTION

# Logical Power Switching Mode = wHubCharacteristics bits 1:0
_LPSM_GANGED = 0b00
_LPSM_PER_PORT = 0b01

# The dock's four external sockets are Hub B's four ports, 1-based.
_USER_PORTS = (1, 2, 3, 4)

# How long to wait for a device to re-enumerate after power is restored, and
# how often to ask. Measured re-power to CONNECT on this dock: 33 ms for a
# CP2102N, 323 ms for a J-Link PLUS. Five seconds is >15x the slowest of those,
# so expiry means "it did not come back", not "it is slow". Polling at 10 ms is
# far finer than the hub's own 100 ms bPwrOn2PwrGood and costs one control
# transfer per tick.
_RECONNECT_WAIT_S = 5.0
_RECONNECT_POLL_S = 0.01

# Link speeds, as reported by sysfs `speed` in Mbit/s. A hub linked at
# SuperSpeed enumerates as TWO virtual hubs and VBUS only drops when PORT_POWER
# is cleared on both halves.
_SPEED_HIGH = 480.0

# Kernel drivers that mean "this is a network interface". Cutting a port whose
# subtree binds one of these can drop the box off the network mid-command, with
# no remote way to restore power.
_NET_DRIVERS = frozenset({
    "r8152", "cdc_ether", "cdc_ncm", "cdc_eem", "ax88179_178a",
    "asix", "rndis_host", "usbnet", "smsc95xx", "lan78xx",
})

# Belt-and-braces for the case where the device is unbound and so has no driver
# symlink to read. The dock's own NIC is an RTL8153.
_PROTECTED_VIDPID = frozenset({("0bda", "8153")})

# Address form: the topology path lives in the VISA serial slot behind a prefix
# that can never be mistaken for a real serial. RTS5411 hubs report iSerial 0,
# so two of them would otherwise synthesize the SAME address.
_PORT_SLOT_RE = re.compile(r"::port-([0-9][0-9.\-]*)::INSTR$", re.IGNORECASE)
_SERIAL_SLOT_RE = re.compile(r"::([^:]+)::INSTR$")


def _require_pyusb():
    """Import pyusb lazily.

    pyusb is a hard box dependency and a declared unit-test dependency, unlike
    the vendor SDKs the sibling drivers guard against. The guard exists for a
    degraded image, and so importing this module never fails at collection.
    """
    try:
        import usb.core
        import usb.util
    except ImportError as exc:  # pragma: no cover - degraded image only
        raise LibraryMissingError(
            "pyusb is required for Plugable hub control. Install with:\n"
            "    pip install pyusb"
        ) from exc
    return usb


def _read(path: Path) -> str | None:
    """Read a small sysfs attribute, non-blocking.

    O_NONBLOCK because string descriptors on a wedged device block forever;
    the same idiom the box's other four sysfs walkers use.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        return os.read(fd, 256).decode("utf-8", "replace").strip()
    except (OSError, BlockingIOError):
        return None
    finally:
        os.close(fd)


def _is_plugable_hub(dev_dir: Path) -> bool:
    return (_read(dev_dir / "idVendor") == f"{VID:04x}"
            and _read(dev_dir / "idProduct") == f"{PID:04x}")


def path_from_address(address: str | None) -> str | None:
    """Extract the hub topology path ('1-1.4') from a saved net address."""
    if not address:
        return None
    m = _PORT_SLOT_RE.search(address)
    if m:
        return m.group(1)
    return None


def serial_from_address(address: str | None) -> str | None:
    """Extract a real serial from a saved net address, if it carries one."""
    if not address:
        return None
    if _PORT_SLOT_RE.search(address):
        return None
    m = _SERIAL_SLOT_RE.search(address)
    return m.group(1) if m and m.group(1) else None


class _HubRef:
    """One hub silicon instance. Metadata only — never a live handle."""

    __slots__ = ("sysfs", "busnum", "devnum", "parent", "speed")

    def __init__(self, sysfs: str, busnum: int, devnum: int, parent: str | None,
                 speed: float | None = None):
        self.sysfs = sysfs
        self.busnum = busnum
        self.devnum = devnum
        self.parent = parent
        self.speed = speed

    @property
    def port_path(self) -> str:
        """The topology path without the bus prefix: '1-1.4' -> '1.4'.

        Two virtual halves of one SuperSpeed hub sit on different buses at the
        SAME port path, which is what makes them pairable.
        """
        _, _, rest = self.sysfs.partition("-")
        return rest

    @property
    def is_superspeed(self) -> bool:
        return self.speed is not None and self.speed > _SPEED_HIGH

    def __repr__(self):
        return f"<_HubRef {self.sysfs} bus={self.busnum} dev={self.devnum}>"


def _scan_plugable_hubs() -> list[_HubRef]:
    """Every Plugable hub currently on the bus, read from sysfs only."""
    found: list[_HubRef] = []
    try:
        entries = sorted(_SYS_USB.iterdir())
    except OSError:
        return found
    for dev_dir in entries:
        if ":" in dev_dir.name or not _is_plugable_hub(dev_dir):
            continue
        busnum, devnum = _read(dev_dir / "busnum"), _read(dev_dir / "devnum")
        if not (busnum and devnum):
            continue
        try:
            parent = os.path.basename(os.path.dirname(os.path.realpath(dev_dir)))
        except OSError:
            parent = None
        try:
            speed = float(_read(dev_dir / "speed") or "")
        except (TypeError, ValueError):
            speed = None
        try:
            found.append(_HubRef(dev_dir.name, int(busnum), int(devnum), parent,
                                 speed))
        except ValueError:
            continue
    return found


def _bus_controller(busnum: int) -> str | None:
    """Realpath of the host controller owning a bus, used to pair virtual hubs.

    The USB2 and USB3 root hubs of one xHCI controller are siblings under the
    same PCI device, so two buses whose roots share a parent directory belong
    to the same physical controller.
    """
    root = _SYS_USB / f"usb{busnum}"
    # Existence is load-bearing, not defensive: realpath() happily normalises a
    # path that is not there, so a missing root hub would resolve every bus to
    # the same parent directory and make two unrelated buses look like the two
    # halves of one controller.
    if not root.exists():
        return None
    try:
        return os.path.dirname(os.path.realpath(root))
    except OSError:
        return None


def _companion_candidates(ref: _HubRef, hubs: list[_HubRef]) -> list[_HubRef]:
    """Plugable hubs that could be *ref*'s other virtual half.

    UNTESTED PATH. The bench dock links at USB 2.0 only, so no SuperSpeed
    sibling exists to validate this against; it is written from USB 3.2 §10.
    A candidate must sit on a different bus of the SAME host controller, at the
    SAME port path, and link at a different speed. Port-count equality is
    checked later, at open time, because it comes from the hub descriptor.
    """
    ours = _bus_controller(ref.busnum)
    if ours is None:
        return []
    return [
        h for h in hubs
        if h.busnum != ref.busnum
        and h.port_path == ref.port_path
        and h.speed != ref.speed
        and _bus_controller(h.busnum) == ours
    ]


class _Hub:
    """An open hub handle plus its once-per-session descriptor facts."""

    def __init__(self, dev, nports: int, characteristics: int, sysfs: str):
        self.dev = dev
        self.nports = nports
        self.characteristics = characteristics
        self.sysfs = sysfs

    @property
    def lpsm(self) -> int:
        return self.characteristics & 0b11

    def assert_per_port(self) -> None:
        """Refuse anything but genuine per-port switching.

        Ganged is the dangerous case, not the harmless one: the hub would
        accept the request and cut EVERY port, so a driver that let it through
        would look like it worked while killing the whole tier. Fail loudly
        instead — a silent no-op is the failure mode this repo has already paid
        for once.
        """
        if self.lpsm == _LPSM_PER_PORT:
            return
        how = "ganged (all ports at once)" if self.lpsm == _LPSM_GANGED \
            else "no power switching"
        raise PortStateError(
            f"hub {self.sysfs} reports {how} "
            f"(wHubCharacteristics=0x{self.characteristics:04x}); "
            "per-port power switching is required and this hub cannot do it"
        )

    def port_status(self, port: int) -> int:
        return self.port_status_full(port)[0]

    def port_status_full(self, port: int) -> tuple[int, int]:
        """(wPortStatus, wPortChange) from one GET_STATUS.

        The change word is what makes re-enumeration observable: after power is
        restored the hub raises C_PORT_CONNECTION, and that is the ONLY signal
        available — see the note on device presence in ``_verify_off``.
        """
        data = self.dev.ctrl_transfer(_RT_PORT_IN, _REQ_GET_STATUS, 0, port, 4)
        return (data[0] | (data[1] << 8), data[2] | (data[3] << 8))

    def set_power(self, port: int, on: bool) -> None:
        req = _REQ_SET_FEATURE if on else _REQ_CLEAR_FEATURE
        self.dev.ctrl_transfer(_RT_PORT_OUT, req, _FEAT_PORT_POWER, port, None)


class PlugableUSBNet(USBNet):
    """USBNet driver for Plugable RTS5411 docks (1-based port numbers).

    Ports are 1-based because the USB hub class numbers them that way — the
    same convention as YKUSH, the opposite of Acroname's 0-based indexing.
    """

    # pyusb opens and disposes inside every public call, so no USB context
    # survives between operations and there is nothing a re-enumeration could
    # orphan. Restarting box_http_server on this driver's behalf would drop
    # every other in-flight box operation to fix nothing. (Contrast Acroname,
    # whose BrainStem connect costs seconds and so parks a session.)
    holds_usb_context_between_ops = False

    def __init__(self, net_info: dict | None = None) -> None:
        net_info = net_info or {}
        self.address = net_info.get("address")
        self.hub_path = path_from_address(self.address)
        self.serial = serial_from_address(self.address)
        params = net_info.get("params") or {}
        self.allow_network = bool(params.get("allow_network"))

    # ────────────────  identity  ────────────────
    def _lock_key(self) -> str:
        """The physical DOCK, not one hub silicon instance.

        Both cascaded hubs are driven inside one session under this one key, so
        the two halves of a dock can never be operated concurrently by two
        processes. Keyed on the ROOT tier's topology path because RTS5411 hubs
        report no serial number — see the module docstring.
        """
        if self.hub_path:
            return f"plugable::{self.hub_path}"
        if self.serial:
            return f"plugable::{self.serial}"
        return "plugable::unknown"

    def _resolve_dock(self) -> dict:
        """Locate this dock's root and downstream hub tiers, live.

        Resolved per call rather than trusted from a compile-time map: re-cabling
        the dock (notably into a USB-C port that negotiates SuperSpeed) changes
        the shape, and a stale map would drive the wrong port.
        """
        hubs = _scan_plugable_hubs()
        if not hubs:
            raise DeviceNotFoundError(
                f"no Plugable hub ({VID:04x}:{PID:04x}) found on the bus",
                classification=HUB_ABSENT, usb_context_healthy=False,
            )

        by_path = {h.sysfs: h for h in hubs}
        if self.hub_path:
            root = by_path.get(self.hub_path)
            if root is None:
                raise DeviceNotFoundError(
                    f"Plugable hub at topology path {self.hub_path} is not on the "
                    f"bus (present: {', '.join(sorted(by_path)) or 'none'}). The "
                    "address pins this net to a physical box port; re-cabling the "
                    "dock requires re-adding the net.",
                    classification=HUB_ABSENT, usb_context_healthy=True,
                )
        else:
            # Collapse the two virtual halves of a SuperSpeed-linked hub, which
            # sit on different buses at the same port path, so one dock does
            # not read as two roots.
            roots = [h for h in hubs if h.parent not in by_path]
            by_port_path: dict[str, _HubRef] = {}
            for h in sorted(roots, key=lambda r: r.busnum):
                by_port_path.setdefault(h.port_path, h)
            if len(by_port_path) != 1:
                raise DeviceNotFoundError(
                    f"cannot identify a single Plugable dock: found "
                    f"{len(by_port_path)} root-tier hubs and the net carries no "
                    "topology path",
                    classification=HUB_OPEN_FAILED, usb_context_healthy=True,
                )
            root = next(iter(by_port_path.values()))

        children = [h for h in hubs if h.parent == root.sysfs]
        if len(children) != 1:
            raise DeviceNotFoundError(
                f"dock topology does not match the validated shape: hub "
                f"{root.sysfs} has {len(children)} downstream Plugable hub(s), "
                "expected exactly 1. Was the dock re-cabled or is this a "
                "different Plugable model?",
                classification=HUB_OPEN_FAILED, usb_context_healthy=True,
            )
        downstream = children[0]
        return {
            "root": root,
            "downstream": downstream,
            "companion": self._companion_of(downstream, hubs),
        }

    @staticmethod
    def _companion_of(ref: _HubRef, hubs: list[_HubRef]) -> _HubRef | None:
        """Resolve the SuperSpeed/HighSpeed twin of the user-tier hub.

        UNTESTED PATH — see ``_companion_candidates``. Clearing PORT_POWER on
        only one half of a SuperSpeed-linked hub leaves VBUS up, which is the
        classic way a hub driver silently half-works, so anything ambiguous is
        refused rather than guessed at.
        """
        candidates = _companion_candidates(ref, hubs)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise PortStateError(
                f"hub {ref.sysfs} has {len(candidates)} possible SuperSpeed "
                f"companions ({', '.join(c.sysfs for c in candidates)}); "
                "refusing to switch power rather than guess which half to "
                "drive. Re-cable the dock so it enumerates unambiguously."
            )
        if ref.is_superspeed:
            raise PortStateError(
                f"hub {ref.sysfs} links at SuperSpeed ({ref.speed:.0f}M) but no "
                "USB 2.0 companion hub could be found. VBUS only drops when "
                "PORT_POWER is cleared on BOTH halves, so switching this half "
                "alone would leave a high-speed device powered — refusing."
            )
        return None

    # ────────────────  safety  ────────────────
    def _subtree(self, hub_sysfs: str, port: int) -> list[str]:
        """sysfs entries at or below one hub port (devices AND interfaces).

        Sorted, with interfaces first. sysfs iteration order is
        filesystem-dependent and ``_guard`` stops at its first match, so without
        an explicit order the SAME port is refused with a different message on
        different machines -- which is how a test pinning one of those messages
        passed on a developer's laptop and failed in CI. Interfaces lead because
        their refusal names the bound kernel driver, which is the more useful
        diagnosis; the vid:pid check behind them is the fallback for a device
        with no driver attached.
        """
        prefix = f"{hub_sysfs}.{port}"
        try:
            names = [d.name for d in _SYS_USB.iterdir()]
        except OSError:
            return []
        matched = [n for n in names
                   if n == prefix or n.startswith(prefix + ".") or n.startswith(prefix + ":")]
        return sorted(matched, key=lambda n: (":" not in n, n))

    def _guard(self, hub_sysfs: str, port: int) -> None:
        """Refuse a switch that would cut something the box depends on.

        The guard is expressed in USB terms, NOT by reading the routing table:
        the box container runs on its own docker network, so its default route
        is a bridge and /sys/class/net is namespace-filtered. USB devices are
        not namespaced, so the sysfs USB tree is the host's and is trustworthy.
        """
        for name in self._subtree(hub_sysfs, port):
            entry = _SYS_USB / name
            if ":" in name:                                  # an interface
                try:
                    drv = os.path.basename(os.path.realpath(entry / "driver"))
                except OSError:
                    continue
                if drv in _NET_DRIVERS:
                    raise PortStateError(
                        f"port {port} carries a network device (driver {drv}) — "
                        "cutting it could drop the box off the network. Set "
                        "params.allow_network=true on this net to override."
                    )
                continue
            vid, pid = _read(entry / "idVendor"), _read(entry / "idProduct")
            if vid and pid and (vid, pid) in _PROTECTED_VIDPID:
                raise PortStateError(
                    f"port {port} carries a known network adapter {vid}:{pid} — "
                    "refusing. Set params.allow_network=true to override."
                )
            if vid == f"{VID:04x}" and pid == f"{PID:04x}":
                raise PortStateError(
                    f"port {port} feeds the dock's second hub tier; cutting it "
                    "would take down every user port at once — refusing."
                )

    # ────────────────  session plumbing  ────────────────
    @staticmethod
    def _dispose(usb, dev, sysfs: str) -> None:
        try:
            usb.util.dispose_resources(dev)
        except Exception:  # noqa: BLE001 - disposal must not mask the result
            logger.debug("dispose_resources failed for %s", sysfs, exc_info=True)

    def _open_one(self, usb, ref: _HubRef) -> _Hub:
        """Open one hub and read its once-per-session descriptor facts.

        Disposes its own handle if anything after the open fails, so a caller
        opening several hubs never leaks the ones that succeeded.
        """
        dev = usb.core.find(bus=ref.busnum, address=ref.devnum)
        if dev is None:
            raise DeviceNotFoundError(
                f"Plugable hub {ref.sysfs} (bus {ref.busnum} dev {ref.devnum}) "
                "is in sysfs but libusb cannot open it",
                classification=HUB_OPEN_FAILED, usb_context_healthy=True,
            )
        try:
            desc = dev.ctrl_transfer(_RT_DEV_IN, _REQ_GET_DESCRIPTOR,
                                     _DESC_HUB << 8, 0, 0x40)
            # The hub descriptor is variable-length (7 bytes + DeviceRemovable
            # and PortPwrCtrlMask, both sized by port count), so ask for plenty
            # and trust bLength rather than assuming a fixed size.
            if len(desc) < 5:
                raise PortStateError(
                    f"hub {ref.sysfs} returned a {len(desc)}-byte hub descriptor"
                )
            hub = _Hub(dev, desc[2], desc[3] | (desc[4] << 8), ref.sysfs)
            hub.assert_per_port()
            return hub
        except BaseException:
            self._dispose(usb, dev, ref.sysfs)
            raise

    def _with_hubs(self, refs: list[_HubRef], fn):
        """Open every hub in *refs*, run *fn(hubs)*, always dispose them all.

        Takes a list because a SuperSpeed-linked hub is TWO virtual hubs whose
        PORT_POWER must be driven together. In the validated USB 2.0 topology
        the list has one entry.

        Never caches a handle: holding one would pin the exclusive usbfs claim
        away from the MCP server and every ``lager python`` subprocess. pyusb's
        open is cheap (no native connect to pay for), so unlike Acroname there
        is nothing to gain from parking a session.
        """
        usb = _require_pyusb()
        opened: list[_Hub] = []
        current = refs[0] if refs else None
        try:
            for ref in refs:
                current = ref
                opened.append(self._open_one(usb, ref))
            if len(opened) > 1:
                sizes = {h.nports for h in opened}
                if len(sizes) != 1:
                    raise PortStateError(
                        "the two halves of this hub report different port "
                        f"counts ({', '.join(f'{h.sysfs}={h.nports}' for h in opened)}); "
                        "refusing to switch power rather than drive mismatched "
                        "port numbering"
                    )
            return fn(opened)
        except (PortStateError, DeviceNotFoundError):
            raise
        except Exception as exc:
            # usb.core.USBError carries errno: EACCES almost always means the
            # udev rule granting group `lager` write access to the usbfs node
            # is missing, which is the single most common first-run failure.
            name = current.sysfs if current is not None else "unknown"
            errno = getattr(exc, "errno", None)
            if errno == 13:
                raise PortStateError(
                    f"permission denied opening hub {name}: libusb needs "
                    "WRITE access to /dev/bus/usb. Install the udev rule for "
                    f"vendor {VID:04x} (or run `lager box-config udev add "
                    f"{VID:04x}:{PID:04x}`)."
                ) from exc
            raise PortStateError(
                f"hub {name}: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            for hub in reversed(opened):
                self._dispose(usb, hub.dev, hub.sysfs)

    def _with_hub(self, ref: _HubRef, fn):
        """Single-hub convenience wrapper over :meth:`_with_hubs`."""
        return self._with_hubs([ref], lambda hubs: fn(hubs[0]))

    def _run(self, what: str, fn, timeout=None):
        """One bounded operation under this dock's lock.

        Mirrors the YKUSH cycle: the caller's budget bounds the WHOLE thing —
        lock wait included — so a contended lock cannot silently double it.
        """
        key = self._lock_key()
        start = time.monotonic()
        if timeout is None:
            with hub_access(key, timeout=_LOCK_TIMEOUT_S):
                return run_hub_op(key, fn, timeout=HUB_OP_TIMEOUT_S)
        budget = min(timeout, HUB_OP_TIMEOUT_S)
        with hub_access(key, timeout=min(_LOCK_TIMEOUT_S, budget)):
            remaining = max(0.5, budget - (time.monotonic() - start))
            return run_hub_op(key, fn, timeout=remaining)

    @staticmethod
    def _validate_port(port: int) -> None:
        if port not in _USER_PORTS:
            raise PortStateError(
                f"port {port} is not a user-facing dock port; valid ports are "
                f"{', '.join(str(p) for p in _USER_PORTS)}"
            )

    @staticmethod
    def _targets(dock: dict) -> list[_HubRef]:
        """The hub halves that must be driven together for one dock.

        One entry in the validated USB 2.0 topology; two when the dock links at
        SuperSpeed, because VBUS only drops when PORT_POWER is cleared on both.
        """
        refs = [dock["downstream"]]
        companion = dock.get("companion")
        if companion is not None:
            refs.append(companion)
        return refs

    @staticmethod
    def _check_port_fits(hubs: list[_Hub], port: int) -> None:
        for hub in hubs:
            if port > hub.nports:
                raise PortStateError(
                    f"port {port} exceeds hub {hub.sysfs} bNbrPorts={hub.nports}"
                )

    @staticmethod
    def _set_power_all(hubs: list[_Hub], port: int, on: bool) -> None:
        for hub in hubs:
            hub.set_power(port, on)

    @staticmethod
    def _powered(hubs: list[_Hub], port: int) -> bool:
        """A port counts as powered only if EVERY half of the hub says so."""
        return all(hub.port_status(port) & _PORT_STAT_POWER for hub in hubs)

    def _verify_off(self, hubs: list[_Hub], port: int) -> None:
        """Confirm the hub accepted the power-down.

        DEVICE PRESENCE IS NOT A VALID SIGNAL HERE, and must never be added
        back. While a port is unpowered the hub raises no change bit, so the
        kernel never polls the port and never processes the disconnect: the
        sysfs device node, its `lsusb` entry, and any `/dev/ttyUSB*` it owns all
        persist for the ENTIRE off window. The kernel only logs "USB disconnect"
        when power comes back. An earlier version of this method polled for the
        sysfs child to disappear, which cannot happen, so it reported every
        successful power-down as a hub-without-a-VBUS-FET failure and then
        undid it — that false negative is what made this dock look unsupportable.

        The hub's own PORT_POWER bit is the only thing observable while the port
        is off, so it is the only thing checked. Proof that VBUS physically
        drops belongs to :meth:`cycle`, the one operation that gets to watch the
        re-power edge.
        """
        for hub in hubs:
            if hub.port_status(port) & _PORT_STAT_POWER:
                raise PortStateError(
                    f"port {port}: PORT_POWER was cleared on hub {hub.sysfs} but "
                    "still reads as on — this hub does not honour per-port "
                    "power switching"
                )

    def _wait_for_reconnect(self, hubs: list[_Hub], port: int) -> bool:
        """Wait for the device to come back after power is restored.

        Watches the hub's port status, NOT the sysfs tree: C_PORT_CONNECTION is
        raised the moment the hub debounces a connect, whereas the kernel's view
        lags by a full enumeration. Measured re-power to CONNECT is 33 ms for a
        CP2102N and 323 ms for a J-Link PLUS, both far inside the budget.

        Returns False on expiry rather than raising — the port is powered either
        way, which is the safe state, and an empty port is a normal outcome.
        """
        deadline = time.monotonic() + _RECONNECT_WAIT_S
        while True:
            for hub in hubs:
                status, change = hub.port_status_full(port)
                if change & _PORT_CHANGE_CONNECT or status & _PORT_STAT_CONNECT:
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_RECONNECT_POLL_S)

    # ────────────────  USBNet interface  ────────────────
    def _set(self, port: int, on: bool):
        self._validate_port(port)

        def _work():
            targets = self._targets(self._resolve_dock())
            if not on and not self.allow_network:
                for ref in targets:
                    self._guard(ref.sysfs, port)

            def _op(hubs: list[_Hub]):
                self._check_port_fits(hubs, port)
                # Captured before the switch so `disable` can tell the caller
                # the device will STAY listed until power returns. Every reader
                # of this tool -- including the author of its first version --
                # has assumed a powered-off device vanishes from lsusb, and
                # acted on that. Saying so at the moment of the switch is worth
                # one extra control transfer.
                was_connected = any(
                    hub.port_status(port) & _PORT_STAT_CONNECT for hub in hubs)
                self._set_power_all(hubs, port, on)
                if on:
                    # Do not wait for the device to reappear: callers of enable
                    # must not block on a re-enumeration. `cycle` is the
                    # operation that waits, because it is the one that knows a
                    # device was there to begin with.
                    time.sleep(0.1)
                else:
                    self._verify_off(hubs, port)
                return was_connected

            return self._with_hubs(targets, _op)

        return self._run("enable" if on else "disable", _work)

    def enable(self, net_name, port):
        self._set(int(port), True)

    def disable(self, net_name, port):
        """Power the port down. Returns True if a device was attached.

        The return value exists so callers can warn that the device stays
        enumerated -- see the note on observability in ``_verify_off``.
        """
        return bool(self._set(int(port), False))

    def state(self, net_name, port):
        port = int(port)
        self._validate_port(port)

        def _work():
            targets = self._targets(self._resolve_dock())
            return self._with_hubs(targets, lambda hubs: self._powered(hubs, port))

        return bool(self._run("state", _work))

    def toggle(self, net_name, port):
        port = int(port)
        new_state = not self.state(net_name, port)
        self._set(port, new_state)
        return new_state

    def cycle(self, net_name, port, off_time=None):
        """Power-cycle one port and wait for the device to come back.

        Overrides the base disable/sleep/enable so the whole sequence runs in
        ONE hub session under ONE lock: nothing else may touch this dock while
        a port is dark, and the port cannot be left dark by an interleaved
        caller. Power is restored on every exit path, including a failure
        partway through — a tool that strands a port is how a remote bench is
        lost.

        Returns True if the device re-enumerated, False if the wait expired,
        and None if the hub reported no device attached before the cycle.

        None does NOT mean the port is unused. A hub only sees a device that
        pulls up D+/D-, so a charge-only cable -- no data lines, a real load on
        the other end -- is indistinguishable from an empty socket here. Power
        was still cut and restored on such a port; there is simply nothing on
        the bus to watch come back, and the DUT has to be confirmed by its own
        behaviour (its UART, or a measurement) instead.
        """
        port = int(port)
        self._validate_port(port)
        off_time = validate_off_time(off_time)

        def _work():
            targets = self._targets(self._resolve_dock())
            if not self.allow_network:
                for ref in targets:
                    self._guard(ref.sysfs, port)

            def _op(hubs: list[_Hub]):
                self._check_port_fits(hubs, port)
                was_connected = any(
                    hub.port_status(port) & _PORT_STAT_CONNECT for hub in hubs)
                restored = False
                try:
                    self._set_power_all(hubs, port, False)
                    self._verify_off(hubs, port)
                    time.sleep(off_time)
                finally:
                    try:
                        self._set_power_all(hubs, port, True)
                        restored = True
                    except Exception:  # noqa: BLE001 - must not mask the original
                        logger.error(
                            "could not restore PORT_POWER on port %s of %s; the "
                            "port may be left unpowered", port,
                            ", ".join(h.sysfs for h in hubs), exc_info=True)
                if not restored:
                    raise PortStateError(
                        f"port {port} was powered down but could not be powered "
                        "back up. Run `lager usb <net> recover` to re-power "
                        "every port on this dock."
                    )
                if not was_connected:
                    return None
                return self._wait_for_reconnect(hubs, port)

            return self._with_hubs(targets, _op)

        return self._run("cycle", _work)

    def recover(self, net_name=None, port=None):
        """Re-assert PORT_POWER on every user port of this dock.

        Dock-wide, not port-scoped, because the situation it exists for is "a
        command died partway through and something is dark". Issued over usbfs
        like every other operation: the box container mounts /sys read-only, so
        the kernel's own `hub` driver unbind/rebind (which re-runs hub_activate
        and re-powers everything) can only be run from the host — see the
        supported-instruments docs.

        Returns the list of ports it re-powered.
        """
        def _work():
            targets = self._targets(self._resolve_dock())

            def _op(hubs: list[_Hub]):
                done = []
                for p in _USER_PORTS:
                    if any(p > hub.nports for hub in hubs):
                        continue
                    self._set_power_all(hubs, p, True)
                    done.append(p)
                return done

            return self._with_hubs(targets, _op)

        return self._run("recover", _work)

    def states(self, ports, *, timeout=None):
        """Read several ports in ONE session.

        The base fallback costs a full open/close per port, serialised behind
        this dock's lock; one session turns a bench sweep from seconds per hub
        into milliseconds. A single unreadable port maps to None rather than
        failing the sweep, matching the Acroname contract.
        """
        wanted = [int(p) for p in ports]

        def _work():
            targets = self._targets(self._resolve_dock())

            def _op(hubs: list[_Hub]):
                out = {}
                for p in wanted:
                    try:
                        self._validate_port(p)
                        out[p] = self._powered(hubs, p)
                    except Exception:
                        logger.warning("plugable hub %s: port %s unreadable",
                                       hubs[0].sysfs, p, exc_info=True)
                        out[p] = None
                return out

            return self._with_hubs(targets, _op)

        return self._run("states", _work, timeout=timeout)
