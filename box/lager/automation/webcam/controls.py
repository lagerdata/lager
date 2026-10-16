# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
lager.automation.webcam.controls

Camera modes, camera controls and the picture transforms the webcam
streamer applies, as plain functions.

The streamer (the script ``service.py`` generates for each stream) loads this
file by path rather than importing the ``lager`` package, so it must stay
standalone: the standard library and numpy only, never ``lager`` or cv2.
That also keeps it testable without a camera.

Cameras differ a great deal -- some have no exposure control, some call it
by another name -- so everything here reports what the camera has, and the
viewer page shows only that.
"""

import re
import subprocess

V4L2_TIMEOUT = 3

# Logical control -> the V4L2 names cameras use for it, newest first. Only
# these can be set through the streamer; anything else a camera offers is
# left alone.
CONTROL_NAMES = {
    "exposure_auto": ("auto_exposure", "exposure_auto"),
    "exposure": ("exposure_time_absolute", "exposure_absolute"),
    "gain": ("gain",),
}
# UVC's values for auto_exposure / exposure_auto.
EXPOSURE_MANUAL = 1
EXPOSURE_AUTO = 3

ROTATIONS = (0, 90, 180, 270)
VIEW_DEFAULTS = {"pan_x": 0.5, "pan_y": 0.5, "rotate": 0, "flip_h": False, "flip_v": False}
DEFAULT_MODE = {"width": 640, "height": 480, "fps": 30}


def _v4l2(device, *args, run=subprocess.run):
    """stdout of one v4l2-ctl call, or '' if it failed or is not installed."""
    try:
        result = run(["v4l2-ctl", "-d", device, *args],
                     capture_output=True, text=True, timeout=V4L2_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


# ---------- modes ----------

_FORMAT = re.compile(r"^\s*\[\d+\]:\s*'(\w+)'")
_SIZE = re.compile(r"^\s*Size:\s*Discrete\s+(\d+)x(\d+)")
_INTERVAL = re.compile(r"^\s*Interval:\s*Discrete\s+[\d.]+s\s+\(([\d.]+)\s*fps\)")


def parse_modes(text):
    """The MJPEG sizes and frame rates in ``v4l2-ctl --list-formats-ext`` output.

    Only MJPEG: the streamer always asks for it, because an uncompressed
    format leaves no USB 2.0 bandwidth for a second camera on the bus.
    Returns ``[{"width", "height", "fps": [..]}]``, largest first, frame
    rates fastest first.
    """
    sizes = {}
    fmt = None
    size = None
    for line in text.splitlines():
        match = _FORMAT.match(line)
        if match:
            fmt = match.group(1)
            size = None
            continue
        if fmt != "MJPG":
            continue
        match = _SIZE.match(line)
        if match:
            size = (int(match.group(1)), int(match.group(2)))
            sizes.setdefault(size, set())
            continue
        match = _INTERVAL.match(line)
        if match and size:
            fps = float(match.group(1))
            sizes[size].add(int(fps) if fps.is_integer() else round(fps, 2))
    modes = [{"width": w, "height": h, "fps": sorted(rates, reverse=True)}
             for (w, h), rates in sizes.items() if rates]
    return sorted(modes, key=lambda m: (m["width"] * m["height"], m["width"]), reverse=True)


def list_modes(device, run=subprocess.run):
    return parse_modes(_v4l2(device, "--list-formats-ext", run=run))


def valid_mode(modes, width, height, fps):
    """True when the camera lists this size at this frame rate.

    An empty list (no v4l2-ctl, or a camera that reports nothing) accepts any
    mode: the capture loop falls back if the camera refuses it.
    """
    if not modes:
        return True
    return any(m["width"] == width and m["height"] == height and fps in m["fps"]
               for m in modes)


# ---------- controls ----------

_CONTROL = re.compile(r"^\s*(\w+)\s+0x[0-9a-fA-F]+\s+\((\w+)\)\s*:\s*(.*)$")


def parse_controls(text):
    """``v4l2-ctl --list-ctrls`` output as {name: {"type", "min", ...}}."""
    controls = {}
    for line in text.splitlines():
        match = _CONTROL.match(line)
        if not match:
            continue
        name, kind, rest = match.groups()
        entry = {"type": kind, "inactive": False}
        for key, value in re.findall(r"(\w+)=(\S+)", rest):
            if key == "flags":
                entry["inactive"] = "inactive" in value.split(",")
                continue
            try:
                entry[key] = int(value)
            except ValueError:
                entry[key] = value
        controls[name] = entry
    return controls


def _find(controls, logical):
    for name in CONTROL_NAMES[logical]:
        if name in controls:
            return name, controls[name]
    return None, None


def describe_controls(device, run=subprocess.run):
    """The exposure and gain this camera has, as the viewer page shows them.

    Returns {"exposure": {"auto", "value", "min", "max", "step"}, "gain": {...}}
    with a key only for what the camera has. ``auto`` is None when the camera
    has an exposure time but no auto mode to switch.
    """
    controls = parse_controls(_v4l2(device, "--list-ctrls", run=run))
    out = {}
    _auto_name, auto = _find(controls, "exposure_auto")
    _exp_name, exposure = _find(controls, "exposure")
    if exposure:
        out["exposure"] = {
            "auto": None if auto is None else auto.get("value") != EXPOSURE_MANUAL,
            "value": exposure.get("value"),
            "min": exposure.get("min"),
            "max": exposure.get("max"),
            "step": exposure.get("step", 1),
        }
    _gain_name, gain = _find(controls, "gain")
    if gain:
        out["gain"] = {key: gain.get(key) for key in ("value", "min", "max")}
        out["gain"]["step"] = gain.get("step", 1)
    return out


def set_control(device, logical, value, run=subprocess.run):
    """Set one of CONTROL_NAMES on the camera. True if the camera took it.

    ``exposure_auto`` takes a bool. Turning it off happens before a manual
    exposure is written, since the camera ignores the time while auto is on.
    """
    if logical not in CONTROL_NAMES:
        return False
    controls = parse_controls(_v4l2(device, "--list-ctrls", run=run))
    name, entry = _find(controls, logical)
    if not name:
        return False
    if logical == "exposure_auto":
        value = EXPOSURE_AUTO if value else EXPOSURE_MANUAL
    else:
        try:
            value = int(value)
        except (TypeError, ValueError):
            return False
        value = max(entry.get("min", value), min(entry.get("max", value), value))
    try:
        result = run(["v4l2-ctl", "-d", device, "-c", f"{name}={value}"],
                     capture_output=True, text=True, timeout=V4L2_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# ---------- the picture ----------

def merge_view(current, update):
    """``current`` with the valid fields of ``update`` applied.

    Pan is the centre of the zoomed region as a fraction of the picture
    (0.5, 0.5 is the middle). Rotation is clockwise, in quarter turns.
    """
    view = dict(VIEW_DEFAULTS)
    view.update(current or {})
    for key in ("pan_x", "pan_y"):
        if key in update:
            try:
                view[key] = max(0.0, min(1.0, float(update[key])))
            except (TypeError, ValueError):
                pass
    if update.get("rotate") in ROTATIONS:
        view["rotate"] = update["rotate"]
    for key in ("flip_h", "flip_v"):
        if isinstance(update.get(key), bool):
            view[key] = update[key]
    return view


def clamp_pan(zoom, pan_x, pan_y):
    """The pan nearest (pan_x, pan_y) that keeps the zoomed region in the picture."""
    if zoom <= 1.0:
        return 0.5, 0.5
    half = 0.5 / zoom
    return (max(half, min(1.0 - half, pan_x)), max(half, min(1.0 - half, pan_y)))


def crop_box(width, height, zoom, pan_x, pan_y):
    """(x1, y1, x2, y2) of the region a zoom and pan show, in pixels."""
    if zoom <= 1.0:
        return 0, 0, width, height
    pan_x, pan_y = clamp_pan(zoom, pan_x, pan_y)
    crop_w = max(1, int(width / zoom))
    crop_h = max(1, int(height / zoom))
    x1 = min(width - crop_w, max(0, int(round(pan_x * width - crop_w / 2))))
    y1 = min(height - crop_h, max(0, int(round(pan_y * height - crop_h / 2))))
    return x1, y1, x1 + crop_w, y1 + crop_h


def orient(frame, rotate, flip_h, flip_v, cv2=None):
    """The frame turned ``rotate`` degrees clockwise, then mirrored.

    Mirrored after the turn, so "horizontal" is left-right as the viewer sees
    it. A quarter turn swaps width and height. Pass ``cv2`` where it is
    loaded: on a 1080p frame its rotate and flip are 7 and 40 times faster
    than numpy's copies, which would otherwise cost more than the JPEG
    encode. Without it (the tests), numpy gives the same pixels.
    """
    turns = (rotate // 90) % 4
    if cv2 is not None:
        if turns:
            codes = {1: cv2.ROTATE_90_CLOCKWISE, 2: cv2.ROTATE_180, 3: cv2.ROTATE_90_COUNTERCLOCKWISE}
            frame = cv2.rotate(frame, codes[turns])
        if flip_h and flip_v:
            frame = cv2.flip(frame, -1)
        elif flip_h:
            frame = cv2.flip(frame, 1)
        elif flip_v:
            frame = cv2.flip(frame, 0)
        return frame

    import numpy as np

    if turns:
        # np.rot90 turns anticlockwise; -turns is clockwise.
        frame = np.rot90(frame, -turns)
    if flip_h:
        frame = frame[:, ::-1]
    if flip_v:
        frame = frame[::-1, :]
    return np.ascontiguousarray(frame)
