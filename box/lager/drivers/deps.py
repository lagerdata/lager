# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.drivers.deps - Python packages a custom driver declares.

A bundle names a requirements file in its manifest::

    "requirements": "requirements.txt"

The file is a complete lock: every package, including each dependency of a
dependency, pinned with ``==`` and at least one ``--hash``. It is what
``pip-compile --generate-hashes`` writes. Packages install at
``lager drivers install`` time into ``<bundle>/.deps``, under /etc/lager, so
they survive ``lager update`` and container rebuilds, and a call never waits
on pip.

Isolation:
  * ``.deps`` is appended to sys.path in the runner child only (loader.py),
    after the box's own site-packages. The box's packages always win.
  * So a pin that differs from a package the box already has would be
    silently ignored at runtime. install() refuses it instead. A pin equal
    to the box's version is dropped: the box already provides it.
  * Each call is a fresh child that adds only its own driver's ``.deps``, so
    two drivers never see each other's packages.

pip runs with ``--only-binary=:all:``: no sdist is built, so no package code
runs at install time. Package code still runs, with the box's privileges,
when the driver imports it. ``--no-deps`` keeps pip from re-fetching
packages the box provides (``--target`` ignores what is installed);
check_complete() then verifies the lock covers every dependency.

A ``wheels/`` folder in the bundle is offered to pip with ``--find-links``,
so a box without internet can install vendored wheels. Set
LAGER_DRIVERS_OFFLINE=1 to stop pip reaching for an index at all.

``.deps/.lager-deps.json`` records the interpreter and CPU the packages were
installed for. A compiled wheel is only valid for those, so after an image
upgrade changes either, the registry lists the driver as "error" until its
dependencies are reinstalled.
"""

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
import tempfile

try:
    from packaging.requirements import InvalidRequirement, Requirement
except ImportError:  # pragma: no cover - pip always vendors packaging
    from pip._vendor.packaging.requirements import InvalidRequirement, Requirement

DEPS_DIR = '.deps'
WHEELS_DIR = 'wheels'
STAMP_FILE = '.lager-deps.json'

DEPS_TIMEOUT_S = 300
MAX_DEPS_BYTES = 200 * 1024 * 1024
# Lines of pip output returned to the user when it fails.
PIP_TAIL_LINES = 20

_NAME_RE = re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[A-Za-z0-9._,\s-]*\])?$')
_HASH_RE = re.compile(r'^sha(256|384|512):[0-9a-fA-F]+$')
_NETWORK_HINTS = ('NewConnectionError', 'Failed to establish a new connection',
                  'Temporary failure in name resolution', 'Name or service not known',
                  'Network is unreachable', 'ConnectTimeoutError', 'ProxyError')


class DepsError(Exception):
    """A dependency install failure with the HTTP status to return."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def normalize(name):
    """PEP 503 name: lower case, runs of -_. become one '-'."""
    return re.sub(r'[-_.]+', '-', name).lower()


def _logical_lines(text):
    """Requirement lines with comments stripped and '\\' continuations joined."""
    buf = ''
    for raw in text.splitlines():
        line = raw.split(' #', 1)[0].rstrip() if not raw.lstrip().startswith('#') else ''
        if line.endswith('\\'):
            buf += line[:-1] + ' '
            continue
        buf += line
        if buf.strip():
            yield buf.strip()
        buf = ''
    if buf.strip():
        yield buf.strip()


