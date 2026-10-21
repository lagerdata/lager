# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence


class HsmBase(ABC):
    """Abstract HSM net; do NOT instantiate directly.

    Methods that need a login take ``auth_key_id`` and ``password`` as
    keyword-only arguments. The driver opens the device and a session for
    each call and closes both before it returns, so it holds no USB claim
    and keeps no credential between calls.
    """

    def __init__(self, name: str, address: Optional[str] = None) -> None:
        self._name = name
        self._address = address

    @property
    def name(self) -> str:
        return self._name

    @property
    def address(self) -> Optional[str]:
        return self._address

    @abstractmethod
    def info(self) -> Dict[str, Any]:
        """Firmware version, serial, log usage, part number and algorithms. No login."""
        raise NotImplementedError

    @abstractmethod
    def echo(self, data: bytes) -> bytes:
        """Send ``data`` to the device and return what it sends back. No login."""
        raise NotImplementedError

    @abstractmethod
    def blink(self, seconds: int = 5, *, auth_key_id: int = 1, password: str) -> None:
        """Blink the device LED for ``seconds`` (0-255)."""
        raise NotImplementedError

    @abstractmethod
    def list_objects(self, type: Optional[str] = None, label: Optional[str] = None, *,
                     auth_key_id: int = 1, password: str) -> List[Dict[str, Any]]:
        """List stored objects, optionally filtered by object type and label."""
        raise NotImplementedError

    @abstractmethod
    def get_object_info(self, object_id: int, object_type: str = "asymmetric-key", *,
                        auth_key_id: int = 1, password: str) -> Dict[str, Any]:
        """Return the metadata of one object."""
        raise NotImplementedError

    @abstractmethod
    def delete_object(self, object_id: int, object_type: str = "asymmetric-key", *,
                      auth_key_id: int = 1, password: str) -> None:
        """Delete one object."""
        raise NotImplementedError

    @abstractmethod
    def generate_key(self, object_id: int, algorithm: str, label: str = "",
                     domains: Sequence[int] = (1,), capabilities: Optional[Sequence[str]] = None, *,
                     auth_key_id: int = 1, password: str) -> Dict[str, Any]:
        """Generate an asymmetric key on the device. Returns ``{"id", "public_key_pem"}``."""
        raise NotImplementedError

    @abstractmethod
    def import_key(self, object_id: int, private_key_pem: str, label: str = "",
                   domains: Sequence[int] = (1,), capabilities: Optional[Sequence[str]] = None, *,
                   auth_key_id: int = 1, password: str) -> int:
        """Import an unencrypted PEM private key. Returns the object ID."""
        raise NotImplementedError

    @abstractmethod
    def get_public_key(self, object_id: int, *, auth_key_id: int = 1, password: str) -> str:
        """Return the public key of an asymmetric key as PEM."""
        raise NotImplementedError

    @abstractmethod
    def sign(self, object_id: int, data: bytes, mechanism: str, *,
             auth_key_id: int = 1, password: str) -> bytes:
        """Sign ``data`` with an asymmetric key. Returns the signature bytes."""
        raise NotImplementedError

    @abstractmethod
    def reset(self, *, auth_key_id: int = 1, password: str) -> None:
        """Erase every object and restore the factory login. Cannot be undone."""
        raise NotImplementedError

    @abstractmethod
    def session(self, *, auth_key_id: int = 1, password: str):
        """Context manager that yields the logged-in vendor session object."""
        raise NotImplementedError
