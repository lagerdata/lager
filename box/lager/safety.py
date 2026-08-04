# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Per-net safety interlocks for instrument commands.

A bench can be driven by a script nobody read closely before it ran. A net
wired to a 3.7 V cell should not accept a 24 V setpoint because a loop index
leaked into a voltage argument, and a relay should not be cycled ten thousand
times because a retry had no backoff. This module enforces a ceiling that the
caller cannot raise.

Limits live on the saved-net record, so they describe the *net* -- the wire and
whatever is on the end of it -- rather than the instrument driving it. Swap the
supply and the ceiling follows the net. A net with no ``safety_limits`` key is
unrestricted, which keeps every existing bench working untouched.

Two tiers of enforcement, deliberately unequal:

* **Hard tier.** ``lager.hardware_service.invoke`` calls :func:`check` before
  dispatching any command to a driver. That service is a separate process, and
  for the nets it covers it is the only route to the instrument, so a test
  script cannot get around it.

* **Advisory tier.** The in-process dispatchers (``lager.power.*.dispatcher``)
  call the same :func:`check` before opening a VISA session directly. A script
  that imports a driver module itself, or shells out, defeats this tier. That
  is inherent to running inside the caller's own process -- it is a speed bump
  for honest mistakes, not a boundary. The guarantee rests on the hard tier.

``max_power`` is intentionally not supported. A single setter call establishes
one of voltage or current, never both, so a per-call power ceiling could not be
evaluated honestly. Declaring a limit that is silently ignored is worse than
declaring none.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

from lager.cache import get_nets_cache


class SafetyViolation(Exception):
    """A command was refused because it exceeded a configured net limit.

    Carries structured fields so callers can render a useful message without
    parsing prose.
    """

    def __init__(
        self,
        net_name: str,
        limit_kind: str,
        limit_value: float,
        requested: float,
        function_name: str,
    ) -> None:
        self.net_name = net_name
        self.limit_kind = limit_kind
        self.limit_value = limit_value
        self.requested = requested
        self.function_name = function_name
        super().__init__(
            f"Refused {function_name}({requested}) on net '{net_name}': "
            f"exceeds {limit_kind} of {limit_value} configured for this net."
        )

    def as_dict(self) -> Dict[str, Any]:
        """Serialize for an HTTP error body."""
        return {
            "error": str(self),
            "reason": "safety_limit_exceeded",
            "net": self.net_name,
            "function": self.function_name,
            "limit_kind": self.limit_kind,
            "limit": self.limit_value,
            "requested": self.requested,
        }


class RateLimitExceeded(Exception):
    """A guarded command was called too often on one net.

    Bounds a runaway loop. The window is per (net, function), so a test that
    legitimately sweeps voltage is not penalised by an unrelated relay cycling
    on another net.
    """

    def __init__(self, net_name: str, function_name: str, max_calls: int, window_s: float) -> None:
        self.net_name = net_name
        self.function_name = function_name
        self.max_calls = max_calls
        self.window_s = window_s
        super().__init__(
            f"Refused {function_name} on net '{net_name}': more than {max_calls} calls "
            f"in {window_s:g}s. Slow the loop, or raise LAGER_SAFETY_RATE_MAX."
        )

    def as_dict(self) -> Dict[str, Any]:
        """Serialize for an HTTP error body."""
        return {
            "error": str(self),
            "reason": "safety_rate_limit_exceeded",
            "net": self.net_name,
            "function": self.function_name,
            "max_calls": self.max_calls,
            "window_seconds": self.window_s,
        }


