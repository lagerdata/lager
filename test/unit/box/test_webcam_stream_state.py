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


@pytest.fixture
def streamer(svc_mod, tmp_path):
    """The generated streamer's HTTP server, running in this process.

    The script is rendered as the box renders it, then run up to the line
    that starts its server, with a stand-in cv2 and v4l2-ctl calls patched
    out. Its own handler is then served on a free local port, so requests
    exercise the real routing rather than a reading of its source.
    """
    import sys
    import threading
    import types

    port = 58087
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

    state_file = tmp_path / 'streams.json'
    state_file.write_text(json.dumps({
        'cam1': {'video_device': '/dev/video0', 'port': port, 'pid': 1,
                 'source': 'cli', 'started_by': 'alice'},
        'cam2': {'video_device': '/dev/video2', 'port': port + 1, 'pid': 2},
    }))
    ns = {'__name__': 'webcam_streamer_under_test'}
    with patch.dict(sys.modules, {'cv2': types.ModuleType('cv2')}), \
            patch('subprocess.run', return_value=completed):
        exec(compile(body, script_path, 'exec'), ns)
    ns['STATE_FILE'] = str(state_file)

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
