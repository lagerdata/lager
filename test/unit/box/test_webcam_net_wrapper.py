# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the webcam net's frame and control methods
(box/lager/automation/webcam/webcam_net_wrapper.py).

``snapshot`` and ``frame`` read the running stream's own ``/snapshot`` over
127.0.0.1, served here by a stand-in HTTP server. The control methods run
v4l2-ctl through ``controls.py``; each test loads a fresh copy of that module
under a fake v4l2-ctl and puts it in the wrapper's place. Settings go to a
temp state directory, never /etc/lager.
"""

import importlib.util
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from lager.automation.webcam import service as svc_mod
from lager.automation.webcam import webcam_net_wrapper as wrapper_mod

_CONTROLS = Path(wrapper_mod.__file__).with_name('controls.py')

# A global-shutter camera with every auto mode on, as one comes out of the box.
CTRLS = """
                     brightness 0x00980900 (int)    : min=-64 max=64 step=1 default=0 value=10 flags=0x00001000
                          gamma 0x00980910 (int)    : min=64 max=300 step=1 default=110 value=110 flags=0x00001000
        white_balance_automatic 0x0098090c (bool)   : default=1 value=1
      white_balance_temperature 0x0098091a (int)    : min=2800 max=6500 step=1 default=4600 value=5100 flags=0x00001000
           power_line_frequency 0x00980918 (menu)   : min=0 max=2 default=1 value=1 (50 Hz)
                0: Disabled
                1: 50 Hz
                2: 60 Hz
                  auto_exposure 0x009a0901 (menu)   : min=0 max=3 default=3 value=3 (Aperture Priority Mode)
                1: Manual Mode
                3: Aperture Priority Mode
         exposure_time_absolute 0x009a0902 (int)    : min=1 max=10000 step=1 default=156 value=156 flags=0x00001000
                 focus_absolute 0x009a090a (int)    : min=0 max=1023 step=1 default=0 value=383 flags=0x00001000
     focus_automatic_continuous 0x009a090c (bool)   : default=1 value=1
