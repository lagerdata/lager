# Copyright 2024-2026 Lager Data LLC
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

    Returns the user from 'lager defaults add --user', falling back to
    the OS system username (getpass.getuser()).
    """
    import getpass
    from .config import read_config_file

    try:
        config = read_config_file()
        if config.has_option('LAGER', 'user'):
            return config.get('LAGER', 'user')
    except Exception:
        pass
    return getpass.getuser()


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
                    click.secho(
                        f"Error: Box '{display}' is locked by {locked_by}",
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


def _acquire_command_lock(ip, box_name, command_name, force=False):
    """Acquire command-in-progress lock on a box. Exits if blocked.

    Replaces _check_box_lock by combining user lock check + busy lock
    acquisition in a single HTTP request.

    Args:
        ip: Box IP address
        box_name: Box name for display purposes
        command_name: Name of the command being run
        force: If True, bypass command lock check
    """
    import click
    import requests

    current_user = get_lager_user()
    try:
        resp = requests.post(
            f'http://{ip}:5000/command-lock',
            json={'user': current_user, 'command': command_name, 'force': force},
            timeout=3,
        )
        if resp.status_code == 409:
            data = resp.json()
            display = box_name or ip
            lock_type = data.get('type', '')

            if lock_type == 'user_lock':
                lock_info = data.get('lock', {})
                locked_by = lock_info.get('user', 'unknown')
                click.secho(
                    f"Error: Box '{display}' is locked by {locked_by}",
                    fg='red', err=True,
                )
                click.echo(
                    f"To force unlock: lager boxes unlock --box {display} --force",
                    err=True,
                )
            elif lock_type == 'command_lock':
                busy_info = data.get('busy', {})
                busy_user = busy_info.get('user', 'unknown')
                busy_command = busy_info.get('command', 'unknown')
                click.secho(
                    f"Error: Box '{display}' is busy — '{busy_command}' in progress by {busy_user}. "
                    f"Use --force-command to override.",
                    fg='red', err=True,
                )
            else:
                click.secho(
                    f"Error: Box '{display}' is unavailable: {data.get('error', 'unknown')}",
                    fg='red', err=True,
                )
            raise SystemExit(1)
    except (requests.exceptions.RequestException, SystemExit) as e:
        if isinstance(e, SystemExit):
            raise
        # Box unreachable - silently skip, command will fail on its own


def _release_command_lock(ip, box_name):
    """Release command-in-progress lock on a box. Best-effort, silently ignores errors.

    Args:
        ip: Box IP address
        box_name: Box name (unused, for consistency)
    """
    import requests

    current_user = get_lager_user()
    try:
        requests.post(
            f'http://{ip}:5000/command-lock/release',
            json={'user': current_user},
            timeout=3,
        )
    except Exception:
        pass  # Best-effort cleanup


def acquire_command_lock_with_cleanup(ctx, ip, box_name, command_name, force=False):
    """Acquire command lock and register auto-release on context close.

    Args:
        ctx: Click context
        ip: Box IP address
        box_name: Box name for display purposes
        command_name: Name of the command being run
        force: If True, bypass command lock check
    """
    _acquire_command_lock(ip, box_name, command_name, force=force)
    ctx.call_on_close(lambda: _release_command_lock(ip, box_name))


def resolve_and_validate_box_with_name(ctx, box_name: Optional[str] = None, _skip_lock_check=False, _force=False) -> tuple:
    """
    Resolve and validate a box name, returning both IP and name.

    Args:
        ctx: Click context
        box_name: Box name to resolve (if None, uses default box)
        _skip_lock_check: If True, skip both user lock and command lock checks
        _force: If True, bypass command-in-progress lock

    Returns:
        Tuple of (resolved_ip_or_box_id, original_box_name_or_None)

    Exits with error if box is invalid or not found.
    """
    import click
    import ipaddress
    import os
    from .context import get_default_box

    # Determine force from context if not explicitly passed
    force = _force or getattr(getattr(ctx, 'obj', None), 'force_command', False)
    # Get command name from click context
    command_name = getattr(ctx, 'info_name', '') or ''

    def _do_lock_check(ip, name):
        if not _skip_lock_check:
            acquire_command_lock_with_cleanup(ctx, ip, name, command_name, force=force)

    # If no box name provided, use default box
    if not box_name:
        # Get the default box name before resolving to IP
        default_name = os.getenv('LAGER_BOX') or getattr(ctx.obj, 'default_box', None)
        resolved_ip = get_default_box(ctx)
        _do_lock_check(resolved_ip, default_name)
        return (resolved_ip, default_name)

    # Check if it's a saved box name
    saved_ip = get_box_ip(box_name)
    if saved_ip:
        _do_lock_check(saved_ip, box_name)
        return (saved_ip, box_name)

    # Check if it's a valid IP address
    try:
        ipaddress.ip_address(box_name)
        _do_lock_check(box_name, None)
        return (box_name, None)  # Direct IP, no box name
    except ValueError:
        # Not a valid IP and not in local boxes - Show helpful error
        click.secho(f"Error: Box '{box_name}' not found.", fg='red', err=True)
        click.echo("", err=True)

        saved_boxes = list_boxes()
        if saved_boxes:
            click.echo("Available boxes:", err=True)
            for name, box_info in sorted(saved_boxes.items(), key=lambda x: natural_sort_key(x[0])):
                if isinstance(box_info, dict):
                    box_ip = box_info.get('ip', 'unknown')
                else:
                    box_ip = box_info
                click.echo(f"  - {name} ({box_ip})", err=True)
        else:
            click.echo("No boxes are currently saved.", err=True)

        click.echo("", err=True)
        click.echo("To add a new box, use:", err=True)
        click.echo(f"  lager boxes add --name {box_name} --ip <IP_ADDRESS>", err=True)
        ctx.exit(1)


def resolve_and_validate_box(ctx, box_name: Optional[str] = None, _skip_lock_check=False, _force=False) -> str:
    """
    Resolve and validate a box name.

    Args:
        ctx: Click context
        box_name: Box name to resolve (if None, uses default box)
        _skip_lock_check: If True, skip both user lock and command lock checks
        _force: If True, bypass command-in-progress lock

    Returns:
        Resolved box IP address or box ID

    Exits with error if box is invalid or not found.
    """
    import click
    import ipaddress
    from .context import get_default_box

    # Determine force from context if not explicitly passed
    force = _force or getattr(getattr(ctx, 'obj', None), 'force_command', False)
    # Get command name from click context
    command_name = getattr(ctx, 'info_name', '') or ''

    def _do_lock_check(ip, name):
        if not _skip_lock_check:
            acquire_command_lock_with_cleanup(ctx, ip, name, command_name, force=force)

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
        # Not a valid IP and not in local boxes - Show helpful error
        click.secho(f"Error: Box '{box_name}' not found.", fg='red', err=True)
        click.echo("", err=True)

        saved_boxes = list_boxes()
        if saved_boxes:
            click.echo("Available boxes:", err=True)
            for name, box_info in sorted(saved_boxes.items(), key=lambda x: natural_sort_key(x[0])):
                if isinstance(box_info, dict):
                    box_ip = box_info.get('ip', 'unknown')
                else:
                    box_ip = box_info
                click.echo(f"  - {name} ({box_ip})", err=True)
        else:
            click.echo("No boxes are currently saved.", err=True)

        click.echo("", err=True)
        click.echo("To add a new box, use:", err=True)
        click.echo(f"  lager boxes add --name {box_name} --ip <IP_ADDRESS>", err=True)
        ctx.exit(1)