# Which limit key applies to which (role, driver method).
#
# Keyed on DRIVER method names -- what actually crosses the wire as the
# "function" field -- not on the dispatcher function that fronts them. The two
# diverge: the battery dispatcher's `set_ovp` calls `drv.ovp`, and the eload
# dispatcher's `set_constant_current` calls `drv.set_current`. Keying on the
# dispatcher names would guard nothing.
#
# A method absent from this table takes no value check: reads, state queries,
# and mode setters pass through untouched.
_VALUE_LIMITS: Dict[Tuple[str, str], str] = {
    # Programmable supplies: voltage()/current() set, ovp()/ocp() trip.
    ("power-supply", "voltage"): "max_voltage",
    ("power-supply", "ovp"): "max_voltage",
    ("power-supply", "current"): "max_current",
    ("power-supply", "ocp"): "max_current",
    ("power-supply-2q", "voltage"): "max_voltage",
    ("power-supply-2q", "ovp"): "max_voltage",
    ("power-supply-2q", "current"): "max_current",
    ("power-supply-2q", "ocp"): "max_current",
    # Battery simulators. There is no `voltage` setter -- the cell profile is
    # described by open-circuit and full/empty terminal voltages instead, and
    # every one of them can present a damaging potential to the DUT.
    ("battery", "voc"): "max_voltage",
    ("battery", "voltage_full"): "max_voltage",
    ("battery", "voltage_empty"): "max_voltage",
    ("battery", "ovp"): "max_voltage",
    ("battery", "current_limit"): "max_current",
    ("battery", "ocp"): "max_current",
    # Electronic loads take their setpoint positionally: set_current(2.0).
    ("eload", "set_current"): "max_current",
    ("eload", "set_voltage"): "max_voltage",
}

# Deliberately absent: the `solar` role. Its driver surface is irradiance,
# resistance, and MPP readbacks -- there is no voltage or current setpoint for
# a ceiling to apply to. Listing it would be decoration.

# Methods that count against the call-rate cap: everything with a value check,
# plus output enables and the protection-trip clears. Repeatedly clearing OVP
# is how a loop re-applies an overvoltage the instrument just refused.
_RATE_LIMITED_FUNCTIONS = frozenset(
    {function for _, function in _VALUE_LIMITS}
    | {"clear_ovp", "clear_ocp", "enable", "disable"}
)

_GUARDED_ROLES = frozenset({role for role, _ in _VALUE_LIMITS})


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _rate_window_seconds() -> float:
    return _env_float("LAGER_SAFETY_RATE_WINDOW", 10.0)


def _rate_max_calls() -> int:
    return _env_int("LAGER_SAFETY_RATE_MAX", 60)


# Fixed-window counter, keyed by (net, function): (window_start, count).
# Fixed rather than sliding on purpose -- a runaway loop trips it in the first
# window either way, and the state stays O(1) per key with no timestamp list.
_rate_state: Dict[Tuple[str, str], Tuple[float, int]] = {}
_rate_lock = threading.Lock()


def reset_rate_state() -> None:
    """Clear all rate-limit counters. For tests."""
    with _rate_lock:
        _rate_state.clear()


def limits_for_net(net_name: str) -> Optional[Dict[str, Any]]:
    """Return the ``safety_limits`` dict for a net, or None if unrestricted.

    Always read through :class:`lager.cache.NetsCache`, never from a
    caller-supplied payload -- see :func:`check`.
    """
    if not net_name:
        return None
    record = get_nets_cache().find_by_name(net_name)
    if not record:
        return None
    limits = record.get("safety_limits")
    if not isinstance(limits, dict) or not limits:
        return None
    return limits


def role_for_net(net_name: str) -> Optional[str]:
    """Return the configured role for a net, or None if it is not saved."""
    if not net_name:
        return None
    record = get_nets_cache().find_by_name(net_name)
    if not record:
        return None
    role = record.get("role")
    return role if isinstance(role, str) else None


def is_guarded(role: Optional[str], function_name: str) -> bool:
    """True when this (role, function) is subject to any interlock."""
    if not role:
        return False
    if (role, function_name) in _VALUE_LIMITS:
        return True
    return role in _GUARDED_ROLES and function_name in _RATE_LIMITED_FUNCTIONS


def _coerce_float(candidate: Any) -> Optional[float]:
    """Best-effort numeric coercion. None when the value is not a number."""
    if candidate is None or isinstance(candidate, bool):
        return None
    try:
        return float(candidate)
    except (TypeError, ValueError):
        return None


