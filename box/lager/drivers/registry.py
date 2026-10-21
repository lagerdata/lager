# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.drivers.registry - The installed custom drivers, on disk.

Each driver is a directory under ``/etc/lager/drivers/<type>/``. /etc/lager
is a host bind mount, so a driver survives ``lager update`` and container
recreation the same way saved_nets.json does::

    /etc/lager/drivers/
      stepper/
        lager_driver.json
        driver.py
        .sha256          # hash of the zip it was installed from

An install unpacks into ``.staging/`` and is swapped in with two renames, so
a call never sees a half-written driver. A bundle that fails to load is still
listed (status "error") and does not hide the others.

User errors raise DriverStoreError carrying the HTTP status to return, the
same pattern as lager.binaries.store.

Driver types arrive in URLs, and zip entry names in uploads. TYPE_RE and
_extract's checks are the real defence. Each function that touches the
filesystem also repeats a normpath + startswith containment check next to
the join it guards: a static analyser only recognises the guard there (see
lager.util.paths), so do not fold those lines into a helper.
"""

import hashlib
import io
import os
import posixpath
import shutil
import stat
import uuid
import zipfile

from lager.drivers.errors import DriverUnavailable
from lager.drivers.manifest import (
    DRIVER_FILE,
    MANIFEST_FILE,
    RESERVED_TYPES,
    TYPE_RE,
    ManifestError,
    load_manifest,
)

# Module-level so tests can point it at a temp directory.
DRIVERS_DIR = os.environ.get('LAGER_DRIVERS_DIR', '/etc/lager/drivers')

MAX_BUNDLE_BYTES = 5 * 1024 * 1024
MAX_UNPACKED_BYTES = 20 * 1024 * 1024
MAX_FILES = 200

_SHA_FILE = '.sha256'


class DriverStoreError(Exception):
    """A request failure with the HTTP status the endpoint should return."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _validate_type(dtype):
    if not isinstance(dtype, str) or not TYPE_RE.match(dtype):
        raise DriverStoreError(400, "Invalid driver type '%s'" % dtype)
    if dtype in RESERVED_TYPES:
        raise DriverStoreError(
            400, "'%s' is the name of a built-in net role or lager command" % dtype)


def _root():
    return os.path.normpath(DRIVERS_DIR)


def driver_dir(dtype):
    root = _root()
    path = os.path.normpath(os.path.join(root, dtype))
    if not path.startswith(root + os.sep):
        raise DriverStoreError(400, "Invalid driver type '%s'" % dtype)
    return path


def has(dtype):
    """True when a driver of this type is installed (loadable or not).

    Cheap enough for every /net/command: one stat, no parsing.
    """
    if not isinstance(dtype, str) or not TYPE_RE.match(dtype):
        return False
    root = _root()
    path = os.path.normpath(os.path.join(root, dtype, MANIFEST_FILE))
    if not path.startswith(root + os.sep):
        return False
    return os.path.isfile(path)


def _read_sha(path):
    try:
        with open(os.path.join(path, _SHA_FILE), 'r', encoding='ascii') as f:
            return f.read().strip() or None
    except OSError:
        return None


def _describe(dtype):
    """The listing entry for one installed driver directory."""
    root = _root()
    path = os.path.normpath(os.path.join(root, dtype))
    if not path.startswith(root + os.sep):
        raise DriverStoreError(400, "Invalid driver type '%s'" % dtype)
    entry = {'type': dtype, 'sha256': _read_sha(path)}
    try:
        manifest = load_manifest(path)
        if manifest['type'] != dtype:
            raise ManifestError("manifest type '%s' does not match its directory '%s'"
                                % (manifest['type'], dtype))
        if not os.path.isfile(os.path.join(path, DRIVER_FILE)):
            raise ManifestError('%s is missing' % DRIVER_FILE)
    except ManifestError as e:
        entry.update({'status': 'error', 'error': str(e), 'version': None,
                      'description': '', 'manifest': None})
        return entry
    entry.update({'status': 'ok', 'version': manifest['version'],
                  'description': manifest['description'], 'manifest': manifest})
    return entry


def list_drivers():
    """Every installed driver, sorted by type."""
    if not os.path.isdir(DRIVERS_DIR):
        return []
    out = []
    for name in sorted(os.listdir(DRIVERS_DIR)):
        if name.startswith('.') or not os.path.isdir(driver_dir(name)):
            continue
        out.append(_describe(name))
    return out


def get_driver(dtype):
    """The listing entry for a loadable driver.

    Raises DriverUnavailable when it is missing or failed to load, so a net
    command on it answers 503 with the reason.
    """
    if not has(dtype):
        raise DriverUnavailable("No custom driver '%s' is installed on this box" % dtype)
    entry = _describe(dtype)
    if entry['status'] != 'ok':
        raise DriverUnavailable("Custom driver '%s' failed to load: %s"
                                % (dtype, entry['error']))
    return entry


