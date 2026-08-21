# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the detached launch path on box/lager/python/service.py.

The defect these pin: `lager python <module> --box <box> -d` did not return.
Every step before the process was spawned ran inside the HTTP request --
unpacking the module, `pip install -r requirements.txt` with no bound on it,
the quiesce gate -- so the client sat on its 320s read timeout waiting for
work it had explicitly asked not to wait for.

The contract now is an ordering one, and the first test is the whole point of
this file: the response is written BEFORE the slow work, not merely before the
job finishes. It is asserted by blocking inside the prep and checking that the
response has already been written, so it fails the moment anyone moves that
work back onto the request thread.

Everything downstream follows from that ordering. A job registered before it
starts needs a log file a racing reattach can open, needs a status that says
"registered but not started", and needs somewhere to report a failure that
happens after its client has been answered.
"""

import io
import os
import sys
import json
import time
import types
import shutil
import threading

import pytest
import requests
from unittest.mock import MagicMock


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
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core', 'pigpio',
    'labjack', 'labjack.ljm', 'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak', 'picoscope',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev', 'smbus', 'smbus2', 'RPi', 'RPi.GPIO', 'gpiod',
]:
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager import lock_state  # noqa: E402
from lager.exec import process as exec_process  # noqa: E402
from lager.python import executor as executor_mod  # noqa: E402
from lager.python.exceptions import PipInstallError  # noqa: E402
from lager.python.service import PythonServiceHandler  # noqa: E402

PID = '33333333-3333-3333-3333-333333333333'


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

def encode(fields):
    """Build a real multipart body with requests' own encoder.

    Same helper as test_python_service_multipart.py, for the same reason: the
    framing under test is the real client's, not a guess at it.
    """
    files = []
    for name, value in fields:
        if isinstance(value, tuple):
            filename, data = value
            files.append((name, (filename, data, 'application/octet-stream')))
        else:
            files.append((name, (None, value)))
    body, content_type = requests.models.RequestEncodingMixin._encode_files(files, {})
    return body, content_type


class FakeWfile:
    """Collects everything the handler writes, and when."""

    def __init__(self):
        self.chunks = []
        self.first_write_at = None

    def write(self, data):
        if self.first_write_at is None:
            self.first_write_at = time.monotonic()
        self.chunks.append(data)
        return len(data)

    def flush(self):
        pass

    @property
    def payload(self):
        return b''.join(self.chunks)


def make_handler(fields):
    """A handler primed with a real multipart body, ready for a POST."""
    body, ctype = encode(fields)
    handler = PythonServiceHandler.__new__(PythonServiceHandler)
    handler.headers = {'Content-Type': ctype, 'Content-Length': str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.wfile = FakeWfile()
    handler.client_address = ('10.0.0.9', 51000)
    handler.responses = {}
    # send_json_response is exercised on its own in test_python_service_framing;
    # here we only care about the body and when it was written.
    handler.send_json_response = lambda code, data: (
        handler.responses.update({'code': code, 'data': data}),
        handler.wfile.write(json.dumps(data).encode()),
    )
    handler.send_error_response = lambda code, msg: (
        handler.responses.update({'code': code, 'error': msg}),
    )
    return handler


@pytest.fixture(autouse=True)
def registry(tmp_path, monkeypatch):
    """Point the job registry at a tmp dir, and never touch the real lock."""
    root = tmp_path / 'lager_processes'
    monkeypatch.setattr(executor_mod, 'PROCESS_REGISTRY_DIR', str(root))
    monkeypatch.setattr(lock_state, 'heartbeat', lambda user: (200, {}))
    monkeypatch.setattr(lock_state, 'release', lambda user, force=False: (200, {}))
    yield root
    shutil.rmtree(root, ignore_errors=True)


def job_files(registry, process_id):
    d = os.path.join(str(registry), process_id)
    return os.path.join(d, 'output.log'), os.path.join(d, 'meta.json')


def read_meta(registry, process_id):
    _log, meta = job_files(registry, process_id)
    with open(meta) as f:
        return json.load(f)


def wait_for(predicate, timeout=5.0):
    """Poll until the background thread has got where we need it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