def _enforce(
    net_name: str,
    function_name: str,
    limits: Dict[str, Any],
    limit_key: str,
    requested: Optional[float],
) -> None:
    """Raise if a requested value exceeds the configured ceiling for ``limit_key``."""
    if requested is None:
        return
    configured = limits.get(limit_key)
    if not isinstance(configured, (int, float)) or isinstance(configured, bool):
        return
    # abs(): a two-quadrant supply legitimately sources negative voltage, and a
    # -24 V setpoint is exactly as damaging as +24 V.
    if abs(requested) > float(configured):
        raise SafetyViolation(
            net_name=net_name,
            limit_kind=limit_key,
            limit_value=float(configured),
            requested=requested,
            function_name=function_name,
        )


def extract_value(args: Any, kwargs: Any) -> Optional[float]:
    """Pull the setpoint out of a call, positional or keyword.

    The power setters are all ``f(value=None, ...)``, and callers use both
    forms. Returns None when no numeric setpoint is present, which is the
    read case (``voltage()`` with no argument queries rather than sets).
    """
    candidate: Any = None
    if isinstance(kwargs, dict) and kwargs.get("value") is not None:
        candidate = kwargs["value"]
    elif isinstance(args, (list, tuple)) and args and args[0] is not None:
        candidate = args[0]

    if candidate is None or isinstance(candidate, bool):
        return None
    try:
        return float(candidate)
    except (TypeError, ValueError):
        return None


def check(
    net_name: Optional[str],
    function_name: str,
    args: Any = None,
    kwargs: Any = None,
    role: Optional[str] = None,
) -> None:
    """Enforce the configured limits for one command. Raises on violation.

    Args:
        net_name: The saved-net name. Required for any guarded call.
        function_name: The driver method about to run.
        args: Positional arguments for that method.
        kwargs: Keyword arguments for that method.
        role: The net's role, if the caller already resolved it. Looked up
            when omitted.

    Raises:
        SafetyViolation: The requested setpoint exceeds a configured ceiling.
        RateLimitExceeded: Too many guarded calls on this net in the window.

    Note the ordering: the value check runs before the rate check, so a
    genuinely unsafe setpoint is reported as unsafe rather than as "too fast"
    when both would trip.
    """
    resolved_role = role if role is not None else role_for_net(net_name or "")
    if not is_guarded(resolved_role, function_name):
        return

    limits = limits_for_net(net_name or "")
    if not limits:
        # No ceiling configured for this net. Unrestricted by design: benches
        # that predate limits keep working, and the rate cap is a property of
        # having limits at all, not a global throttle.
        return

    limit_key = _VALUE_LIMITS.get((resolved_role or "", function_name))
    if limit_key is not None:
        _enforce(net_name or "", function_name, limits, limit_key, extract_value(args, kwargs))

    # The power setters take the protection trips inline -- voltage(value=3.7,
    # ovp=30.0) sets a 30 V trip while the setpoint itself looks harmless.
    # Checking only the primary value would leave the instrument's own guard
    # raised above the net's ceiling, which is most of the protection gone.
    if isinstance(kwargs, dict):
        for trip_kwarg, trip_limit_key in (("ovp", "max_voltage"), ("ocp", "max_current")):
            if kwargs.get(trip_kwarg) is not None:
                _enforce(
                    net_name or "",
                    f"{function_name}({trip_kwarg}=)",
                    limits,
                    trip_limit_key,
                    _coerce_float(kwargs[trip_kwarg]),
                )

    if function_name in _RATE_LIMITED_FUNCTIONS:
        _check_rate(net_name or "", function_name)


class DestructiveOperationRefused(Exception):
    """A destructive debug operation was refused for this net.

    Erase and flash are the normal business of a hardware test, so they are
    permitted unless a net explicitly opts out. A net carrying a golden sample,
    a provisioned production unit, or a part whose bootloader cannot be
    recovered sets ``allow_destructive: false`` and stops being writable.
    """

    def __init__(self, net_name: str, operation: str) -> None:
        self.net_name = net_name
        self.operation = operation
        super().__init__(
            f"Refused {operation} on net '{net_name}': this net is configured "
            f"with allow_destructive: false."
        )

    def as_dict(self) -> Dict[str, Any]:
        """Serialize for an HTTP error body."""
        return {
            "error": str(self),
            "reason": "safety_destructive_refused",
            "net": self.net_name,
            "operation": self.operation,
        }


