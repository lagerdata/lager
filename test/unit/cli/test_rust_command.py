# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for `lager rust` (cli/commands/development/rust.py).

Covers the ELF pre-flight warning, the multipart body assembled by
run_rust_internal (binary + env/passenv/args/portforwards/timeout), --add-file
staging, detach messaging, download-after-exit, and the kill/reattach dispatch in
the click command. The box is mocked; nothing touches the network.
"""

from __future__ import annotations

import io
import os
import sys
from unittest import mock

import pytest
from click.testing import CliRunner

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
sys.path.insert(0, _REPO_ROOT)

import importlib  # noqa: E402

# The development package binds `rust` to the command, shadowing the submodule;
# load the module itself via sys.modules so we can patch its globals.
rust_mod = importlib.import_module('cli.commands.development.rust')  # noqa: E402
from cli.commands.development.rust import rust, run_rust_internal, _build_rust_runnable  # noqa: E402
from cli.core.param_types import PortForwardSpecifier  # noqa: E402
from cli.core.utils import StreamDatatypes  # noqa: E402


# ── Fakes ────────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = ''

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f'HTTP {self.status_code}')


class _Download:
    def __init__(self, content=b'result-bytes'):
        self.status_code = 200
        self.content = content
        self.text = ''

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Session:
    def __init__(self, resp=None):
        self.resp = resp or _Resp()
        self.run_exec_files = None
        self.kill_calls = []
        self.downloaded = []

    def run_exec(self, box, files):
        self.run_exec_files = files
        return self.resp

    def kill_python(self, box, pid, sig):
        self.kill_calls.append((box, pid, sig))
        return _Resp(200)

    def download_file(self, box, filename):
        self.downloaded.append(filename)
        return _Download()


class _Obj:
    def __init__(self, session):
        self._s = session

    def get_session_for_box(self, box_ip, box_name=None):
        return self._s


class _Ctx:
    def __init__(self, obj):
        self.obj = obj

    def exit(self, code=0):
        raise SystemExit(code)


def _fields_dict(files):
    """Group a multipart files list into {name: [values]} for assertions."""
    out = {}
    for name, value in files:
        out.setdefault(name, []).append(value)
    return out


# ── _build_rust_runnable: ELF pre-flight + fields ────────────────────────────

def test_elf_binary_no_warning(tmp_path):
    binpath = tmp_path / 'prog'
    binpath.write_bytes(b'\x7fELF' + b'\x00' * 16)
    post_data = []
    with mock.patch.object(rust_mod.click, 'secho') as secho:
        _build_rust_runnable(None, str(binpath), post_data, [])
    secho.assert_not_called()
    fields = _fields_dict(post_data)
    assert 'binary' in fields
    assert fields['binary'][0][0] == 'prog'  # (filename, BytesIO, content-type)


def test_non_elf_binary_warns(tmp_path):
    binpath = tmp_path / 'prog.exe'
    binpath.write_bytes(b'MZ' + b'\x00' * 16)
    post_data = []
    with mock.patch.object(rust_mod.click, 'secho') as secho:
        _build_rust_runnable(None, str(binpath), post_data, [])
    assert secho.called
    assert 'not a Linux ELF' in secho.call_args[0][0]


def test_add_file_fields_emitted(tmp_path):
    binpath = tmp_path / 'prog'
    binpath.write_bytes(b'\x7fELF')
    f1 = tmp_path / 'a.csv'
    f1.write_bytes(b'1,2')
    f2 = tmp_path / 'b.txt'
    f2.write_bytes(b'hi')
    post_data = []
    _build_rust_runnable(None, str(binpath), post_data, [str(f1), str(f2)])
    fields = _fields_dict(post_data)
    names = [v[0] for v in fields['add_file']]
    assert names == ['a.csv', 'b.txt']


# ── run_rust_internal: multipart body assembly ───────────────────────────────

def _run(tmp_path, session, **overrides):
    binpath = tmp_path / 'prog'
    binpath.write_bytes(b'\x7fELF' + b'\x00' * 8)
    ctx = _Ctx(_Obj(session))
    kwargs = dict(
        ctx=ctx, runnable=str(binpath), box='10.0.0.5',
        env=[], passenv=[], kill=False, download=[], allow_overwrite=False,
        signum='SIGTERM', timeout=0, detach=False, port=[], org=None, args=[],
        extra_files=[], dut_name='mybox',
    )
    kwargs.update(overrides)
    return run_rust_internal(**kwargs)


def test_post_data_has_binary_env_args_timeout(tmp_path):
    session = _Session()
    with mock.patch.object(rust_mod, 'run_internal', wraps=rust_mod.run_internal), \
         mock.patch('cli.commands.development._runner.stream_python_output', return_value=iter([])):
        _run(tmp_path, session, env=['FOO=bar'], args=('--x', 'y'), timeout=30)

    fields = _fields_dict(session.run_exec_files)
    assert ('1' in fields['detach'] or '0' in fields['detach'])
    assert fields['detach'] == ['0']
    assert fields['args'] == ['--x', 'y']
    assert 'binary' in fields
    assert fields['timeout'] == [30]
    env_joined = '\n'.join(fields['env'])
    assert 'FOO=bar' in env_joined
    assert 'LAGER_PROCESS_ID=' in env_joined
    assert 'LAGER_RUNNABLE=' in env_joined


def test_post_data_passenv_and_portforwards(tmp_path, monkeypatch):
    monkeypatch.setenv('MY_SECRET', 'sssh')
    session = _Session()
    port = [PortForwardSpecifier(src=8080, dst=9000, proto='tcp')]
    with mock.patch('cli.commands.development._runner.stream_python_output', return_value=iter([])):
        _run(tmp_path, session, passenv=['MY_SECRET'], port=port)

    fields = _fields_dict(session.run_exec_files)
    assert any('MY_SECRET=sssh' == e for e in fields['env'])
    assert fields['portforwards'] == ['{"src": 8080, "dst": 9000, "proto": "tcp"}']


def test_add_file_passed_through_internal(tmp_path):
    session = _Session()
    extra = tmp_path / 'data.bin'
    extra.write_bytes(b'xyz')
    with mock.patch('cli.commands.development._runner.stream_python_output', return_value=iter([])):
        _run(tmp_path, session, extra_files=[str(extra)])
    fields = _fields_dict(session.run_exec_files)
    assert [v[0] for v in fields['add_file']] == ['data.bin']


# ── detach / download / streaming ────────────────────────────────────────────

def test_detach_prints_rust_hints_and_returns(tmp_path, capsys):
    session = _Session(_Resp(200, {'lager_process_id': 'PID-9'}))
    with mock.patch('cli.commands.development._runner.stream_python_output') as stream:
        _run(tmp_path, session, detach=True)
        stream.assert_not_called()  # detached: no streaming
    out = capsys.readouterr().out
    assert 'Process detached (Process ID: PID-9)' in out
    assert 'lager rust --reattach PID-9 --box mybox' in out
    assert 'lager rust --kill PID-9 --box mybox' in out


def test_exit_triggers_downloads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    session = _Session()
    with mock.patch('cli.commands.development._runner.stream_python_output',
                    return_value=iter([(StreamDatatypes.EXIT, 0)])):
        with pytest.raises(SystemExit) as exc:
            _run(tmp_path, session, download=['results.csv'])
    assert exc.value.code == 0
    assert session.downloaded == ['results.csv']
    assert (tmp_path / 'results.csv').read_bytes() == b'result-bytes'


# ── click command: kill / reattach dispatch ──────────────────────────────────

def test_command_kill_calls_kill_python():
    session = _Session()
    runner = CliRunner()
    with mock.patch('cli.box_storage.resolve_and_validate_box', return_value='10.0.0.5'):
        result = runner.invoke(rust, ['--kill', 'PID-1', '--box', 'mybox'], obj=_Obj(session))
    assert result.exit_code == 0, result.output
    assert session.kill_calls and session.kill_calls[0][1] == 'PID-1'
    assert 'Process PID-1 killed' in result.output


def test_command_reattach_dispatches(tmp_path):
    session = _Session()
    runner = CliRunner()
    with mock.patch('cli.box_storage.resolve_and_validate_box', return_value='10.0.0.5'), \
         mock.patch.object(rust_mod, 'handle_reattach') as reattach:
        result = runner.invoke(rust, ['--reattach', 'PID-2', '--box', 'mybox'], obj=_Obj(session))
    assert result.exit_code == 0, result.output
    assert reattach.called
    assert reattach.call_args.kwargs.get('cli_name') == 'rust'


def test_command_requires_runnable_or_management_op():
    runner = CliRunner()
    with mock.patch('cli.box_storage.resolve_and_validate_box', return_value='10.0.0.5'):
        result = runner.invoke(rust, ['--box', 'mybox'], obj=_Obj(_Session()))
    assert result.exit_code != 0
    assert 'Please supply a RUNNABLE' in result.output