DETACH_FIELDS = [
    ('detach', b'1'),
    ('timeout', b'0'),
    ('env', f'LAGER_PROCESS_ID={PID}'.encode()),
    ('script', ('s.py', b'print("hi")')),
]


def prep_that_blocks(events, release):
    """A _prepare_and_spawn double that stalls where pip would.

    Records when it starts and finishes so a test can assert the response was
    written while it was still stalled -- which is the whole contract.
    """
    def _prepare_and_spawn(self, **kwargs):
        events.append('prep-started')
        release.wait(10)
        events.append('prep-finished')
        raise RuntimeError('the test stops the job here')
    return _prepare_and_spawn


def prep_that_raises(exc):
    """A _prepare_and_spawn double for a job that fails to start."""
    def _prepare_and_spawn(self, **kwargs):
        raise exc
    return _prepare_and_spawn


# --------------------------------------------------------------------------
# The ordering that #309 is about
# --------------------------------------------------------------------------

class TestTheResponsePrecedesTheWork:

    def test_response_is_written_before_the_slow_setup_finishes(
            self, registry, monkeypatch):
        """The pin. Blocking INSIDE prep, not merely before Popen.

        A test that only checked "the handler returned" would still pass if the
        pip install moved back into the request, as long as it eventually
        finished. Holding prep open and requiring the response to already be on
        the wire is what makes this a regression test.
        """
        events = []
        release = threading.Event()
        monkeypatch.setattr(
            executor_mod.PythonExecutor, '_prepare_and_spawn',
            prep_that_blocks(events, release))

        handler = make_handler(DETACH_FIELDS)
        handler._handle_python_execute()

        # Answered -- while the setup is still stalled where pip would be.
        assert handler.responses['code'] == 200
        assert handler.responses['data']['status'] == 'detached'
        assert handler.responses['data']['lager_process_id'] == PID
        assert wait_for(lambda: 'prep-started' in events)
        assert 'prep-finished' not in events

        release.set()

    def test_the_launch_does_not_wait_for_a_pip_install(self, registry, monkeypatch):
        """Timing, stated as the user experiences it."""
        release = threading.Event()
        monkeypatch.setattr(
            executor_mod.PythonExecutor, '_prepare_and_spawn',
            prep_that_blocks([], release))

        handler = make_handler(DETACH_FIELDS)
        started = time.monotonic()
        handler._handle_python_execute()
        elapsed = time.monotonic() - started

        assert elapsed < 2.0, f'detached launch blocked for {elapsed:.1f}s'
        release.set()

    def test_pid_is_null_because_no_process_exists_yet(self, registry, monkeypatch):
        """Null, not absent: dropping the key would KeyError a consumer."""
        monkeypatch.setattr(
            executor_mod.PythonExecutor, '_prepare_and_spawn',
            prep_that_raises(RuntimeError('never starts')))
        handler = make_handler(DETACH_FIELDS)
        handler._handle_python_execute()

        data = handler.responses['data']
        assert 'pid' in data
        assert data['pid'] is None


# --------------------------------------------------------------------------
# The registry a racing reattach depends on
# --------------------------------------------------------------------------

