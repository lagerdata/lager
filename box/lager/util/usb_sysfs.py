"""Read the USB bus from sysfs, without touching a device.

This lives in ``util`` rather than next to its HTTP route because both the
handler layer and the drivers need it, and ``util`` is the only layer both may
import: ``automation.usb_hub`` importing ``http_handlers.usb`` would be a cycle
(``usb_hub/__init__`` -> ``acroname`` -> ``http_handlers.usb`` -> ``lager``
-> ``automation.usb_hub``).

Everything here is a pure kernel-topology read: no exclusive claim, no libusb
context, no vendor SDK. That is exactly why a driver may call it from a failure
path — when a vendor library says a device is absent, sysfs is the independent
second opinion, and it stays truthful even when the calling process's own USB
context is stale.
"""
import os

_SYSFS_USB_ROOT = "/sys/bus/usb/devices"

# sysfs attributes copied verbatim onto each device entry (missing files
# simply yield None, e.g. `serial` on devices with no iSerial descriptor).
_SYSFS_DEVICE_ATTRS = (
    ("idVendor", "vid"),
    ("idProduct", "pid"),
    ("serial", "serial"),
    ("product", "product"),
    ("manufacturer", "manufacturer"),
    ("busnum", "busnum"),
    ("devnum", "devnum"),
    ("devpath", "devpath"),
    ("bDeviceClass", "device_class"),
    ("speed", "speed"),
)


def _read_sysfs_attr(dev_dir, name):
    """Best-effort non-blocking read of a sysfs string attribute.

    Mirrors ``usb_scanner._read_sysfs_text``: USB string descriptors
    (product/manufacturer/serial) can block on a wedged device when read
    through ordinary buffered I/O, which would make ``GET /usb/devices``
    hang instead of returning in a few milliseconds.
    """
    path = os.path.join(dev_dir, name)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        data = os.read(fd, 256)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        text = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return text or None


def _norm_hex_id(value):
    """Normalize a vid/pid for comparison: lowercase, no 0x, zero-padded."""
    if value is None:
        return None
    raw = str(value).strip().lower().removeprefix("0x")
    if not raw:
        return None
    # Sysfs writes 4-digit hex (``0483``); accept short query forms (``483``).
    try:
        return f"{int(raw, 16):04x}"
    except ValueError:
        return raw


def enumerate_usb_devices(sysfs_root=None, vid=None, pid=None, serial=None):
    """Enumerate USB devices on the bus from sysfs (lsusb-like).

    Pure sysfs reads — a few milliseconds, no exclusive device access —
    so it is safe to poll frequently (e.g. watching for a DUT to
    re-enumerate after a hub power-cycle or a DFU detach).

    Interface nodes (``1-1:1.0``) are skipped; every real device including
    root hubs is returned. Optional ``vid`` / ``pid`` (hex, with or without
    ``0x``) and ``serial`` (exact iSerial) filters narrow the result.
    """
    if sysfs_root is None:
        sysfs_root = _SYSFS_USB_ROOT
    want_vid = _norm_hex_id(vid)
    want_pid = _norm_hex_id(pid)
    devices = []
    try:
        entries = sorted(os.listdir(sysfs_root))
    except OSError:
        return devices
    for name in entries:
        if ":" in name:  # interface node, not a device
            continue
        dev_dir = os.path.join(sysfs_root, name)
        dev_vid = _norm_hex_id(_read_sysfs_attr(dev_dir, "idVendor"))
        if dev_vid is None:  # not a USB device node
            continue
        # Values are str|None: an attribute file that is absent (no iSerial
        # descriptor) or unreadable yields None rather than being omitted.
        entry: dict[str, str | None] = {"sysfs_name": name}
        for attr, key in _SYSFS_DEVICE_ATTRS:
            entry[key] = _read_sysfs_attr(dev_dir, attr)
        if want_vid and _norm_hex_id(entry["vid"]) != want_vid:
            continue
        if want_pid and _norm_hex_id(entry["pid"]) != want_pid:
            continue
        if serial and entry["serial"] != serial:
            continue
        devices.append(entry)
    return devices
