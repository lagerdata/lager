# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The client's half of the run record (cli/run_record.py) and its wiring.

What the client asserts is unverified, but it still has to be right, because
a consumer uses it to map the box's hashes back to a repository: a wrong
repoPath makes an honest run fail its integrity check. And the client is the
party that tells the user when what they received does not match what the box
recorded, which is only worth anything if the comparison is exact.

The `lager python` tests drive run_python_internal against a fake session, so
they cover the real control flow (stream, exit, download, record) without a
box.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import zipfile

import click
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..', '..'))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from cli import run_record as crr  # noqa: E402
from cli.core.utils import zip_dir  # noqa: E402

import cli.commands.development.python  # noqa: E402,F401
cli_python = sys.modules['cli.commands.development.python']


def sha(data):
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------- helpers ---

class TestRedactArgv:
    def test_env_values_are_redacted_and_names_kept(self):
        argv = ['/home/me/.venv/bin/lager', 'python', 't.py', '--env', 'TOKEN=abc',
                '--env=KEY=v=w', '--passenv', 'HOME', '--label', 'execution=1']
        assert crr.redact_argv(argv) == [
            'lager', 'python', 't.py', '--env', 'TOKEN=<redacted>',
            '--env=KEY=<redacted>', '--passenv', 'HOME', '--label', 'execution=1']


class TestLabels:
    def test_parsed(self):
        assert crr.parse_labels(['execution=EX-1', 'suite=rtd/v2', 'empty=']) == {
            'execution': 'EX-1', 'suite': 'rtd/v2', 'empty': ''}

    @pytest.mark.parametrize('bad', ['noequals', '=v', 'bad key=v', '-x=v'])
    def test_rejected(self, bad):
        with pytest.raises(click.BadParameter):
            crr.parse_labels([bad])

    def test_capped(self):
        with pytest.raises(click.BadParameter):
            crr.parse_labels([f'k{i}=v' for i in range(crr.MAX_LABELS + 1)])


class TestLogHasher:
    def test_frames_hash_as_the_box_writes_them(self):
        hasher = crr.LogHasher()
        hasher(1, b'hello\n')
        hasher(2, b'')            # empty frames are not part of the log
        hasher(3, b'1 2 {}')
        expected = b'1 6 hello\n' + b'3 6 1 2 {}'
        assert hasher.sha256 == sha(expected)
        assert hasher.size == len(expected)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'firmware-mono'
    (root / 'xl' / 'tests' / 'data').mkdir(parents=True)
    (root / 'xl' / 'tests' / 'rtd.py').write_bytes(b'print("rtd")\n')
    (root / 'xl' / 'tests' / 'data' / 'calibration.csv').write_bytes(b'1,2\n')
    (root / 'xl' / 'lib').mkdir()
    (root / 'xl' / 'lib' / 'helper.py').write_bytes(b'X = 1\n')
    env = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}
    for cmd in (['init', '-q'], ['add', '.'], ['commit', '-qm', 'init']):
        subprocess.run(['git', *cmd], cwd=root, check=True, env=env)
    return root