class TestTheJobIsRegisteredBeforeItStarts:

    def test_log_and_meta_exist_before_the_response(self, registry, monkeypatch):
        """A reattach racing the launch must find a file it can open.

        stream_log_file opens output.log directly, so without this a reattach
        issued straight after the launch would hit FileNotFoundError and be
        answered with a 500 for a job that is perfectly healthy.
        """
        release = threading.Event()
        monkeypatch.setattr(
            executor_mod.PythonExecutor, '_prepare_and_spawn',
            prep_that_blocks([], release))

        handler = make_handler(DETACH_FIELDS)
        handler._handle_python_execute()

        log_path, meta_path = job_files(registry, PID)
        assert os.path.exists(log_path), 'output.log must exist before the response'
        assert os.path.exists(meta_path)

        with open(log_path, 'rb') as f:      # the call stream_log_file makes
            assert f.read() == b''

        meta = read_meta(registry, PID)
        assert meta['status'] == exec_process.STATUS_STARTING
        assert meta['pid'] is None
        assert meta['returncode'] is None
        assert meta['lager_process_id'] == PID

        release.set()

    def test_reattach_tails_a_starting_job_then_stops_when_it_fails(self, registry):
        """stream_log_file must treat 'failed' as terminal, not just 'finished'.

        Without that, a job that never started would be tailed forever: the
        reader's stop condition would never be met and the client would hang on
        a job that is already over.
        """
        os.makedirs(os.path.join(str(registry), PID), exist_ok=True)
        log_path, meta_path = job_files(registry, PID)
        with open(log_path, 'wb'):
            pass
        exec_process.write_meta(meta_path, {
            'status': exec_process.STATUS_STARTING, 'returncode': None})

        gen = exec_process.stream_log_file(log_path, meta_path)

        with open(log_path, 'ab') as f:
            for part in exec_process.emit(1, b'working'):
                f.write(part)
        assert b'working' in next(gen)

        exec_process.append_failure(log_path, b'pip died', returncode=1)
        exec_process.update_meta(
            meta_path, status=exec_process.STATUS_FAILED, returncode=1)

        rest = b''.join(gen)          # terminates because 'failed' is terminal
        assert b'pip died' in rest
        assert rest.endswith(b'- 1 1')


# --------------------------------------------------------------------------
# Failure reporting, now that the client has already gone
# --------------------------------------------------------------------------

class TestAFailedStartReportsThroughTheJob:

    def _fail_with(self, monkeypatch, exc):
        monkeypatch.setattr(
            executor_mod.PythonExecutor, '_prepare_and_spawn', prep_that_raises(exc))

    def test_pip_failure_lands_in_the_log_and_meta(self, registry, monkeypatch):
        pip_output = b'ERROR: Could not find a version that satisfies the requirement nope==9.9.9'
        self._fail_with(monkeypatch, PipInstallError(pip_output))

        handler = make_handler(DETACH_FIELDS)
        handler._handle_python_execute()

        # Still answered 200: the box accepted the job, and only then failed it.
        assert handler.responses['code'] == 200

        assert wait_for(
            lambda: read_meta(registry, PID)['status'] == exec_process.STATUS_FAILED)
        meta = read_meta(registry, PID)
        assert meta['returncode'] == executor_mod.START_FAILURE_EXIT_CODE

        log_path, _ = job_files(registry, PID)
        with open(log_path, 'rb') as f:
            log = f.read()
        assert log.startswith(b'2 ')             # stderr, in the wire format
        assert b'failed to start' in log
        assert b'nope==9.9.9' in log
        assert log.endswith(b'- 1 1')            # the exit marker a reader stops on

    def test_a_corrupt_module_zip_reports_the_same_way(self, registry, monkeypatch):
        import zipfile
        self._fail_with(monkeypatch, zipfile.BadZipFile('File is not a zip file'))

        handler = make_handler(DETACH_FIELDS)
        handler._handle_python_execute()

        assert wait_for(
            lambda: read_meta(registry, PID)['status'] == exec_process.STATUS_FAILED)
        log_path, _ = job_files(registry, PID)
        with open(log_path, 'rb') as f:
            assert b'not a zip file' in f.read()

    def test_nothing_can_leave_a_job_at_starting(self, registry, monkeypatch):
        """The backstop, exercised through a failure with no message of its own."""
        self._fail_with(monkeypatch, OSError('boom'))

        handler = make_handler(DETACH_FIELDS)
        handler._handle_python_execute()

        assert wait_for(
            lambda: read_meta(registry, PID)['status'] in exec_process.TERMINAL_STATUSES)

    def test_finalize_meta_does_not_overwrite_a_real_exit_code(self, registry):
        """The capture loop wins; the backstop is only for when nothing ran."""
        os.makedirs(os.path.join(str(registry), PID), exist_ok=True)
        log_path, meta_path = job_files(registry, PID)
        with open(log_path, 'wb'):
            pass
        exec_process.write_meta(meta_path, {
            'status': exec_process.STATUS_FINISHED, 'returncode': 0})

        assert exec_process.finalize_meta(meta_path, log_path, message=b'late') is False
        meta = read_meta(registry, PID)
        assert meta['status'] == exec_process.STATUS_FINISHED
        assert meta['returncode'] == 0
        with open(log_path, 'rb') as f:
            assert f.read() == b''


