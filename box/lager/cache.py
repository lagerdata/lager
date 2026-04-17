# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Caching layer for saved nets with file modification detection."""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, List, Optional


class NetsCache:
    """
    Thread-safe singleton cache for saved nets.

    This cache loads nets from /etc/lager/saved_nets.json and maintains
    an index for O(1) lookup by net name. The cache automatically detects
    file modifications via mtime and reloads when necessary.

    Usage:
        from lager.cache import get_nets_cache

        cache = get_nets_cache()
        nets = cache.get_nets()  # Get all nets
        net = cache.find_by_name("my-net")  # O(1) lookup by name
        cache.invalidate()  # Force reload on next access
    """
    _instance: Optional["NetsCache"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "NetsCache":
        if cls._instance is None:
            with cls._lock:
                # Double-check locking pattern
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init()
        return cls._instance

    def _init(self) -> None:
        """Initialize instance attributes (called once on singleton creation)."""
        from .constants import SAVED_NETS_PATH
        self._nets: Optional[List[dict]] = None
        self._mtime: float = 0
        self._name_index: Dict[str, dict] = {}
        self._path = SAVED_NETS_PATH
        self._data_lock = threading.Lock()

    def _reload_if_needed(self) -> None:
        """Reload from file if modified. Must be called with _data_lock held."""
        try:
            current_mtime = os.path.getmtime(self._path)
        except FileNotFoundError:
            self._nets = []
            self._name_index = {}
            self._mtime = 0
            return
        except OSError:
            # Handle other OS errors (permission denied, etc.)
            if self._nets is None:
                self._nets = []
                self._name_index = {}
            return

        if current_mtime != self._mtime:
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # Validate that we got a list
                    if not isinstance(data, list):
                        data = []
                    self._nets = data
            except (json.JSONDecodeError, IOError):
                # If file is corrupt or unreadable, use empty list
                self._nets = []

            self._mtime = current_mtime
            # Build name index for O(1) lookups
            self._name_index = {n.get("name"): n for n in self._nets if n.get("name")}

    def get_nets(self) -> List[dict]:
        """
        Get all nets (reloads if file changed).

        Returns:
            List of net dictionaries from saved_nets.json
        """
        with self._data_lock:
            self._reload_if_needed()
            return list(self._nets) if self._nets else []

    def find_by_name(self, name: str) -> Optional[dict]:
        """
        O(1) lookup by net name.

        Args:
            name: The net name to search for

        Returns:
            The net dictionary if found, None otherwise
        """
        with self._data_lock:
            self._reload_if_needed()
            return self._name_index.get(name)

    def find_by_name_and_role(self, name: str, role: str) -> Optional[dict]:
        """
        Find a net by name and role.

        This is useful when multiple nets might share a name but have different roles.
        Falls back to name-only lookup if no exact match found.

        Args:
            name: The net name to search for
            role: The role to match (e.g., "power-supply", "battery")

        Returns:
            The net dictionary if found, None otherwise
        """
        with self._data_lock:
            self._reload_if_needed()
            # First try exact name match
            net = self._name_index.get(name)
            if net and net.get("role") == role:
                return net
            # If name matches but role doesn't, search the full list
            # (handles edge case of duplicate names with different roles)
            if self._nets:
                for n in self._nets:
                    if n.get("name") == name and n.get("role") == role:
                        return n
            return None

    def invalidate(self) -> None:
        """
        Force reload on next access.

        Call this after modifying the saved_nets.json file to ensure
        the cache picks up the changes immediately.
        """
        with self._data_lock:
            self._mtime = 0

    def get_path(self) -> str:
        """Get the path to the nets file."""
        return self._path


def get_nets_cache() -> NetsCache:
    """
    Get the singleton cache instance.

    Returns:
        The NetsCache singleton
    """
    return NetsCache()
