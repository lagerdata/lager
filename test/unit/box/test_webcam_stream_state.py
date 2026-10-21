# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the webcam stream state file and the streamer template
(box/lager/automation/webcam/service.py).

The module is loaded straight from its file so the test does not drag in
the `lager` package (and its hardware-only imports). Covered:

  - the per-stream record carries ``source`` / ``started_by`` and readers
    tolerate state files written before those keys existed
  - ``start_stream`` threads the origin through to the record
  - the generated streamer script still compiles (its f-string doubles
    every brace, so an edit that gets one wrong only fails on the box)
    and serves ``/snapshot``
"""

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

_SERVICE = Path(__file__).resolve().parents[3] / 'box' / 'lager' / 'automation' / 'webcam' / 'service.py'


@pytest.fixture(scope='module')
def svc_mod():
    spec = importlib.util.spec_from_file_location('webcam_service_under_test', _SERVICE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_add_stream_persists_origin(svc_mod, tmp_path):
    state = svc_mod.WebcamStreamState(state_file=tmp_path / 'streams.json')
    state.add_stream('cam1', '/dev/video0', 8086, 4242, source='cli', started_by='alice')
    on_disk = json.loads((tmp_path / 'streams.json').read_text())
    assert on_disk['cam1']['source'] == 'cli'
    assert on_disk['cam1']['started_by'] == 'alice'
    assert on_disk['cam1']['port'] == 8086


def test_add_stream_without_origin_writes_nulls(svc_mod, tmp_path):
    state = svc_mod.WebcamStreamState(state_file=tmp_path / 'streams.json')
    state.add_stream('cam1', '/dev/video0', 8086, 4242)
    on_disk = json.loads((tmp_path / 'streams.json').read_text())
    assert on_disk['cam1']['source'] is None
    assert on_disk['cam1']['started_by'] is None


def _service(svc_mod, tmp_path):
    # WebcamService() would create /etc/lager on construction; bypass it and
    # point the state at a temp file instead.
    svc = svc_mod.WebcamService.__new__(svc_mod.WebcamService)
    svc.state = svc_mod.WebcamStreamState(state_file=tmp_path / 'streams.json')
    return svc


def test_start_stream_threads_origin_into_info(svc_mod, tmp_path):
    svc = _service(svc_mod, tmp_path)
    # start_stream checks the device node exists; a real temp file stands in
    # for /dev/video0 so nothing global has to be patched.
    device = tmp_path / 'video0'
    device.touch()
    with patch.object(svc, '_start_streaming_process', return_value=4242), \
            patch.object(svc, '_is_process_alive', return_value=True), \
            patch.object(svc_mod.time, 'sleep'):
        res = svc.start_stream('cam1', str(device), '10.0.0.5',
                               source='api', started_by='bob')
        info = svc.get_stream_info('cam1', '10.0.0.5')
    assert res['port'] == 8086 and res['already_running'] is False
    assert info['source'] == 'api'
    assert info['started_by'] == 'bob'
    assert info['url'] == 'http://10.0.0.5:8086/'


def test_get_stream_info_tolerates_pre_origin_state(svc_mod, tmp_path):
    # A state file from before the origin fields existed.
    (tmp_path / 'streams.json').write_text(json.dumps({
        'cam1': {'video_device': '/dev/video0', 'port': 8086, 'pid': 1,
                 'started_at': 0, 'zoom': 1.0, 'focus_mode': 'auto',
                 'focus_value': 0, 'brightness': 128}}))
    svc = _service(svc_mod, tmp_path)
    with patch.object(svc, '_is_process_alive', return_value=True):
        info = svc.get_stream_info('cam1', '10.0.0.5')
    assert info['source'] is None
    assert info['started_by'] is None


def test_generated_streamer_compiles_and_serves_snapshot(svc_mod, tmp_path):
    port = 58086
    script_path = f'/tmp/webcam_stream_{port}.py'
    svc = _service(svc_mod, tmp_path)
    completed = svc_mod.subprocess.CompletedProcess(args='', returncode=0, stdout='999\n', stderr='')
    try:
        with patch.object(svc_mod.subprocess, 'run', return_value=completed), \
                patch.object(svc_mod.os, 'chmod'):
            pid = svc._start_streaming_process('/dev/video0', port, 'cam1', '10.0.0.5')
        assert pid == 999
        source = Path(script_path).read_text()
        compile(source, script_path, 'exec')  # every brace in the template survived
        assert '/snapshot' in source
        assert 'image/jpeg' in source
        assert 'No frame available' in source
    finally:
        if os.path.exists(script_path):
            os.remove(script_path)


# A camera's controls as v4l2-ctl --list-ctrls-menus prints them: the
# global-shutter camera's ranges (brightness -64..64, focus 0..1023), with
# autofocus off at 383.
CAMERA_CTRLS = """
                     brightness 0x00980900 (int)    : min=-64 max=64 step=1 default=0 value=0 flags=0x00001000
        white_balance_automatic 0x0098090c (bool)   : default=1 value=0
      white_balance_temperature 0x0098091a (int)    : min=2800 max=6500 step=1 default=4600 value=4600 flags=0x00001000
                 focus_absolute 0x009a090a (int)    : min=0 max=1023 step=1 default=0 value=383 flags=0x00001000
     focus_automatic_continuous 0x009a090c (bool)   : default=1 value=0
