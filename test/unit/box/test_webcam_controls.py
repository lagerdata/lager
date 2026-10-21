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
        elif '--list-ctrls' in args or '--list-ctrls-menus' in args:
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
    described = ctl.describe_controls('/dev/video0', run=FakeV4l2(controls=only_brightness))
    assert list(described) == ['brightness']


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
    ('tilt', 10),             # not one of the settable controls
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


# ---------- a global-shutter camera, as v4l2-ctl prints it ----------
#
# Captured from an ELP Global Shutter camera (AR0234, 32e4:0234) on kernel
# 7.0, with its controls locked for LED work. Every numeric line carries
# flags=0x00001000, a bit newer than the installed v4l2-ctl can name.

GLOBAL_SHUTTER_CTRLS = """
User Controls

                     brightness 0x00980900 (int)    : min=-64 max=64 step=1 default=0 value=0 flags=0x00001000
                       contrast 0x00980901 (int)    : min=0 max=95 step=1 default=0 value=32 flags=0x00001000
                     saturation 0x00980902 (int)    : min=0 max=255 step=1 default=56 value=56 flags=0x00001000
                            hue 0x00980903 (int)    : min=-2000 max=2000 step=1 default=0 value=0 flags=0x00001000
        white_balance_automatic 0x0098090c (bool)   : default=1 value=0
                          gamma 0x00980910 (int)    : min=64 max=300 step=1 default=110 value=100 flags=0x00001000
                           gain 0x00980913 (int)    : min=0 max=1023 step=1 default=0 value=0 flags=0x00001000
           power_line_frequency 0x00980918 (menu)   : min=0 max=2 default=1 value=2 (60 Hz)
                0: Disabled
                1: 50 Hz
                2: 60 Hz
      white_balance_temperature 0x0098091a (int)    : min=2800 max=6500 step=1 default=4600 value=4600 flags=0x00001000
                      sharpness 0x0098091b (int)    : min=0 max=7 step=1 default=0 value=0 flags=0x00001000
         backlight_compensation 0x0098091c (int)    : min=36 max=160 step=1 default=54 value=36 flags=0x00001000

Camera Controls

                  auto_exposure 0x009a0901 (menu)   : min=0 max=3 default=3 value=1 (Manual Mode)
                1: Manual Mode
                3: Aperture Priority Mode
         exposure_time_absolute 0x009a0902 (int)    : min=1 max=10000 step=1 default=156 value=312 flags=0x00001000
                   pan_absolute 0x009a0908 (int)    : min=-648000 max=648000 step=3600 default=0 value=0 flags=0x00001000
                  tilt_absolute 0x009a0909 (int)    : min=-648000 max=648000 step=3600 default=0 value=0 flags=0x00001000
                 focus_absolute 0x009a090a (int)    : min=0 max=1023 step=1 default=0 value=383 flags=0x00001000
     focus_automatic_continuous 0x009a090c (bool)   : default=1 value=0
                  zoom_absolute 0x009a090d (int)    : min=0 max=60 step=1 default=0 value=0 flags=0x00001000
"""

# Every MJPEG size at 90 down to 5 fps; YUYV only as fast as USB 2.0 allows.
# 1920x1200 is listed after 1920x1080, as the camera lists it.
_SIZES = ['1920x1080', '1920x1200', '1600x1200', '1280x960', '1280x720',
          '1024x768', '960x720', '800x600', '640x480', '320x240']
_MJPG_RATES = [90, 60, 30, 25, 20, 15, 10, 5]
_YUYV_RATES = {'1920x1080': [5], '1920x1200': [5], '1600x1200': [5], '1280x960': [5],
               '1280x720': [10, 5], '1024x768': [10, 5], '960x720': [15, 10, 5],
               '800x600': [20, 15, 10, 5], '640x480': [30, 25, 20, 15, 10, 5],
               '320x240': _MJPG_RATES}


def _format_block(index, fourcc, label, rates_for):
    lines = [f"\t[{index}]: '{fourcc}' ({label})"]
    for size in _SIZES:
        lines.append(f'\t\tSize: Discrete {size}')
        for fps in rates_for(size):
            lines.append(f'\t\t\tInterval: Discrete {1 / fps:.3f}s ({fps:.3f} fps)')
    return '\n'.join(lines)


GLOBAL_SHUTTER_FORMATS = '\n'.join([
    'ioctl: VIDIOC_ENUM_FMT', '\tType: Video Capture', '',
    _format_block(0, 'MJPG', 'Motion-JPEG, compressed', lambda size: _MJPG_RATES),
    '',
    _format_block(1, 'YUYV', 'YUYV 4:2:2', lambda size: _YUYV_RATES[size]),
    '',
])


