# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Base dispatcher class providing common patterns for hardware backend dispatchers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple, Type

from lager.dispatchers import helpers


class BaseDispatcher(ABC):
    """
    Abstract base class for hardware dispatchers.

    This class provides the common infrastructure for routing operations
    to the appropriate hardware backend drivers. Subclasses must implement
    the abstract methods to customize behavior for their specific hardware type.

    Class Attributes:
        ROLE: The net role this dispatcher handles (e.g., "power-supply", "battery").
              Subclasses must override this.
        ERROR_CLASS: The exception class to raise for domain-specific errors
              (e.g., SupplyBackendError, BatteryBackendError). Subclasses must override this.
        _driver_cache: Class-level dictionary caching driver instances by their cache key.
              Shared across all instances of each dispatcher subclass.
              This provides singleton-like driver reuse, matching the pattern
              used by module-level dispatchers (e.g., solar/dispatcher.py).

    Note:
        The cache is class-level, not instance-level. Each subclass gets its own
        independent cache dictionary (due to Python's class attribute inheritance).
        This means:
        - All SupplyDispatcher instances share one cache
        - All BatteryDispatcher instances share another cache
        - etc.
    """

    ROLE: str = ""  # Subclasses must override this
    ERROR_CLASS: Type[Exception] = Exception  # Subclasses must override this
    _driver_cache: Dict[str, Any] = {}  # Class-level cache, shared by all instances

    def __init__(self):
        """Initialize the dispatcher.

        Note: The driver cache is class-level, not instance-level.
        Subclasses should define their own _driver_cache = {} to get
        an independent cache per dispatcher type.
        """
        # Ensure each subclass has its own cache dictionary
        # This prevents sharing cache across different dispatcher types
        if "_driver_cache" not in self.__class__.__dict__:
            self.__class__._driver_cache = {}

    @abstractmethod
    def _choose_driver(self, instrument_name: str) -> Type[Any]:
        """
        Return the driver class based on the instrument string.

        Args:
            instrument_name: The instrument identifier from the net configuration.

        Returns:
            The driver class to use for this instrument.

        Raises:
            An appropriate error (via _make_error) if the instrument is not supported.
        """
        pass

    @abstractmethod
    def _make_error(self, message: str) -> Exception:
        """
        Create an appropriate error for this dispatcher type.

        Args:
            message: The error message.

        Returns:
            An exception instance appropriate for this dispatcher type.
        """
        pass

    def _find_net(self, netname: str) -> Dict[str, Any]:
        """
        Find a net by name and validate its role.

        Uses the NetsCache for O(1) lookup and validates that the net
        has the correct role for this dispatcher. Delegates to the helpers
        module using self.ERROR_CLASS for consistent error handling.

        Args:
            netname: The name of the net to find.

        Returns:
            The net configuration dictionary.

        Raises:
            ERROR_CLASS: If the net is not found or has wrong role.
        """
        net = helpers.find_saved_net(netname, self.ERROR_CLASS)
        helpers.ensure_role(net, self.ROLE, self.ERROR_CLASS)
        return net

    def _find_mapping_for_net(
        self, rec: Dict[str, Any], netname: str
    ) -> Optional[Dict[str, Any]]:
        """
        Find the mapping entry for a specific net name.

        Args:
            rec: The net configuration record.
            netname: The net name to find mapping for.

        Returns:
            The mapping dictionary if found, None otherwise.
        """
        for m in rec.get("mappings") or []:
            if m.get("net") == netname:
                return m
        return None

    def _resolve_channel(self, rec: Dict[str, Any], netname: str) -> int:
        """
        Resolve the channel/pin number for the net.

        Prefers mappings[].pin that matches this net; else falls back to
        top-level pin. Delegates to the helpers module using self.ERROR_CLASS.

        Args:
            rec: The net configuration record.
            netname: The net name to resolve channel for.

        Returns:
            The channel number as an integer.

        Raises:
            ERROR_CLASS: If the channel cannot be resolved.
        """
        return helpers.resolve_channel(rec, netname, self.ERROR_CLASS)

    def _resolve_address(self, rec: Dict[str, Any], netname: str) -> str:
        """
        Resolve the VISA/device address for the net.

        Prefers mappings[].device_override for this net if present;
        else uses rec['address']. Delegates to the helpers module using self.ERROR_CLASS.

        Args:
            rec: The net configuration record.
            netname: The net name to resolve address for.

        Returns:
            The device address string.

        Raises:
            ERROR_CLASS: If no address is configured.
        """
        return helpers.resolve_address(rec, netname, self.ERROR_CLASS)

    def _get_cache_key(
        self, instrument: str, address: str, netname: str, channel: Optional[int] = None
    ) -> str:
        """
        Generate a cache key for driver instances.

        Args:
            instrument: The instrument identifier.
            address: The device address.
            netname: The net name.
            channel: Optional channel number.

        Returns:
            A unique cache key string.
        """
        if channel is not None:
            return f"{instrument}_{address}_{netname}_{channel}"
        return f"{instrument}_{address}_{netname}"

    def _get_cached_driver(self, cache_key: str) -> Optional[Any]:
        """
        Get a cached driver instance if it exists and is still alive.

        This method accesses the class-level cache, which is shared across
        all instances of this dispatcher type. This provides singleton-like
        driver reuse, preventing redundant connections to the same instrument.

        Args:
            cache_key: The cache key to look up.

        Returns:
            The cached driver if found and alive, None otherwise.
        """
        cache = self.__class__._driver_cache
        if cache_key not in cache:
            return None

        cached_driver = cache[cache_key]

        # Verify cached driver is still alive if it has such a method
        try:
            if hasattr(cached_driver, "_is_connection_alive"):
                if cached_driver._is_connection_alive():
                    return cached_driver
                else:
                    # Remove dead connection from cache
                    del cache[cache_key]
                    return None
            # No alive check method, assume it's still valid
            return cached_driver
        except Exception:
            # Error checking alive status, remove from cache
            del cache[cache_key]
            return None

    def _cache_driver(self, cache_key: str, driver: Any) -> None:
        """
        Store a driver instance in the class-level cache.

        This allows driver instances to be reused across multiple calls
        and even across multiple dispatcher instances of the same type.
        This matches the pattern used by module-level caching in
        solar/dispatcher.py.

        Args:
            cache_key: The cache key to store under.
            driver: The driver instance to cache.
        """
        self.__class__._driver_cache[cache_key] = driver

    def _clear_cache(self) -> None:
        """Clear all cached driver instances for this dispatcher type.

        This clears the class-level cache, affecting all instances of
        this dispatcher type.
        """
        self.__class__._driver_cache.clear()

    @classmethod
    def clear_cache(cls) -> None:
        """Clear all cached driver instances for this dispatcher type.

        This is a class method alternative to _clear_cache() that can be
        called without an instance:
            SupplyDispatcher.clear_cache()
        """
        cls._driver_cache.clear()

    @classmethod
    def get_cache_stats(cls) -> Dict[str, Any]:
        """Get statistics about the current cache state.

        Returns:
            A dictionary with cache statistics:
            - 'size': Number of cached drivers
            - 'keys': List of cache keys (for debugging)
        """
        return {
            "size": len(cls._driver_cache),
            "keys": list(cls._driver_cache.keys()),
        }

    def _resolve_net_and_driver(self, netname: str) -> Tuple[Any, int]:
        """
        Resolve net configuration and create/retrieve driver instance.

        This is the main entry point for getting a driver for a net.
        It handles net lookup, role validation, driver selection, and caching.

        Args:
            netname: The name of the net.

        Returns:
            A tuple of (driver_instance, channel_number).

        Raises:
            An appropriate error if resolution fails.
        """
        rec = self._find_net(netname)
        channel = self._resolve_channel(rec, netname)
        driver = self._make_driver(rec, netname, channel)
        return driver, channel

    def _make_driver(
        self, rec: Dict[str, Any], netname: str, channel: int
    ) -> Any:
        """
        Construct or retrieve a cached driver instance.

        Override this method in subclasses that need custom driver construction
        logic (e.g., different constructor signatures for different drivers).

        Args:
            rec: The net configuration record.
            netname: The net name.
            channel: The resolved channel number.

        Returns:
            A driver instance.

        Raises:
            ERROR_CLASS: If driver construction fails (via _make_error).
        """
        instrument = rec.get("instrument") or ""
        address = self._resolve_address(rec, netname)

        # Check cache first
        cache_key = self._get_cache_key(instrument, address, netname, channel)
        cached = self._get_cached_driver(cache_key)
        if cached is not None:
            return cached

        # Create new driver
        Driver = self._choose_driver(instrument)

        try:
            # Default construction - subclasses may override for custom signatures
            driver = Driver(address=address, channel=channel)
            self._cache_driver(cache_key, driver)
            return driver
        except Exception as exc:
            raise self._make_error(str(exc)) from exc