"""


class FakeV4l2:
    """v4l2-ctl for the streamer: lists CAMERA_CTRLS, records every -c."""

    def __init__(self):
        self.sets = []

    def __call__(self, args, **_kwargs):
        import subprocess
        if isinstance(args, list) and '-c' in args:
            self.sets.append(args[args.index('-c') + 1])
            return subprocess.CompletedProcess(args, 0, '', '')
        if isinstance(args, list) and '--list-ctrls-menus' in args:
            return subprocess.CompletedProcess(args, 0, CAMERA_CTRLS, '')
        return subprocess.CompletedProcess(args, 0, '', '')


def _load_streamer(svc_mod, tmp_path, port, records=None, settings=None, v4l2=None):
    """Render the streamer as the box does and run it up to its server.

    ``records`` and ``settings`` are written to the state and settings files
    first, so what the script does as it starts can be checked. v4l2-ctl is
    ``v4l2`` (default: a call that answers nothing), and cv2 a bare stand-in.
    """
    import sys
    import types

    script_path = f'/tmp/webcam_stream_{port}.py'
    svc = _service(svc_mod, tmp_path)
    completed = svc_mod.subprocess.CompletedProcess(args='', returncode=0, stdout='999\n', stderr='')
    with patch.object(svc_mod.subprocess, 'run', return_value=completed), \
            patch.object(svc_mod.os, 'chmod'):
        svc._start_streaming_process('/dev/video0', port, 'cam1', '10.0.0.5')
    try:
        source = Path(script_path).read_text()
    finally:
        os.remove(script_path)
    body = source.split('\nserver = ThreadedHTTPServer')[0]

    if records is not None:
        svc.state.save(records)
    if settings is not None:
        svc.state.save_settings(settings)
    ns = {'__name__': 'webcam_streamer_under_test'}
    run = patch('subprocess.run', side_effect=v4l2) if v4l2 else patch('subprocess.run', return_value=completed)
    with patch.dict(sys.modules, {'cv2': types.ModuleType('cv2')}), run:
        exec(compile(body, script_path, 'exec'), ns)
    assert ns['STATE_FILE'] == str(svc.state.state_file)
    assert ns['SETTINGS_FILE'] == str(svc.state.settings_file)
    return ns


def _serve(ns):
    import threading
    server = ns['ThreadedHTTPServer'](('127.0.0.1', 0), ns['StreamingHandler'])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def streamer(svc_mod, tmp_path):
    """The generated streamer's HTTP server, running in this process.

    The script is rendered as the box renders it, then run up to the line
    that starts its server, with a stand-in cv2 and v4l2-ctl calls patched
    out. Its own handler is then served on a free local port, so requests
    exercise the real routing rather than a reading of its source.
    """
    import threading

    port = 58087
    ns = _load_streamer(svc_mod, tmp_path, port, records={
        'cam1': {'video_device': '/dev/video0', 'port': port, 'pid': 1,
                 'source': 'cli', 'started_by': 'alice'},
        'cam2': {'video_device': '/dev/video2', 'port': port + 1, 'pid': 2},
    })

    server = ns['ThreadedHTTPServer'](('127.0.0.1', 0), ns['StreamingHandler'])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield ns, f'http://127.0.0.1:{server.server_address[1]}'
    finally:
        server.shutdown()
        server.server_close()


def _get(url):
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.headers.get('Content-Type'), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers.get('Content-Type'), err.read()


def test_viewer_page_is_served_from_the_static_files(streamer):
    _ns, base = streamer
    status, ctype, body = _get(base + '/')
    assert status == 200 and ctype.startswith('text/html')
    page = (Path(__file__).resolve().parents[3] / 'box' / 'lager' / 'static'
            / 'webcam' / 'index.html').read_bytes()
    assert body == page
    for name, kind in (('webcam.css', 'text/css'), ('webcam.js', 'text/javascript')):
        status, ctype, body = _get(f'{base}/static/{name}')
        assert status == 200, name
        assert ctype.startswith(kind), ctype
        assert body


@pytest.mark.parametrize('path', [
    '/static/../service.py',
    '/static/..%2Fservice.py',
    '/static/index.html/../../etc/passwd',
    '/static/missing.js',
    '/static/',
])
def test_only_the_named_static_files_are_served(streamer, path):
    _ns, base = streamer
    status, _ctype, _body = _get(base + path)
    assert status == 404


def test_api_info_describes_the_stream(streamer):
    ns, base = streamer
    status, ctype, body = _get(base + '/api/info')
    assert status == 200 and ctype == 'application/json'
    info = json.loads(body)
    assert info['net'] == 'cam1'
    assert info['video_device'] == '/dev/video0'
    assert info['port'] == 58087
    assert info['source'] == 'cli' and info['started_by'] == 'alice'
    # Before the first frame there is no size and no age to report.
    assert info['frame_age'] is None and info['width'] is None

    import time
    ns['frame_width'], ns['frame_height'] = 640, 480
    ns['last_frame_at'] = time.time() - 5
    info = json.loads(_get(base + '/api/info')[2])
    assert (info['width'], info['height']) == (640, 480)
    assert 4.5 < info['frame_age'] < 10


def test_api_info_survives_a_missing_state_file(streamer, tmp_path):
    ns, base = streamer
    ns['STATE_FILE'] = str(tmp_path / 'gone.json')
    info = json.loads(_get(base + '/api/info')[2])
    assert info['net'] == 'cam1'
    assert info['started_by'] is None


def test_api_streams_lists_every_stream_in_the_state_file(streamer):
    _ns, base = streamer
    data = json.loads(_get(base + '/api/streams')[2])
    assert data['current'] == 'cam1'
    assert data['streams'] == [{'name': 'cam1', 'port': 58087},
                               {'name': 'cam2', 'port': 58088}]


def _post(url, body):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b'{}')


def _record(ns):
    """cam1's saved settings, as the streamer wrote them."""
    return json.loads(Path(ns['SETTINGS_FILE']).read_text())['cam1']


