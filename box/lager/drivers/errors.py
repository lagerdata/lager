# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Exceptions a custom-driver call can raise, each with its HTTP status.

Kept in a module with no imports so net_command.py can map them without
pulling in the registry or subprocess code.

Each message is written for the user, and handlers return ``e.message``
rather than ``str(e)``: it is the sentence the code chose, not whatever text
an exception happens to carry.
"""


class _DriverError(Exception):
    status = 500

    def __init__(self, message):
        super().__init__(message)
        self.message = str(message)


class DriverUnavailable(_DriverError):
    """The driver is not installed, or its bundle failed to load (503)."""

    status = 503


class DriverBusy(_DriverError):
    """The net, or the net it is built on, is in use by another session (409)."""

    status = 409


class DriverTimeout(_DriverError):
    """The action ran past its timeout and was killed (504)."""

    status = 504


class DriverFailed(_DriverError):
    """The driver raised, or exited without a result (502)."""

    status = 502
