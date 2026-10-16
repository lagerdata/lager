# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for box/lager/automation/webcam/controls.py: the camera modes and
controls read from v4l2-ctl, and the pan, zoom and orientation the streamer
applies to each frame.

The module is loaded from its file, as the streamer loads it, so nothing
from the lager package is imported. v4l2-ctl is replaced by canned output
taken from the shapes real UVC cameras print.
"""

import importlib.util
import subprocess
from pathlib import Path

import numpy as np
import pytest

_CONTROLS = (Path(__file__).resolve().parents[3] / 'box' / 'lager' / 'automation'
             / 'webcam' / 'controls.py')

FORMATS = """ioctl: VIDIOC_ENUM_FMT
\tType: Video Capture

\t[0]: 'YUYV' (YUYV 4:2:2)
\t\tSize: Discrete 640x480
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t\tSize: Discrete 1920x1080
\t\t\tInterval: Discrete 0.200s (5.000 fps)
\t[1]: 'MJPG' (Motion-JPEG, compressed)
\t\tSize: Discrete 640x480
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t\t\tInterval: Discrete 0.067s (15.000 fps)
\t\tSize: Discrete 1920x1080
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t\t\tInterval: Discrete 0.040s (25.000 fps)
\t\tSize: Discrete 1280x720
\t\t\tInterval: Discrete 0.017s (60.000 fps)
\t\t\tInterval: Discrete 0.033s (29.970 fps)
"""

CONTROLS_NEW = """
User Controls

                     brightness 0x00980900 (int)    : min=0 max=255 step=1 default=128 value=128
                           gain 0x00980913 (int)    : min=0 max=255 step=1 default=0 value=12

Camera Controls

                  auto_exposure 0x009a0901 (menu)   : min=0 max=3 default=3 value=3 (Aperture Priority Mode)
         exposure_time_absolute 0x009a0902 (int)    : min=3 max=2047 step=1 default=250 value=250 flags=inactive
"""

CONTROLS_OLD = """
                  exposure_auto 0x009a0901 (menu)   : min=0 max=3 default=3 value=1
              exposure_absolute 0x009a0902 (int)    : min=3 max=2047 step=1 default=250 value=400
