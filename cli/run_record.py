# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
The client's half of the run record (docs/reference/run-record.md).

The box records what it observes. The client adds what only it knows -- the
command line, where the code sits in its Git repository, any labels the
launcher wants on the run -- as ``clientAsserted``, which the box records but
cannot verify. After a run with --download the client fetches the box's final
record, downloads every output it lists into a folder named after the run,
saves the record there, and checks the files against it.
"""

import hashlib
import io
import json
import os
import re
import subprocess
import zipfile

import click

from . import __version__

#: Multipart field the description travels in. Must match the box.
CLIENT_FIELD = 'run_record'

#: Request header carrying the run id, so a gateway in front of the box can
#: attribute the run without parsing the multipart body.
RUN_ID_HEADER = 'Lager-Process-Id'

RECORD_SUFFIX = '.lager-run.json'

# How many times to ask for a record the box is still finalizing. The box
# itself waits up to 10 s per request before answering 202, so this is about a
# minute: room for a busy box hashing large outputs, not an open-ended wait.
FETCH_ATTEMPTS = 6

_LABEL_KEY_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$')
MAX_LABELS = 32
MAX_LABEL_VALUE = 4096


def parse_labels(values):
    """``KEY=VALUE`` strings from ``--label`` to a dict. Raises click errors."""
    labels = {}
    for raw in values or ():
        key, sep, value = raw.partition('=')
        if not sep or not _LABEL_KEY_RE.match(key):
            raise click.BadParameter(
                f'{raw!r}: expected KEY=VALUE, with KEY of letters, digits and . _ : / -',
                param_hint='--label')
        if len(value) > MAX_LABEL_VALUE:
            raise click.BadParameter(f'{key}: values are limited to {MAX_LABEL_VALUE} characters',
                                     param_hint='--label')
        labels[key] = value
    if len(labels) > MAX_LABELS:
        raise click.BadParameter(f'at most {MAX_LABELS} labels', param_hint='--label')
    return labels


def redact_argv(argv):
    """The command line with every ``--env`` value replaced by ``<redacted>``.

    Keeps the variable name, so a reader can see what was set without seeing
    what it was set to. argv[0] becomes ``lager``: the full path to the
    executable on someone's laptop is noise.
    """
    out = []
    redact_next = False
    for i, arg in enumerate(argv):
        if i == 0:
            out.append('lager')
            continue
        if redact_next:
            name = arg.partition('=')[0]
            out.append(f'{name}=<redacted>')
            redact_next = False
        elif arg == '--env':
            out.append(arg)
            redact_next = True
        elif arg.startswith('--env='):
            name = arg[len('--env='):].partition('=')[0]
            out.append(f'--env={name}=<redacted>')
        else:
            out.append(arg)
    return out


def _git(args, cwd):
    try:
        result = subprocess.run(
            ['git', *args], cwd=cwd, capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def git_context(runnable):
    """``(git, toplevel)`` for the repository holding ``runnable``, or (None, None)."""
    path = os.path.abspath(runnable)
    cwd = path if os.path.isdir(path) else os.path.dirname(path)
    toplevel = _git(['rev-parse', '--show-toplevel'], cwd)
    commit = _git(['rev-parse', 'HEAD'], cwd)
    if not toplevel or not commit:
        return None, None
    status = _git(['status', '--porcelain', '--untracked-files=no'], cwd)
    return {'commit': commit, 'dirty': bool(status)}, os.path.realpath(toplevel)


def _repo_path(path, toplevel):
    if not toplevel or not path:
        return None
    real = os.path.realpath(path)
    rel = os.path.relpath(real, toplevel)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return rel.replace(os.sep, '/')


def client_files(*, script_bytes=None, script_name=None, script_source=None,
                 module_bytes=None, manifest=None, toplevel=None):
    """The client's ``files`` list: what it sent, where each file came from.

    Hashed from the exact bytes posted, so an honest client's list matches the
    box's ``code.files`` entry for entry. ``repoPath`` is the file's path in the
    Git repository, or null for a file outside it.
    """
    files = []
    if module_bytes is not None:
        manifest = manifest or {}
        with zipfile.ZipFile(io.BytesIO(bytes(module_bytes))) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                data = zf.read(info)
                files.append({
                    'path': info.filename,
                    'repoPath': _repo_path(manifest.get(info.filename), toplevel),
                    'size': len(data),
                    'sha256': hashlib.sha256(data).hexdigest(),
                })
    elif script_bytes is not None:
        files.append({
            'path': os.path.basename(script_name or '') or 'script.py',
            'repoPath': _repo_path(script_source, toplevel),
            'size': len(script_bytes),
            'sha256': hashlib.sha256(script_bytes).hexdigest(),
        })
    return sorted(files, key=lambda f: f['path'])


def build_client_field(*, runnable, argv, passenv, labels, files, git, toplevel):
    """The JSON the box records as clientAsserted (plus passenv)."""
    root = os.path.abspath(runnable)
    if not os.path.isdir(root):
        root = os.path.dirname(root)
    return json.dumps({
        'cliVersion': __version__,
        'argv': redact_argv(argv),
        'git': git,
        'repoRelativeRoot': _repo_path(root, toplevel),
        'files': files,
        'labels': labels or {},
        'passenv': list(passenv or ()),
    })


class LogHasher:
    """Hashes output frames exactly as the box hashes them for ``log``."""

    def __init__(self):
        self._hash = hashlib.sha256()
        self.size = 0

    def __call__(self, fileno, chunk):
        if not chunk:
            return
        frame = f'{fileno} {len(chunk)} '.encode() + chunk
        self._hash.update(frame)
        self.size += len(frame)

    @property
    def sha256(self):
        return self._hash.hexdigest()


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def fetch(session, box, run_id):
    """The box's final record of this run, or None with a warning saying why.

    Never raises: the run is over and its exit code is decided. A box too old
    to keep records is silent.
    """
    for _ in range(FETCH_ATTEMPTS):
        try:
            resp = session.get_run_record(box, run_id)
        except Exception as exc:  # pylint: disable=broad-except
            click.secho(f'Run record: the box did not send it: {exc}', fg='yellow', err=True)
            return None
        # 202: the box is still finalizing the record (hashing large outputs
        # on a busy box). Ask again; it only ever serves a final record.
        if resp.status_code != 202:
            break
    if resp.status_code == 202:
        click.secho('Run record: the box is still finishing the record; nothing downloaded. '
                    'The box keeps the record and the files.',
                    fg='yellow', err=True)
        return None
    if resp.status_code == 404:
        return None  # a box without run records, or one that pruned it
    if resp.status_code != 200:
        click.secho(f'Run record: the box answered HTTP {resp.status_code}; not saved',
                    fg='yellow', err=True)
        return None
    try:
        return resp.json()
    except ValueError:
        click.secho('Run record: the box sent something that is not JSON; not saved',
                    fg='yellow', err=True)
        return None


def run_folder(record, run_id):
    """The local folder a run's downloads and record go in: named after the run."""
    return record.get('runId') or run_id


