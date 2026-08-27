# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    Box storage utilities for managing local box configurations
"""
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from .sort_utils import natural_sort_key


def get_lager_file_path() -> Path:
    """Get the path to the .lager file in home directory."""
    # Check for environment variable override
    if lager_config := os.getenv('LAGER_CONFIG_FILE_DIR'):
        return Path(lager_config) / '.lager'

    # Always use global config in home directory
    return Path.home() / '.lager'


def _load_boxes_from_file(path) -> Dict[str, any]:
    """Load boxes from a single .lager file path.

    Args:
        path: Path (str or Path) to a .lager file

    Returns a dict where values can be either:
    - str: IP address (legacy format)
    - dict: {"ip": str, "user": str} (new format)
    """
    path = Path(path) if not isinstance(path, Path) else path
    if not path.exists():
        return {}

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get('BOXES') or data.get('DUTS') or data.get('duts', {})
    except (json.JSONDecodeError, FileNotFoundError):
        return {}


def _load_global_boxes() -> Dict[str, any]:
    """Load boxes from only the global ~/.lager file.

    Used by write operations to avoid leaking project boxes into global storage.
    """
    return _load_boxes_from_file(get_lager_file_path())


def load_boxes() -> Dict[str, any]:
    """Load boxes from global and project-level .lager files.

    Merges boxes from all discovered .lager files. Project-level boxes
    (closest to cwd) take precedence over global boxes.

    Returns a dict where values can be either:
    - str: IP address (legacy format)
    - dict: {"ip": str, "user": str} (new format)
    """
    from .config import _find_config_files

    # Start with global boxes
    merged = _load_global_boxes()

    # Overlay project-level boxes (closest file wins, so apply farthest first)
    try:
        project_configs = _find_config_files()
    except (FileNotFoundError, OSError):
        # cwd may have been deleted (e.g., rm -rf while still cd'd into it)
        project_configs = []
    for config_path in reversed(project_configs):
        project_boxes = _load_boxes_from_file(config_path)
        merged.update(project_boxes)

    return merged


def save_boxes(boxes: Dict[str, str]) -> None:
    """Save boxes to the .lager file, preserving all existing data."""
    lager_file = get_lager_file_path()

    # Load existing data or create new structure
    data = {}
    if lager_file.exists():
        try:
            with open(lager_file, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if not content:
                    data = {}
                elif content[0] in ('{', '['):
                    # JSON format - migrate legacy keys to new format
                    data = json.loads(content)
                    # Migrate legacy lowercase keys to uppercase
                    if 'duts' in data:
                        # Migrate legacy 'duts' to 'BOXES'
                        data['BOXES'] = data.pop('duts')
                    if 'DUTS' in data:
                        # Migrate 'DUTS' to 'BOXES'
                        data['BOXES'] = data.pop('DUTS')
                    if 'nets' in data:
                        data['NETS'] = data.pop('nets')
                    if 'devenv' in data:
                        data['DEVENV'] = data.pop('devenv')
                    if 'LAGER' in data:
                        data['DEFAULTS'] = data.pop('LAGER')
                else:
                    # INI format - convert to JSON preserving all sections
                    from .config import read_config_file, _configparser_to_json
                    config = read_config_file(str(lager_file))
                    data = _configparser_to_json(config)
        except (json.JSONDecodeError, Exception):
            # If we can't parse it, start fresh
            data = {}

    # Use new BOXES key
    data['BOXES'] = boxes

    with open(lager_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def add_box(name: str, ip: str, user: Optional[str] = None, version: Optional[str] = None) -> None:
    """Add a box to the local storage.

    Args:
        name: Box name
        ip: IP address
        user: Optional username (if None and version is None, stores in legacy format)
        version: Optional version/branch name (e.g., "staging", "main")
    """
    boxes = _load_global_boxes()
    if user or version:
        # New format with user and/or version
        box_dict = {"ip": ip}
        if user:
            box_dict["user"] = user
        if version:
            box_dict["version"] = version
        boxes[name] = box_dict
    else:
        # Legacy format (just IP string)
        boxes[name] = ip
    save_boxes(boxes)


def get_box_ip(name: str) -> Optional[str]:
    """Get the IP address for a named box."""
    boxes = load_boxes()
    box_info = boxes.get(name)
    if isinstance(box_info, dict):
        # Dict format: extract IP
        return box_info.get("ip")
    elif isinstance(box_info, str):
        # Legacy format: just the IP
        return box_info
    return None


def get_box_user(name: str) -> Optional[str]:
    """Get the username for a named box.

    Args:
        name: Box name

    Returns:
        Username if stored, None otherwise (will use default)
    """
    boxes = load_boxes()
    box_info = boxes.get(name)
    if isinstance(box_info, dict):
        return box_info.get("user")
    # Legacy format (string IP) has no username
    return None


def get_box_version(name: str) -> Optional[str]:
    """Get the version for a named box.

    Args:
        name: Box name

    Returns:
        Version if stored, None otherwise
    """
    boxes = load_boxes()
    box_info = boxes.get(name)
    if isinstance(box_info, dict):
        return box_info.get("version")
    # Legacy format (string IP) has no version
    return None


def update_box_version(name: str, version: str) -> bool:
    """Update the version for a named box.

    Only updates boxes in the global ~/.lager file.

    Args:
        name: Box name
        version: Version/branch name (e.g., "staging", "main")

    Returns:
        True if updated, False if box not found in global config
    """
    boxes = _load_global_boxes()
    if name not in boxes:
        return False

    box_info = boxes[name]
    if isinstance(box_info, dict):
        # Update version in existing dict
        box_info["version"] = version
    else:
        # Upgrade from legacy format to dict format
        boxes[name] = {"ip": box_info, "version": version}

    save_boxes(boxes)
    return True


def get_box_name_by_ip(ip: str) -> Optional[str]:
    """Reverse lookup: find box name by IP address.

    Args:
        ip: IP address to lookup

    Returns:
        Box name if found, None otherwise
    """
    boxes = load_boxes()
    for name, box_info in boxes.items():
        box_ip = None
        if isinstance(box_info, dict):
            box_ip = box_info.get("ip")
        elif isinstance(box_info, str):
            box_ip = box_info

        if box_ip == ip:
            return name
    return None


def delete_box(name: str) -> bool:
    """Delete a box from the global storage. Returns True if deleted, False if not found.

    Global only, deliberately — see :func:`_load_global_boxes`. A name that
    is ALSO defined in a project ``.lager`` still resolves after this returns
    True, so callers that report the deletion to a user should ask
    :func:`project_files_defining_box` what survived.
    """
    boxes = _load_global_boxes()
    if name in boxes:
        del boxes[name]
        save_boxes(boxes)
        return True
    return False


def project_files_defining_box(name: str) -> List[Path]:
    """Project-level ``.lager`` files (not the global one) that define ``name``.

    Reads are merged across the global file and every ``.lager`` found walking
    up from the cwd, but writes only ever touch the global one. That asymmetry
    is intentional — a project's boxes must not leak into global storage — but
    it means "deleted" and "gone" are different things, and saying the first
    while meaning the second is how `lager uninstall` came to report
    ``Removed 'STG-2' from .lager config`` about a box that was still there.
    """
    from .config import _find_config_files

    try:
        candidates = _find_config_files()
    except (FileNotFoundError, OSError):
        # cwd may have been deleted out from under us (rm -rf while cd'd in).
        return []
    return [
        Path(path) for path in candidates
        if name in _load_boxes_from_file(path)
    ]


def list_boxes() -> Dict[str, str]:
    """List all stored boxes."""
    return load_boxes()


def delete_all_boxes() -> int:
    """Delete all boxes from the global storage. Returns the number of boxes deleted."""
    boxes = _load_global_boxes()
    count = len(boxes)
    save_boxes({})
    return count


def get_lager_user():
    """Get the effective lager user.

    Resolution order:
    1. LAGER_USER environment variable
    2. 'user' from 'lager defaults add --user' (stored in ~/.lager)
    3. OS system username (getpass.getuser())
    """
    import getpass
    from .config import read_config_file

    if env_user := os.getenv('LAGER_USER'):
        return env_user

    try:
        config = read_config_file()
        if config.has_option('LAGER', 'user'):
            return config.get('LAGER', 'user')
    except Exception:
        pass
    return getpass.getuser()


def format_lock_user(user):
    """Format a lock user string for display.

    Recognized formats:
    - ``<origin>:<id>:<email>``                   -> just the email
      (reservations written by other services, e.g. the web dashboard)
    - ``ci:github:<repo>#<run>-<attempt>/<job>@<runner>:<pid>``
                                                  -> ``github <repo> run <run> job <job> on <runner>``
    - ``ci:drone:<repo>#<build>:<pid>@<host>``    -> ``drone <repo> build <build>``
    - ``ci:gitlab:<project>#<pipeline>/<job>:<pid>@<host>``
                                                  -> ``gitlab <project> pipeline <pipeline> job <job>``
    - ``ci:bitbucket:<repo>#<build>:<pid>@<host>``
                                                  -> ``bitbucket <repo> build <build>``
    - ``ci:jenkins:<tag>:<pid>@<host>``           -> ``jenkins <tag>``
    - ``ci:generic:<host>:<pid>``                 -> ``ci on <host>``

    Falls back to returning the raw string unchanged for anything we don't
    recognise so we never hide unexpected holders.
    """
    if not user:
        return user

    if user.startswith('ci:'):
        parts = user.split(':', 2)
        if len(parts) < 3:
            return user
        provider = parts[1]
        rest = parts[2]
        try:
            if provider == 'github':
                # <repo>#<run>-<attempt>/<job>@<runner>:<pid>
                # NOTE: <repo> can contain `/` (e.g. "lager/lager"), so we
                # split on `#` *first* to lift the repo out, then `/` on the
                # remainder to separate run/attempt from job@runner.
                run_part, _, _pid = rest.rpartition(':')
                repo, _, after_hash = run_part.partition('#')
                run_attempt, _, job_runner = after_hash.partition('/')
                run_id, _, _attempt = run_attempt.partition('-')
                job, _, runner = job_runner.partition('@')
                if not repo.strip() or not run_id.strip():
                    # Malformed holder: partition() never raises, so without
                    # this check we'd render garbage like 'github  run '
                    # instead of falling back to the raw string.
                    return user
                bits = ['github', repo.strip(), f'run {run_id.strip()}']
                if job:
                    bits.append(f'job {job.strip()}')
                if runner:
                    bits.append(f'on {runner.strip()}')
                return ' '.join(b for b in bits if b)
            if provider == 'drone':
                # <repo>#<build>:<pid>@<host>
                build_part, _, _suffix = rest.partition(':')
                repo, _, build = build_part.partition('#')
                return f'drone {repo} build {build}' if build else f'drone {repo}'
            if provider == 'gitlab':
                # <project>#<pipeline>/<job>:<pid>@<host>
                pipeline_part, _, _suffix = rest.partition(':')
                project_pipeline, _, job = pipeline_part.partition('/')
                project, _, pipeline = project_pipeline.partition('#')
                bits = ['gitlab', project, f'pipeline {pipeline}' if pipeline else '']
                if job:
                    bits.append(f'job {job}')
                return ' '.join(b for b in bits if b)
            if provider == 'bitbucket':
                build_part, _, _suffix = rest.partition(':')
                repo, _, build = build_part.partition('#')
                return f'bitbucket {repo} build {build}' if build else f'bitbucket {repo}'
            if provider == 'jenkins':
                tag, _, _suffix = rest.partition(':')
                return f'jenkins {tag}' if tag else 'jenkins'
            if provider == 'generic':
                host, _, _pid = rest.partition(':')
                return f'ci on {host}' if host else 'ci'
        except Exception:  # pylint: disable=broad-except
            return user

    # Reservation holders written by other services (e.g. the web dashboard)
    # look like ``<origin>:<id>:<email>``; show just the email. The ``ci:``
    # prefix is excluded above, and requiring an ``@`` keeps genuinely
    # unrecognized strings visible unchanged.
    if not user.startswith('ci:'):
        parts = user.split(':', 2)
        if len(parts) == 3 and '@' in parts[2]:
            return parts[2]

    return user


def get_lock_holder():
    """Get a unique-per-process lock holder identity.

    Resolution order:
    1. ``LAGER_LOCK_HOLDER`` env var (explicit override, e.g. for tests that
       intentionally share an identity across matrix items).
    2. CI-aware identity derived from the detected CI environment. The string
       always ends with ``:<pid>`` (and ``@<host>`` outside GitHub, which has
       ``RUNNER_NAME``) so concurrent matrix items can never accidentally
       collide on the same holder.
    3. Dev fallback: ``get_lager_user()``.
    """
    import socket

    override = os.getenv('LAGER_LOCK_HOLDER')
    if override:
        return override

    try:
        from .context.ci_detection import get_ci_environment, CIEnvironment
    except Exception:  # pylint: disable=broad-except
        return get_lager_user()

    env = get_ci_environment()
    if env == CIEnvironment.HOST:
        return get_lager_user()

    pid = os.getpid()
    host = socket.gethostname()

    if env == CIEnvironment.GITHUB:
        repo = os.getenv('GITHUB_REPOSITORY', 'unknown')
        run_id = os.getenv('GITHUB_RUN_ID', '0')
        attempt = os.getenv('GITHUB_RUN_ATTEMPT', '1')
        job = os.getenv('GITHUB_JOB', 'job')
        runner = os.getenv('RUNNER_NAME', host)
        return f'ci:github:{repo}#{run_id}-{attempt}/{job}@{runner}:{pid}'

    if env == CIEnvironment.DRONE:
        repo = os.getenv('DRONE_REPO', 'unknown')
        build = os.getenv('DRONE_BUILD_NUMBER', '0')
        return f'ci:drone:{repo}#{build}:{pid}@{host}'

    if env == CIEnvironment.GITLAB:
        project = os.getenv('CI_PROJECT_PATH', os.getenv('CI_PROJECT_NAME', 'unknown'))
        pipeline = os.getenv('CI_PIPELINE_ID', '0')
        job = os.getenv('CI_JOB_NAME', 'job')
        return f'ci:gitlab:{project}#{pipeline}/{job}:{pid}@{host}'

    if env == CIEnvironment.BITBUCKET:
        repo = os.getenv('BITBUCKET_REPO_FULL_NAME', os.getenv('BITBUCKET_REPO_SLUG', 'unknown'))
        build = os.getenv('BITBUCKET_BUILD_NUMBER', '0')
        return f'ci:bitbucket:{repo}#{build}:{pid}@{host}'

    if env == CIEnvironment.JENKINS:
        tag = os.getenv('BUILD_TAG', os.getenv('BUILD_NUMBER', '0'))
        return f'ci:jenkins:{tag}:{pid}@{host}'

    return f'ci:generic:{host}:{pid}'


# The pid that makes a holder unique per process sits in a different place
# depending on the provider (see ``get_lock_holder``): trailing for GitHub and
# generic, mid-string before ``@{host}`` for Drone, GitLab, Bitbucket and
# Jenkins. Both shapes are a ``:<digits>`` run that is either at the end of the
# string or immediately before an ``@``.
_LOCK_PID_RE = re.compile(r':\d+(?=@|$)')


def lock_scope(identity):
    """Return ``identity`` with the per-process part removed.

    Every ``lager`` invocation is a new process, so comparing raw holder
    strings means a CI job never recognises its own lock on its second
    command. Dropping the pid leaves the run/attempt/job/runner scope, which
    is what "the same job" means: two jobs of one run stay distinct, and so do
    two runs of one workflow.
    """
    if not identity:
        return identity
    return _LOCK_PID_RE.sub('', identity)


def _lock_held_by_self(locked_by):
    """Is ``locked_by`` a lock this process is entitled to use?

    There are two acquire paths writing two kinds of holder, and the check has
    to accept both: ``lager boxes lock`` (and ``test/framework/harness.sh``)
    stores a plain user, while auto-lock stores ``get_lock_holder()``'s CI
    identity. Comparing only against ``get_lager_user()`` is what refused a CI
    job its own auto-lock -- the two strings can never be equal in CI, and the
    bug is invisible on a dev machine because ``get_lock_holder()`` falls back
    to ``get_lager_user()`` there, making both arms the same string.
    """
    if lock_scope(locked_by) == lock_scope(get_lock_holder()):
        return True
    return locked_by == get_lager_user()


# Boxes we've already warned about missing :9000 lock support (old images);
# once per CLI process so every command doesn't repeat the warning.
_lock_check_unsupported_warned = set()


def _check_box_lock(ip, box_name):
    """Check if a box is locked by another user. Exits if locked.

    Args:
        ip: Box IP address
        box_name: Box name for display purposes
    """
    import click
    import requests

    try:
        resp = requests.get(f'http://{ip}:9000/lock', timeout=3, **_gateway_kwargs(ip))
        resp = _check_gateway(resp, ip)
        if resp.status_code == 404:
            # :9000 answered but has no /lock route: the box image predates
            # the lock API on :9000. Locks can't be enforced against it, so
            # say so (once per process per box) instead of silently skipping.
            if ip not in _lock_check_unsupported_warned:
                _lock_check_unsupported_warned.add(ip)
                display = box_name or ip
                click.secho(
                    f"Warning: Box '{display}' runs an old image without "
                    f"lock support on its :9000 API — lock checks are skipped. "
                    f"Run: lager update --box {display}",
                    fg='yellow', err=True,
                )
        elif resp.status_code == 200:
            data = resp.json()
            if data.get('locked'):
                locked_by = data.get('user', 'unknown')
                if not _lock_held_by_self(locked_by):
                    display = box_name or ip
                    display_user = format_lock_user(locked_by)
                    click.secho(
                        f"Error: Box '{display}' is locked by {display_user}",
                        fg='red', err=True,
                    )
                    click.echo(
                        f"To force unlock: lager boxes unlock --box {display} --force",
                        err=True,
                    )
                    raise SystemExit(1)
    except (requests.exceptions.RequestException, SystemExit) as e:
        if isinstance(e, SystemExit):
            raise
        # Box unreachable - silently skip, command will fail on its own


def acquire_command_lock_with_cleanup(ctx, ip, box_name, command_name, force=False):
    """Check user lock before running a command.

    Ephemeral command lock (busy lock) has been removed. This now only
    checks the explicit user lock (``lager boxes lock``).

    Args:
        ctx: Click context
        ip: Box IP address
        box_name: Box name for display purposes
        command_name: Name of the command being run (unused)
        force: Unused, kept for call-site compatibility
    """
    _check_box_lock(ip, box_name)


# ---------------------------------------------------------------------------
# Box-lock acquire/release used by `lager python` auto-locking
# ---------------------------------------------------------------------------


# Default wait-on-collision values:
#   - dev (HOST): 0 -> fail fast on collision
#   - CI:        1800 (30 min) -> queue/wait for the other CI run to finish
_DEFAULT_LOCK_WAIT_DEV = 0
_DEFAULT_LOCK_WAIT_CI = 1800
_DEFAULT_LOCK_TTL_SECONDS = 1800
_DEFAULT_HEARTBEAT_INTERVAL = 60


def default_lock_wait_seconds():
    """Default ``wait_seconds`` for :func:`acquire_box_lock`.

    ``LAGER_LOCK_WAIT`` env var wins. Otherwise CI gets a long wait so matrix
    jobs queue, and dev gets fail-fast so a typo doesn't silently block.
    """
    env = os.getenv('LAGER_LOCK_WAIT')
    if env is not None:
        try:
            return max(0, int(env))
        except ValueError:
            return _DEFAULT_LOCK_WAIT_DEV
    try:
        from .context.ci_detection import get_ci_environment, CIEnvironment
        if get_ci_environment() != CIEnvironment.HOST:
            return _DEFAULT_LOCK_WAIT_CI
    except Exception:  # pylint: disable=broad-except
        pass
    return _DEFAULT_LOCK_WAIT_DEV


def default_lock_ttl_seconds():
    """Default ``ttl_seconds`` for ephemeral test locks.

    ``LAGER_LOCK_TTL`` env var wins. ``None`` is encoded as the literal string
    ``"none"``/``"null"`` for callers that want eternal locks.
    """
    env = os.getenv('LAGER_LOCK_TTL')
    if env is None:
        return _DEFAULT_LOCK_TTL_SECONDS
    if env.lower() in ('none', 'null', ''):
        return None
    try:
        return max(1, int(env))
    except ValueError:
        return _DEFAULT_LOCK_TTL_SECONDS


def default_heartbeat_interval():
    """Default heartbeat interval in seconds."""
    env = os.getenv('LAGER_LOCK_HEARTBEAT')
    if env is None:
        return _DEFAULT_HEARTBEAT_INTERVAL
    try:
        return max(1, int(env))
    except ValueError:
        return _DEFAULT_HEARTBEAT_INTERVAL


def _gateway_kwargs(ip):
    """Bearer header for boxes behind an authenticating gateway; {} otherwise."""
    from .gateway_auth import auth_headers_for_box
    headers = auth_headers_for_box(ip)
    return {'headers': headers} if headers else {}


def _resend_with_auth(prepared, headers, *, timeout: Optional[float] = 30,
                      stream: bool = True, session=None):
    """Re-send an already-prepared request with extra headers merged in.
    Returns the new response, or None if the resend itself failed.

    ``timeout`` and ``stream`` MUST mirror the original call. Replaying a
    streaming request with ``stream=False`` blocks inside ``send()``
    buffering a body that never ends — the debug service's RTT endpoint
    streams until interrupted, so the read timeout never fires while the
    target is emitting. ``session`` lets a caller that owns a long-lived
    Session hand it in, so a retried stream stays on that connection pool
    rather than on a throwaway that goes out of scope mid-stream.

    ``stream`` defaults to True because that is the safe direction: a
    buffered caller reads ``.json()``/``.text`` off a streamed response
    identically, whereas a streaming caller replayed with ``stream=False``
    hangs. Callers that know they are buffered may pass ``stream=False``.

    Only replayable bodies are safe here: ``prepared.copy()`` cannot rewind
    a file-like or multipart body. Every current caller sends JSON.
    """
    import requests
    req = prepared.copy()
    for key, value in headers.items():
        req.headers[key] = value
    try:
        return (session or requests.Session()).send(req, timeout=timeout, stream=stream)
    except requests.RequestException:
        return None


def _resolve_gateway(resp, ip, *, timeout: Optional[float] = 30,
                     stream: bool = True, session=None):
    """Record-and-retry core shared by :func:`_check_gateway` and
    :func:`check_gateway_status` — the single implementation of gateway
    discovery. Returns ``(resp, denied)``:

    - Non-gateway responses (no discovery header) pass through untouched
      with ``denied=False`` — plain boxes are never affected.
    - First contact — we sent no token (the box→auth-server link is only
      learned from this very 401's discovery header) but we already hold a
      session for that server: record the mapping, attach the token, and
      retry the request once. On success the retried, authenticated response
      is returned with ``denied=False``. A plain box never receives the
      token, because only a gateway sends the discovery header.
    - Anything else is a genuine denial: the mapping is still recorded (so
      the next attempt authenticates) and ``denied=True`` tells the caller
      to handle ``resp`` as a gateway denial.

    ``timeout``/``stream``/``session`` are forwarded to the retry and must
    mirror the original call; see :func:`_resend_with_auth`. They exist for
    the debug service's streaming RTT endpoint, which cannot be replayed
    buffered.
    """
    from .gateway_auth import (
        DISCOVERY_HEADER, record_box_auth_server, auth_headers_for_box,
    )
    # getattr: tolerate response-shaped fakes without a headers attribute.
    resp_headers = getattr(resp, 'headers', None) or {}
    if not (resp.status_code in (401, 403, 503) and DISCOVERY_HEADER in resp_headers):
        return resp, False

    record_box_auth_server(ip, resp_headers[DISCOVERY_HEADER])
    sent_auth = 'Authorization' in getattr(resp.request, 'headers', {})
    if resp.status_code == 401 and not sent_auth:
        headers = auth_headers_for_box(ip)
        if headers:
            retried = _resend_with_auth(resp.request, headers, timeout=timeout,
                                        stream=stream, session=session)
            if retried is not None:
                gated = (retried.status_code in (401, 403, 503)
                         and DISCOVERY_HEADER in retried.headers)
                if not gated:
                    return retried, False   # transparently authenticated
                resp = retried              # still refused — report on the retry

    return resp, True


def _check_gateway(resp, ip, *, timeout: Optional[float] = 30,
                   stream: bool = True, session=None):
    """Resolve a gateway response, returning the response the caller should use.

    On a plain (un-gated) box this is a passthrough. On a gated box the
    first contact is retried transparently (see :func:`_resolve_gateway`);
    genuine denials (revoked session, no access grant, auth server down)
    raise the actionable `lager login` / "ask your admin" error as before.

    Callers should adopt the return value: ``resp = _check_gateway(resp, ip)``.

    ``timeout``/``stream``/``session`` are forwarded to the retry and must
    mirror the original call; see :func:`_resend_with_auth`. Existing callers
    pass none of them and keep the previous behavior.
    """
    from .gateway_auth import handle_gateway_denial
    resp, denied = _resolve_gateway(resp, ip, timeout=timeout,
                                    stream=stream, session=session)
    if denied:
        handle_gateway_denial(resp, ip)     # raises the actionable error
    return resp


def check_gateway_status(resp, ip):
    """Non-raising variant of :func:`_check_gateway` for fan-out and
    fail-open callers (`lager boxes`, health polls) that must not abort on a
    single box's denial.

    Performs the same record-and-retry as ``_check_gateway``. Returns
    ``(resp, label)``: ``label`` is None when the caller should simply use
    ``resp`` (plain box, or the retry authenticated transparently), else a
    short user-facing verdict — 'sign-in required', 'session rejected',
    'no access', or 'auth server down'.
    """
    from .gateway_auth import denial_label
    resp, denied = _resolve_gateway(resp, ip)
    if not denied:
        return resp, None
    return resp, denial_label(resp)


def _lock_url(ip, suffix=''):
    # Lock state is shared box-wide; both the :5000 and :9000 servers expose
    # it via lager.lock_state. The CLI talks to :9000 (the primary HTTP API).
    return f'http://{ip}:9000/lock{suffix}'


def acquire_box_lock(
    ip,
    box_name,
    holder,
    *,
    holder_type='ephemeral',
    ttl_seconds=_DEFAULT_LOCK_TTL_SECONDS,
    wait_seconds=0,
    poll=2.0,
    quiet=False,
):
    """Acquire the box lock for ``holder``.

    Returns ``(state, lock_data)`` where ``state`` is one of:
        - ``"acquired"``    -> we took the lock just now (caller owns it,
                               should release on exit).
        - ``"already_ours"`` -> the lock was already held by ``holder``
                               (e.g. a pre-existing ``lager boxes lock``).
                               Caller MUST NOT release on exit so the user's
                               persistent lock survives.

    On collision with a different holder:
        - if ``wait_seconds <= 0``: print an error and ``sys.exit(1)``.
        - otherwise: poll ``GET /lock`` every ``poll`` seconds until the lock
          is released, then retry. Fail (and exit) after ``wait_seconds``.

    ``holder_type`` and ``ttl_seconds`` are forwarded to the server. Older
    box versions that don't understand these fields will just ignore them.
    """
    import time
    import click
    import requests

    # Check the lock state BEFORE posting an acquire. If the box is already
    # locked by us (a pre-existing `lager boxes lock`), we must not touch it
    # at all: a re-acquire POST would let the server rewrite the lock's
    # holder_type/ttl (old servers store whatever we send; see the refresh
    # guard in box/lager/lock_state.py for the new ones), and against an old
    # server the 200-without-previous_user response below would misclassify
    # the pre-existing lock as freshly acquired — and release it on exit.
    try:
        pre = requests.get(_lock_url(ip), timeout=5, **_gateway_kwargs(ip))
        pre = _check_gateway(pre, ip)
        if pre.status_code == 200:
            try:
                pre_data = pre.json()
            except ValueError:
                pre_data = {}
            # Scope, not raw equality: the stored holder ends in the pid of
            # whichever process took the lock, and this is a later one.
            if pre_data.get('locked') and \
                    lock_scope(pre_data.get('user', '')) == lock_scope(holder):
                return ('already_ours', pre_data)
    except requests.exceptions.RequestException:
        # Unreachable for the GET; let the POST loop below produce the
        # canonical 'unreachable' result (or succeed if it was transient).
        pass

    payload = {'user': holder, 'holder_type': holder_type}
    if ttl_seconds is None:
        payload['ttl_seconds'] = None
    else:
        payload['ttl_seconds'] = int(ttl_seconds)

    display = box_name or ip
    deadline = time.monotonic() + max(0, wait_seconds)
    waited_message_printed = False

    while True:
        try:
            resp = requests.post(_lock_url(ip), json=payload, timeout=5, **_gateway_kwargs(ip))
            resp = _check_gateway(resp, ip)
        except requests.exceptions.RequestException as exc:
            if not quiet:
                click.secho(
                    f"Warning: box '{display}' did not answer the lock request: {exc}",
                    fg='yellow', err=True,
                )
            # Unreachable - fall through with no lock held; the actual command
            # will fail on its own with a clearer error.
            return ('unreachable', None)

        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            previous_holder = data.get('previous_user')
            if previous_holder is None:
                # Box doesn't echo previous_user (older server). The GET
                # check above already returned 'already_ours' for any
                # pre-existing lock of ours, so reaching a 200 here means
                # we genuinely created the lock.
                state = 'acquired'
            elif lock_scope(previous_holder) == lock_scope(holder):
                state = 'already_ours'
            else:
                state = 'acquired'
            return (state, data)

        if resp.status_code == 409:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            lock_info = data.get('lock', {}) or {}
            other = lock_info.get('user', 'unknown')

            # A 409 naming our own scope means the pre-acquire GET raced, or
            # the box did not report the lock on that GET. Waiting here blocks
            # for LAGER_LOCK_WAIT -- 1800s under CI -- on a lock this job
            # already holds, which is how a refusal became a half-hour stall.
            if lock_scope(other) == lock_scope(holder):
                return ('already_ours', lock_info or data)

            now = time.monotonic()
            if now >= deadline:
                if not quiet:
                    display_other = format_lock_user(other)
                    click.secho(
                        f"Error: Box '{display}' is locked by {display_other}",
                        fg='red', err=True,
                    )
                    if wait_seconds > 0:
                        click.echo(
                            f"Gave up after waiting {wait_seconds}s.", err=True,
                        )
                    click.echo(
                        f"To force unlock: lager boxes unlock --box {display} --force",
                        err=True,
                    )
                raise SystemExit(1)

            if not waited_message_printed and not quiet:
                remaining = int(deadline - now)
                display_other = format_lock_user(other)
                click.secho(
                    f"Box '{display}' is locked by {display_other}; waiting up to {remaining}s for release...",
                    fg='yellow', err=True,
                )
                waited_message_printed = True

            time.sleep(min(poll, max(0.1, deadline - time.monotonic())))
            continue

        # Any other status: bail out.
        if not quiet:
            click.secho(
                f"Error: Unexpected response acquiring lock on '{display}' (HTTP {resp.status_code})",
                fg='red', err=True,
            )
        raise SystemExit(1)


def release_box_lock(ip, holder, *, quiet=True):
    """Release the box lock held by ``holder``. Best-effort, never raises.

    Returns ``True`` if the server confirmed release, ``False`` otherwise.
    """
    import click
    import requests

    try:
        resp = requests.post(
            f'http://{ip}:9000/unlock',
            json={'user': holder},
            timeout=5,
            **_gateway_kwargs(ip),
        )
    except requests.exceptions.RequestException as exc:
        if not quiet:
            click.secho(
                f"Warning: the box at {ip} did not answer the lock release: {exc}",
                fg='yellow', err=True,
            )
        return False

    # Non-raising gateway check (this function promises never to raise);
    # a denial falls through to the failure warning below.
    resp, _gate_verdict = check_gateway_status(resp, ip)
    if resp.status_code == 200:
        return True
    if not quiet:
        try:
            data = resp.json()
            detail = data.get('error') or data
        except ValueError:
            detail = resp.text
        click.secho(
            f"Warning: Failed to release lock on {ip} (HTTP {resp.status_code}): {detail}",
            fg='yellow', err=True,
        )
    return False


def heartbeat_box_lock(ip, holder, *, quiet=True):
    """Refresh the lock's ``last_heartbeat`` on the box.

    Returns ``True`` on success, ``False`` on transport error or a server that
    doesn't know about heartbeats yet (404). Callers should treat ``False`` as
    "carry on" rather than "abort the test" — the box-side TTL is the
    authoritative reaper, and a heartbeat-less server simply means TTL/heartbeat
    isn't enforced server-side yet.
    """
    import click
    import requests

    try:
        resp = requests.post(
            _lock_url(ip, '/heartbeat'),
            json={'user': holder},
            timeout=5,
            **_gateway_kwargs(ip),
        )
    except requests.exceptions.RequestException as exc:
        if not quiet:
            click.secho(
                f"Warning: heartbeat to {ip} failed: {exc}",
                fg='yellow', err=True,
            )
        return False
    # Non-raising gateway check; a denial reads as a failed heartbeat, which
    # callers already treat as "carry on" (server TTL is authoritative).
    resp, _gate_verdict = check_gateway_status(resp, ip)
    return resp.status_code == 200


# ---------------------------------------------------------------------------
# Heartbeat thread + context manager for auto-locking around a command
# ---------------------------------------------------------------------------
#
# Used by `lager python` (the original site) and by the admin commands —
# `install`, `uninstall`, `update`, `install-wheel` — to keep the
# server-side TTL from reaping a still-running command.


import contextlib as _contextlib_for_lock
import threading as _threading_for_heartbeat


class HeartbeatThread(_threading_for_heartbeat.Thread):
    """Refreshes the box lock periodically while a command is running.

    Daemon thread so it dies with the CLI process. Stop by calling ``stop()``;
    that wakes the sleep so shutdown is prompt. ``join()`` is inherited from
    ``threading.Thread`` (used by unit tests and admin commands that want
    to wait for the heartbeat to finish before continuing).

    Heartbeat failures never abort the command — the server-side TTL is the
    authoritative reaper, and treating a flaky network as a fatal error would
    generate more flake than it prevents.

    They also don't warn immediately. A renewal is attempted every
    ``interval`` seconds (60 by default) against a TTL that is 1800, so a
    single failure has consumed 1/30th of the budget and means nothing.
    Warning on the first one made the warning fire on runs that were fine:
    `lager install` replaces the container serving the ``:9000`` lock API, so
    several minutes of failed renewals are *expected* mid-install and the lock
    outlives them comfortably. A line that cries wolf on every successful run
    is worse than no line, because it trains everyone to skip the one that
    matters.

    So the warning waits until the unrenewed window is a real threat to the
    lock — half the TTL — and then says how long it has actually been, which
    is the number that tells you whether to worry. A renewal that succeeds
    resets the window and re-arms the warning, so a box that keeps dropping
    out is reported each time it gets close, not once per process.
    """

    #: Fraction of the TTL that may pass unrenewed before we say anything.
    WARN_AT_TTL_FRACTION = 0.5

    #: Consecutive failures tolerated when the lock has no TTL to measure
    #: against (``ttl_seconds=None`` — an eternal lock, e.g. ``--detach``).
    #: Such a lock cannot expire, so this is purely "the box has been
    #: unreachable for a while and you probably want to know".
    WARN_AFTER_FAILURES_NO_TTL = 5

    def __init__(self, ip, holder, interval, *, warn_label='lock heartbeat',
                 ttl_seconds=None):
        super().__init__(daemon=True, name='lager-lock-heartbeat')
        self._ip = ip
        self._holder = holder
        self._interval = max(1, int(interval))
        self._warn_label = warn_label
        self._ttl_seconds = ttl_seconds
        # NOTE: must NOT be named ``_stop`` — ``threading.Thread`` itself
        # uses ``self._stop`` as a method during teardown, and assigning an
        # Event there raises ``TypeError: 'Event' object is not callable``
        # when the thread finishes normally.
        self._stop_event = _threading_for_heartbeat.Event()
        self._paused = _threading_for_heartbeat.Event()
        self._warned = False
        self._consecutive_failures = 0

    def stop(self):
        self._stop_event.set()

    def pause(self):
        """Stop attempting renewals until :meth:`unpause`.

        For a caller that is about to take the lock server down on purpose —
        ``lager install`` replaces the very container serving ``:9000``. A
        renewal during that window cannot succeed, so attempting one only
        burns its timeout and manufactures a "failure" that means nothing.
        Paused attempts are not counted, so the window can be any length
        without pushing the warning toward firing.
        """
        self._paused.set()

    def unpause(self):
        """Resume renewals, with the failure window reset.

        The reset matters: whatever happened while paused was expected, so it
        must not be added to whatever happens next. The lock's survival across
        the pause is the caller's problem, not the heartbeat's — see the TTL
        note on `lager install`.
        """
        self._consecutive_failures = 0
        self._warned = False
        self._paused.clear()

    def _should_warn(self):
        """True when the unrenewed window has grown worth reporting."""
        if self._warned:
            return False
        if self._ttl_seconds is None:
            return self._consecutive_failures >= self.WARN_AFTER_FAILURES_NO_TTL
        unrenewed = self._consecutive_failures * self._interval
        return unrenewed >= self._ttl_seconds * self.WARN_AT_TTL_FRACTION

    def _warning_text(self):
        unrenewed = self._consecutive_failures * self._interval
        if self._ttl_seconds is None:
            return (
                f'Warning: {self._warn_label} has failed '
                f'{self._consecutive_failures} times in a row '
                f'({_format_duration(unrenewed)}); the box may be unreachable.'
            )
        return (
            f'Warning: {self._warn_label} has not renewed for '
            f'{_format_duration(unrenewed)} of its '
            f'{_format_duration(self._ttl_seconds)} TTL; '
            f'the lock will expire if this continues.'
        )

    def run(self):
        import click

        while not self._stop_event.wait(self._interval):
            if self._paused.is_set():
                continue
            try:
                ok = heartbeat_box_lock(self._ip, self._holder)
            except Exception:  # pylint: disable=broad-except
                ok = False
            if ok:
                # Renewed: the clock restarts, and a later outage is allowed
                # to speak up on its own merits.
                self._consecutive_failures = 0
                self._warned = False
                continue
            self._consecutive_failures += 1
            if self._should_warn():
                self._warned = True
                try:
                    click.secho(self._warning_text(), fg='yellow', err=True)
                except Exception:  # pylint: disable=broad-except
                    pass


def _format_duration(seconds):
    """Render a whole number of seconds as the shortest honest unit."""
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds}s'
    minutes, rem = divmod(seconds, 60)
    if minutes < 60 and rem == 0:
        return f'{minutes}m'
    if minutes < 60:
        return f'{minutes}m{rem}s'
    hours, rem_min = divmod(minutes, 60)
    return f'{hours}h' if rem_min == 0 else f'{hours}h{rem_min}m'


def default_auto_holder_type():
    """Return ``'ci'`` when running under any known CI provider, else
    ``'ephemeral'``.

    Shared by `lager python` and the admin commands so every auto-lock
    carries the same classification. The distinction matters because matrix
    CI jobs need their holder string to encode run/job coordinates (already
    handled by ``get_lock_holder``) AND the server applies the same TTL
    policy. Failing closed (treating unknown environments as
    ``'ephemeral'``) is safe: ephemeral locks still heartbeat + reap, just
    without the explicit "this came from CI" tag.
    """
    try:
        from .context.ci_detection import get_ci_environment, CIEnvironment
        return 'ci' if get_ci_environment() != CIEnvironment.HOST else 'ephemeral'
    except Exception:  # pylint: disable=broad-except
        return 'ephemeral'


class LockSession:
    """Handle yielded by :func:`auto_lock_around_command`.

    Unpacks as ``(holder, state)`` so the historical
    ``with auto_lock_around_command(...) as (holder, state):`` form keeps
    working, and adds two affordances for commands that take down the lock
    server they are holding a lock on:

    * :meth:`dissolve` — it is gone for good (`lager uninstall`).
    * :meth:`suspended` — it is going away and coming back (`lager install`).
    """

    __slots__ = ('holder', 'state', '_dissolve_fn', '_suspend_fn', '_resume_fn')

    def __init__(self, holder, state, dissolve_fn,
                 suspend_fn=None, resume_fn=None):
        self.holder = holder
        self.state = state
        self._dissolve_fn = dissolve_fn
        self._suspend_fn = suspend_fn or (lambda: None)
        self._resume_fn = resume_fn or (lambda: None)

    def __iter__(self):
        return iter((self.holder, self.state))

    def dissolve(self):
        """Declare the lock gone because this command removed its server.

        `lager uninstall` deletes the lager container partway through its
        own run — and that container is the process serving the ``:9000``
        lock API this session's lock lives in. From that moment there is
        nothing left to keep alive and nobody left to release to: the lock
        state died with the container.

        Calling this stops the heartbeat thread and makes both the context
        manager's ``finally`` and the ``atexit`` fallback skip the release
        POST. It is neither a release (there is no server to tell) nor a
        leak (no state persists to block the next command). Idempotent, and
        a no-op when no lock was ever acquired.
        """
        self._dissolve_fn()

    @_contextlib_for_lock.contextmanager
    def suspended(self):
        """Declare a window in which the lock server is expected to be down.

        `lager install` replaces the lager container — the process serving
        the ``:9000`` lock API — and that rebuild runs upwards of fifteen
        minutes. Renewals cannot succeed across it, so left alone the
        heartbeat accumulates "failures" that are not failures and,
        eventually, warns that a lock is at risk when nothing is wrong.
        Under this block the renewals simply do not happen, and the failure
        window resets on the way out, so anything reported once the server
        is back is about the box's actual state.

        This does NOT keep the lock alive — nothing can, while the process
        owning the lock file is gone. Covering the window is the caller's
        job, by taking a TTL longer than the outage (see
        ``INSTALL_LOCK_TTL_SECONDS``). The heartbeat's job here is only to
        stop misreporting it.

        Resumes on the way out even if the body raises.
        """
        self._suspend_fn()
        try:
            yield
        finally:
            self._resume_fn()


#: Default budget for the deploy subprocess `lager install` runs. A cold
#: container build on ordinary box hardware is roughly 14 minutes, so this is
#: about a 2x margin over a known-good case — a margin that disappears on an
#: emulated guest, a throttled VM, a cold apt cache or a slow mirror. Override
#: per-run with `--timeout` or `LAGER_INSTALL_TIMEOUT`.
_DEFAULT_INSTALL_TIMEOUT_SECONDS = 1800


def default_install_timeout_seconds():
    """Default deploy-subprocess timeout for `lager install`, in seconds.

    ``LAGER_INSTALL_TIMEOUT`` env var wins. ``0`` means no timeout, matching
    `lager python --timeout`; anything unparseable falls back to the default
    rather than failing the install for a typo in the environment.

    A negative value is a typo, not a request, and falls back to the default.
    Clamping it to 0 the way `default_lock_wait_seconds` does would be wrong
    here: 0 means *unbounded* for this setting, so `LAGER_INSTALL_TIMEOUT=-5`
    would silently remove the budget rather than restore it.
    """
    env = os.getenv('LAGER_INSTALL_TIMEOUT')
    if env is None:
        return _DEFAULT_INSTALL_TIMEOUT_SECONDS
    try:
        value = int(env)
    except ValueError:
        return _DEFAULT_INSTALL_TIMEOUT_SECONDS
    if value < 0:
        return _DEFAULT_INSTALL_TIMEOUT_SECONDS
    return value


#: Floor for `lager install`'s auto-lock TTL. Deliberately longer than every
#: other admin command's: install spends most of its run with the lock server
#: torn down (it is rebuilding the container that serves it), so the lock has
#: to survive on TTL alone rather than on renewals. The cost of the larger
#: number is that a hard-killed install (SIGKILL, power loss — anything that
#: beats both the `finally` and the atexit release) leaves the box locked for
#: up to an hour; `lager boxes unlock` is the way out.
INSTALL_LOCK_TTL_SECONDS = 3600


def install_lock_ttl_seconds(deploy_timeout=None):
    """TTL for `lager install`'s auto-lock, derived from the deploy timeout.

    The two must move together. The TTL has to outlast the deploy subprocess
    it wraps, because the lock cannot be renewed while the container serving
    the lock API is torn down. Hardcoding 3600 against a hardcoded 1800 was
    fine while both were literals; once `--timeout` can raise the deploy
    budget, a fixed TTL would let a legitimately-running install have its own
    lock reaped mid-deploy.

    ``deploy_timeout`` of 0 (no timeout) yields ``None`` — an unbounded deploy
    cannot be outlasted by any finite TTL, so the lock lives on renewals and
    the explicit release instead.
    """
    if deploy_timeout is None:
        deploy_timeout = default_install_timeout_seconds()
    if not deploy_timeout:
        return None
    return max(INSTALL_LOCK_TTL_SECONDS, deploy_timeout * 2)


def auto_lock_around_command(
    ip,
    box_label,
    command_name,
    *,
    holder=None,
    ttl_seconds=None,
    wait_seconds=None,
    holder_type=None,
    heartbeat_interval=None,
):
    """Context manager: auto-acquire a box lock for the duration of an
    admin command (`install`, `uninstall`, `update`, `install-wheel`).

    Use as::

        with auto_lock_around_command(ip, box_label, 'install'):
            ... do the install ...

    On enter the box lock is acquired with ``holder_type=ephemeral`` (or
    ``'ci'`` under CI) and a heartbeat thread keeps it alive. On exit the
    lock is released — including on exception, ``ctx.exit()``, and
    ``SystemExit``. ``atexit`` provides a final-safety release for paths
    that bypass the with-block's ``__exit__`` (fatal signals, ``os._exit``).

    Defaults:
      * ``holder``      = :func:`get_lock_holder` (CI-aware identity).
      * ``holder_type`` = ``'ci'`` in CI, ``'ephemeral'`` otherwise.
      * ``ttl_seconds`` = :func:`default_lock_ttl_seconds` (1800).
      * ``wait_seconds`` = :func:`default_lock_wait_seconds` (0 in dev,
                            1800 in CI; overridable via ``LAGER_LOCK_WAIT``).

    Set the ``LAGER_AUTO_LOCK_DISABLE`` environment variable to skip the
    lock entirely (emergency escape hatch for a wedged box).

    Collision behavior follows :func:`acquire_box_lock`: it raises
    ``SystemExit(1)`` with a "locked by ..." message after the wait
    deadline.

    Yields a :class:`LockSession`, which unpacks as ``(holder, state)``
    — ``state`` being one of ``"acquired"``, ``"already_ours"``,
    ``"unreachable"``, or ``"disabled"``. Callers can use ``state`` to
    short-circuit downstream work; the lock lifecycle is otherwise fully
    handled by the context manager, the sole exception being commands that
    remove the lock server themselves, which call
    :meth:`LockSession.dissolve` (see `lager uninstall`).
    """
    import contextlib

    @contextlib.contextmanager
    def _cm():
        import atexit as _atexit
        import os as _os

        if _os.getenv('LAGER_AUTO_LOCK_DISABLE'):
            # No lock was taken, so there is nothing to dissolve — but the
            # handle must still offer the method, or a caller that dissolves
            # would crash under the escape hatch.
            yield LockSession(
                holder or get_lock_holder(), 'disabled', lambda: None,
            )
            return

        resolved_holder = holder or get_lock_holder()
        resolved_ttl = (
            default_lock_ttl_seconds() if ttl_seconds is None else ttl_seconds
        )
        resolved_wait = (
            default_lock_wait_seconds() if wait_seconds is None else wait_seconds
        )
        resolved_type = holder_type or default_auto_holder_type()

        state, lock_data = acquire_box_lock(
            ip,
            box_label,
            resolved_holder,
            holder_type=resolved_type,
            ttl_seconds=resolved_ttl,
            wait_seconds=resolved_wait,
        )

        should_release = (state == 'acquired')
        released = {'done': not should_release}

        def _safe_release():
            if released['done']:
                return
            released['done'] = True
            # Drop our atexit registration once we've run: callers that
            # take many locks in one process (test suites, scripted use)
            # shouldn't accumulate spent handlers.
            try:
                _atexit.unregister(_safe_release)
            except Exception:  # pylint: disable=broad-except
                pass
            try:
                release_box_lock(ip, resolved_holder)
            except Exception:  # pylint: disable=broad-except
                pass

        if should_release:
            # atexit covers paths that bypass __exit__ (signals not raised
            # as Python exceptions, ``os._exit``). Registering here rather
            # than at module load time means we only register when we
            # actually hold the lock — no surprise release of someone
            # else's lock at interpreter shutdown.
            _atexit.register(_safe_release)

        heartbeat = None
        # Heartbeat whenever the lock we're running under has a TTL: locks
        # we just acquired with one, and pre-existing ephemeral locks we
        # resumed (e.g. left by a crashed run) — without a heartbeat the
        # latter could expire mid-command. Pre-existing user locks have
        # ttl null and get no heartbeat, as before.
        resumed_ttl = (
            (lock_data or {}).get('ttl_seconds') if state == 'already_ours' else None
        )
        if (should_release and resolved_ttl is not None) or resumed_ttl is not None:
            heartbeat = HeartbeatThread(
                ip,
                resolved_holder,
                heartbeat_interval or default_heartbeat_interval(),
                warn_label=f'{command_name} lock heartbeat',
                # The TTL the warning is measured against is whichever one
                # this lock is actually living under: ours if we took it,
                # the existing lock's if we resumed one.
                ttl_seconds=resolved_ttl if should_release else resumed_ttl,
            )
            heartbeat.start()

        dissolved = {'done': False}

        def _dissolve():
            # Back end for LockSession.dissolve(); see that docstring for
            # why a removed lock server means neither release nor leak.
            if dissolved['done']:
                return
            dissolved['done'] = True
            if heartbeat is not None:
                heartbeat.stop()
            # Marking the release done skips it in BOTH the finally below
            # and the atexit fallback: a POST to the deleted server can
            # only burn its 5s timeout on the way to failing.
            released['done'] = True
            try:
                _atexit.unregister(_safe_release)
            except Exception:  # pylint: disable=broad-except
                pass

        def _suspend():
            if heartbeat is not None:
                heartbeat.pause()

        def _resume():
            # Never un-pause a dissolved session: the server is not coming
            # back, and resuming would restart the failure count against it.
            if heartbeat is not None and not dissolved['done']:
                heartbeat.unpause()

        try:
            yield LockSession(
                resolved_holder, state, _dissolve, _suspend, _resume,
            )
        finally:
            if heartbeat is not None:
                heartbeat.stop()
            _safe_release()

    return _cm()


def auto_lock_acquire_for_command(
    ip,
    box_label,
    command_name,
    *,
    holder=None,
    ttl_seconds=None,
    wait_seconds=None,
    holder_type=None,
    heartbeat_interval=None,
):
    """Imperative variant of :func:`auto_lock_around_command` for commands
    whose destructive section sits inside a long, multi-branch function
    that would be impractical to re-indent under a ``with`` block (e.g.
    ``_update_logic`` in ``commands/utility/update.py``).

    Acquires the box lock, registers an ``atexit`` release for paths that
    bypass the explicit release call (``ctx.exit``, ``sys.exit``, fatal
    signals), starts a heartbeat thread, and returns a callable that
    releases the lock + stops the heartbeat when invoked.

    Usage::

        release = auto_lock_acquire_for_command(ip, box, 'update')
        try:
            ... destructive work ...
        finally:
            release()

    The returned callable is idempotent — calling it more than once is a
    no-op after the first successful release. State is one of
    ``"acquired"``, ``"already_ours"``, ``"unreachable"``, or
    ``"disabled"``; it's stored on the returned callable as
    ``release.state`` for callers that need to branch on it.

    Collision behavior is identical to :func:`auto_lock_around_command`:
    a 409 past ``wait_seconds`` raises ``SystemExit(1)``.
    """
    import atexit as _atexit
    import os as _os

    @_contextlib_for_lock.contextmanager
    def _noop_suspended():
        yield

    def _noop_release():
        return None
    _noop_release.state = 'disabled'
    # The suspend affordances must exist on every return path, or a caller
    # that declares its outage crashes the moment locking is disabled.
    _noop_release.suspend = lambda: None
    _noop_release.resume = lambda: None
    _noop_release.suspended = _noop_suspended

    if _os.getenv('LAGER_AUTO_LOCK_DISABLE'):
        return _noop_release

    resolved_holder = holder or get_lock_holder()
    resolved_ttl = (
        default_lock_ttl_seconds() if ttl_seconds is None else ttl_seconds
    )
    resolved_wait = (
        default_lock_wait_seconds() if wait_seconds is None else wait_seconds
    )
    resolved_type = holder_type or default_auto_holder_type()

    state, lock_data = acquire_box_lock(
        ip,
        box_label,
        resolved_holder,
        holder_type=resolved_type,
        ttl_seconds=resolved_ttl,
        wait_seconds=resolved_wait,
    )

    should_release = (state == 'acquired')
    done = {'done': False}
    heartbeat = None

    def _release():
        if done['done']:
            return
        done['done'] = True
        # See _safe_release in auto_lock_around_command: don't accumulate
        # spent atexit handlers across many locks in one process.
        try:
            _atexit.unregister(_release)
        except Exception:  # pylint: disable=broad-except
            pass
        if heartbeat is not None:
            heartbeat.stop()
        if should_release:
            try:
                release_box_lock(ip, resolved_holder)
            except Exception:  # pylint: disable=broad-except
                pass

    _release.state = state

    # Suspend affordances, mirroring LockSession.suspended() for the callers
    # that cannot use it. `lager update` takes down the very container serving
    # the :9000 lock API (it stops it in Step 8 and rebuilds it in Step 9),
    # so every renewal across that window fails for a reason that is not a
    # fault — which is how a wholly successful update came to warn that its
    # lock heartbeat had failed five times running.
    #
    # Exposed as plain suspend()/resume() calls as well as a context manager,
    # because the whole reason this imperative variant exists is that the
    # window sits inside a long branchy function that cannot be re-indented
    # under a `with` (see this function's docstring).
    #
    # This changes no lock semantics. While the server is down a renewal
    # cannot succeed whether or not it is attempted, so the lock is already
    # riding its TTL either way; suspending only stops the misreporting.
    # Keeping the lock alive across the outage remains the caller's job, via
    # a TTL longer than the window.
    def _suspend():
        if heartbeat is not None:
            heartbeat.pause()

    def _resume():
        # Never un-pause after release: the heartbeat is stopped by then, and
        # resuming would only reset counters on a dead thread.
        if heartbeat is not None and not done['done']:
            heartbeat.unpause()

    @_contextlib_for_lock.contextmanager
    def _suspended():
        _suspend()
        try:
            yield
        finally:
            _resume()

    _release.suspend = _suspend
    _release.resume = _resume
    _release.suspended = _suspended

    if should_release:
        _atexit.register(_release)
        if resolved_ttl is not None:
            heartbeat = HeartbeatThread(
                ip,
                resolved_holder,
                heartbeat_interval or default_heartbeat_interval(),
                warn_label=f'{command_name} lock heartbeat',
                # Same as the `with` variant: the warning is measured against
                # the TTL this lock is actually living under. Omitting it sent
                # every heartbeat here down the no-TTL branch, which warns
                # after 5 consecutive failures and blames the box's
                # reachability — when the box is usually fine and the thing
                # actually at risk is the lock expiring.
                ttl_seconds=resolved_ttl,
            )
            heartbeat.start()
    elif state == 'already_ours' and (lock_data or {}).get('ttl_seconds') is not None:
        # Resumed a pre-existing ephemeral lock (e.g. left by a crashed
        # run): keep it alive for the duration of this command, but never
        # release it — that's the original holder's call. User locks have
        # ttl null and skip this branch.
        heartbeat = HeartbeatThread(
            ip,
            resolved_holder,
            heartbeat_interval or default_heartbeat_interval(),
            warn_label=f'{command_name} lock heartbeat',
            # The resumed lock's own TTL, not ours — we are keeping someone
            # else's lock alive and it expires on their clock.
            ttl_seconds=(lock_data or {}).get('ttl_seconds'),
        )
        heartbeat.start()

    return _release


def empty_box_name_error():
    """``--box ""`` is a user error, not a request for the default box.

    An empty string is falsy, so without this check it lands in the "no box
    given" branch and silently resolves to whatever the DEFAULT box is -- a
    different box than the caller named, with no indication that happened.

    ``lager boxes add --name ""`` already refuses (with "Box name cannot be
    empty"), so an empty name was validated on one path and silently
    reinterpreted on the other. This makes the two agree.
    """
    from .errors import LagerError

    return LagerError(
        'Box name cannot be empty.',
        fixes=[
            'Omit --box to use your default box.',
            'Or pass a saved box name / an IP address with --box.',
        ],
    )


def box_not_found_error(box_name):
    """Build an actionable LagerError for an unrecognized ``--box`` value.

    Lists the user's saved boxes (so they can spot a typo) and shows how to
    add the new one. Shared by every box-resolution path so the message is
    identical no matter which command hit it.
    """
    from .errors import LagerError

    saved_boxes = list_boxes()
    if saved_boxes:
        lines = []
        for name, box_info in sorted(saved_boxes.items(), key=lambda x: natural_sort_key(x[0])):
            box_ip = box_info.get('ip', 'unknown') if isinstance(box_info, dict) else box_info
            lines.append(f'      - {name} ({box_ip})')
        cause = 'Your saved boxes:\n' + '\n'.join(lines)
    else:
        cause = 'You have no saved boxes yet.'

    return LagerError(
        f"No box named '{box_name}'.",
        cause=cause,
        fixes=[
            f'Add it: lager boxes add --name {box_name} --ip [IP_ADDRESS]',
            'Or use an existing name / an IP address with --box.',
        ],
    )


def resolve_and_validate_box_with_name(ctx, box_name: Optional[str] = None, _skip_lock_check=False, _force=False) -> tuple:
    """
    Resolve and validate a box name, returning both IP and name.

    Args:
        ctx: Click context
        box_name: Box name to resolve (if None, uses default box)
        _skip_lock_check: If True, skip user lock check
        _force: Unused, kept for call-site compatibility

    Returns:
        Tuple of (resolved_ip_or_box_id, original_box_name_or_None)

    Exits with error if box is invalid or not found.
    """
    if box_name is not None and not box_name.strip():
        raise empty_box_name_error()

    import click
    import ipaddress
    import os
    from .context import get_default_box

    def _do_lock_check(ip, name):
        if not _skip_lock_check:
            _check_box_lock(ip, name)

    def _do_version_check(ip, name):
        # 0.20.0+: warn once per process if the CLI is a minor version
        # ahead of the box. Fails open — wraps so a flaky import / network
        # error can never break a working command.
        try:
            from .core.version_skew import check_and_warn
            check_and_warn(ip, name)
        except Exception:
            pass

    # If no box name provided, use default box
    if not box_name:
        # Get the default box name before resolving to IP
        default_name = os.getenv('LAGER_BOX') or getattr(ctx.obj, 'default_box', None)
        resolved_ip = get_default_box(ctx)
        _do_lock_check(resolved_ip, default_name)
        _do_version_check(resolved_ip, default_name)
        return (resolved_ip, default_name)

    # Check if it's a saved box name
    saved_ip = get_box_ip(box_name)
    if saved_ip:
        _do_lock_check(saved_ip, box_name)
        _do_version_check(saved_ip, box_name)
        return (saved_ip, box_name)

    # Check if it's a valid IP address
    try:
        ipaddress.ip_address(box_name)
        _do_lock_check(box_name, None)
        _do_version_check(box_name, None)
        return (box_name, None)  # Direct IP, no box name
    except ValueError:
        # Not a valid IP and not in local boxes - show an actionable error.
        raise box_not_found_error(box_name)


def resolve_and_validate_box(ctx, box_name: Optional[str] = None, _skip_lock_check=False, _force=False) -> str:
    """
    Resolve and validate a box name.

    Args:
        ctx: Click context
        box_name: Box name to resolve (if None, uses default box)
        _skip_lock_check: If True, skip user lock check
        _force: Unused, kept for call-site compatibility

    Returns:
        Resolved box IP address or box ID

    Exits with error if box is invalid or not found.
    """
    if box_name is not None and not box_name.strip():
        raise empty_box_name_error()

    import click
    import ipaddress
    from .context import get_default_box

    def _do_lock_check(ip, name):
        if not _skip_lock_check:
            _check_box_lock(ip, name)

    def _do_version_check(ip, name):
        # Warn once per process if the CLI is a minor version ahead of the
        # box. This is the resolution path used by the Tier-1 :9000-only
        # commands, which hard-fail against old box images — the warning must
        # precede that failure. Fails open so a flaky import / network error
        # can never break a working command.
        try:
            from .core.version_skew import check_and_warn
            check_and_warn(ip, name)
        except Exception:
            pass

    # If no box name provided, use default box
    if not box_name:
        resolved_ip = get_default_box(ctx)
        _do_lock_check(resolved_ip, None)
        _do_version_check(resolved_ip, None)
        return resolved_ip

    # Check if it's a saved box name
    saved_ip = get_box_ip(box_name)
    if saved_ip:
        _do_lock_check(saved_ip, box_name)
        _do_version_check(saved_ip, box_name)
        return saved_ip

    # Check if it's a valid IP address
    try:
        ipaddress.ip_address(box_name)
        _do_lock_check(box_name, None)
        _do_version_check(box_name, None)
        return box_name
    except ValueError:
        # Not a valid IP and not in local boxes - show an actionable error.
        raise box_not_found_error(box_name)
