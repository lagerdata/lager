# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Where the agent finds the Box's configuration directory.

Inside the container that directory is always ``/etc/lager``, and the Box
code names it by that literal path in many modules. The agent runs outside
the container, where creating ``/etc/lager`` would need administrator
rights, so the same directory lives under the user's home instead and is
shared into the container at ``/etc/lager``.

Rather than edit every Box module to take the path from a setting -- each
edit being a change to code a Linux Box runs -- the agent repoints those
modules after importing them. The trade is explicit: Linux code is left
exactly as it is, and a path the agent misses surfaces as a bug on the host
platform only. ``test_host_paths.py`` pins the names this relies on.
"""
from __future__ import annotations

import os
import sys
from pathlib import PurePath
from typing import Optional, Set

CONTAINER_CONFIG_DIR = "/etc/lager"
CONFIG_DIR_ENV = "LAGER_HOST_CONFIG_DIR"

# Packages that describe the container's view on purpose and must keep it.
_SKIP_PREFIXES = ("lager.hostlink", "lager.host")


def default_config_dir() -> str:
    """The agent's configuration directory when none is given."""
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return override
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Lager/etc")
    return CONTAINER_CONFIG_DIR


def _translate(value: str, config_dir: str) -> Optional[str]:
    if value == CONTAINER_CONFIG_DIR:
        return config_dir
    if value.startswith(CONTAINER_CONFIG_DIR + "/"):
        return config_dir + value[len(CONTAINER_CONFIG_DIR):]
    return None


def remap_loaded_modules(config_dir: str, seen: Set[str]) -> int:
    """Repoint module-level config paths in ``lager`` modules not yet seen.

    Drivers are imported lazily, on first use, so this is called again
    before each request; ``seen`` makes the repeat calls cheap. Returns how
    many attributes were changed.
    """
    if config_dir == CONTAINER_CONFIG_DIR:
        return 0
    changed = 0
    for name, module in list(sys.modules.items()):
        if module is None or name in seen:
            continue
        if name != "lager" and not name.startswith("lager."):
            continue
        seen.add(name)
        if name.startswith(_SKIP_PREFIXES):
            continue
        for attr, value in list(vars(module).items()):
            if isinstance(value, str):
                new = _translate(value, config_dir)
                if new is not None:
                    setattr(module, attr, new)
                    changed += 1
            elif isinstance(value, PurePath):
                new = _translate(str(value), config_dir)
                if new is not None:
                    setattr(module, attr, type(value)(new))
                    changed += 1
    return changed


def remap_singletons(config_dir: str) -> None:
    """Repoint objects that captured a config path when they were built."""
    if config_dir == CONTAINER_CONFIG_DIR:
        return
    from lager.cache import get_nets_cache

    cache = get_nets_cache()
    new = _translate(cache._path, config_dir)
    if new is not None:
        cache._path = new