def test_pan_and_orientation_are_kept_in_the_settings_file(streamer):
    ns, base = streamer
    status, body = _post(base + '/api/pan/set', {'x': 0.2, 'y': 0.9})
    assert status == 200
    # At 1x there is nothing to pan, so the reported pan is the middle; the
    # asked-for one is kept for when the picture is zoomed.
    assert (body['view']['pan_x'], body['view']['pan_y']) == (0.5, 0.5)
    assert _record(ns)['view']['pan_x'] == 0.2

    status, body = _post(base + '/api/orientation/set', {'rotate': 90, 'flip_h': True, 'flip_v': 'no'})
    assert status == 200
    assert body['view']['rotate'] == 90 and body['view']['flip_h'] is True
    assert body['view']['flip_v'] is False
    info = json.loads(_get(base + '/api/info')[2])
    assert info['view']['rotate'] == 90


@pytest.mark.parametrize('path', ['/api/zoom/in', '/api/zoom/out', '/api/zoom/reset', '/api/focus/auto'])
def test_bodiless_endpoints_accept_a_body(streamer, path):
    # Answering before reading a sent body reset the connection.
    _ns, base = streamer
    status, body = _post(base + path, {})
    assert status == 200 and body['success'] is not None


def test_zoom_reset_recentres_the_pan(streamer):
    ns, base = streamer
    _post(base + '/api/pan/set', {'x': 0.1, 'y': 0.1})
    _post(base + '/api/zoom/reset', {})
    assert (_record(ns)['view']['pan_x'], _record(ns)['view']['pan_y']) == (0.5, 0.5)


def test_a_mode_set_before_capture_starts_is_used_at_once(streamer):
    ns, base = streamer
    status, body = _post(base + '/api/mode/set', {'width': 1280, 'height': 720, 'fps': 30})
    assert status == 200 and body['success']
    info = json.loads(_get(base + '/api/info')[2])
    assert info['mode'] == {'width': 1280, 'height': 720, 'fps': 30}
    assert _record(ns)['mode'] == {'width': 1280, 'height': 720, 'fps': 30}