def check_destructive(net: Any, operation: str) -> None:
    """Gate an irreversible debug operation (erase, flash) on a net's policy.

    Deliberately NOT an env flag defaulting to off. Flashing is what a hardware
    test does -- gating it globally would break every existing bench and the
    automated review path on upgrade, which is a real cost paid for no gain on
    the benches nobody was worried about. Opting a specific net out is the
    proportionate control, and it stays dormant until someone uses it.

    Args:
        net: The net record or payload identifying the target.
        operation: Human-readable operation name, used in the refusal.

    Raises:
        DestructiveOperationRefused: The net forbids destructive operations.
    """
    net_name = resolve_net_name(net)
    if not net_name:
        return
    limits = limits_for_net(net_name)
    if not limits:
        return
    if limits.get("allow_destructive") is False:
        raise DestructiveOperationRefused(net_name, operation)


class UnidentifiedNet(Exception):
    """A guarded command arrived without enough information to identify its net.

    Only raised on a box that has at least one net with limits configured. On a
    bench with no limits anywhere the interlock is dormant and this never fires,
    so adding limits is what turns enforcement on -- there is no flag day.
    """

    def __init__(self, function_name: str) -> None:
        self.function_name = function_name
        super().__init__(
            f"Refused {function_name}: the target net could not be identified, and this box "
            f"has safety limits configured. Include the net name in the request."
        )

    def as_dict(self) -> Dict[str, Any]:
        """Serialize for an HTTP error body."""
        return {
            "error": str(self),
            "reason": "safety_net_unidentified",
            "function": self.function_name,
        }


_POSSIBLY_GUARDED_FUNCTIONS = frozenset(
    set(_RATE_LIMITED_FUNCTIONS) | {function for _, function in _VALUE_LIMITS}
)


def any_limits_configured() -> bool:
    """True when at least one saved net on this box declares safety limits."""
    for record in get_nets_cache().get_nets():
        limits = record.get("safety_limits")
        if isinstance(limits, dict) and limits:
            return True
    return False


def resolve_net_name(net_info: Any) -> Optional[str]:
    """Identify which saved net a request refers to.

    Prefers an explicit ``name``. Falls back to matching address and channel
    against the saved nets, which covers callers built before the name was
    carried on the wire.

    The returned name is only ever used as a *lookup key* into the saved-net
    records. Limits themselves are never read from ``net_info`` -- that payload
    is attacker-controlled on a shared box, and trusting a limit from it would
    let a caller raise its own ceiling by asking nicely.
    """
    if not isinstance(net_info, dict):
        return None

    name = net_info.get("name")
    if isinstance(name, str) and name:
        return name

    address = net_info.get("address")
    if not address:
        return None
    channel = net_info.get("channel")
    for record in get_nets_cache().get_nets():
        if record.get("address") != address:
            continue
        if channel is not None and record.get("channel") != channel:
            continue
        candidate = record.get("name")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def check_invocation(
    net_info: Any,
    function_name: str,
    args: Any = None,
    kwargs: Any = None,
) -> None:
    """Hard-tier entry point, called by ``lager.hardware_service.invoke``.

    Raises:
        SafetyViolation: The setpoint exceeds a configured ceiling.
        RateLimitExceeded: Too many guarded calls on this net.
        UnidentifiedNet: A guarded call whose net could not be identified on a
            box that has limits configured.
    """
    net_name = resolve_net_name(net_info)
    if not net_name:
        if function_name in _POSSIBLY_GUARDED_FUNCTIONS and any_limits_configured():
            raise UnidentifiedNet(function_name)
        return
    check(net_name, function_name, args, kwargs)


def _check_rate(net_name: str, function_name: str) -> None:
    """Fixed-window call cap for one (net, function)."""
    window = _rate_window_seconds()
    max_calls = _rate_max_calls()
    key = (net_name, function_name)
    now = time.monotonic()

    with _rate_lock:
        window_start, count = _rate_state.get(key, (now, 0))
        if now - window_start >= window:
            window_start, count = now, 0
        count += 1
        _rate_state[key] = (window_start, count)
        exceeded = count > max_calls

    if exceeded:
        raise RateLimitExceeded(net_name, function_name, max_calls, window)