"""


class Camera:
    """v4l2-ctl over a camera that keeps what it is sent, as a real one does."""

    def __init__(self, text=CTRLS):
        self.values = {}
        self.text = text
        self.sets = []

    def listing(self):
        out = []
        for line in self.text.splitlines():
            name = line.split()[0] if line.split() else ''
            if name in self.values:
                head, _sep, tail = line.partition(' value=')
                rest = tail.split(' ', 1)
                line = f"{head} value={self.values[name]}" + (' ' + rest[1] if len(rest) > 1 else '')
            out.append(line)
        return '\n'.join(out)

    def __call__(self, args, **_kwargs):
        if '-c' in args:
            name, value = args[args.index('-c') + 1].split('=')
            self.sets.append(f'{name}={value}')
            self.values[name] = int(value)
            return subprocess.CompletedProcess(args, 0, '', '')
        return subprocess.CompletedProcess(args, 0, self.listing(), '')


@pytest.fixture
def camera():
    return Camera()


@pytest.fixture
def webcam(tmp_path, camera):
    """A webcam net on /dev/video8 whose v4l2-ctl is ``camera``."""
    with patch('subprocess.run', side_effect=camera):
        spec = importlib.util.spec_from_file_location('webcam_controls_for_wrapper', _CONTROLS)
        controls = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controls)   # binds the fake as its default run
    service = svc_mod.WebcamService.__new__(svc_mod.WebcamService)
    service.state = svc_mod.WebcamStreamState(state_file=tmp_path / 'webcam_streams.json')
    with patch.object(wrapper_mod, '_controls', controls), \
            patch.object(wrapper_mod, 'WebcamService', return_value=service):
        yield wrapper_mod.WebcamNetWrapper('led_cam', {'pin': '/dev/video8'})


def _saved(webcam):
    return webcam._service.state.get_settings('led_cam').get('controls')


# ---------- controls ----------

def test_controls_are_the_cameras_with_their_ranges(webcam):
    described = webcam.controls()
    assert described['brightness']['min'] == -64 and described['brightness']['max'] == 64
    assert described['focus']['auto'] is True
    assert [o['label'] for o in described['power_line_frequency']['options']] == \
        ['Disabled', '50 Hz', '60 Hz']


def test_set_control_writes_and_keeps_it_for_the_net(webcam, camera):
    assert webcam.set_control('focus', 383) is True
    assert webcam.set_control('focus_auto', 0) is True
    assert camera.sets == ['focus_absolute=383', 'focus_automatic_continuous=0']
    assert _saved(webcam) == {'focus': 383, 'focus_auto': False}


def test_a_value_the_camera_refuses_is_not_kept(webcam, camera):
    assert webcam.set_control('power_line_frequency', 7) is False
    assert camera.sets == [] and _saved(webcam) is None
    with pytest.raises(ValueError, match='zoom'):
        webcam.set_control('zoom', 2)


def test_lock_turns_every_auto_off_and_keeps_the_rest(webcam, camera):
    report = webcam.lock_controls(exposure=312, power_line_frequency=2)
    assert report['ok'] is True
    assert camera.sets[:3] == ['auto_exposure=1', 'white_balance_automatic=0',
                               'focus_automatic_continuous=0']
    assert report['controls']['exposure'] == {'wanted': 312, 'actual': 312}
    assert report['controls']['focus'] == {'wanted': 383, 'actual': 383}
    assert report['controls']['white_balance_auto'] == {'wanted': False, 'actual': False}
    # Saved whole, so the next stream start puts every one back.
    assert _saved(webcam) == {
        'exposure_auto': False, 'exposure': 312, 'white_balance_auto': False,
        'white_balance_temperature': 5100, 'focus_auto': False, 'focus': 383,
        'brightness': 10, 'gamma': 110, 'power_line_frequency': 2}


def test_a_lock_value_the_camera_clamps_is_reported(webcam):
    report = webcam.lock_controls(brightness=128)
    assert report['ok'] is False
    assert report['controls']['brightness'] == {'wanted': 128, 'actual': 64}


def test_lock_with_no_camera_raises(tmp_path):
    with patch('subprocess.run', side_effect=FileNotFoundError('v4l2-ctl')):
        spec = importlib.util.spec_from_file_location('webcam_controls_absent', _CONTROLS)
        controls = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controls)
    service = svc_mod.WebcamService.__new__(svc_mod.WebcamService)
    service.state = svc_mod.WebcamStreamState(state_file=tmp_path / 'webcam_streams.json')
    with patch.object(wrapper_mod, '_controls', controls), \
            patch.object(wrapper_mod, 'WebcamService', return_value=service):
        cam = wrapper_mod.WebcamNetWrapper('led_cam', {'pin': '/dev/video8'})
        with pytest.raises(RuntimeError, match='/dev/video8'):
            cam.lock_controls()


# ---------- frames ----------

class _Snapshots(BaseHTTPRequestHandler):
    """The streamer's /snapshot: VIEW, or RAW with ?raw=1, or 503 if empty."""
    empty = False
    paths = []

    def do_GET(self):
        type(self).paths.append(self.path)
        if type(self).empty:
            self.send_response(503)
            self.end_headers()
            return
        body = b'RAW' if self.path.endswith('?raw=1') else b'VIEW'
        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def stream(webcam):
    _Snapshots.empty, _Snapshots.paths = False, []
    server = HTTPServer(('127.0.0.1', 0), _Snapshots)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # This test process stands in for the streamer, so the pid is alive.
    webcam._service.state.add_stream('led_cam', '/dev/video8', server.server_address[1], os.getpid())
    try:
        yield webcam
    finally:
        server.shutdown()
        server.server_close()


def test_snapshot_reads_the_running_streams_frame(stream):
    assert stream.snapshot() == b'VIEW'
    assert stream.snapshot(raw=True) == b'RAW'
    assert _Snapshots.paths == ['/snapshot', '/snapshot?raw=1']


def test_snapshot_without_a_frame_says_so(stream):
    _Snapshots.empty = True
    with pytest.raises(RuntimeError, match='no frame'):
        stream.snapshot()


def test_snapshot_of_a_stopped_stream_says_to_start_it(webcam):
    with pytest.raises(RuntimeError, match='not running'):
        webcam.snapshot()


def test_frame_decodes_the_raw_frame_by_default(stream):
    import sys
    import types

    import numpy as np

    decoded = []
    fake_cv2 = types.ModuleType('cv2')
    fake_cv2.IMREAD_COLOR = 1
    fake_cv2.imdecode = lambda buf, flags: decoded.append(bytes(buf)) or np.zeros((2, 3, 3), np.uint8)
    with patch.dict(sys.modules, {'cv2': fake_cv2}):
        image = stream.frame()
    assert image.shape == (2, 3, 3)
    assert decoded == [b'RAW']

