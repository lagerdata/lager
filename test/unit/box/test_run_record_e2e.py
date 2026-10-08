# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Run records end to end: a real /python service, real child processes.

The unit tests in test_run_record.py check the recorder in isolation. These
check that the service actually drives it: that the record the box writes is
for the run that happened, that the log hash the CLI computes from the stream
it received equals the one the box recorded, that a downloaded file matches
its recorded hash, and that a cancel and a disconnect are each called what
they are.

They need GNU timeout at the box's path, which the executor wraps every
attached run in; the script interpreter is pointed at this one. Elsewhere they
skip.
"""

import io
import json
import os
import socket
import sys
import threading
import time
import uuid
from http.server import ThreadingHTTPServer

import jsonschema
import pytest
import requests

from lager.python import run_record as rr
from lager.python.service import PythonServiceHandler

from cli import run_record as client_rr
from cli.core.utils import StreamDatatypes, stream_python_output

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
SCHEMA_PATH = os.path.join(REPO_ROOT, 'docs', 'reference', 'run-record.v1.schema.json')

pytestmark = pytest.mark.skipif(
    not os.path.exists('/usr/bin/timeout'),
    reason='needs GNU timeout at /usr/bin/timeout (the box path)',
)

SCRIPT = b'''
import os, sys
sys.stdout.write("hello\\n"); sys.stdout.flush()
sys.stderr.write("warn\\n"); sys.stderr.flush()
with open(os.environ["OUT_PATH"], "w") as f:
    f.write("a,b\\n1,2\\n")
with open(os.environ["LAGER_RUN_ASSERTIONS"], "a") as f:
    f.write('{"key": "dut.firmware", "value": "2.14.0"}\\n')
sys.exit(3)
'''

SLEEPER = b'''
import sys, time
sys.stdout.write("up\\n"); sys.stdout.flush()
time.sleep(60)
'''


@pytest.fixture
def service(tmp_path, monkeypatch):
    from lager.python import executor
    monkeypatch.setattr(executor, 'SCRIPT_PYTHON', sys.executable)
    monkeypatch.setenv(rr.ROOT_ENV, str(tmp_path / 'records'))
    server = ThreadingHTTPServer(('127.0.0.1', 0), PythonServiceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_address[1]}', str(tmp_path / 'records')
    server.shutdown()
    server.server_close()


@pytest.fixture(scope='module')
def validator():
    with open(SCHEMA_PATH) as f:
        return jsonschema.Draft202012Validator(json.load(f))


def post(base, script, run_id, *, env=(), downloads=(), labels=None, timeout=0, stream=True):
    files = client_rr.client_files(script_bytes=script, script_name='probe.py')
    field = client_rr.build_client_field(
        runnable=__file__, argv=['/usr/bin/lager', 'python', 'probe.py', '--env', 'SECRET=hunter2'],
        passenv=['HOME'], downloads=list(downloads), labels=labels or {}, files=files,
        git=None, toplevel=None)
    post_data = [
        ('stdout_is_stderr', 'false'),
        ('detach', '0'),
        ('timeout', str(timeout)),
        ('env', f'LAGER_PROCESS_ID={run_id}'),
        *[('env', e) for e in env],
        ('script', ('probe.py', io.BytesIO(script), 'application/octet-stream')),
        (client_rr.CLIENT_FIELD, field),
    ]
    return requests.post(f'{base}/python', files=post_data, stream=stream, timeout=(5, 60),
                         headers={'Connection': 'close', client_rr.RUN_ID_HEADER: run_id})


def drain(resp):
    hasher = client_rr.LogHasher()
    exit_code = None
    for datatype, content in stream_python_output(resp, frame_observer=hasher):
        if datatype == StreamDatatypes.EXIT:
            exit_code = content
    return hasher, exit_code


def fetch(base, run_id):
    return requests.get(f'{base}/run-records/{run_id}', timeout=(5, 20))


def wait_final(root, run_id, timeout_s=20):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status, record = rr.load_final(run_id, root=root, wait_s=0)
        if status == 200:
            return record
        time.sleep(0.1)
    raise AssertionError(f'no final record for {run_id}')


def test_the_record_describes_the_run_that_happened(service, validator, tmp_path):
    base, _root = service
    run_id = str(uuid.uuid4())
    out_dir = '/tmp/lager-output'
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f'{run_id}.csv')

    resp = post(base, SCRIPT, run_id, env=[f'OUT_PATH={out_path}'], downloads=[out_path],
                labels={'execution': 'EX-20419'})
    assert resp.status_code == 200
    hasher, exit_code = drain(resp)
    assert exit_code == 3

    got = fetch(base, run_id)
    assert got.status_code == 200
    record = got.json()
    validator.validate(record)

    assert record['runId'] == run_id
    assert record['exit']['code'] == 3 and record['exit']['reason'] == 'exited'
    # What the client received is what the box recorded, byte for byte.
    assert record['log']['sha256'] == hasher.sha256
    assert record['log']['size'] == hasher.size
    # The downloaded file can be checked against its recorded hash.
    assert client_rr.check(record, hasher, {out_path: out_path}) == []
    # The box's hash of what it received matches what the client sent.
    assert record['code']['files'] == [
        {k: v for k, v in f.items() if k != 'repoPath'}
        for f in record['clientAsserted']['files']]
    assert record['scriptAsserted'] == {'dut.firmware': '2.14.0'}
    assert record['clientAsserted']['labels'] == {'execution': 'EX-20419'}
    assert record['environmentPassed'] == ['HOME']
    assert 'hunter2' not in json.dumps(record)
    os.unlink(out_path)


def test_a_tampered_download_is_reported(service, tmp_path):
    base, _root = service
    run_id = str(uuid.uuid4())
    out_path = os.path.join('/tmp/lager-output', f'{run_id}.csv')
    os.makedirs('/tmp/lager-output', exist_ok=True)
    resp = post(base, SCRIPT, run_id, env=[f'OUT_PATH={out_path}'], downloads=[out_path])
    hasher, _ = drain(resp)
    record = fetch(base, run_id).json()
    local = tmp_path / 'copy.csv'
    local.write_text('a,b\n9,9\n')
    problems = client_rr.check(record, hasher, {out_path: str(local)})
    assert problems and 'does not match' in problems[0]
    os.unlink(out_path)


def test_a_cancelled_run_says_so(service):
    base, root = service
    run_id = str(uuid.uuid4())
    resp = post(base, SLEEPER, run_id)
    lines = resp.iter_content(chunk_size=1)
    next(lines)  # the script is running
    kill = requests.post(f'{base}/python/kill', json={'lager_process_id': run_id, 'signal': 15},
                         headers={client_rr.RUN_ID_HEADER: run_id}, timeout=(5, 30))
    assert kill.status_code == 200
    for _ in lines:
        pass
    record = wait_final(root, run_id)
    assert record['exit']['reason'] == 'cancelled'
    assert record['exit']['cancelRequestedAt'] is not None


def test_a_client_that_goes_away_leaves_a_disconnected_record(service):
    base, root = service
    run_id = str(uuid.uuid4())
    host, port = base[len('http://'):].split(':')
    resp = post(base, SLEEPER, run_id)
    next(resp.iter_content(chunk_size=1))
    resp.raw._fp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    resp.close()
    record = wait_final(root, run_id)
    assert record['exit']['reason'] == 'disconnected'


def test_a_collected_box_refuses_a_run_it_cannot_record(service, monkeypatch):
    base, root = service
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, 'policy.json'), 'w') as f:
        json.dump({'mode': 'collected'}, f)

    class Full:
        f_bavail = 0
        f_frsize = 4096
    monkeypatch.setattr(rr.os, 'statvfs', lambda path: Full())
    run_id = str(uuid.uuid4())
    resp = post(base, SCRIPT, run_id, stream=False)
    assert resp.status_code == 503
    assert 'not started' in resp.json()['error']
    assert fetch(base, run_id).status_code == 404


def test_nothing_to_run_is_rejected_and_not_recorded(service):
    base, root = service
    resp = requests.post(f'{base}/python', files=[('detach', (None, '0'))], timeout=(5, 10))
    assert resp.status_code == 422
    assert not os.path.exists(os.path.join(root, 'sequence'))


def test_the_record_is_compressed_for_a_client_that_accepts_it(service):
    """Records carry the whole net configuration; on a VPN link the plain body
    costs extra round trips. Compressed transfer, same JSON."""
    import gzip
    import urllib.request

    base, _root = service
    run_id = str(uuid.uuid4())
    drain(post(base, SCRIPT, run_id, env=['OUT_PATH=/tmp/lager-output/gz.csv']))

    got = fetch(base, run_id)  # requests sends Accept-Encoding: gzip by default
    assert got.headers.get('Content-Encoding') == 'gzip'
    assert got.json()['runId'] == run_id

    raw = urllib.request.urlopen(urllib.request.Request(f'{base}/run-records/{run_id}'), timeout=20)
    assert raw.headers.get('Content-Encoding') is None
    assert json.loads(raw.read())['runId'] == run_id

    gz = urllib.request.urlopen(urllib.request.Request(
        f'{base}/run-records/{run_id}', headers={'Accept-Encoding': 'gzip'}), timeout=20)
    assert json.loads(gzip.decompress(gz.read()))['runId'] == run_id
