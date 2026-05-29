# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for PythonExecutor.execute() on the binary (`lager rust`) path:
direct execution of an uploaded executable, companion --add-file staging, and the
detached-process registry. subprocess/streaming primitives are patched so no real
process is spawned.
"""

import io
import os
import shutil
import stat
import sys
import types
import uuid
from unittest.mock import MagicMock, patch


def _make_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


def _stub(dotted: str) -> None:
    parts = dotted.split('.')
    for i in range(1, len(parts) + 1):
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)


for _dep in [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py', 'usb', 'usb.util', 'usb.core',
    'pigpio', 'labjack', 'labjack.ljm', 'nidaqmx', 'phidget22', 'phidget22.Phidget',
    'phidget22.Net', 'bleak', 'picoscope', 'serial', 'serial.tools',
    'serial.tools.list_ports', 'spidev', 'smbus', 'smbus2', 'RPi', 'RPi.GPIO', 'gpiod',
]:
    _stub(_dep)
sys.modules['simplejson'] = sys.modules['json']  # type: ignore[assignment]
for _m in [m for m in sys.modules if m == 'lager' or m.startswith('lager.')]:
    del sys.modules[_m]
_BOX_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box'))
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

import lager.python.executor as exmod  # noqa: E402
from lager.python.executor import PythonExecutor  # noqa: E402


def _fake_proc():
    proc = MagicMock()
    proc.pid = 4321
    proc.stdout = MagicMock()
    proc.stdout.fileno.return_value = 7
    proc.stderr = None
    return proc


def _run_execute(**kwargs):
    """Run execute() with subprocess + streaming primitives patched out.
    Returns (result, popen_mock)."""
    proc = _fake_proc()
    popen = MagicMock(return_value=proc)
    with patch.object(exmod.subprocess, 'Popen', popen), \
         patch.object(exmod, 'stream_process_output', lambda p, oc, fns: 'STREAM'), \
         patch.object(exmod, 'stream_process_output_to_file', lambda *a, **k: None), \
         patch.object(exmod, '_boost_process_priority', lambda pid: None), \
         patch.object(exmod, 'set_pipe_size', lambda fd: None), \
         patch.object(exmod, 'make_output_channel', lambda fns: MagicMock()):
        result = PythonExecutor().execute(**kwargs)
    return result, popen


def test_binary_written_chmod_and_command():
    result, popen = _run_execute(
        binary_file=io.BytesIO(b'\x7fELFexecutable-bytes'),
        env_vars=['LAGER_PROCESS_ID=' + str(uuid.uuid4())],
        timeout=5,
    )
    assert result == 'STREAM'

    _args, kwargs = popen.call_args
    base_command = _args[0]
    cwd = kwargs['cwd']
    # Non-detached runs are wrapped with /usr/bin/timeout <secs> <binary>
    assert base_command[0] == '/usr/bin/timeout'
    binary_path = base_command[-1]
    assert os.path.basename(binary_path) == 'program'
    assert os.path.dirname(binary_path) == cwd          # cwd == binary dir
    assert os.path.isfile(binary_path)
    mode = stat.S_IMODE(os.stat(binary_path).st_mode)
    assert mode & 0o111                                  # executable bit set
    shutil.rmtree(cwd, ignore_errors=True)


def test_args_appended_to_binary_command():
    _result, popen = _run_execute(
        binary_file=io.BytesIO(b'\x7fELF'),
        args=[b'--foo', b'bar'],
        env_vars=['LAGER_PROCESS_ID=' + str(uuid.uuid4())],
        timeout=5,
    )
    base_command = popen.call_args[0][0]
    assert base_command[-2:] == ['--foo', 'bar']
    shutil.rmtree(popen.call_args[1]['cwd'], ignore_errors=True)


def test_add_files_staged_alongside_binary():
    _result, popen = _run_execute(
        binary_file=io.BytesIO(b'\x7fELF'),
        add_files=[('data.csv', b'1,2,3'), ('cfg.txt', b'hello')],
        env_vars=['LAGER_PROCESS_ID=' + str(uuid.uuid4())],
        timeout=5,
    )
    cwd = popen.call_args[1]['cwd']
    with open(os.path.join(cwd, 'data.csv'), 'rb') as f:
        assert f.read() == b'1,2,3'
    with open(os.path.join(cwd, 'cfg.txt'), 'rb') as f:
        assert f.read() == b'hello'
    shutil.rmtree(cwd, ignore_errors=True)


def test_detach_creates_process_registry():
    proc_id = str(uuid.uuid4())
    proc_dir = f'/tmp/lager_processes/{proc_id}'
    try:
        with patch.object(exmod.threading, 'Thread') as thread_cls:
            thread_cls.return_value = MagicMock()
            result, popen = _run_execute(
                binary_file=io.BytesIO(b'\x7fELF'),
                env_vars=[f'LAGER_PROCESS_ID={proc_id}'],
                detach=True,
                timeout=5,
            )
        assert result['status'] == 'detached'
        assert result['lager_process_id'] == proc_id
        assert os.path.isdir(proc_dir)
        assert os.path.isfile(os.path.join(proc_dir, 'meta.json'))
        # Detached runs are NOT timeout-wrapped (the process owns its lifetime).
        assert popen.call_args[0][0][0] != '/usr/bin/timeout'
        shutil.rmtree(popen.call_args[1]['cwd'], ignore_errors=True)
    finally:
        shutil.rmtree(proc_dir, ignore_errors=True)


def test_detach_does_not_register_dir_cleanup():
    proc_id = str(uuid.uuid4())
    proc_dir = f'/tmp/lager_processes/{proc_id}'
    recorded = []
    try:
        real_add = exmod.add_cleanup_fn

        def _record(cleanup_fns, fn, *a, **k):
            recorded.append(fn)
            return real_add(cleanup_fns, fn, *a, **k)

        with patch.object(exmod.threading, 'Thread', return_value=MagicMock()), \
             patch.object(exmod, 'add_cleanup_fn', _record):
            _run_execute(
                binary_file=io.BytesIO(b'\x7fELF'),
                env_vars=[f'LAGER_PROCESS_ID={proc_id}'],
                detach=True,
                timeout=5,
            )
        # The binary temp dir must NOT be scheduled for removal while the
        # detached process is still using it.
        assert shutil.rmtree not in recorded
    finally:
        shutil.rmtree(proc_dir, ignore_errors=True)


def test_non_detach_registers_dir_cleanup():
    recorded = []
    real_add = exmod.add_cleanup_fn

    def _record(cleanup_fns, fn, *a, **k):
        recorded.append(fn)
        return real_add(cleanup_fns, fn, *a, **k)

    with patch.object(exmod, 'add_cleanup_fn', _record):
        _result, popen = _run_execute(
            binary_file=io.BytesIO(b'\x7fELF'),
            env_vars=['LAGER_PROCESS_ID=' + str(uuid.uuid4())],
            timeout=5,
        )
    assert shutil.rmtree in recorded
    shutil.rmtree(popen.call_args[1]['cwd'], ignore_errors=True)