def parse_requirements(text):
    """Parse a hashed lock file.

    Returns a list of {"name", "version", "line"}: name normalised, line the
    whole requirement with its hashes, ready to hand back to pip. Raises
    DepsError(400) for anything that is not a pinned, hashed requirement:
    pip options (-r, -e, --index-url ...) would let a bundle reach outside
    itself, and an unpinned line would install whatever is newest.
    """
    out = []
    seen = set()
    for line in _logical_lines(text):
        if line.startswith('-'):
            raise DepsError(400, "requirements: '%s' is not allowed; list only pinned "
                                 "packages with --hash" % line.split()[0])
        spec_tokens, hashes = [], []
        tokens = iter(line.split())
        for tok in tokens:
            # "--hash=sha256:..." or "--hash sha256:..."
            if tok == '--hash':
                tok = '--hash=' + next(tokens, '')
            if tok.startswith('--hash='):
                if not _HASH_RE.match(tok[len('--hash='):]):
                    raise DepsError(400, "requirements: bad hash '%s'" % tok)
                hashes.append(tok)
            elif tok.startswith('-'):
                raise DepsError(400, "requirements: option '%s' is not allowed" % tok)
            else:
                spec_tokens.append(tok)
        spec = ' '.join(spec_tokens)
        try:
            req = Requirement(spec)
        except InvalidRequirement:
            raise DepsError(400, "requirements: cannot parse '%s'" % spec)
        if not _NAME_RE.match(req.name + ('[%s]' % ','.join(sorted(req.extras))
                                          if req.extras else '')):
            raise DepsError(400, "requirements: bad package name '%s'" % req.name)
        if req.url:
            raise DepsError(400, "requirements: '%s' is a URL; list a pinned version" % req.name)
        pins = [s for s in req.specifier if s.operator in ('==', '===')]
        if len(pins) != 1 or len(req.specifier) != 1 or '*' in pins[0].version:
            raise DepsError(400, "requirements: '%s' must be pinned to one exact version "
                                 "(name==X.Y.Z)" % req.name)
        if not hashes:
            raise DepsError(400, "requirements: '%s' needs at least one --hash=sha256:... "
                                 "(generate the file with pip-compile --generate-hashes)"
                            % req.name)
        if req.marker is not None and not req.marker.evaluate():
            continue
        name = normalize(req.name)
        if name in seen:
            raise DepsError(400, "requirements: '%s' is listed twice" % req.name)
        seen.add(name)
        out.append({'name': name, 'version': pins[0].version,
                    'line': ' '.join([spec] + hashes)})
    return out


def box_packages():
    """{normalised name: version} of every distribution in the box's environment."""
    out = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get('Name')
        if name:
            out.setdefault(normalize(name), dist.version)
    return out


def split_against_box(reqs, provided):
    """Split requirements into those to install and those the box provides.

    Raises DepsError(400) for a pin that differs from the box's version: the
    box's copy is first on sys.path, so the pinned one would never load.
    """
    install, skipped = [], []
    conflicts = []
    for r in reqs:
        have = provided.get(r['name'])
        if have is None:
            install.append(r)
        elif have == r['version']:
            skipped.append(r)
        else:
            conflicts.append('%s==%s (the box has %s)' % (r['name'], r['version'], have))
    if conflicts:
        raise DepsError(400, "requirements conflict with packages the box provides, which "
                             "always load first: %s. Pin the box's version or remove the "
                             "line." % ', '.join(conflicts))
    return install, skipped


def check_complete(target, provided):
    """Verify every installed package's own requirements are satisfied.

    pip ran with --no-deps, so a lock missing a transitive dependency would
    otherwise install cleanly and fail on import. Extras-only requirements
    are skipped; version ranges are checked.
    """
    installed = {}
    dists = list(importlib.metadata.distributions(path=[target]))
    for dist in dists:
        installed[normalize(dist.metadata.get('Name') or '')] = dist.version
    available = dict(provided)
    available.update(installed)
    missing = []
    for dist in dists:
        for spec in dist.requires or ():
            try:
                req = Requirement(spec)
            except InvalidRequirement:
                continue
            if req.marker is not None:
                try:
                    if not req.marker.evaluate({'extra': ''}):
                        continue
                except Exception:  # an unknown marker variable: not ours to judge
                    continue
            have = available.get(normalize(req.name))
            if have is None or (req.specifier and not req.specifier.contains(have, prereleases=True)):
                missing.append('%s needs %s%s' % (dist.metadata.get('Name'), req.name,
                                                  ' (have %s)' % have if have else ''))
    if missing:
        raise DepsError(400, 'requirements file is incomplete: %s. Regenerate it with '
                             'pip-compile --generate-hashes.' % '; '.join(sorted(missing)))


def runtime_tag():
    """The interpreter and CPU a compiled wheel must match."""
    return {'python': 'cp%d%d' % sys.version_info[:2], 'machine': platform.machine()}


