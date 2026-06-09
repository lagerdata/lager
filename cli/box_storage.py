# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    Box storage utilities for managing local box configurations
"""
import json
import os
from pathlib import Path
from typing import Dict, Optional

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
    """Delete a box from the global storage. Returns True if deleted, False if not found."""
    boxes = _load_global_boxes()
    if name in boxes:
        del boxes[name]
        save_boxes(boxes)
        return True
    return False


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
    - ``stout:<uuid>:<email>``                    -> just the email
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

    if user.startswith('stout:'):
        parts = user.split(':', 2)
        if len(parts) == 3:
            return parts[2]
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


def _check_box_lock(ip, box_name):
    """Check if a box is locked by another user. Exits if locked.

    Args:
        ip: Box IP address
        box_name: Box name for display purposes
    """
    import click
    import requests

    try:
        resp = requests.get(f'http://{ip}:5000/lock', timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('locked'):
                locked_by = data.get('user', 'unknown')
                current_user = get_lager_user()
                if locked_by != current_user:
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


def _lock_url(ip, suffix=''):
    return f'http://{ip}:5000/lock{suffix}'


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
            resp = requests.post(_lock_url(ip), json=payload, timeout=5)
        except requests.exceptions.RequestException as exc:
            if not quiet:
                click.secho(
                    f"Warning: Could not reach box '{display}' to acquire lock: {exc}",
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
                # Box doesn't echo previous_user (older server). Fall back to
                # locked_at: if locked_at is recent (<= 2s) assume we acquired.
                state = 'acquired'
            elif previous_holder == holder:
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
            f'http://{ip}:5000/unlock',
            json={'user': holder},
            timeout=5,
        )
    except requests.exceptions.RequestException as exc:
        if not quiet:
            click.secho(
                f"Warning: Could not reach box at {ip} to release lock: {exc}",
                fg='yellow', err=True,
            )
        return False

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
        )
    except requests.exceptions.RequestException as exc:
        if not quiet:
            click.secho(
                f"Warning: heartbeat to {ip} failed: {exc}",
                fg='yellow', err=True,
            )
        return False
    return resp.status_code == 200


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
    import click
    import ipaddress
    from .context import get_default_box

    def _do_lock_check(ip, name):
        if not _skip_lock_check:
            _check_box_lock(ip, name)

    # If no box name provided, use default box
    if not box_name:
        resolved_ip = get_default_box(ctx)
        _do_lock_check(resolved_ip, None)
        return resolved_ip

    # Check if it's a saved box name
    saved_ip = get_box_ip(box_name)
    if saved_ip:
        _do_lock_check(saved_ip, box_name)
        return saved_ip

    # Check if it's a valid IP address
    try:
        ipaddress.ip_address(box_name)
        _do_lock_check(box_name, None)
        return box_name
    except ValueError:
        # Not a valid IP and not in local boxes - show an actionable error.
        raise box_not_found_error(box_name)