def local_path(record, box_path):
    """Where an output goes inside the run folder, or None if it must not be written.

    A file from the run's output folder keeps its place below that folder; one
    an older client named directly goes in by its basename. A name that would
    climb out of the run folder is refused: the record comes off the network.
    """
    marker = f"/{record.get('runId')}/"
    relative = box_path.split(marker, 1)[1] if marker in box_path else os.path.basename(box_path)
    parts = [p for p in relative.replace('\\', '/').split('/') if p not in ('', '.')]
    if not parts or '..' in parts:
        return None
    return os.path.join(*parts)


def save(record, folder):
    """Write the record into the run folder. Returns its path, or None."""
    path = os.path.join(folder, f"{record.get('runId')}{RECORD_SUFFIX}")
    try:
        os.makedirs(folder, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(record, f, indent=2, sort_keys=True)
            f.write('\n')
    except OSError as exc:
        click.secho(f'Run record: cannot write {path}: {exc}', fg='yellow', err=True)
        return None
    click.secho(f'Run record: {path}', dim=True, err=True)
    return path


def check(record, log_hasher, downloaded):
    """What disagrees between the box's record and what this client got."""
    problems = []
    log = record.get('log') or {}
    if log_hasher is not None and log.get('sha256') and log['sha256'] != log_hasher.sha256:
        problems.append(
            'the output this client received does not match the log the box recorded '
            f'({log_hasher.size} bytes received, {log.get("size")} recorded)')
    by_name = {o.get('name'): o for o in record.get('outputs') or []}
    for name, local in (downloaded or {}).items():
        entry = by_name.get(name)
        if entry is None or not entry.get('sha256'):
            problems.append(f'{name} has no hash in the record')
            continue
        try:
            actual = _sha256_file(local)
        except OSError:
            continue
        if actual != entry['sha256']:
            problems.append(f'{local} does not match the hash the box recorded for {name}')
    return problems