class TestGitAndFiles:
    def test_git_context(self, repo):
        git, toplevel = crr.git_context(str(repo / 'xl' / 'tests' / 'rtd.py'))
        assert len(git['commit']) == 40 and git['dirty'] is False
        assert toplevel == os.path.realpath(repo)
        (repo / 'xl' / 'tests' / 'rtd.py').write_bytes(b'changed\n')
        assert crr.git_context(str(repo / 'xl' / 'tests' / 'rtd.py'))[0]['dirty'] is True

    def test_outside_a_repo(self, tmp_path):
        (tmp_path / 'loose.py').write_text('x')
        assert crr.git_context(str(tmp_path / 'loose.py')) == (None, None)

    def test_module_files_map_back_to_the_repo(self, repo, tmp_path):
        """The single-file-plus-extras shape: the runnable is renamed main.py
        and extras land at the zip root, so archive paths alone say nothing
        about where the files live. repoPath has to."""
        stage = tmp_path / 'stage'
        stage.mkdir()
        (stage / 'main.py').write_bytes((repo / 'xl' / 'tests' / 'rtd.py').read_bytes())
        manifest = {}
        zipped = bytes(zip_dir(str(stage), [str(repo / 'xl' / 'tests' / 'data' / 'calibration.csv')],
                               include_dirs={'lib': str(repo / 'xl' / 'lib')}, manifest=manifest))
        manifest['main.py'] = str(repo / 'xl' / 'tests' / 'rtd.py')
        _, toplevel = crr.git_context(str(repo / 'xl' / 'tests' / 'rtd.py'))
        files = crr.client_files(module_bytes=zipped, manifest=manifest, toplevel=toplevel)
        assert [(f['path'], f['repoPath']) for f in files] == [
            ('calibration.csv', 'xl/tests/data/calibration.csv'),
            ('lib/helper.py', 'xl/lib/helper.py'),
            ('main.py', 'xl/tests/rtd.py'),
        ]
        assert files[2]['sha256'] == sha(b'print("rtd")\n')

    def test_client_field_carries_the_repo_root(self, repo):
        runnable = str(repo / 'xl' / 'tests' / 'rtd.py')
        git, toplevel = crr.git_context(runnable)
        field = json.loads(crr.build_client_field(
            runnable=runnable, argv=['lager', 'python'], passenv=['HOME'], downloads=['/tmp/lager-output/a'],
            labels={'execution': '1'}, files=[], git=git, toplevel=toplevel))
        assert field['repoRelativeRoot'] == 'xl/tests'
        assert field['git'] == git
        assert field['passenv'] == ['HOME'] and field['labels'] == {'execution': '1'}


class TestCheck:
    def record(self, log=b'1 2 hi', outputs=()):
        return {'log': {'sha256': sha(log), 'size': len(log)},
                'outputs': [{'name': n, 'sha256': sha(d), 'size': len(d)} for n, d in outputs]}

    def test_agreement(self, tmp_path):
        local = tmp_path / 'out.csv'
        local.write_bytes(b'data')
        hasher = crr.LogHasher()
        hasher(1, b'hi')
        record = self.record(outputs=[('/tmp/lager-output/out.csv', b'data')])
        assert crr.check(record, hasher, {'/tmp/lager-output/out.csv': str(local)}) == []

    def test_incomplete_output_is_reported(self):
        hasher = crr.LogHasher()
        assert 'does not match the log' in crr.check(self.record(), hasher, {})[0]

    def test_changed_download_is_reported(self, tmp_path):
        local = tmp_path / 'out.csv'
        local.write_bytes(b'tampered')
        record = self.record(outputs=[('/tmp/lager-output/out.csv', b'data')])
        hasher = crr.LogHasher()
        hasher(1, b'hi')
        assert 'does not match the hash' in crr.check(
            record, hasher, {'/tmp/lager-output/out.csv': str(local)})[0]


# ------------------------------------------------------------ lager python ---

@pytest.fixture(autouse=True)
def _restore_dispositions():
    saved = {sig: signal.getsignal(sig) for sig in cli_python._STOP_SIGNALS}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


def frames(*parts, exit_code=None):
    out = b''.join(f'{n} {len(d)} '.encode() + d for n, d in parts)
    if exit_code is not None:
        code = str(exit_code).encode()
        out += b'- ' + str(len(code)).encode() + b' ' + code
    return out


class FakeStream:
    status_code = 200
    headers = {'Lager-Output-Version': '1'}

    def __init__(self, body):
        self._body = body

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]


class FakeJSON:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, body, record_status=200):
        self.body = body
        self.record_status = record_status
        self.posted = None
        self.run_id = None

    def run_python(self, box, files, run_id=None):
        self.posted = files
        self.run_id = run_id
        return FakeStream(self.body)

    def kill_python(self, box, lager_process_id, sig=signal.SIGTERM):
        return FakeJSON(200, {})

    def get_run_record(self, box, run_id):
        hasher = crr.LogHasher()
        hasher(1, b'hello\n')
        return FakeJSON(self.record_status, {
            'runId': run_id, 'log': {'sha256': hasher.sha256, 'size': hasher.size},
            'outputs': [], 'exit': {'code': 0, 'reason': 'exited'}})


