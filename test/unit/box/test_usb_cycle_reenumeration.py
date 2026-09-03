# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`cycle` must not say "no device on this port" about a port that has one.

`USBNet.cycle` ended `return None` unconditionally, and only the Plugable
driver overrode it. Acroname and YKUSH define no `cycle` at all, so on those
hubs the box answered `None` for every cycle whatever was plugged in, and the
handler rendered `None` as:

    USB port 'usbN' power-cycled; no device on this port to watch for, so
    re-enumeration was not confirmed

That is not an edge case on those drivers -- it is the only behaviour they had.
Cycling usb1..usb4 on the bench printed it every time while the devices behind
those ports demonstrably re-enumerated, taking new USB device numbers across
the window (MCC USB-202 043 -> 054, J-Link PLUS 050 -> 055, J-Link 062 -> 056).

It was read as an authoritative statement that the hub saw nothing attached,
during a hardware fault (#417), and produced a written conclusion that an
instrument "is not even asserting its USB data-line pullup" -- which nothing
supported. During a fault a false "no device here" is close to the most
expensive thing a tool can say, because it points the investigation at the
device rather than at the tool.

The base class now answers from the kernel's own USB topology, so all three
drivers are fixed at once. The bus is sampled before the port is cut and again
while it is dark: whatever left the bus in between is what this port carries.
Sampling either side of the call cannot establish that, which is why it is done
here rather than in the handler.

These tests pin all four outcomes, and that power is restored on every path.
"""

import pytest

from lager.automation.usb_hub import usb_net


class FakeClock:
    """Deterministic monotonic clock; sleeping advances it."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeHub(usb_net.USBNet):
    """A driver that does NOT override `cycle` -- Acroname and YKUSH's shape."""

    def __init__(self):
        self.calls = []
        self.powered = True

    def enable(self, net_name, port):
        self.calls.append("enable")
        self.powered = True

    def disable(self, net_name, port):
        self.calls.append("disable")
        self.powered = False

    def toggle(self, net_name, port):
        raise NotImplementedError

    def state(self, net_name, port):
        return self.powered


#: A root hub, and a device on the port under test.
POWERED = [{"sysfs_name": "1-0:1.0-root", "devnum": "1"},
           {"sysfs_name": "1-1.2", "devnum": "43"}]
#: The same bus with that port dark.
DARK = [{"sysfs_name": "1-0:1.0-root", "devnum": "1"}]
#: The device back, with the new device number a re-enumeration always takes.
RETURNED = [{"sysfs_name": "1-0:1.0-root", "devnum": "1"},
            {"sysfs_name": "1-1.2", "devnum": "54"}]


@pytest.fixture()
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(usb_net.time, "monotonic", c.monotonic)
    monkeypatch.setattr(usb_net.time, "sleep", c.sleep)
    return c


def _scripted(monkeypatch, snapshots):
    """Answer each `enumerate_usb_devices` call from `snapshots`, then repeat
    the last one for every further poll."""
    seen = []

    def fake(*_a, **_kw):
        seen.append(None)
        index = min(len(seen) - 1, len(snapshots) - 1)
        return snapshots[index]

    monkeypatch.setattr(usb_net, "enumerate_usb_devices", fake)
    return seen


def test_a_device_that_comes_back_is_reported_as_returned(clock, monkeypatch):
    """The regression: this is the case that used to answer None."""
    _scripted(monkeypatch, [POWERED, DARK, RETURNED])
    hub = FakeHub()

    assert hub.cycle("usb1", 2) is True
    assert hub.calls == ["disable", "enable"]


def test_a_new_device_number_alone_is_enough(clock, monkeypatch):
    """Re-enumeration is what the bench evidence showed: same path, new devnum.

    The device is matched by its topology path, which is stable across a
    re-enumeration on the same physical port, so the new `devnum` does not have
    to be predicted -- only its return has to be seen.
    """
    _scripted(monkeypatch, [POWERED, DARK, RETURNED])

    assert FakeHub().cycle("usb1", 2) is True


def test_a_device_that_never_returns_is_reported_as_missing(clock, monkeypatch):
    """False, not None: something WAS there, and it did not come back."""
    _scripted(monkeypatch, [POWERED, DARK])  # stays dark for every poll
    hub = FakeHub()

    assert hub.cycle("usb1", 2) is False
    # The port is powered regardless -- a failed confirmation must never be
    # the reason a bench nobody can reach physically is left dark.
    assert hub.calls == ["disable", "enable"]
    assert hub.powered is True


def test_an_empty_port_still_reports_nothing_to_watch_for(clock, monkeypatch):
    """None keeps its meaning where it was always true.

    Nothing left the bus while the port was dark, so there was nothing on it --
    which includes a charge-only cable, drawing power but presenting no device.
    """
    _scripted(monkeypatch, [DARK, DARK, DARK])

    assert FakeHub().cycle("usb1", 2) is None


def test_an_unreadable_bus_does_not_claim_the_port_is_empty(clock, monkeypatch):
    """Off a box there is no sysfs, and `enumerate_usb_devices` returns [].

    That must not be read as "the bus is empty" -- every real device including
    root hubs is returned, so an empty answer means the question could not be
    asked. `cycle` returns None here too; the caller distinguishes the two by
    asking the bus itself, which is what the handler and the MCP tool do.
    """
    _scripted(monkeypatch, [[], [], []])

    assert FakeHub().cycle("usb1", 2) is None


def test_power_is_restored_when_the_dark_sample_raises(clock, monkeypatch):
    """The re-power is in a `finally` and must stay there."""
    calls = []

    def exploding(*_a, **_kw):
        calls.append(None)
        if len(calls) == 2:      # the sample taken while the port is dark
            raise OSError("sysfs went away")
        return POWERED

    monkeypatch.setattr(usb_net, "enumerate_usb_devices", exploding)
    hub = FakeHub()

    with pytest.raises(OSError):
        hub.cycle("usb1", 2)

    assert hub.calls == ["disable", "enable"]
    assert hub.powered is True


def test_the_wait_is_bounded(clock, monkeypatch):
    """A device that never returns must not hold the hub lock indefinitely."""
    _scripted(monkeypatch, [POWERED, DARK])

    FakeHub().cycle("usb1", 2, off_time=0.5)

    assert clock.now <= 0.5 + usb_net.USB_REENUM_WAIT_S + 1.0


@pytest.mark.parametrize("module_name, class_name", [
    ("lager.automation.usb_hub.acroname", "AcronameUSBNet"),
    ("lager.automation.usb_hub.ykush", "YKUSHUSBNet"),
])
def test_the_drivers_that_had_no_answer_now_inherit_one(module_name, class_name):
    """Guard the fix's reach.

    These two never overrode `cycle`, which is the whole reason the false
    message was 100% of their behaviour. If either grows its own `cycle` it
    stops getting this and needs its own evidence -- so that is a decision to
    make deliberately, not to discover on a bench.
    """
    import importlib

    driver = getattr(importlib.import_module(module_name), class_name)

    assert driver.cycle is usb_net.USBNet.cycle, (
        f"{class_name} now overrides cycle() -- it no longer inherits the "
        "topology-based confirmation, and needs its own"
    )