def test_a_mode_the_camera_does_not_list_is_refused(streamer):
    ns, base = streamer
    ns['controls'].list_modes = lambda device: [{'width': 640, 'height': 480, 'fps': [30]}]
    status, body = _post(base + '/api/mode/set', {'width': 3840, 'height': 2160, 'fps': 30})
    assert status == 400
    assert '3840x2160 at 30 fps' in body['error']
    status, _body = _post(base + '/api/mode/set', {'width': 'big'})
    assert status == 400


def test_unknown_controls_are_refused(streamer):
    _ns, base = streamer
    status, _body = _post(base + '/api/controls/set', {'control': 'tilt', 'value': 9})
    assert status == 400
    status, _body = _post(base + '/api/controls/set', {'control': 'gain'})
    assert status == 400


class _FakeCap:
    """A camera that delivers frames only in the sizes it was built for."""

    def __init__(self, mode, works):
        self.mode, self.works, self.released = mode, works, False

    def read(self):
        if not self.works:
            return False, None
        return True, np.zeros((self.mode['height'], self.mode['width'], 3), dtype=np.uint8)

    def release(self):
        self.released = True


def test_switch_mode_moves_to_a_mode_the_camera_streams(streamer):
    ns, _base = streamer
    ns['open_camera'] = lambda mode: _FakeCap(mode, works=True)
    old_cap = _FakeCap({'width': 640, 'height': 480}, True)
    new = {'width': 1280, 'height': 720, 'fps': 30}
    cap, mode = ns['switch_mode'](old_cap, {'width': 640, 'height': 480, 'fps': 30}, new)
    assert mode == new and old_cap.released
    assert ns['get_mode']() == (new, None)
    assert _record(ns)['mode'] == new


def test_switch_mode_falls_back_when_the_camera_refuses(streamer):
    ns, _base = streamer
    old = {'width': 640, 'height': 480, 'fps': 30}
    new = {'width': 3840, 'height': 2160, 'fps': 30}
    ns['open_camera'] = lambda mode: _FakeCap(mode, works=mode == old)
    ns['first_frame'].__defaults__ = (0.2,)
    cap, mode = ns['switch_mode'](_FakeCap(old, True), old, new)
    assert mode == old and cap.mode == old
    current, error = ns['get_mode']()
    assert current == old
    assert error == 'The camera would not stream 3840x2160 at 30 fps, so it stays at 640x480 at 30 fps.'


def test_switch_mode_falls_back_when_the_camera_picks_another_size(streamer):
    ns, _base = streamer
    old = {'width': 640, 'height': 480, 'fps': 30}
    # Asked for 1920x1080, the camera quietly delivers 640x480.
    ns['open_camera'] = lambda mode: _FakeCap(old, works=True)
    _cap, mode = ns['switch_mode'](_FakeCap(old, True), old, {'width': 1920, 'height': 1080, 'fps': 30})
    assert mode == old
    assert ns['get_mode']()[1].startswith('The camera would not stream 1920x1080')


def test_transform_rotates_then_crops_to_the_pan(streamer):
    ns, base = streamer
    seen = []

    def resize(img, size, interpolation=None):
        seen.append((img.shape[:2], size))
        return np.zeros((size[1], size[0], 3), dtype=np.uint8)
    ns['cv2'].resize = resize
    ns['cv2'].INTER_LINEAR = 1
    # Stand-ins for the OpenCV calls orient makes, done the numpy way.
    ns['cv2'].ROTATE_90_CLOCKWISE, ns['cv2'].ROTATE_180, ns['cv2'].ROTATE_90_COUNTERCLOCKWISE = 1, 2, 3
    ns['cv2'].rotate = lambda img, code: np.ascontiguousarray(np.rot90(img, -code))
    ns['cv2'].flip = lambda img, code: np.ascontiguousarray(
        img[:, ::-1] if code == 1 else img[::-1] if code == 0 else img[::-1, ::-1])

    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    assert ns['transform'](frame) is frame          # nothing to do
    _post(base + '/api/orientation/set', {'rotate': 90})
    assert ns['transform'](frame).shape == (640, 480, 3)
    _post(base + '/api/zoom/set', {'zoom': 2.0})
    out = ns['transform'](frame)
    # The turned 480x640 frame, cropped to half each way, scaled back up.
    assert seen == [((320, 240), (480, 640))]
    assert out.shape == (640, 480, 3)


