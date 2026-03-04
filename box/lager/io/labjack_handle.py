# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Global LabJack handle manager - thread-safe singleton for managing LabJack device handles.

This module provides a centralized way to manage LabJack T7 device connections,
preventing issues where GPIO operations close SPI connections (or vice versa)
by maintaining a single shared handle.

Usage:
    from lager.io.labjack_handle import get_labjack_handle, release_labjack_handle

    # Get a handle (opens device if not already open)
    handle = get_labjack_handle()

    # Use handle for operations...
    ljm.eWriteName(handle, "FIO0", 1)

    # Optionally release when completely done (usually not needed)
    release_labjack_handle()
"""
from __future__ import annotations

import atexit
import os
import sys
import threading
import time
import importlib
import importlib.util
from typing import Optional

DEBUG = bool(os.environ.get("LAGER_LABJACK_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_LABJACK_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"LABJACK_HANDLE: {msg}\n")
        sys.stderr.flush()


def _demote_shadowing_paths() -> list[str]:
    """
    If any sys.path entry contains a shadowing 'labjack.py',
    move that path to the end so the real package wins.
    """
    demoted: list[str] = []
    for p in list(sys.path):
        try:
            if os.path.isfile(os.path.join(p, "labjack.py")):
                demoted.append(p)
        except Exception:
            pass
    for p in demoted:
        try:
            sys.path.remove(p)
            sys.path.append(p)
        except ValueError:
            pass
    return demoted


def _prefer_dist_path(dist_name: str, wanted_rel: str) -> None:
    """
    Put the distribution root for *dist_name* at the front of sys.path,
    specifically the parent folder that contains *wanted_rel* (e.g. 'labjack/ljm.py').
    """
    try:
        from importlib.metadata import distribution
    except Exception:
        return

    try:
        dist = distribution(dist_name)
        parent = None
        for f in (dist.files or []):
            if str(f).endswith(wanted_rel):
                abs_path = dist.locate_file(f)
                parent = os.path.dirname(os.path.dirname(os.fspath(abs_path)))
                break
        if parent and parent not in sys.path:
            sys.path.insert(0, parent)
    except Exception:
        pass  # best-effort


def _load_ljm():
    """
    Import the real pip-installed 'labjack.ljm', even if a shadowing 'labjack.py'
    exists earlier on sys.path.
    """
    first_exc = None

    # 1) Fast path
    try:
        from labjack import ljm as _ljm
        from labjack.ljm import ljm as _inner
        if getattr(_inner, '_staticLib', None) is None:
            raise ImportError(
                "labjack.ljm loaded but the native LJM library (libLabJackM.so) "
                "is not available. Install the LJM SDK from "
                "https://labjack.com/support/software/installers/ljm"
            )
        return _ljm
    except Exception as e1:
        first_exc = e1

    # 2) Hardened path
    mod = sys.modules.get("labjack")
    if mod is not None and not hasattr(mod, "__path__"):
        sys.modules.pop("labjack", None)
        sys.modules.pop("labjack.ljm", None)

    demoted = _demote_shadowing_paths()
    if demoted:
        _debug(f"demoted paths (shadowing labjack.py): {demoted}")

    _prefer_dist_path("labjack-ljm", "labjack/ljm.py")

    spec = importlib.util.find_spec("labjack.ljm")
    if spec is None:
        _debug("importlib.util.find_spec('labjack.ljm') returned None")
        _debug(f"sys.path (post-fix): {sys.path[:6]} ...")
        if first_exc:
            raise first_exc
        raise ImportError(
            "labjack.ljm not found. Ensure 'labjack-ljm' is installed in the container "
            "and that no local 'labjack.py' or 'labjack/' shadows the package."
        )

    try:
        _ljm = importlib.import_module("labjack.ljm")
        _inner = importlib.import_module("labjack.ljm.ljm")
        if getattr(_inner, '_staticLib', None) is None:
            raise ImportError(
                "labjack.ljm loaded but the native LJM library (libLabJackM.so) "
                "is not available. Install the LJM SDK from "
                "https://labjack.com/support/software/installers/ljm"
            )
        return _ljm
    except Exception as e2:
        _debug(f"Failed to import labjack.ljm; first={first_exc!r}, second={e2!r}")
        raise e2


# Load ljm module at import time
try:
    ljm = _load_ljm()
    _LJM_ERR = None
except Exception as _exc:  # pragma: no cover
    ljm = None
    _LJM_ERR = _exc


class LabJackHandleManager:
    """
    Thread-safe singleton for managing LabJack device handles.

    This class ensures that only one LabJack handle is opened at a time,
    preventing issues where multiple modules (GPIO, SPI, ADC, DAC) would
    each open and close their own connections.
    """
    _instance: Optional['LabJackHandleManager'] = None
    _lock = threading.Lock()

    def __new__(cls) -> 'LabJackHandleManager':
        if cls._instance is None:
            with cls._lock:
                # Double-check locking pattern
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._handle: Optional[int] = None
                    cls._instance._handle_lock = threading.Lock()
                    cls._instance._ref_count = 0
                    cls._instance._initialized = False  # Track if we've ever opened successfully
        return cls._instance

    def get_handle(self, device_type: str = "T7", connection: str = "ANY",
                   identifier: str = "ANY", max_retries: int = 5) -> int:
        """
        Get a LabJack handle, opening the device if necessary.

        Args:
            device_type: LabJack device type (default "T7")
            connection: Connection type ("ANY", "USB", "ETHERNET", "WIFI")
            identifier: Device identifier ("ANY" or specific serial/IP)
            max_retries: Maximum number of retry attempts for opening

        Returns:
            The LabJack device handle (integer)

        Raises:
            RuntimeError: If LabJack library is not available
            Exception: For LabJack communication errors
        """
        if ljm is None:
            raise RuntimeError(f"LabJack LJM library not available: {_LJM_ERR}")

        with self._handle_lock:
            # If we already have a valid handle, return it
            if self._handle is not None:
                # Verify the handle is still valid by reading a register
                try:
                    ljm.eReadName(self._handle, "SERIAL_NUMBER")
                    self._ref_count += 1
                    _debug(f"Reusing existing handle {self._handle}, ref_count={self._ref_count}")
                    return self._handle
                except Exception as e:
                    _debug(f"Existing handle {self._handle} is stale: {e}")
                    # Handle is stale, close it and reopen
                    try:
                        ljm.close(self._handle)
                    except Exception:
                        pass
                    self._handle = None
                    self._ref_count = 0

            # Open a new handle with retry logic
            # NOTE: We deliberately do NOT call closeAll() on first initialization.
            # closeAll() puts the LabJack SPI in a state where high throttle values
            # (low frequencies like 100kHz) fail with error 1239. Instead, we just
            # try to open the device directly and let the LJM library handle any
            # existing connections.
            if not self._initialized:
                _debug("First initialization - attempting direct open (no closeAll)")

            for attempt in range(max_retries):
                try:
                    _debug(f"Opening LabJack {device_type} attempt {attempt + 1}/{max_retries}")

                    if attempt > 0:
                        # On retry, just wait - don't call closeAll() as it's too aggressive
                        # and interferes with USB device recovery
                        sleep_time = 1.0 + (0.5 * attempt)
                        _debug(f"Sleeping {sleep_time}s before retry")
                        time.sleep(sleep_time)
                    else:
                        # Minimal delay on first attempt
                        time.sleep(0.1)

                    # Set open timeout to prevent indefinite blocking
                    try:
                        ljm.writeLibraryConfigS("LJM_OPEN_TCP_DEVICE_TIMEOUT_MS", 2000)
                        ljm.writeLibraryConfigS("LJM_OPEN_USB_DEVICE_TIMEOUT_MS", 2000)
                    except Exception as timeout_err:
                        _debug(f"Warning: Could not set LJM timeout: {timeout_err}")

                    # Open the device
                    self._handle = ljm.openS(device_type, connection, identifier)
                    self._ref_count = 1
                    self._initialized = True
                    _debug(f"Successfully opened handle {self._handle}")
                    return self._handle

                except Exception as e:
                    error_str = str(e)
                    _debug(f"Attempt {attempt + 1} failed: {e}")

                    # Retry on recoverable errors:
                    # - 1227: LJME_DEVICE_NOT_FOUND (device initializing)
                    # - 1230: LJME_DEVICE_ALREADY_OPEN (device locked)
                    # - 1239: LJME_RECONNECT_FAILED (connection lost)
                    is_recoverable = (
                        "1227" in error_str or
                        "1230" in error_str or
                        "1239" in error_str or
                        "RECONNECT" in error_str.upper() or
                        "NOT_FOUND" in error_str.upper()
                    )

                    if is_recoverable and attempt < max_retries - 1:
                        _debug(f"Recoverable error, will retry after delay")
                        time.sleep(1.0)
                        continue
                    else:
                        raise

            # Should not reach here, but just in case
            raise RuntimeError("Failed to open LabJack device after all retries")

    def release_handle(self) -> None:
        """
        Release a reference to the LabJack handle.

        The handle is only actually closed when all references are released.
        This method is optional - handles can be kept open for the lifetime
        of the process.
        """
        with self._handle_lock:
            if self._handle is not None and self._ref_count > 0:
                self._ref_count -= 1
                _debug(f"Released handle reference, ref_count={self._ref_count}")
                # Note: We don't close the handle here to avoid the
                # open/close overhead. The handle stays open until
                # force_close() is called or the process exits.

    def force_close(self) -> None:
        """
        Force close the LabJack handle regardless of reference count.

        Use this for cleanup or when you need to reset the device state.
        """
        with self._handle_lock:
            if self._handle is not None:
                try:
                    _debug(f"Force closing handle {self._handle}")
                    ljm.close(self._handle)
                    time.sleep(0.1)  # Allow kernel to release device
                except Exception as e:
                    _debug(f"Error closing handle: {e}")
                finally:
                    self._handle = None
                    self._ref_count = 0

    def close_all(self) -> int:
        """
        Close all LabJack handles (not just the managed one).

        Returns:
            Number of handles closed
        """
        with self._handle_lock:
            self._handle = None
            self._ref_count = 0
            self._initialized = False  # Reset so next open does full initialization
            if ljm is not None:
                try:
                    num_closed = ljm.closeAll()
                    _debug(f"Closed {num_closed} handles via closeAll()")
                    return num_closed
                except Exception as e:
                    _debug(f"closeAll() failed: {e}")
            return 0


# Module-level convenience functions
_manager: Optional[LabJackHandleManager] = None


def _get_manager() -> LabJackHandleManager:
    """Get the singleton manager instance."""
    global _manager
    if _manager is None:
        _manager = LabJackHandleManager()
    return _manager


def get_labjack_handle(device_type: str = "T7", connection: str = "ANY",
                       identifier: str = "ANY") -> int:
    """
    Get a LabJack handle from the global manager.

    Args:
        device_type: LabJack device type (default "T7")
        connection: Connection type ("ANY", "USB", "ETHERNET", "WIFI")
        identifier: Device identifier ("ANY" or specific serial/IP)

    Returns:
        The LabJack device handle (integer)
    """
    return _get_manager().get_handle(device_type, connection, identifier)


def release_labjack_handle() -> None:
    """Release a reference to the global LabJack handle."""
    _get_manager().release_handle()


def force_close_labjack() -> None:
    """Force close the global LabJack handle."""
    _get_manager().force_close()


def close_all_labjack_handles() -> int:
    """Close all LabJack handles."""
    return _get_manager().close_all()


# Register cleanup handler to close handles on process exit
def _cleanup_on_exit() -> None:
    """Close all LabJack handles when the process exits."""
    global _manager
    if _manager is not None:
        try:
            _debug("Process exiting - closing all LabJack handles")
            _manager.close_all()
        except Exception as e:
            _debug(f"Error during exit cleanup: {e}")


atexit.register(_cleanup_on_exit)


class PinRegistry:
    """Track which LabJack DIO pins are claimed by which subsystem (SPI/I2C/GPIO).

    Singleton that emits a warning to stderr when multiple subsystems claim the
    same physical pin within a single process (e.g. inside a ``lager python``
    script).  Separate CLI commands run in separate processes so the registry
    resets naturally.
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._pin_claims = {}   # pin_name -> (subsystem, role)
                    inst._warned = set()    # (pin_name, old_subsys, new_subsys)
                    cls._instance = inst
        return cls._instance

    @staticmethod
    def dio_to_name(dio_num: int) -> str:
        """Convert a DIO number (0-22) to the LabJack register name."""
        if dio_num <= 7:
            return f"FIO{dio_num}"
        elif dio_num <= 15:
            return f"EIO{dio_num - 8}"
        elif dio_num <= 19:
            return f"CIO{dio_num - 16}"
        elif dio_num <= 22:
            return f"MIO{dio_num - 20}"
        return f"DIO{dio_num}"

    def register_pins(self, subsystem: str, pins: dict) -> None:
        """Register *pins* for *subsystem*.  Warn on cross-subsystem conflicts.

        Args:
            subsystem: Label such as ``"SPI"``, ``"I2C"``, or ``"GPIO"``.
            pins: Mapping of pin name (e.g. ``"FIO0"``) to role
                  (e.g. ``"CS"``, ``"SDA"``, ``"GPIO"``).
        """
        for pin_name, role in pins.items():
            existing = self._pin_claims.get(pin_name)
            if existing and existing[0] != subsystem:
                warn_key = (pin_name, existing[0], subsystem)
                if warn_key not in self._warned:
                    self._warned.add(warn_key)
                    sys.stderr.write(
                        f"WARNING: Pin {pin_name} is already claimed by "
                        f"{existing[0]} ({existing[1]}). Now being used by "
                        f"{subsystem} ({role}).\n"
                        f"  Using the same physical pin for different functions "
                        f"in one script may cause unexpected behavior.\n"
                    )
                    sys.stderr.flush()
            else:
                self._pin_claims[pin_name] = (subsystem, role)


def register_labjack_pins(subsystem: str, pins: dict) -> None:
    """Module-level helper so callers can avoid importing the class directly."""
    PinRegistry().register_pins(subsystem, pins)


# Export the ljm module and error for use by other modules
__all__ = [
    'LabJackHandleManager',
    'get_labjack_handle',
    'release_labjack_handle',
    'force_close_labjack',
    'close_all_labjack_handles',
    'ljm',
    '_LJM_ERR',
    'PinRegistry',
    'register_labjack_pins',
]
