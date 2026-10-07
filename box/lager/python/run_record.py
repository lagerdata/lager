# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.python.run_record - one verifiable record per `lager python` run.

The normative description is docs/reference/run-record.md and its schema. This
module is the box's half of it: the box receives the code, spawns the process,
emits the output, sees the exit and holds the output files, so it is the party
that can state those things truthfully. What the client and the script say
about the run rides along in ``clientAsserted`` and ``scriptAsserted`` and is
recorded, not verified.

Layout under the records root (``/etc/lager/run_records``):

    box_uid               this box's record-store identity (boxId), minted once
    policy.json           {"mode": "local" | "collected"}; absent means local
    sequence              last boxSequence handed out
    sequence.lock         flock target serialising the counter
    open/<runId>.json     a run in flight
    open/<runId>.cancel   /python/kill asked this run to stop (holds the time)
    open/<runId>.log      the log being captured
    open/<runId>.script   key-values the script asserted (JSON lines)
    final/<runId>.json    finished records, served by GET /run-records/<runId>
    outbox/<runId>.json   collected mode only: records for a consumer to take
    blobs/<sha256>        collected mode only: retained log and output bytes

Two modes, because the two kinds of box want opposite things from a full disk:

* ``local`` (default). Nothing collects records, so the box keeps the most
  recent FINAL_KEEP and retains no bytes. A record that cannot be written is
  logged and the run goes ahead.
* ``collected``. A consumer collects the outbox and owns the archive. Nothing
  is pruned, and the box refuses to start a run it cannot record: for an
  evidence system an unrecorded run is worse than a refused one.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import fcntl
import functools
import hashlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone

from lager.util.jcs import canonical_sha256

logger = logging.getLogger(__name__)

SCHEMA = 'lager.run-record/v1'

DEFAULT_ROOT = '/etc/lager/run_records'
ROOT_ENV = 'LAGER_RUN_RECORDS_DIR'

MODE_LOCAL = 'local'
MODE_COLLECTED = 'collected'

# Bytes larger than this are hashed but not retained. 100 MiB covers any log
# the box will stream (attached runs are capped at 300s) and the CSVs and
# captures tests normally download, without letting one run fill the disk.
DEFAULT_BLOB_MAX = 100 * 1024 * 1024
BLOB_MAX_ENV = 'LAGER_RUN_RECORD_BLOB_MAX'

# Free space a collected-mode box insists on before starting a run: room for a
# full-size log, a full-size output, and the record itself.
FREE_SPACE_SLACK = 16 * 1024 * 1024

# final/ is the local store in local mode and a convenience copy for the
# client's fetch in collected mode, where the outbox is the archive.
FINAL_KEEP = 500

# How long GET /run-records/<id> waits for a run that is still finalizing.
FETCH_WAIT_S = 10.0

# The exit codes /usr/bin/timeout produces when it fires: 124 after SIGTERM,
# 137 (128+SIGKILL) once --kill-after escalates.
_TIMEOUT_EXIT_CODES = (124, 137)

# Bounds on what the client and the script may assert.
MAX_LABELS = 32
MAX_SCRIPT_KEYS = 256
MAX_VALUE_LEN = 4096
_KEY_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$')

# runIds become file names; anything else is refused rather than escaped.
_RUN_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')
_ENV_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_GIT_SHA_RE = re.compile(r'^[0-9a-f]{40}([0-9a-f]{24})?$')
_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')

ETC_LAGER = '/etc/lager'
HOST_ETC = '/host/etc'
SYS_DEVICES = '/sys/devices'

# Multipart field the client sends its own description of the run in.
CLIENT_FIELD = 'run_record'

# Environment variable that tells a script where to write its assertions.
SCRIPT_ASSERT_ENV = 'LAGER_RUN_ASSERTIONS'


class RunRecordUnavailable(Exception):
    """A collected-mode box cannot record this run, so it must not start it."""


# ----------------------------------------------------------------- helpers ---

def records_root():
    return os.environ.get(ROOT_ENV) or DEFAULT_ROOT


def blob_max():
    try:
        return int(os.environ.get(BLOB_MAX_ENV, DEFAULT_BLOB_MAX))
    except ValueError:
        return DEFAULT_BLOB_MAX