def test_global_shutter_modes_are_mjpeg_only_and_sorted_by_area(ctl):
    modes = ctl.parse_modes(GLOBAL_SHUTTER_FORMATS)
    sizes = [(m['width'], m['height']) for m in modes]
    # By area, not by the order the camera lists them in.
    assert sizes[:3] == [(1920, 1200), (1920, 1080), (1600, 1200)]
    assert sizes[-1] == (320, 240) and len(sizes) == 10
    # Every size at every MJPEG rate, fastest first: no YUYV-only limit leaks in.
    assert all(m['fps'] == _MJPG_RATES for m in modes)
    assert ctl.valid_mode(modes, 1920, 1080, 90)
    assert not ctl.valid_mode(modes, 1920, 1080, 120)


def test_a_flag_v4l2_ctl_cannot_name_is_not_inactive(ctl):
    parsed = ctl.parse_controls(GLOBAL_SHUTTER_CTRLS)
    assert parsed['brightness']['inactive'] is False
    assert (parsed['brightness']['min'], parsed['brightness']['max']) == (-64, 64)
    assert parsed['focus_absolute']['value'] == 383


def test_menu_items_are_kept_with_their_control(ctl):
    parsed = ctl.parse_controls(GLOBAL_SHUTTER_CTRLS)
    assert parsed['power_line_frequency']['menu'] == {0: 'Disabled', 1: '50 Hz', 2: '60 Hz'}
    assert parsed['power_line_frequency']['value'] == 2
    assert parsed['auto_exposure']['menu'] == {1: 'Manual Mode', 3: 'Aperture Priority Mode'}
    # An item line belongs to the menu above it, never to the next control.
    assert 'menu' not in parsed['white_balance_temperature']
    assert 'menu' not in parsed['exposure_time_absolute']


def test_global_shutter_controls_are_described_in_their_own_ranges(ctl):
    described = ctl.describe_controls('/dev/video0', run=FakeV4l2(controls=GLOBAL_SHUTTER_CTRLS))
    assert set(described) == {
        'exposure', 'gain', 'white_balance_temperature', 'focus', 'brightness', 'contrast',
        'saturation', 'hue', 'gamma', 'sharpness', 'backlight_compensation',
        'power_line_frequency'}
    assert described['brightness'] == {'value': 0, 'min': -64, 'max': 64, 'default': 0, 'step': 1}
    assert described['focus'] == {'value': 383, 'min': 0, 'max': 1023, 'default': 0,
                                  'step': 1, 'auto': False}
    assert described['exposure']['auto'] is False and described['exposure']['value'] == 312
    assert described['white_balance_temperature']['auto'] is False
    assert described['power_line_frequency']['options'] == [
        {'value': 0, 'label': 'Disabled'}, {'value': 1, 'label': '50 Hz'},
        {'value': 2, 'label': '60 Hz'}]
    # Pan, tilt and zoom are the camera's, not lager's to set.
    assert 'pan_absolute' not in described and 'zoom' not in described


def test_values_are_read_with_switches_as_bools(ctl):
    values = ctl.read_values('/dev/video0', run=FakeV4l2(controls=GLOBAL_SHUTTER_CTRLS))
    assert values['exposure_auto'] is False
    assert values['white_balance_auto'] is False
    assert values['focus_auto'] is False
    assert (values['focus'], values['exposure'], values['backlight_compensation']) == (383, 312, 36)


@pytest.mark.parametrize('logical,value,written', [
    ('brightness', 128, 'brightness=64'),           # clamped to -64..64, not 0..255
    ('brightness', -100, 'brightness=-64'),
    ('focus', 383, 'focus_absolute=383'),           # beyond the old 0..255 limit
    ('focus', 5000, 'focus_absolute=1023'),
    ('gamma', 10, 'gamma=64'),
    ('white_balance_auto', False, 'white_balance_automatic=0'),
    ('white_balance_auto', True, 'white_balance_automatic=1'),
    ('focus_auto', True, 'focus_automatic_continuous=1'),
    ('power_line_frequency', 1, 'power_line_frequency=1'),
])
def test_each_control_is_written_in_the_cameras_range(ctl, logical, value, written):
    v4l2 = FakeV4l2(controls=GLOBAL_SHUTTER_CTRLS)
    assert ctl.set_control('/dev/video0', logical, value, run=v4l2)
    assert v4l2.sets == [written]