class FakeObj:
    debug = False

    def __init__(self, session):
        self.session = session

    def get_session_for_box(self, box, box_name=None):
        return self.session


def run(tmp_path, monkeypatch, session, **kwargs):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('LAGER_CONFIG_FILE_DIR', str(tmp_path))
    script = tmp_path / 't.py'
    script.write_text('print("hello")\n')
    ctx = click.Context(click.Command('python'), obj=FakeObj(session))
    defaults = dict(env=[], passenv=[], kill=False, download=(), allow_overwrite=False,
                    signum='SIGTERM', timeout=0, detach=False, port=(), org=None, args=(),
                    extra_files=[], watch_stdin_resume=False, save_run_record=True)
    defaults.update(kwargs)
    with ctx:
        with pytest.raises(SystemExit) as exc:
            cli_python.run_python_internal(ctx, str(script), '10.0.0.5', **{
                k: defaults.pop(k) for k in ('env', 'passenv', 'kill', 'download', 'allow_overwrite',
                                             'signum', 'timeout', 'detach', 'port', 'org', 'args')},
                **defaults)
    return exc.value.code


def test_a_completed_run_saves_its_record_beside_the_downloads(tmp_path, monkeypatch, capsys):
    session = FakeSession(frames((1, b'hello\n'), exit_code=0))
    assert run(tmp_path, monkeypatch, session, labels={'execution': 'EX-1'}) == 0
    saved = tmp_path / f'{session.run_id}{crr.RECORD_SUFFIX}'
    assert json.loads(saved.read_text())['runId'] == session.run_id
    assert 'Run record:' in capsys.readouterr().err

    fields = dict((k, v) for k, v in session.posted if k == crr.CLIENT_FIELD)
    sent = json.loads(fields[crr.CLIENT_FIELD])
    assert sent['labels'] == {'execution': 'EX-1'}
    assert sent['files'][0]['sha256'] == sha(b'print("hello")\n')
    assert ('env', f'LAGER_PROCESS_ID={session.run_id}') in session.posted


def test_a_mismatched_log_is_warned_about(tmp_path, monkeypatch, capsys):
    session = FakeSession(frames((1, b'hello\n'), (1, b'more\n'), exit_code=0))
    run(tmp_path, monkeypatch, session)
    assert 'does not match the log' in capsys.readouterr().err


def test_no_run_record_skips_the_file(tmp_path, monkeypatch):
    session = FakeSession(frames((1, b'hello\n'), exit_code=0))
    run(tmp_path, monkeypatch, session, save_run_record=False)
    assert not list(tmp_path.glob(f'*{crr.RECORD_SUFFIX}'))


def test_a_box_without_records_is_silent(tmp_path, monkeypatch, capsys):
    session = FakeSession(frames((1, b'hello\n'), exit_code=0), record_status=404)
    assert run(tmp_path, monkeypatch, session) == 0
    assert 'Run record' not in capsys.readouterr().err


def test_a_stream_that_ends_without_an_exit_is_not_a_success(tmp_path, monkeypatch):
    """It used to exit 0: nothing after the read loop set a code."""
    session = FakeSession(frames((1, b'hello\n')))
    assert run(tmp_path, monkeypatch, session) != 0


def test_an_unset_passenv_is_a_usage_error_not_a_traceback(tmp_path, monkeypatch):
    monkeypatch.delenv('NOT_SET_ANYWHERE', raising=False)
    monkeypatch.chdir(tmp_path)
    script = tmp_path / 't.py'
    script.write_text('x')
    ctx = click.Context(click.Command('python'), obj=FakeObj(FakeSession(b'')))
    with ctx, pytest.raises(click.UsageError, match='NOT_SET_ANYWHERE'):
        cli_python.run_python_internal(ctx, str(script), '10.0.0.5', [], ['NOT_SET_ANYWHERE'],
                                       False, (), False, 'SIGTERM', 0, False, (), None, ())