def _bundle_root(names):
    """The prefix inside the zip that holds the manifest.

    Accepts a zip of the bundle's contents ('' prefix) or of the bundle
    folder itself ('stepper/' prefix), since both are what people make.
    """
    if MANIFEST_FILE in names:
        return ''
    tops = {n.split('/', 1)[0] for n in names}
    if len(tops) == 1:
        top = tops.pop()
        if '%s/%s' % (top, MANIFEST_FILE) in names:
            return top + '/'
    raise DriverStoreError(400, 'Bundle has no %s at its top level' % MANIFEST_FILE)


def _extract(data, dest):
    """Unpack a bundle zip into dest, refusing anything that escapes it."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise DriverStoreError(400, 'Bundle is not a zip file')

    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        if len(infos) > MAX_FILES:
            raise DriverStoreError(413, 'Bundle has more than %d files' % MAX_FILES)
        if sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
            raise DriverStoreError(413, 'Bundle unpacks to more than %d MB'
                                   % (MAX_UNPACKED_BYTES // (1024 * 1024)))

        names = [i.filename for i in infos]
        for name in names:
            norm = posixpath.normpath(name)
            if name.startswith('/') or '\\' in name or norm == '..' \
                    or norm.startswith('../') or ':' in name.split('/', 1)[0]:
                raise DriverStoreError(400, "Bundle path '%s' is not allowed" % name)
        for info in infos:
            if stat.S_ISLNK(info.external_attr >> 16):
                raise DriverStoreError(400, "Bundle entry '%s' is a symlink" % info.filename)

        prefix = _bundle_root(names)
        for info in infos:
            if not info.filename.startswith(prefix):
                continue
            rel = info.filename[len(prefix):]
            if not rel:
                continue
            target = os.path.normpath(os.path.join(dest, *rel.split('/')))
            if not target.startswith(dest + os.sep):
                raise DriverStoreError(400, "Bundle path '%s' is not allowed" % info.filename)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with archive.open(info) as src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out)


def install(dtype, data):
    """Install or replace a driver from its zipped bundle.

    Returns {"type", "version", "sha256", "replaced"}.
    """
    _validate_type(dtype)
    if not data:
        raise DriverStoreError(400, 'Bundle is empty')
    if len(data) > MAX_BUNDLE_BYTES:
        raise DriverStoreError(413, 'Bundle is larger than %d MB'
                               % (MAX_BUNDLE_BYTES // (1024 * 1024)))

    root = _root()
    staging_root = os.path.join(root, '.staging')
    os.makedirs(staging_root, exist_ok=True)
    staging = os.path.normpath(os.path.join(staging_root, '%s-%s' % (dtype, uuid.uuid4().hex)))
    if not staging.startswith(staging_root + os.sep):
        raise DriverStoreError(400, "Invalid driver type '%s'" % dtype)
    live = os.path.normpath(os.path.join(root, dtype))
    if not live.startswith(root + os.sep):
        raise DriverStoreError(400, "Invalid driver type '%s'" % dtype)
    os.makedirs(staging)
    try:
        _extract(data, staging)
        try:
            manifest = load_manifest(staging)
        except ManifestError as e:
            raise DriverStoreError(400, str(e))
        if manifest['type'] != dtype:
            raise DriverStoreError(
                400, "Bundle declares type '%s', not '%s'" % (manifest['type'], dtype))
        if not os.path.isfile(os.path.join(staging, DRIVER_FILE)):
            raise DriverStoreError(400, 'Bundle has no %s' % DRIVER_FILE)

        sha = hashlib.sha256(data).hexdigest()
        with open(os.path.join(staging, _SHA_FILE), 'w', encoding='ascii') as f:
            f.write(sha + '\n')

        replaced = os.path.isdir(live)
        retired = None
        if replaced:
            retired = os.path.normpath(
                os.path.join(staging_root, '%s-old-%s' % (dtype, uuid.uuid4().hex)))
            if not retired.startswith(staging_root + os.sep):
                raise DriverStoreError(400, "Invalid driver type '%s'" % dtype)
            os.rename(live, retired)
        os.rename(staging, live)
        staging = None
        if retired:
            shutil.rmtree(retired, ignore_errors=True)
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)

    return {'type': dtype, 'version': manifest['version'], 'sha256': sha,
            'replaced': replaced}


def remove(dtype, nets_using=(), force=False):
    """Remove an installed driver.

    Refuses (409) while nets use it, unless force: those nets would answer
    503 on every call until the driver came back.
    """
    _validate_type(dtype)
    root = _root()
    path = os.path.normpath(os.path.join(root, dtype))
    if not path.startswith(root + os.sep):
        raise DriverStoreError(400, "Invalid driver type '%s'" % dtype)
    if not os.path.isdir(path):
        raise DriverStoreError(404, "No custom driver '%s' is installed" % dtype)
    nets_using = sorted(nets_using)
    if nets_using and not force:
        raise DriverStoreError(
            409, "Driver '%s' is used by %s: %s. Delete those nets first, or "
                 "remove it anyway with `lager drivers remove --force`"
                 % (dtype, 'a net' if len(nets_using) == 1 else 'nets',
                    ', '.join(nets_using)))
    shutil.rmtree(path)
    return {'type': dtype, 'removed': True, 'orphaned_nets': nets_using}
