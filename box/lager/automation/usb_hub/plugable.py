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

# Logical Power Switching Mode = wHubCharacteristics bits 1:0
_LPSM_GANGED = 0b00
_LPSM_PER_PORT = 0b01

# The dock's four external sockets are Hub B's four ports, 1-based.
_USER_PORTS = (1, 2, 3, 4)

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

    __slots__ = ("sysfs", "busnum", "devnum", "parent")

    def __init__(self, sysfs: str, busnum: int, devnum: int, parent: str | None):
        self.sysfs = sysfs
        self.busnum = busnum
        self.devnum = devnum
        self.parent = parent

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
            found.append(_HubRef(dev_dir.name, int(busnum), int(devnum), parent))
        except ValueError:
            continue
    return found


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
        data = self.dev.ctrl_transfer(_RT_PORT_IN, _REQ_GET_STATUS, 0, port, 4)
        return data[0] | (data[1] << 8)

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
            roots = [h for h in hubs if h.parent not in by_path]
            if len(roots) != 1:
                raise DeviceNotFoundError(
                    f"cannot identify a single Plugable dock: found {len(roots)} "
                    "root-tier hubs and the net carries no topology path",
                    classification=HUB_OPEN_FAILED, usb_context_healthy=True,
                )
            root = roots[0]

        children = [h for h in hubs if h.parent == root.sysfs]
        if len(children) != 1:
            raise DeviceNotFoundError(
                f"dock topology does not match the validated shape: hub "
                f"{root.sysfs} has {len(children)} downstream Plugable hub(s), "
                "expected exactly 1. Was the dock re-cabled or is this a "
                "different Plugable model?",
                classification=HUB_OPEN_FAILED, usb_context_healthy=True,
            )
        return {"root": root, "downstream": children[0]}

    # ────────────────  safety  ────────────────
    def _subtree(self, hub_sysfs: str, port: int) -> list[str]:
        """sysfs entries at or below one hub port (devices AND interfaces)."""
        prefix = f"{hub_sysfs}.{port}"
        try:
            names = [d.name for d in _SYS_USB.iterdir()]
        except OSError:
            return []
        return [n for n in names
                if n == prefix or n.startswith(prefix + ".") or n.startswith(prefix + ":")]

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
    def _with_hub(self, ref: _HubRef, fn):
        """Open one hub, run *fn(hub)*, and always dispose the handle.

        Never caches the handle: holding it would pin the exclusive usbfs claim
        away from the MCP server and every ``lager python`` subprocess. pyusb's
        open is cheap (no native connect to pay for), so unlike Acroname there
        is nothing to gain from parking a session.
        """
        usb = _require_pyusb()
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
            return fn(hub)
        except PortStateError:
            raise
        except DeviceNotFoundError:
            raise
        except Exception as exc:
            # usb.core.USBError carries errno: EACCES almost always means the
            # udev rule granting group `lager` write access to the usbfs node
            # is missing, which is the single most common first-run failure.
            errno = getattr(exc, "errno", None)
            if errno == 13:
                raise PortStateError(
                    f"permission denied opening hub {ref.sysfs}: libusb needs "
                    "WRITE access to /dev/bus/usb. Install the udev rule for "
                    f"vendor {VID:04x} (or run `lager box-config udev add "
                    f"{VID:04x}:{PID:04x}`)."
                ) from exc
            raise PortStateError(
                f"hub {ref.sysfs}: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            try:
                usb.util.dispose_resources(dev)
            except Exception:  # noqa: BLE001 - disposal must not mask the result
                logger.debug("dispose_resources failed for %s", ref.sysfs,
                             exc_info=True)

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

    def _verify_off(self, hub: _Hub, port: int, was_connected: bool) -> None:
        """Prove VBUS actually dropped, not just that the bit reads back clear.

        A hub can advertise per-port switching in its descriptor and accept
        CLEAR_FEATURE while the VBUS FET is absent, leaving the device powered
        and enumerated. The status bit alone would report success. Only the
        device leaving the bus proves the switch is real.
        """
        if hub.port_status(port) & _PORT_STAT_POWER:
            raise PortStateError(
                f"port {port}: PORT_POWER was cleared but still reads as on — "
                "this hub does not honour per-port power switching"
            )
        if not was_connected:
            return
        child = _SYS_USB / f"{hub.sysfs}.{port}"
        for delay in (0.1, 0.2, 0.4, 0.8, 1.0):
            if not child.exists():
                return
            time.sleep(delay)
        if child.exists():
            raise PortStateError(
                f"port {port}: PORT_POWER cleared and read back as off, but the "
                f"device at {child.name} is still enumerated — this hub "
                "advertises per-port power switching but does not actually "
                "switch VBUS on this port"
            )

    # ────────────────  USBNet interface  ────────────────
    def _set(self, port: int, on: bool) -> None:
        self._validate_port(port)

        def _cycle():
            dock = self._resolve_dock()
            target = dock["downstream"]
            if not on and not self.allow_network:
                self._guard(target.sysfs, port)

            def _op(hub: _Hub):
                if port > hub.nports:
                    raise PortStateError(
                        f"port {port} exceeds hub {hub.sysfs} bNbrPorts={hub.nports}"
                    )
                was_connected = bool(hub.port_status(port) & _PORT_STAT_CONNECT)
                hub.set_power(port, on)
                if on:
                    # Do not wait for the device to reappear: re-enumeration
                    # takes seconds (a J-Link needs ~2-3s) and callers must not
                    # block on it. Just let the port settle.
                    time.sleep(0.1)
                else:
                    self._verify_off(hub, port, was_connected)

            return self._with_hub(target, _op)

        self._run("enable" if on else "disable", _cycle)

    def enable(self, net_name, port):
        self._set(int(port), True)

    def disable(self, net_name, port):
        self._set(int(port), False)

    def state(self, net_name, port):
        port = int(port)
        self._validate_port(port)

        def _cycle():
            target = self._resolve_dock()["downstream"]
            return self._with_hub(
                target, lambda hub: bool(hub.port_status(port) & _PORT_STAT_POWER)
            )

        return bool(self._run("state", _cycle))

    def toggle(self, net_name, port):
        port = int(port)
        new_state = not self.state(net_name, port)
        self._set(port, new_state)
        return new_state

    def states(self, ports, *, timeout=None):
        """Read several ports in ONE session.

        The base fallback costs a full open/close per port, serialised behind
        this dock's lock; one session turns a bench sweep from seconds per hub
        into milliseconds. A single unreadable port maps to None rather than
        failing the sweep, matching the Acroname contract.
        """
        wanted = [int(p) for p in ports]

        def _cycle():
            target = self._resolve_dock()["downstream"]

            def _op(hub: _Hub):
                out = {}
                for p in wanted:
                    try:
                        self._validate_port(p)
                        out[p] = bool(hub.port_status(p) & _PORT_STAT_POWER)
                    except Exception:
                        logger.warning("plugable hub %s: port %s unreadable",
                                       hub.sysfs, p, exc_info=True)
                        out[p] = None
                return out

            return self._with_hub(target, _op)

        return self._run("states", _cycle, timeout=timeout)
