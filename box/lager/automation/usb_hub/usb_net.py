# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager

from lager.util.device_lock import device_lock

# ─────────────  Exclusive access to a physical USB hub  ─────────────
#
# A hub's USB/libusb interface is EXCLUSIVE, yet it is driven from several box
# processes (box_http_server's `lager usb` path, the MCP server, and each
# `lager python` test — its own subprocess) AND potentially several threads in
# one of them. Two layers are needed:
#   * cross-process — `device_lock` (fcntl.flock, shared /tmp). Its manager is
#     re-entrant WITHIN a process, so it alone does not serialise threads.
#   * in-process — a per-hub `threading.Lock`.
# `hub_access` combines both (the same belt-and-suspenders as hardware_service),
# keyed on the physical hub so different hubs never block each other.
_local_hub_locks: dict = {}
_local_hub_locks_guard = threading.Lock()


def _local_hub_lock(key: str) -> threading.Lock:
    with _local_hub_locks_guard:
        lock = _local_hub_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _local_hub_locks[key] = lock
        return lock


@contextmanager
def hub_access(key: str, timeout: float):
    """Exclusive access to one physical USB hub, within AND across processes.
    Wrap a driver's whole open→operate→release cycle in this."""
    with _local_hub_lock(key):
        with device_lock(key, timeout=timeout):
            yield


class USBNet(ABC):
    """Abstract base class for USB network controllers."""
    @abstractmethod
    def enable(self, net_name, port):
        """Enable (power on) the specified port on the given USB net."""
        raise NotImplementedError()

    @abstractmethod
    def disable(self, net_name, port):
        """Disable (power off) the specified port on the given USB net."""
        raise NotImplementedError()

    @abstractmethod
    def toggle(self, net_name, port):
        """Toggle the power state of the specified port on the given USB net.

        Returns:
            bool: the resulting port state — True if the port is now enabled
            (powered on), False if it is now disabled (powered off).
        """
        raise NotImplementedError()

    @abstractmethod
    def state(self, net_name, port):
        """Read the current power state of the specified port without changing it.

        Returns:
            bool: True if the port is currently enabled (powered on), False if
            it is currently disabled (powered off).
        """
        raise NotImplementedError()
    
    # ─────────────  Common backend exceptions  ─────────────

class USBBackendError(RuntimeError):
    """Base class for all lager.usb_hub backend failures."""


class LibraryMissingError(USBBackendError):
    """Required vendor SDK (BrainStem, pykush, …) is not present."""


class DeviceNotFoundError(USBBackendError):
    """Requested hub (by serial) could not be opened."""


class PortStateError(USBBackendError):
    """Hub reported an error while reading or changing port state."""