"""


@pytest.fixture(scope='module')
def ctl():
    spec = importlib.util.spec_from_file_location('webcam_controls_under_test', _CONTROLS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeV4l2:
    """Answers v4l2-ctl as a camera would, and records every -c it is sent."""

    def __init__(self, formats='', controls='', fail_set=False):
        self.formats, self.controls, self.fail_set = formats, controls, fail_set
        self.sets = []

    def __call__(self, args, **_kwargs):
        if '--list-formats-ext' in args:
            out = self.formats
        elif '--list-ctrls' in args:
            out = self.controls
        else:
            self.sets.append(args[args.index('-c') + 1])
            return subprocess.CompletedProcess(args, 1 if self.fail_set else 0, '', '')
        return subprocess.CompletedProcess(args, 0, out, '')


# ---------- modes ----------

def test_only_mjpeg_modes_are_offered_largest_first(ctl):
    assert ctl.parse_modes(FORMATS) == [
        {'width': 1920, 'height': 1080, 'fps': [30, 25]},
        {'width': 1280, 'height': 720, 'fps': [60, 29.97]},
        {'width': 640, 'height': 480, 'fps': [30, 15]},
    ]


def test_no_v4l2_ctl_means_no_modes(ctl):
    def missing(*_a, **_k):
        raise FileNotFoundError('v4l2-ctl')
    assert ctl.list_modes('/dev/video0', run=missing) == []


def test_a_mode_must_be_one_the_camera_lists(ctl):
    modes = ctl.parse_modes(FORMATS)
    assert ctl.valid_mode(modes, 1920, 1080, 25)
    assert not ctl.valid_mode(modes, 1920, 1080, 60)
    assert not ctl.valid_mode(modes, 3840, 2160, 30)
    # A camera that lists nothing cannot be checked; the capture loop is.
    assert ctl.valid_mode([], 3840, 2160, 30)


# ---------- controls ----------

def test_controls_are_parsed_with_their_flags(ctl):
    parsed = ctl.parse_controls(CONTROLS_NEW)
    assert parsed['gain'] == {'type': 'int', 'inactive': False, 'min': 0, 'max': 255,
                              'step': 1, 'default': 0, 'value': 12}
    assert parsed['exposure_time_absolute']['inactive'] is True
    assert parsed['auto_exposure']['value'] == 3


@pytest.mark.parametrize('text,auto,value', [(CONTROLS_NEW, True, 250), (CONTROLS_OLD, False, 400)])
def test_exposure_is_found_under_either_kernel_name(ctl, text, auto, value):
    described = ctl.describe_controls('/dev/video0', run=FakeV4l2(controls=text))
    assert described['exposure']['auto'] is auto
    assert described['exposure']['value'] == value
    assert (described['exposure']['min'], described['exposure']['max']) == (3, 2047)


def test_a_camera_without_exposure_or_gain_describes_neither(ctl):
    only_brightness = CONTROLS_NEW.split('gain')[0]
    assert ctl.describe_controls('/dev/video0', run=FakeV4l2(controls=only_brightness)) == {}


def test_exposure_without_an_auto_control_reports_none(ctl):
    text = CONTROLS_OLD.split('\n', 2)[2]  # drop the exposure_auto line
    described = ctl.describe_controls('/dev/video0', run=FakeV4l2(controls=text))
    assert described['exposure']['auto'] is None


def test_setting_uses_the_cameras_own_name_and_clamps(ctl):
    v4l2 = FakeV4l2(controls=CONTROLS_OLD)
    assert ctl.set_control('/dev/video0', 'exposure', 99999, run=v4l2)
    assert ctl.set_control('/dev/video0', 'exposure_auto', False, run=v4l2)
    assert ctl.set_control('/dev/video0', 'exposure_auto', True, run=v4l2)
    assert v4l2.sets == ['exposure_absolute=2047', 'exposure_auto=1', 'exposure_auto=3']


@pytest.mark.parametrize('logical,value', [
    ('brightness', 10),       # not one of the settable controls
    ('gain', 'loud'),         # not a number
    ('gain', None),
])
def test_unknown_or_bad_settings_are_refused_without_a_call(ctl, logical, value):
    v4l2 = FakeV4l2(controls=CONTROLS_NEW)
    assert not ctl.set_control('/dev/video0', logical, value, run=v4l2)
    assert v4l2.sets == []


def test_a_control_the_camera_lacks_is_refused(ctl):
    v4l2 = FakeV4l2(controls=CONTROLS_OLD)
    assert not ctl.set_control('/dev/video0', 'gain', 10, run=v4l2)
    assert v4l2.sets == []


def test_a_camera_that_rejects_the_value_reports_false(ctl):
    v4l2 = FakeV4l2(controls=CONTROLS_NEW, fail_set=True)
    assert not ctl.set_control('/dev/video0', 'gain', 10, run=v4l2)


# ---------- view ----------

def test_merge_view_keeps_only_valid_fields(ctl):
    view = ctl.merge_view(None, {'pan_x': 2, 'pan_y': 'x', 'rotate': 45, 'flip_h': 'yes', 'flip_v': True})
    assert view == {'pan_x': 1.0, 'pan_y': 0.5, 'rotate': 0, 'flip_h': False, 'flip_v': True}
    assert ctl.merge_view(view, {'rotate': 270})['rotate'] == 270
    assert ctl.merge_view(view, {})['flip_v'] is True


@pytest.mark.parametrize('zoom,pan,expected', [
    (1.0, (0.9, 0.1), (0.5, 0.5)),     # nothing to pan at 1x
    (2.0, (0.1, 0.9), (0.25, 0.75)),   # a half-size region stops at the edge
    (4.0, (0.3, 0.6), (0.3, 0.6)),     # inside the limits it is kept
])
def test_pan_stays_inside_the_picture(ctl, zoom, pan, expected):
    assert ctl.clamp_pan(zoom, *pan) == pytest.approx(expected)


def test_crop_box_follows_the_pan(ctl):
    assert ctl.crop_box(640, 480, 1.0, 0.1, 0.1) == (0, 0, 640, 480)
    assert ctl.crop_box(640, 480, 2.0, 0.5, 0.5) == (160, 120, 480, 360)
    assert ctl.crop_box(640, 480, 2.0, 0.0, 1.0) == (0, 240, 320, 480)
    x1, y1, x2, y2 = ctl.crop_box(641, 479, 3.0, 0.77, 0.21)
    assert 0 <= x1 < x2 <= 641 and 0 <= y1 < y2 <= 479


def test_orient_turns_clockwise_then_mirrors(ctl):
    # A 2x3 frame whose pixels are numbered row by row.
    frame = np.arange(6).reshape(2, 3)
    assert ctl.orient(frame, 0, False, False).tolist() == [[0, 1, 2], [3, 4, 5]]
    assert ctl.orient(frame, 90, False, False).tolist() == [[3, 0], [4, 1], [5, 2]]
    assert ctl.orient(frame, 180, False, False).tolist() == [[5, 4, 3], [2, 1, 0]]
    assert ctl.orient(frame, 270, False, False).tolist() == [[2, 5], [1, 4], [0, 3]]
    assert ctl.orient(frame, 0, True, False).tolist() == [[2, 1, 0], [5, 4, 3]]
    assert ctl.orient(frame, 0, False, True).tolist() == [[3, 4, 5], [0, 1, 2]]
    # Mirrored after the turn, so left-right is the viewer's left-right.
    assert ctl.orient(frame, 90, True, False).tolist() == [[0, 3], [1, 4], [2, 5]]


def test_orient_keeps_colour_channels(ctl):
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    turned = ctl.orient(frame, 90, False, False)
    assert turned.shape == (640, 480, 3)
    assert turned.flags['C_CONTIGUOUS']


@pytest.mark.parametrize('rotate', [0, 90, 180, 270])
@pytest.mark.parametrize('flip_h,flip_v', [(False, False), (True, False), (False, True), (True, True)])
def test_cv2_and_numpy_orient_alike(ctl, rotate, flip_h, flip_v):
    cv2 = pytest.importorskip('cv2')
    frame = np.arange(4 * 6 * 3, dtype=np.uint8).reshape(4, 6, 3)
    fast = ctl.orient(frame, rotate, flip_h, flip_v, cv2=cv2)
    plain = ctl.orient(frame, rotate, flip_h, flip_v)
    assert np.array_equal(fast, plain)
