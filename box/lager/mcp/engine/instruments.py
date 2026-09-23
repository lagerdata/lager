# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The bench's live instrument inventory, behind a short cache.

``discover_bench`` used to answer ``instruments: []`` on every real box: the
file-based loader had nothing to read them from, and the only lister,
``lager.http_handlers.usb_scanner.list_instruments``, is a live USB scan that
also writes a G-code handshake at candidate arm ports. Calling that on every
MCP request would be too much; never calling it left agents blind to what is
plugged in.

So the scan runs at most once per ``DEFAULT_TTL_S`` and only when something
asks. A scan that raises is remembered as "no instruments" for the same
period rather than retried on the next request, so a broken USB stack cannot
turn every tool call into a scan.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from ..schemas.bench import InstrumentDescriptor

logger = logging.getLogger(__name__)

#: How long one scan's result is served before the next request scans again.
DEFAULT_TTL_S = 60.0


def _scan() -> list[dict]:
    # Imported here, not at module level: the scanner pulls in the box's
    # hardware libraries, which a test host does not have.
    from lager.http_handlers.usb_scanner import list_instruments

    return list_instruments()


class InstrumentCache:
    """Serves the last scan for ``ttl_s`` seconds, then scans again."""

    def __init__(
        self,
        scan: Callable[[], list[dict]] = _scan,
        *,
        ttl_s: float = DEFAULT_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._scan = scan
        self._ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._records: list[dict] = []
        self._fetched_at: float | None = None
        self._last_error: str | None = None

    def get(self, *, force: bool = False) -> list[dict]:
        """The instrument records, scanning first when the cache is stale.

        Callers that arrive while a scan is running wait for it and share the
        result: one scan, not one per caller.
        """
        with self._lock:
            now = self._clock()
            stale = (
                force
                or self._fetched_at is None
                or now - self._fetched_at >= self._ttl_s
            )
            if stale:
                self._refresh_locked()
                self._fetched_at = now
            return list(self._records)

    def _refresh_locked(self) -> None:
        try:
            found = self._scan()
        except Exception as exc:  # a scan failure must never fail a tool call
            self._records = []
            self._last_error = str(exc)
            logger.warning(
                "instrument scan failed; reporting no instruments for the "
                "next %.0f s: %s", self._ttl_s, exc,
            )
            return
        self._records = [r for r in found if isinstance(r, dict)] if isinstance(found, list) else []
        self._last_error = None
        logger.info(
            "instrument scan for the bench found %d instrument(s)", len(self._records),
        )

    @property
    def last_error(self) -> str | None:
        """Why the last scan reported nothing, or None when it succeeded."""
        return self._last_error

    def invalidate(self) -> None:
        """Make the next ``get`` scan again."""
        with self._lock:
            self._fetched_at = None


_cache = InstrumentCache()


def cached_instrument_records(*, force: bool = False) -> list[dict]:
    """The scanner's raw records, from the process-wide cache."""
    return _cache.get(force=force)


def instrument_from_record(rec: dict[str, Any]) -> InstrumentDescriptor:
    """Build an ``InstrumentDescriptor`` from one scanner (or legacy) record.

    The USB scanner reports ``channels`` as ``{role: [channel, ...]}``; older
    callers passed a flat list. Both are accepted: the flat ``channels`` on
    the descriptor lists every channel as ``"<role>:<channel>"`` when roles
    are known, and the per-role map is kept under ``metadata``.
    """
    name = rec.get("name") or rec.get("instrument") or ""
    raw_channels = rec.get("channels")
    channels: list[str] = []
    metadata: dict[str, Any] = {}
    if isinstance(raw_channels, dict):
        by_role = {
            str(role): [str(ch) for ch in (chs or [])]
            for role, chs in raw_channels.items()
        }
        channels = [f"{role}:{ch}" for role, chs in by_role.items() for ch in chs]
        metadata["channels_by_role"] = by_role
    elif isinstance(raw_channels, list):
        channels = [str(ch) for ch in raw_channels]

    net_types = rec.get("net_type")
    capabilities = [str(t) for t in net_types] if isinstance(net_types, list) else []

    for key in ("serial", "tty_path", "custom"):
        if rec.get(key) not in (None, "", False):
            metadata[key] = rec[key]

    return InstrumentDescriptor(
        name=str(name),
        instrument_type=str(rec.get("type") or rec.get("instrument") or name),
        connection=str(rec.get("address") or rec.get("connection") or ""),
        channels=channels,
        capabilities=capabilities,
        metadata=metadata,
    )


def cached_instruments(*, force: bool = False) -> list[InstrumentDescriptor]:
    """The bench's instruments as descriptors, from the process-wide cache."""
    return [instrument_from_record(r) for r in cached_instrument_records(force=force)]


__all__ = [
    "DEFAULT_TTL_S",
    "InstrumentCache",
    "cached_instrument_records",
    "cached_instruments",
    "instrument_from_record",
]