def _dir_bytes(path):
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def _pip_failure(output):
    lines = [ln for ln in output.splitlines() if ln.strip()]
    tail = '\n'.join(lines[-PIP_TAIL_LINES:])
    hint = ''
    if any(h in output for h in _NETWORK_HINTS):
        hint = (' The box could not reach the package index: put the wheels in the '
                "bundle's wheels/ folder, or give the box network access.")
    elif 'No matching distribution' in output or 'Could not find a version' in output:
        hint = (' A package has no wheel for this box (Python %s, %s): vendor one built '
                "for it in the bundle's wheels/ folder." % (runtime_tag()['python'],
                                                           runtime_tag()['machine']))
    elif 'THESE PACKAGES DO NOT MATCH THE HASHES' in output:
        hint = ' A downloaded file does not match its --hash in the requirements file.'
    return 'pip could not install the requirements.%s\n%s' % (hint, tail)


def install(bundle, requirements_name, target):
    """Install a bundle's requirements into ``target`` (a new directory).

    ``bundle`` holds the requirements file and an optional wheels/ folder.
    Returns the stamp written to ``target``. Raises DepsError; ``target`` is
    left for the caller to discard.
    """
    bundle = os.path.normpath(bundle)
    req_path = os.path.normpath(os.path.join(bundle, requirements_name))
    if not req_path.startswith(bundle + os.sep):
        raise DepsError(400, "requirements path '%s' is not allowed" % requirements_name)
    try:
        with open(req_path, 'r', encoding='utf-8') as f:
            text = f.read()
    except FileNotFoundError:
        raise DepsError(400, 'Bundle has no %s, which its manifest names as requirements'
                        % requirements_name)
    except (OSError, UnicodeDecodeError):
        raise DepsError(400, '%s is not a readable text file' % requirements_name)

    provided = box_packages()
    to_install, skipped = split_against_box(parse_requirements(text), provided)
    os.makedirs(target)

    if to_install:
        with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False,
                                         encoding='utf-8') as lock:
            lock.write('\n'.join(r['line'] for r in to_install) + '\n')
        cmd = [sys.executable, '-I', '-m', 'pip', 'install', '--target', target,
               '--only-binary=:all:', '--require-hashes', '--no-deps', '--no-compile',
               '--disable-pip-version-check', '--no-input', '--progress-bar', 'off',
               '--no-warn-script-location']
        wheels = os.path.normpath(os.path.join(bundle, WHEELS_DIR))
        if wheels.startswith(bundle + os.sep) and os.path.isdir(wheels):
            cmd += ['--find-links', wheels]
        if os.environ.get('LAGER_DRIVERS_OFFLINE', '').lower() in ('1', 'true', 'yes'):
            cmd.append('--no-index')
        cmd += ['-r', lock.name]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, timeout=DEPS_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            raise DepsError(504, 'Installing the requirements took longer than %d s and was '
                                 'stopped' % DEPS_TIMEOUT_S)
        finally:
            os.unlink(lock.name)
        if proc.returncode != 0:
            raise DepsError(400, _pip_failure(proc.stdout or ''))
        if _dir_bytes(target) > MAX_DEPS_BYTES:
            raise DepsError(413, 'The requirements install to more than %d MB'
                            % (MAX_DEPS_BYTES // (1024 * 1024)))
        check_complete(target, provided)

    stamp = {
        **runtime_tag(),
        'requirements_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'packages': sorted('%s==%s' % (r['name'], r['version']) for r in to_install),
        'provided_by_box': sorted(r['name'] for r in skipped),
    }
    with open(os.path.join(target, STAMP_FILE), 'w', encoding='utf-8') as f:
        json.dump(stamp, f, indent=2, sort_keys=True)
    return stamp


def status(bundle):
    """The listing's ``deps`` field for an installed bundle that declares requirements.

    Returns (summary, error): error is None when the packages are usable.
    """
    stamp_path = os.path.join(bundle, DEPS_DIR, STAMP_FILE)
    try:
        with open(stamp_path, 'r', encoding='utf-8') as f:
            stamp = json.load(f)
    except (OSError, ValueError):
        return ({'status': 'missing'},
                'its Python packages are not installed; run `lager drivers reinstall-deps`')
    summary = {'status': 'ok', 'packages': stamp.get('packages') or [],
               'python': stamp.get('python'), 'machine': stamp.get('machine')}
    now = runtime_tag()
    if (stamp.get('python'), stamp.get('machine')) != (now['python'], now['machine']):
        summary['status'] = 'stale'
        return (summary,
                'its Python packages were installed for %s/%s and this box runs %s/%s; '
                'run `lager drivers reinstall-deps`'
                % (stamp.get('python'), stamp.get('machine'), now['python'], now['machine']))
    return summary, None
