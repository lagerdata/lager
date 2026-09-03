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