@pytest.mark.parametrize('value', [3, -1, 'off', True])
def test_a_menu_value_the_camera_does_not_list_is_refused(ctl, value):
    v4l2 = FakeV4l2(controls=GLOBAL_SHUTTER_CTRLS)
    assert not ctl.set_control('/dev/video0', 'power_line_frequency', value, run=v4l2)
    assert v4l2.sets == []


def test_values_are_saved_as_the_camera_keeps_them(ctl):
    parsed = ctl.parse_controls(GLOBAL_SHUTTER_CTRLS)
    assert ctl.as_saved(parsed, {
        'brightness': 128, 'contrast': -100, 'focus': 2000, 'gamma': '120',
        'focus_auto': 0, 'white_balance_auto': 1, 'exposure_auto': False,
    }) == {'brightness': 64, 'contrast': 0, 'focus': 1023, 'gamma': 120,
           'focus_auto': False, 'white_balance_auto': True, 'exposure_auto': False}
    # Zero is a value, not a missing one.
    assert ctl.as_saved(parsed, {'brightness': 0}) == {'brightness': 0}


def test_a_value_the_camera_would_refuse_is_not_saved(ctl):
    parsed = ctl.parse_controls(GLOBAL_SHUTTER_CTRLS)
    assert ctl.as_saved(parsed, {'power_line_frequency': 3, 'tilt': 1,
                                 'brightness': 'bright'}) == {}
    # Nor a control this camera does not have.
    assert ctl.as_saved(ctl.parse_controls(CONTROLS_OLD), {'gain': 3}) == {}


def test_saved_controls_are_written_switches_first(ctl):
    v4l2 = FakeV4l2(controls=GLOBAL_SHUTTER_CTRLS)
    taken = ctl.apply_controls('/dev/video0', {
        'focus': 383, 'gain': 0, 'white_balance_temperature': 4600,
        'focus_auto': False, 'exposure': 312, 'exposure_auto': False,
        'white_balance_auto': False, 'not_a_control': 1}, run=v4l2)
    assert v4l2.sets[:3] == ['auto_exposure=1', 'white_balance_automatic=0',
                             'focus_automatic_continuous=0']
    assert set(v4l2.sets[3:]) == {'exposure_time_absolute=312', 'gain=0',
                                  'white_balance_temperature=4600', 'focus_absolute=383'}
    assert 'not_a_control' not in taken and all(taken.values())


def test_a_value_its_switch_hands_to_the_camera_is_not_written(ctl):
    v4l2 = FakeV4l2(controls=GLOBAL_SHUTTER_CTRLS)
    ctl.apply_controls('/dev/video0', {'focus_auto': True, 'focus': 383,
                                       'brightness': 0}, run=v4l2)
    assert v4l2.sets == ['focus_automatic_continuous=1', 'brightness=0']


def test_nothing_saved_writes_nothing(ctl):
    v4l2 = FakeV4l2(controls=GLOBAL_SHUTTER_CTRLS)
    assert ctl.apply_controls('/dev/video0', {}, run=v4l2) == {}
    assert v4l2.sets == []


def test_a_lock_keeps_every_value_and_turns_every_auto_off(ctl):
    parsed = ctl.parse_controls(CONTROLS_NEW)   # auto exposure on
    assert ctl.lock_values(parsed) == {'exposure_auto': False, 'exposure': 250,
                                       'gain': 12, 'brightness': 128}
    locked = ctl.lock_values(ctl.parse_controls(GLOBAL_SHUTTER_CTRLS), {'focus': 332})
    assert locked['focus'] == 332 and locked['focus_auto'] is False
    assert locked['white_balance_temperature'] == 4600 and locked['power_line_frequency'] == 2
    # A control the camera lacks is not added by an override.
    assert 'gain' not in ctl.lock_values(ctl.parse_controls(CONTROLS_OLD), {'gain': 3})
    with pytest.raises(ValueError, match='zoom'):
        ctl.lock_values(parsed, {'zoom': 2})


def test_compare_reports_what_did_not_stick(ctl):
    report, ok = ctl.compare({'focus': 383, 'gain': 0}, {'focus': 383, 'gain': 0})
    assert ok and report['focus'] == {'wanted': 383, 'actual': 383}
    report, ok = ctl.compare({'brightness': 128}, {'brightness': 64})
    assert not ok and report['brightness'] == {'wanted': 128, 'actual': 64}