# ---------- what a stream start does to the camera ----------

def test_a_stream_start_writes_no_control_nobody_set(svc_mod, tmp_path):
    # Settings made outside lager (a fixed focus, a locked brightness) must
    # survive a stream start: nothing saved means nothing written.
    v4l2 = FakeV4l2()
    _load_streamer(svc_mod, tmp_path, 58090, v4l2=v4l2)
    assert v4l2.sets == []


def test_an_older_records_focus_and_brightness_defaults_are_not_applied(svc_mod, tmp_path):
    # An older box wrote focus_mode "auto" and brightness 128 into every new
    # record, wanted or not. They were defaults, never a user's choice.
    v4l2 = FakeV4l2()
    _load_streamer(svc_mod, tmp_path, 58091, v4l2=v4l2, records={
        'cam1': {'video_device': '/dev/video0', 'port': 58091, 'pid': 1, 'zoom': 1.0,
                 'focus_mode': 'auto', 'focus_value': 0, 'brightness': 128}})
    assert v4l2.sets == []


def test_saved_controls_are_put_back_at_start_switches_first(svc_mod, tmp_path):
    v4l2 = FakeV4l2()
    _load_streamer(svc_mod, tmp_path, 58092, v4l2=v4l2, settings={
        'cam1': {'controls': {'focus': 383, 'focus_auto': False, 'brightness': 0}}})
    assert v4l2.sets == ['focus_automatic_continuous=0', 'focus_absolute=383', 'brightness=0']


def test_settings_an_older_box_kept_in_the_record_are_still_read(svc_mod, tmp_path):
    ns = _load_streamer(svc_mod, tmp_path, 58093, records={
        'cam1': {'video_device': '/dev/video0', 'port': 58093, 'pid': 1, 'zoom': 2.5,
                 'view': {'rotate': 180}}})
    assert ns['get_zoom']() == 2.5
    assert ns['get_view']()['rotate'] == 180


@pytest.fixture
def camera_streamer(svc_mod, tmp_path):
    """The streamer serving a camera with CAMERA_CTRLS' ranges."""
    v4l2 = FakeV4l2()
    ns = _load_streamer(svc_mod, tmp_path, 58094, v4l2=v4l2)
    server = _serve(ns)
    try:
        yield ns, f'http://127.0.0.1:{server.server_address[1]}', v4l2
    finally:
        server.shutdown()
        server.server_close()


def test_focus_reports_and_takes_the_cameras_own_range(camera_streamer):
    ns, base, v4l2 = camera_streamer
    focus = json.loads(_get(base + '/api/focus')[2])
    assert focus == {'mode': 'manual', 'value': 383, 'min': 0, 'max': 1023, 'step': 1}
    status, body = _post(base + '/api/focus/manual', {'value': 383})
    assert status == 200 and body['success'] is True
    # Beyond the 0..255 an older box clamped every camera to.
    assert v4l2.sets == ['focus_automatic_continuous=0', 'focus_absolute=383']
    assert _record(ns)['controls'] == {'focus_auto': False, 'focus': 383}


def test_manual_focus_without_a_value_keeps_the_distance(camera_streamer):
    _ns, base, v4l2 = camera_streamer
    status, body = _post(base + '/api/focus/manual', {})
    assert status == 200 and body['success'] is True
    assert v4l2.sets == ['focus_automatic_continuous=0']
    status, _body = _post(base + '/api/focus/manual', {'value': 'far'})
    assert status == 400


def test_autofocus_is_saved_as_a_switch(camera_streamer):
    ns, base, v4l2 = camera_streamer
    _post(base + '/api/focus/auto', {})
    assert v4l2.sets == ['focus_automatic_continuous=1']
    assert _record(ns)['controls'] == {'focus_auto': True}


def test_brightness_is_clamped_to_the_cameras_range(camera_streamer):
    ns, base, v4l2 = camera_streamer
    brightness = json.loads(_get(base + '/api/brightness')[2])
    assert brightness == {'value': 0, 'min': -64, 'max': 64, 'step': 1}
    status, body = _post(base + '/api/brightness/set', {'value': 128})
    assert status == 200 and body['success'] is True
    assert v4l2.sets == ['brightness=64']
    # Saved as the camera holds it, not as asked for.
    assert _record(ns)['controls'] == {'brightness': 64}
    assert _post(base + '/api/brightness/set', {})[1]['success'] is False