# --------------------------------------------------------------------------
# Validation that must stay synchronous
# --------------------------------------------------------------------------

class TestWhatStaysOnTheRequestThread:

    def test_nothing_to_run_is_still_a_synchronous_422(self, registry):
        """do_POST turns MissingModuleFolderError into a 422, as it always has.

        Answering 200 and failing the job a minute later would turn a client
        mistake into something you have to go and look up.
        """
        handler = make_handler([('detach', b'1'), ('timeout', b'0')])

        with pytest.raises(executor_mod.MissingModuleFolderError):
            handler._handle_python_execute()

        assert not os.path.exists(os.path.join(str(registry), PID))

    def test_an_empty_script_part_still_counts_as_nothing_to_run(self, registry):
        handler = make_handler([('detach', b'1'), ('script', b'')])
        with pytest.raises(executor_mod.MissingModuleFolderError):
            handler._handle_python_execute()


# --------------------------------------------------------------------------
# The id, which is how a job is found
# --------------------------------------------------------------------------

class TestTheProcessId:

    def test_a_missing_id_is_minted_and_injected_into_the_child_env(self):
        """Minting is not just so the directory has a name.

        A job is found by reading LAGER_PROCESS_ID out of /proc/*/environ, so
        an id the child does not carry is an id `--kill` can never resolve.
        """
        process_id, env_vars = executor_mod.resolve_lager_process_id([])

        import uuid
        uuid.UUID(process_id)                      # parses, so --kill accepts it
        assert f'LAGER_PROCESS_ID={process_id}' in env_vars

    def test_a_supplied_id_passes_through_without_duplication(self):
        process_id, env_vars = executor_mod.resolve_lager_process_id(
            [f'LAGER_PROCESS_ID={PID}', 'FOO=bar'])

        assert process_id == PID
        assert env_vars.count(f'LAGER_PROCESS_ID={PID}') == 1
        assert 'FOO=bar' in env_vars

    def test_an_empty_id_is_replaced_rather_than_used(self):
        process_id, env_vars = executor_mod.resolve_lager_process_id(
            ['LAGER_PROCESS_ID='])
        assert process_id
        assert f'LAGER_PROCESS_ID={process_id}' in env_vars

    def test_no_job_is_ever_registered_under_none(self, registry, monkeypatch):
        """The literal /tmp/lager_processes/None directory this used to create."""
        monkeypatch.setattr(
            executor_mod.PythonExecutor, '_prepare_and_spawn',
            prep_that_raises(RuntimeError('never starts')))
        handler = make_handler([
            ('detach', b'1'), ('timeout', b'0'), ('script', ('s.py', b'print(1)')),
        ])
        handler._handle_python_execute()

        assert not os.path.exists(os.path.join(str(registry), 'None'))
        minted = handler.responses['data']['lager_process_id']
        assert minted
        assert os.path.isdir(os.path.join(str(registry), minted))
