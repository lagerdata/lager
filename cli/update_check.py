# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.update_check

    Background update checker for lager-cli.
    Checks PyPI once per day and notifies users when a newer version is available.
"""
import json
import os
import threading

PYPI_URL = 'https://pypi.org/pypi/lager-cli/json'
CACHE_FILE = os.path.expanduser('~/.lager_update_check')
CHECK_INTERVAL_SECONDS = 86400  # 24 hours


def _parse_version(version_str):
    """Parse a version string into a comparable tuple of ints."""
    try:
        return tuple(int(x) for x in version_str.strip().split('.'))
    except (ValueError, AttributeError):
        return (0,)


def _read_cache():
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _write_cache(latest_version, notified_at=None):
    try:
        import time
        cache = _read_cache() or {}
        cache['latest_version'] = latest_version
        cache['checked_at'] = time.time()
        if notified_at is not None:
            cache['notified_at'] = notified_at
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache, f)
    except Exception:
        pass


def _should_check():
    """Return True if enough time has passed since the last check."""
    import time
    cache = _read_cache()
    if cache is None:
        return True
    try:
        return time.time() - cache['checked_at'] > CHECK_INTERVAL_SECONDS
    except Exception:
        return True


def _fetch_latest_version():
    """Fetch the latest lager-cli version from PyPI. Returns version string or None."""
    try:
        import requests
        response = requests.get(PYPI_URL, timeout=5)
        if response.status_code == 200:
            return response.json()['info']['version']
    except Exception:
        pass
    return None


def _run_check(result_holder):
    """Background worker: use cache if fresh, otherwise fetch and update cache."""
    cache = _read_cache()
    if cache and not _should_check():
        result_holder['latest_version'] = cache.get('latest_version')
        return

    latest = _fetch_latest_version()
    if latest:
        _write_cache(latest)
    elif cache:
        # Network failed — still report last known version from cache
        latest = cache.get('latest_version')
    result_holder['latest_version'] = latest


def start_background_check():
    """
    Start an update check in a background daemon thread.

    Returns:
        (thread, result_holder) — join the thread (with a timeout) then read
        result_holder['latest_version'] to get the latest PyPI version.
    """
    result_holder = {}
    thread = threading.Thread(target=_run_check, args=(result_holder,), daemon=True)
    thread.start()
    return thread, result_holder


def _should_notify():
    """Return True if the notification hasn't been shown in the last 24 hours."""
    import time
    cache = _read_cache()
    if cache is None:
        return True
    try:
        return time.time() - cache.get('notified_at', 0) > CHECK_INTERVAL_SECONDS
    except Exception:
        return True


def notify_if_update_available(current_version, thread, result_holder):
    """
    Wait briefly for the background check, then print a notification if an
    update is available.  Should be called from ctx.call_on_close().
    """
    import time
    import click
    thread.join(timeout=2)
    latest = result_holder.get('latest_version')
    if not latest:
        return
    if _parse_version(latest) > _parse_version(current_version) and _should_notify():
        click.echo()
        click.secho(
            f'Update available: {current_version} \u2192 {latest}',
            fg='yellow', err=True,
        )
        click.secho(
            'Run: pip install --upgrade lager-cli',
            fg='yellow', err=True,
        )
        _write_cache(latest, notified_at=time.time())