def test_a_clamped_control_is_saved_as_the_camera_holds_it(camera_streamer):
    ns, base, v4l2 = camera_streamer
    status, body = _post(base + '/api/controls/set', {'control': 'brightness', 'value': -100})
    assert status == 200 and body['success'] is True
    assert v4l2.sets == ['brightness=-64']
    assert _record(ns)['controls'] == {'brightness': -64}


def test_every_named_control_can_be_set_and_is_saved(camera_streamer):
    ns, base, v4l2 = camera_streamer
    status, body = _post(base + '/api/controls/set',
                         {'control': 'white_balance_temperature', 'value': 5000})
    assert status == 200 and body['success'] is True
    assert body['controls']['white_balance_temperature']['auto'] is False
    _post(base + '/api/controls/set', {'control': 'white_balance_auto', 'value': 1})
    assert v4l2.sets == ['white_balance_temperature=5000', 'white_balance_automatic=1']
    assert _record(ns)['controls'] == {'white_balance_temperature': 5000,
                                       'white_balance_auto': True}


def test_raw_snapshot_is_the_frame_before_the_view(camera_streamer):
    ns, base, _v4l2 = camera_streamer
    raw = np.full((4, 6, 3), 7, dtype=np.uint8)
    encoded = []

    def imencode(ext, frame, params):
        encoded.append((ext, frame, params))
        return True, np.frombuffer(b'RAWJPEG', dtype=np.uint8)
    ns['cv2'].imencode = imencode
    ns['cv2'].IMWRITE_JPEG_QUALITY = 1
    ns['capture_started'] = True       # no capture thread: the frames are set here
    ns['latest_jpeg'], ns['latest_raw'] = b'VIEWJPEG', raw

    assert _get(base + '/snapshot')[2] == b'VIEWJPEG'
    assert encoded == []               # the view's JPEG is already made
    status, ctype, body = _get(base + '/snapshot?raw=1')
    assert (status, ctype, body) == (200, 'image/jpeg', b'RAWJPEG')
    assert encoded[0][1] is raw


def test_camera_model_follows_a_by_id_link(svc_mod, tmp_path):
    ns = _load_streamer(svc_mod, tmp_path, 58095)
    node = tmp_path / 'video8'
    node.touch()
    link = tmp_path / 'usb-Global_Shutter_Camera-video-index0'
    link.symlink_to(node)
    ns['VIDEO_DEVICE'] = str(link)
    opened = []
    real_open = open

    def fake_open(path, *args, **kwargs):
        opened.append(path)
        return real_open(os.devnull, *args, **kwargs)
    ns['open'] = fake_open
    ns['camera_model']()
    assert opened == ['/sys/class/video4linux/video8/name']


# ---------- settings outlive the stream ----------

def test_settings_survive_a_stop_and_follow_a_rename(svc_mod, tmp_path):
    state = svc_mod.WebcamStreamState(state_file=tmp_path / 'streams.json')
    assert state.settings_file == tmp_path / 'webcam_settings.json'
    state.add_stream('cam1', '/dev/video8', 8086, 4242)
    state.save_controls('cam1', {'focus_auto': False, 'focus': 383})
    state.save_controls('cam1', {'gain': 0})
    state.remove_stream('cam1')
    assert state.get_stream('cam1') is None
    assert state.get_settings('cam1')['controls'] == {'focus_auto': False, 'focus': 383, 'gain': 0}
    # A stopped net's settings move with it too, though there is no stream.
    assert state.rename_stream('cam1', 'led_cam') is False
    assert state.get_settings('cam1') == {}
    assert state.get_settings('led_cam')['controls']['focus'] == 383


def test_a_new_stream_record_carries_no_camera_settings(svc_mod, tmp_path):
    state = svc_mod.WebcamStreamState(state_file=tmp_path / 'streams.json')
    state.add_stream('cam1', '/dev/video8', 8086, 4242)
    record = state.get_stream('cam1')
    assert not {'focus_mode', 'focus_value', 'brightness', 'zoom'} & set(record)


def test_a_corrupt_settings_file_reads_as_empty(svc_mod, tmp_path):
    state = svc_mod.WebcamStreamState(state_file=tmp_path / 'streams.json')
    state.settings_file.write_text('{not json')
    assert state.get_settings('cam1') == {}
    state.settings_file.write_text('[1, 2]')
    assert state.load_settings() == {}
