# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Debug Net Configuration Caching

Caches debug net configuration to avoid repeated SSH lookups.
Cache is invalidated when:
- User explicitly changes net name
- Cache is older than 5 minutes
- User runs 'lager debug clear-cache'
- A `lager nets` command changes a net record on the box (every entry for
  that box goes: an entry carries the net's `jlink_script`, which erase and
  flash send to the box, and the box prefers it over the saved net)
"""
import json
import time
from pathlib import Path
from typing import Optional, Dict, Any


class DebugNetCache:
    """Manages cached debug net configurations."""

    CACHE_FILE = Path.home() / '.lager_cache' / 'debug_net_cache.json'
    CACHE_TTL = 300  # 5 minutes

    def __init__(self):
        self.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._cache = self._load_cache()

    def _load_cache(self) -> Dict[str, Any]:
        """Load cache from disk."""
        if not self.CACHE_FILE.exists():
            return {}

        try:
            with open(self.CACHE_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def _save_cache(self):
        """Save cache to disk."""
        try:
            with open(self.CACHE_FILE, 'w') as f:
                json.dump(self._cache, f, indent=2)
        except IOError as e:
            # Non-fatal: cache save failure shouldn't break commands
            pass

    def get_cache_key(self, box: str, net_name: Optional[str]) -> str:
        """Generate cache key for box + net combination."""
        return f"{box}:{net_name or 'default'}"

    def get(self, box: str, net_name: Optional[str]) -> Optional[Dict[str, Any]]:
        """
        Get cached debug net configuration.

        Args:
            box: Lager Box name or IP
            net_name: Debug net name (or None for default)

        Returns:
            Cached net config dict, or None if cache miss/expired
        """
        key = self.get_cache_key(box, net_name)
        entry = self._cache.get(key)

        if not entry:
            return None

        # Check if cache entry is still valid
        if time.time() - entry.get('timestamp', 0) > self.CACHE_TTL:
            # Cache expired
            del self._cache[key]
            self._save_cache()
            return None

        return entry.get('net_config')

    def set(self, box: str, net_name: Optional[str], net_config: Dict[str, Any]):
        """
        Store debug net configuration in cache.

        Args:
            box: Lager Box name or IP
            net_name: Debug net name (or None for default)
            net_config: Net configuration dict to cache
        """
        key = self.get_cache_key(box, net_name)

        self._cache[key] = {
            'timestamp': time.time(),
            'net_config': net_config,
        }

        self._save_cache()

    def clear(self, box: Optional[str] = None):
        """
        Clear cache entries.

        Args:
            box: If specified, only clear entries for this box.
                 If None, clear entire cache.
        """
        if box is None:
            self._cache = {}
        else:
            # Clear all entries for this box
            keys_to_delete = [k for k in self._cache.keys() if k.startswith(f"{box}:")]
            for key in keys_to_delete:
                del self._cache[key]

        self._save_cache()

    def invalidate_on_error(self, box: str, net_name: Optional[str]):
        """
        Invalidate cache entry after an error.

        This ensures that if net configuration changes (e.g., probe disconnected),
        we'll refetch on the next command.
        """
        key = self.get_cache_key(box, net_name)
        if key in self._cache:
            del self._cache[key]
            self._save_cache()


# Global cache instance
_net_cache = DebugNetCache()


def get_net_cache() -> DebugNetCache:
    """Get the global debug net cache instance."""
    return _net_cache


def forget_box_debug_nets(box: str) -> None:
    """Drop every cached debug net for *box* after a net record on it changed.

    Called by `lager nets` and the Net-Manager TUI after each write. An entry
    carries the net's `jlink_script`, and `flash` / `erase` send that copy to
    the box, which prefers it over the saved net, so a `set-script` did not
    take effect until the entry expired. Every entry for the box goes, not
    only the named net's: one cached without a net name holds the box's first
    debug net. Best effort: a cache that cannot be read or written must never
    fail the command that changed the net.
    """
    try:
        get_net_cache().clear(box=box)
    except Exception:
        pass