def read_mode(root=None):
    """The record policy: ``local`` unless policy.json says ``collected``."""
    try:
        with open(os.path.join(root or records_root(), 'policy.json')) as f:
            mode = json.load(f).get('mode')
    except (OSError, ValueError, AttributeError):
        return MODE_LOCAL
    return MODE_COLLECTED if mode == MODE_COLLECTED else MODE_LOCAL


def utc_timestamp(t=None):
    """RFC 3339, UTC, exactly six fractional digits, ``Z`` suffix."""
    if t is None:
        t = time.time()
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def normalize_exit_code(raw):
    """The CLI's normalization (cli/core/utils.py), so both sides agree.

    Negative codes are death by signal and become 128+N. -1 passes through:
    it is what terminate_process returns when it had to kill.
    """
    if raw is None:
        return None
    raw = int(raw)
    if raw < -1:
        return 128 + abs(raw)
    return raw


def valid_run_id(run_id):
    return isinstance(run_id, str) and bool(_RUN_ID_RE.match(run_id))


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    h = hashlib.sha256()
    size = 0
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _write_json_atomic(path, data):
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        _unlink_quietly(tmp)
        raise


def _unlink_quietly(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _dirs(root):
    paths = {name: os.path.join(root, name) for name in ('open', 'final', 'outbox', 'blobs')}
    for path in paths.values():
        os.makedirs(path, exist_ok=True)
    return paths


def _locked(root, fn):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, 'sequence.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _write_text_atomic(path, text):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def next_sequence(root):
    """Hand out the next boxSequence. Serialised across processes by flock."""
    seq_path = os.path.join(root, 'sequence')

    def bump():
        try:
            with open(seq_path) as f:
                current = int(f.read().strip() or 0)
        except FileNotFoundError:
            current = 0
        _write_text_atomic(seq_path, str(current + 1))
        return current + 1

    return _locked(root, bump)


def store_box_id(root):
    """This box's record-store identity, minted on first use.

    Not /etc/lager/box_id: nothing provisions that file, so on most boxes it
    does not exist, and other consumers already read its absence as "use the
    hostname". A record needs an identity that is never "unknown". This one
    lives with the records, so it survives Lager updates and container
    rebuilds, and is replaced, together with boxSequence, when the records
    directory is lost -- an OS re-image, say. ``hardwareId`` is what links the
    old and new identity across that.
    """
    path = os.path.join(root, 'box_uid')

    def read_or_mint():
        try:
            with open(path) as f:
                value = f.read().strip()
            if value:
                return value
        except FileNotFoundError:
            pass
        value = str(uuid.uuid4())
        _write_text_atomic(path, value)
        return value

    return _locked(root, read_or_mint)


# --------------------------------------------------------------- box state ---

def _read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _read_os_release(path):
    text = _read_text(path)
    if text is None:
        return None
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition('=')
        if sep:
            values[key.strip()] = value.strip().strip('"').strip("'")
    return {
        'prettyName': values.get('PRETTY_NAME'),
        'id': values.get('ID'),
        'versionId': values.get('VERSION_ID'),
    }


@functools.lru_cache(maxsize=4)
def hardware_id(sys_devices=SYS_DEVICES):
    """``mac:<address>`` of the lowest-addressed physical network interface.

    Physical means under a bus device rather than /sys/devices/virtual, so
    bridges, veths and docker0 are skipped. Survives re-imaging, which the
    store's own boxId does not. None when nothing qualifies.

    Walked without following symlinks: sysfs is full of them, and a glob that
    follows them takes minutes. Cached, because the hardware does not change
    while the service runs.
    """
    addresses = []
    for dirpath, dirnames, filenames in os.walk(sys_devices, followlinks=False):
        if dirpath == sys_devices and 'virtual' in dirnames:
            dirnames.remove('virtual')
        if os.path.basename(os.path.dirname(dirpath)) == 'net' and 'address' in filenames:
            dirnames[:] = []
            mac = (_read_text(os.path.join(dirpath, 'address')) or '').lower()
            if re.match(r'^([0-9a-f]{2}:){5}[0-9a-f]{2}$', mac) and mac != '00:00:00:00:00:00':
                addresses.append(mac)
    return f'mac:{min(addresses)}' if addresses else None


class _Timeval(ctypes.Structure):
    _fields_ = [('tv_sec', ctypes.c_long), ('tv_usec', ctypes.c_long)]


class _Timex(ctypes.Structure):
    # glibc's struct timex (sys/timex.h), identical on x86_64 and aarch64.
    _fields_ = [
        ('modes', ctypes.c_uint), ('offset', ctypes.c_long), ('freq', ctypes.c_long),
        ('maxerror', ctypes.c_long), ('esterror', ctypes.c_long), ('status', ctypes.c_int),
        ('constant', ctypes.c_long), ('precision', ctypes.c_long), ('tolerance', ctypes.c_long),
        ('time', _Timeval), ('tick', ctypes.c_long), ('ppsfreq', ctypes.c_long),
        ('jitter', ctypes.c_long), ('shift', ctypes.c_int), ('stabil', ctypes.c_long),
        ('jitcnt', ctypes.c_long), ('calcnt', ctypes.c_long), ('errcnt', ctypes.c_long),
        ('stbcnt', ctypes.c_long), ('tai', ctypes.c_int), ('_reserved', ctypes.c_int * 11),
    ]


_TIME_ERROR = 5
_STA_UNSYNC = 0x0040


def clock_state():
    """Whether the kernel considers the system clock synchronised.

    Read with adjtimex(2) in read-only mode, which needs no privilege and sees
    the host's clock from inside the container: it is the same signal
    ``timedatectl`` reports as "System clock synchronized". Returns
    ``{synced, maxErrorMicroseconds}``, both null when it cannot be read.
    """
    unknown = {'synced': None, 'maxErrorMicroseconds': None}
    try:
        libc = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6', use_errno=True)
        adjtimex = getattr(libc, 'adjtimex', None)
        if adjtimex is None:
            return unknown
        tx = _Timex()
        state = adjtimex(ctypes.byref(tx))
        if state < 0:
            return unknown
        synced = state != _TIME_ERROR and not (tx.status & _STA_UNSYNC)
        return {'synced': bool(synced), 'maxErrorMicroseconds': int(tx.maxerror)}
    except Exception:  # pylint: disable=broad-except
        return unknown


def _load_nets(path):
    try:
        with open(path) as f:
            nets = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(nets, list):
        return []
    return [n for n in nets if isinstance(n, dict)]


def serial_from_address(address):
    """``(serialNumber, serialSource)`` read out of an instrument address.

    No I/O: the address a net is saved with already names the unit. A USB
    instrument's VISA address carries its iSerialNumber, and the box can only
    open the unit that address names, so it is the unit the run used. A
    ``serial://`` address names the USB-to-serial cable, not the instrument
    behind it, and says so.
    """
    if not isinstance(address, str) or not address:
        return None, None
    if address.upper().startswith('USB'):
        parts = address.split('::')
        if len(parts) >= 4:
            serial = parts[3]
            # `port-…` is a topology path standing in for a missing serial.
            if serial and not serial.startswith('port-'):
                return serial, 'usb-device'
        return None, None
    if address.startswith('ppk2:'):
        serial = address[len('ppk2:'):]
        return (serial, 'usb-device') if serial else (None, None)
    if address.startswith('serial://'):
        match = re.match(r'^serial://[0-9a-fA-F]{4}:[0-9a-fA-F]{4}/serial/(.+)$', address)
        if match:
            return match.group(1), 'usb-adapter'
        return None, None
    return None, None


def _net_address(net):
    address = net.get('address')
    if address:
        return str(address)
    for mapping in net.get('mappings') or []:
        if isinstance(mapping, dict) and mapping.get('device_override'):
            return str(mapping['device_override'])
    return ''


def instruments_from_nets(nets):
    """One entry per distinct (instrument, address) behind the saved nets."""
    by_key = {}
    for net in nets:
        instrument = net.get('instrument')
        if not instrument:
            continue
        address = _net_address(net)
        key = (str(instrument), address)
        entry = by_key.get(key)
        if entry is None:
            serial, source = serial_from_address(address)
            entry = {
                'type': str(instrument),
                'connection': address,
                'serialNumber': serial,
                'serialSource': source,
                'firmwareVersion': None,
                'nets': [],
            }
            by_key[key] = entry
        name = net.get('name')
        if name and str(name) not in entry['nets']:
            entry['nets'].append(str(name))
    instruments = list(by_key.values())
    for entry in instruments:
        entry['nets'].sort()
    instruments.sort(key=lambda e: (e['connection'], e['type']))
    return instruments


def read_box_state(etc=ETC_LAGER, host_etc=HOST_ETC):
    version = _read_text(os.path.join(etc, 'version'))
    lager_version = deployed_by = None
    if version:
        lager_version, _, deployed_by = version.partition('|')
        lager_version = lager_version or None
        deployed_by = deployed_by or None
    nets = _load_nets(os.path.join(etc, 'saved_nets.json'))
    try:
        kernel = os.uname().release
    except Exception:  # pylint: disable=broad-except
        kernel = None
    return {
        'lagerVersion': lager_version,
        'deployedByCliVersion': deployed_by,
        'ref': _read_text(os.path.join(etc, 'ref')) or None,
        'os': _read_os_release(os.path.join(host_etc, 'os-release')),
        'kernel': kernel,
        'clock': clock_state(),
        'nets': nets,
        'netsHash': canonical_sha256(nets),
        'instruments': instruments_from_nets(nets),
    }


def read_box(root, etc=ETC_LAGER, host_etc=HOST_ETC, sys_devices=SYS_DEVICES):
    name = (_read_text(os.path.join(etc, 'box_id'))
            or _read_text(os.path.join(host_etc, 'hostname')))
    return {
        'boxId': store_box_id(root),
        'boxName': name or None,
        'hostname': _read_text(os.path.join(host_etc, 'hostname')),
        'hardwareId': hardware_id(sys_devices),
    }


# -------------------------------------------------------------------- code ---

def _read_upload(upload):
    """Bytes of an uploaded part, leaving a file-like object rewound."""
    if upload is None:
        return None
    if isinstance(upload, (bytes, bytearray)):
        return bytes(upload)
    if hasattr(upload, 'read'):
        pos = upload.tell() if hasattr(upload, 'tell') else 0
        data = upload.read()
        if hasattr(upload, 'seek'):
            upload.seek(pos)
        return data
    return None


def describe_code(script_file=None, script_name=None, module_zip=None, args=None):
    """The ``code`` section: what the box received, hashed by the box."""
    decoded_args = [a.decode('utf-8', errors='replace') if isinstance(a, bytes) else str(a)
                    for a in (args or [])]
    module_bytes = _read_upload(module_zip)
    if module_bytes:
        files = []
        with zipfile.ZipFile(io.BytesIO(module_bytes)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                data = zf.read(info)
                files.append({'path': info.filename, 'size': len(data), 'sha256': _sha256(data)})
        files.sort(key=lambda f: f['path'])
        return {
            'kind': 'module',
            'archiveSha256': _sha256(module_bytes),
            'files': files,
            'entrypoint': 'main.py',
            'args': decoded_args,
        }
    script_bytes = _read_upload(script_file) or b''
    name = os.path.basename(script_name or '') or 'script.py'
    return {
        'kind': 'script',
        'archiveSha256': None,
        'files': [{'path': name, 'size': len(script_bytes), 'sha256': _sha256(script_bytes)}],
        'entrypoint': name,
        'args': decoded_args,
    }


# ------------------------------------------------- asserted (unverified) ---

def _scalar(value):
    """A value fit for an asserted map: string, number, boolean or null."""
    if value is None or isinstance(value, bool):
        return True, value
    if isinstance(value, int):
        return True, value
    if isinstance(value, float):
        return (value == value and value not in (float('inf'), float('-inf'))), value
    if isinstance(value, str):
        return len(value) <= MAX_VALUE_LEN, value
    return False, None


def parse_client_field(raw):
    """The client's run description, or an empty dict if absent or malformed.

    Returns a dict with keys ``passenv`` (names), ``downloads`` (box paths) and
    ``asserted`` (the clientAsserted section, or None when nothing was sent).
    Every value is checked for shape: this arrives off the wire and lands in a
    record.
    """
    if raw is None:
        return {}
    if hasattr(raw, 'read'):
        raw = raw.read()
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', errors='replace')
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning('run record: ignoring malformed %s field', CLIENT_FIELD)
        return {}
    if not isinstance(data, dict):
        return {}

    def str_list(value, limit=4096):
        if not isinstance(value, list):
            return []
        return [v for v in value if isinstance(v, str)][:limit]

    def opt_str(value):
        return value if isinstance(value, str) else None

    passenv = sorted({n for n in str_list(data.get('passenv')) if _ENV_NAME_RE.match(n)})
    downloads = str_list(data.get('downloads'), limit=256)

    git = data.get('git')
    if not (isinstance(git, dict) and isinstance(git.get('commit'), str)
            and _GIT_SHA_RE.match(git['commit']) and isinstance(git.get('dirty'), bool)):
        git = None
    else:
        git = {'commit': git['commit'], 'dirty': git['dirty']}

    files = []
    for entry in data.get('files') or []:
        if (isinstance(entry, dict) and isinstance(entry.get('path'), str)
                and isinstance(entry.get('size'), int) and entry['size'] >= 0
                and isinstance(entry.get('sha256'), str) and _SHA256_RE.match(entry['sha256'])):
            files.append({
                'path': entry['path'],
                'repoPath': opt_str(entry.get('repoPath')),
                'size': entry['size'],
                'sha256': entry['sha256'],
            })

    labels = {}
    raw_labels = data.get('labels')
    if isinstance(raw_labels, dict):
        for key, value in raw_labels.items():
            if len(labels) >= MAX_LABELS:
                break
            if (isinstance(key, str) and _KEY_RE.match(key)
                    and isinstance(value, str) and len(value) <= MAX_VALUE_LEN):
                labels[key] = value

    asserted = {
        'cliVersion': opt_str(data.get('cliVersion')),
        'argv': str_list(data.get('argv')),
        'git': git,
        'repoRelativeRoot': opt_str(data.get('repoRelativeRoot')),
        'files': sorted(files, key=lambda f: f['path']),
        'labels': labels,
    }
    return {'passenv': passenv, 'downloads': downloads, 'asserted': asserted}


def read_script_assertions(path):
    """The key-values a script wrote with ``lager.run_record.assert_value``.

    JSON lines of ``{"key": ..., "value": ...}``; the last value for a key
    wins. Lines that are not well-formed, keys outside the key grammar, and
    anything past MAX_SCRIPT_KEYS are dropped. Null when the script asserted
    nothing.
    """
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            lines = f.read(MAX_SCRIPT_KEYS * (MAX_VALUE_LEN + 256)).splitlines()
    except OSError:
        return None
    values = {}
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        key = item.get('key')
        ok, value = _scalar(item.get('value'))
        if not (ok and isinstance(key, str) and _KEY_RE.match(key)):
            continue
        if key not in values and len(values) >= MAX_SCRIPT_KEYS:
            continue
        values[key] = value
    return values or None


# ---------------------------------------------------------------- recorder ---

class RunRecorder:
    """The record for one run, from acceptance to its final file.

    Create with ``RunRecorder.begin``. ``log_frame`` is called for every output
    frame and ``finalize`` exactly once; both are no-ops after a failure.
    """

    def __init__(self, root, mode, run_id, record, effective_timeout, declared_outputs):
        self.root = root
        self.mode = mode
        self.run_id = run_id
        self.record = record
        self.effective_timeout = effective_timeout
        self.declared_outputs = declared_outputs
        self.accepted_monotonic = time.monotonic()
        self.dirs = _dirs(root)
        self.open_path = os.path.join(self.dirs['open'], f'{run_id}.json')
        self.log_path = os.path.join(self.dirs['open'], f'{run_id}.log')
        self.script_path = os.path.join(self.dirs['open'], f'{run_id}.script')
        self.cancel_path = os.path.join(self.dirs['open'], f'{run_id}.cancel')
        self.retain = mode == MODE_COLLECTED
        self._log_file = open(self.log_path, 'wb') if self.retain else None
        self._log_hash = hashlib.sha256()
        self._log_size = 0
        self._finalized = False
        self._broken = False

    @property
    def finalized(self):
        return self._finalized

    @property
    def env(self):
        """Environment variables the run's process should carry."""
        return {SCRIPT_ASSERT_ENV: self.script_path}

    # -- construction --

    @classmethod
    def begin(cls, run_id, *, script_file=None, script_name=None, module_zip=None,
              args=None, timeout_seconds=0, client_field=None, root=None,
              etc=ETC_LAGER, host_etc=HOST_ETC, sys_devices=SYS_DEVICES):
        """Open the record for a run the box has just accepted.

        Returns the recorder, or None when a local-mode box could not open one.

        Raises:
            RunRecordUnavailable: a collected-mode box could not open one. The
                caller must refuse the run.
        """
        root = root or records_root()
        mode = read_mode(root)
        try:
            if mode == MODE_COLLECTED:
                _require_free_space(root)
            if not valid_run_id(run_id):
                logger.warning('run record: refusing run id %r; recording under a new id', run_id)
                run_id = str(uuid.uuid4())
            dirs = _dirs(root)
            if any(os.path.exists(os.path.join(dirs[d], f'{run_id}.json'))
                   for d in ('open', 'final')):
                # The CLI retries a POST that failed to connect; if one of
                # those attempts did reach the box, the first run owns the id.
                fresh = str(uuid.uuid4())
                logger.warning('run record: id %s already recorded; recording this run as %s',
                               run_id, fresh)
                run_id = fresh

            client = parse_client_field(client_field)
            record = {
                'schema': SCHEMA,
                'runId': run_id,
                'boxSequence': next_sequence(root),
                'state': 'open',
                'startedAt': utc_timestamp(),
                'finishedAt': None,
                'box': read_box(root, etc, host_etc, sys_devices),
                'boxState': read_box_state(etc, host_etc),
                'code': describe_code(script_file, script_name, module_zip, args),
                'environmentPassed': client.get('passenv', []),
                'exit': None,
                'outputs': [],
                'log': None,
                'retention': {'mode': mode, 'maxBlobBytes': blob_max() if mode == MODE_COLLECTED else 0},
                'clientAsserted': client.get('asserted'),
                'scriptAsserted': None,
            }
            recorder = cls(root, mode, run_id, record, int(timeout_seconds or 0),
                           client.get('downloads', []))
            recorder._persist_open()
            return recorder
        except Exception as exc:  # pylint: disable=broad-except
            if mode == MODE_COLLECTED:
                logger.error('run record: cannot record run %s, refusing it: %s', run_id, exc)
                raise RunRecordUnavailable(str(exc)) from exc
            logger.exception('run record: could not open a record for run %s', run_id)
            return None

    def _persist_open(self):
        meta = dict(self.record)
        meta['_effectiveTimeout'] = self.effective_timeout
        _write_json_atomic(self.open_path, meta)

    # -- capture --

    def log_frame(self, fileno, chunk):
        """Add one output frame to the log. Empty frames are not part of it."""
        if self._broken or self._finalized or not chunk or fileno not in (1, 2, 3):
            return
        try:
            frame = f'{fileno} {len(chunk)} '.encode() + chunk
            self._log_hash.update(frame)
            self._log_size += len(frame)
            if self._log_file is not None:
                if self._log_size <= blob_max():
                    self._log_file.write(frame)
                else:
                    self._drop_log_bytes()
        except Exception:  # pylint: disable=broad-except
            logger.exception('run record: log capture failed for run %s', self.run_id)
            self._broken = True

    def _drop_log_bytes(self):
        try:
            self._log_file.close()
        finally:
            self._log_file = None
            _unlink_quietly(self.log_path)

    # -- finishing --

    def finalize(self, returncode, ended='exited'):
        """Write the final record.

        ``ended`` is how the box saw the run end: ``exited``, ``disconnected``
        or ``start-failed``. Cancellation and timeout are worked out here.
        """
        if self._finalized:
            return
        self._finalized = True
        try:
            code = normalize_exit_code(returncode)
            elapsed = time.monotonic() - self.accepted_monotonic
            cancel_requested_at = _read_text(self.cancel_path)
            if cancel_requested_at is not None:
                reason = 'cancelled'
            elif ended in ('disconnected', 'start-failed'):
                reason = ended
            elif (self.effective_timeout > 0 and code in _TIMEOUT_EXIT_CODES
                  and elapsed >= self.effective_timeout - 0.5):
                reason = 'timeout'
            else:
                reason = 'exited'

            if self._log_file is not None:
                self._log_file.close()
                self._log_file = None
            log_sha = self._log_hash.hexdigest()
            log_retained = (self.retain and not self._broken
                            and self._log_size <= blob_max() and os.path.exists(self.log_path))
            if log_retained:
                _store_blob(self.dirs['blobs'], self.log_path, log_sha, move=True)
            else:
                _unlink_quietly(self.log_path)

            self.record.update({
                'state': 'final',
                'finishedAt': utc_timestamp(),
                'exit': {
                    'code': code,
                    'reason': reason,
                    'timeoutSeconds': self.effective_timeout,
                    'cancelRequestedAt': cancel_requested_at or None,
                },
                'outputs': hash_outputs(self.declared_outputs, self.dirs['blobs'], self.retain),
                'log': {'sha256': log_sha, 'size': self._log_size, 'retained': log_retained},
                'scriptAsserted': read_script_assertions(self.script_path),
            })
            publish_final(self.root, self.record, self.mode)
            for path in (self.open_path, self.cancel_path, self.script_path):
                _unlink_quietly(path)
        except Exception:  # pylint: disable=broad-except
            logger.exception('run record: could not finalize run %s', self.run_id)


def _require_free_space(root):
    os.makedirs(root, exist_ok=True)
    stat = os.statvfs(root)
    free = stat.f_bavail * stat.f_frsize
    needed = 2 * blob_max() + FREE_SPACE_SLACK
    if free < needed:
        raise OSError(errno.ENOSPC,
                      f'{free} bytes free under {root}; recording a run needs {needed}')


def _store_blob(blobs_dir, src_path, sha, move=False):
    dest = os.path.join(blobs_dir, sha)
    if os.path.exists(dest):
        if move:
            _unlink_quietly(src_path)
        return
    tmp = os.path.join(blobs_dir, f'.tmp-{uuid.uuid4().hex}')
    if move:
        shutil.move(src_path, tmp)
    else:
        shutil.copyfile(src_path, tmp)
    os.replace(tmp, dest)


def hash_outputs(declared, blobs_dir, retain):
    """Hash each declared download as it sits on the box after the run."""
    from lager.binaries import store as binaries_store

    outputs = []
    for name in declared:
        entry = {'name': name, 'size': None, 'sha256': None, 'retained': False}
        try:
            path, _size = binaries_store.resolve_download_path(name)
            sha, size = _sha256_file(path)
            entry['size'] = size
            entry['sha256'] = sha
            if retain and size <= blob_max():
                _store_blob(blobs_dir, path, sha)
                entry['retained'] = True
        except binaries_store.StoreError:
            pass  # missing, or outside the download allowlist: recorded as null
        except OSError as exc:
            logger.warning('run record: could not hash output %s: %s', name, exc)
        outputs.append(entry)
    return outputs


def publish_final(root, record, mode):
    """Write a final record to final/ and, in collected mode, the outbox."""
    dirs = _dirs(root)
    final_path = os.path.join(dirs['final'], f"{record['runId']}.json")
    _write_json_atomic(final_path, record)
    if mode == MODE_COLLECTED:
        outbox_path = os.path.join(dirs['outbox'], f"{record['runId']}.json")
        try:
            os.link(final_path, outbox_path)
        except FileExistsError:
            pass
        except OSError as exc:
            if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK):
                raise
            _write_json_atomic(outbox_path, record)
    _prune_final(dirs['final'])


def _prune_final(final_dir, keep=None):
    if keep is None:
        keep = FINAL_KEEP
    try:
        entries = [e for e in os.scandir(final_dir) if e.name.endswith('.json')]
    except OSError:
        return
    if len(entries) <= keep:
        return
    entries.sort(key=lambda e: e.stat().st_mtime)
    for entry in entries[:len(entries) - keep]:
        _unlink_quietly(entry.path)


# ------------------------------------------------------- box-wide entry points

def mark_cancelled(run_id=None, root=None):
    """Record that /python/kill asked a run (or every open run) to stop.

    Called before the signal is sent, so the run's own finalize sees it. The
    marker holds the time of the request, which lands in exit.cancelRequestedAt.
    """
    try:
        dirs = _dirs(root or records_root())
        if run_id is not None:
            if not valid_run_id(run_id):
                return
            targets = [run_id] if os.path.exists(
                os.path.join(dirs['open'], f'{run_id}.json')) else []
        else:
            targets = [n[:-len('.json')] for n in os.listdir(dirs['open'])
                       if n.endswith('.json') and not n.startswith('.')]
        now = utc_timestamp()
        for target in targets:
            path = os.path.join(dirs['open'], f'{target}.cancel')
            if not os.path.exists(path):  # the first request is the one that counts
                with open(path, 'w') as f:
                    f.write(now)
    except Exception:  # pylint: disable=broad-except
        logger.exception('run record: could not mark run %s cancelled', run_id)


def sweep_lost(root=None):
    """Finalize every run left open by a restart, as ``lost``.

    Run once when the execution service starts, before it accepts a request,
    so a box restart never leaves a hole in boxSequence.
    """
    root = root or records_root()
    mode = read_mode(root)
    try:
        dirs = _dirs(root)
        names = [n for n in os.listdir(dirs['open']) if n.endswith('.json') and not n.startswith('.')]
    except Exception:  # pylint: disable=broad-except
        logger.exception('run record: could not scan for lost runs')
        return 0
    swept = 0
    for name in names:
        run_id = name[:-len('.json')]
        path = os.path.join(dirs['open'], name)
        leftovers = [os.path.join(dirs['open'], f'{run_id}{ext}')
                     for ext in ('.json', '.log', '.cancel', '.script')]
        try:
            with open(path) as f:
                record = json.load(f)
            timeout = int(record.pop('_effectiveTimeout', 0) or 0)
            # What the box saw before it went down is kept so a reader sees the
            # run happened and how far it got, but the log is not retained: it
            # may stop mid-frame, and nobody can say it is the whole output.
            # Outputs are never hashed for a lost run: the run did not reach
            # the point where its files are final.
            log_path = leftovers[1]
            if os.path.exists(log_path):
                log_sha, log_size = _sha256_file(log_path)
            else:
                log_sha, log_size = _sha256(b''), 0
            record.update({
                'state': 'final',
                'finishedAt': None,
                'exit': {'code': None, 'reason': 'lost', 'timeoutSeconds': timeout,
                         'cancelRequestedAt': _read_text(leftovers[2]) or None},
                'outputs': [],
                'log': {'sha256': log_sha, 'size': log_size, 'retained': False},
                'scriptAsserted': read_script_assertions(leftovers[3]),
            })
            publish_final(root, record, mode)
            for leftover in leftovers:
                _unlink_quietly(leftover)
            swept += 1
        except Exception:  # pylint: disable=broad-except
            logger.exception('run record: could not finalize lost run %s', run_id)
    if swept:
        logger.warning('run record: finalized %d run(s) left open by a restart as lost', swept)
    return swept


def load_final(run_id, root=None, wait_s=FETCH_WAIT_S, poll_s=0.1):
    """The final record for ``run_id``, waiting briefly if it is finishing.

    Returns ``(status, record)``: 200 with the record, 202 when the run is
    still open after the wait, 404 when the box has no record of it.
    """
    if not valid_run_id(run_id):
        return 404, None
    dirs = _dirs(root or records_root())
    final_path = os.path.join(dirs['final'], f'{run_id}.json')
    open_path = os.path.join(dirs['open'], f'{run_id}.json')
    deadline = time.monotonic() + wait_s
    while True:
        try:
            with open(final_path) as f:
                return 200, json.load(f)
        except FileNotFoundError:
            pass
        if not os.path.exists(open_path):
            # One more look: finalize writes final/ before removing open/.
            try:
                with open(final_path) as f:
                    return 200, json.load(f)
            except FileNotFoundError:
                return 404, None
        if time.monotonic() >= deadline:
            return 202, None
        time.sleep(poll_s)


def outbox_size(root=None):
    try:
        outbox = os.path.join(root or records_root(), 'outbox')
        return sum(1 for n in os.listdir(outbox) if n.endswith('.json') and not n.startswith('.'))
    except OSError:
        return 0


def effective_timeout(timeout, detach, max_timeout):
    """The timeout the box actually enforces (see executor._wrap_with_timeout)."""
    timeout = int(timeout or 0)
    if not timeout or detach:
        return timeout
    return min(timeout, max_timeout)
