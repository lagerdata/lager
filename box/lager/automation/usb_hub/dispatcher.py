# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging
import os
from typing import Dict

from .usb_net import USBNet
from .acroname import AcronameUSBNet
from .ykush import YKUSHUSBNet

logger = logging.getLogger(__name__)

# ANSI color codes
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'

# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
NET_DEFS_PATH = os.environ.get(
    "LAGER_USB_NETS_FILE",
    os.path.expanduser("/etc/lager/saved_nets.json"),
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _load_net_definitions() -> Dict[str, Dict]:
    """Return mapping net_name → {port, instrument, address, …} (USB only)."""
    if not os.path.exists(NET_DEFS_PATH):
        raise FileNotFoundError(f"USB nets file not found: {NET_DEFS_PATH}")

    with open(NET_DEFS_PATH, "r") as fh:
        data = json.load(fh)

    mapping: Dict[str, Dict] = {}
    for row in data if isinstance(data, list) else []:
        if (row.get("role") or row.get("net_type")) != "usb":
            continue
        port = row.get("pin") or row.get("channel")
        if port is None:
            continue
        mapping[row["name"]] = {
            "port": int(port),
            "instrument": row.get("instrument", ""),
            "address": row.get("address", ""),
        }

    if not mapping:
        raise RuntimeError("No USB nets defined in saved_nets.json")
    return mapping


def _controller_for(net_info: Dict) -> USBNet:
    instr = (net_info.get("instrument") or "").lower()
    if "acroname" in instr:
        return AcronameUSBNet(net_info)
    if "ykush" in instr:
        return YKUSHUSBNet(net_info)
    raise RuntimeError(f"Unsupported USB instrument type '{instr or 'unknown'}'")


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def enable(net_name: str) -> None:
    nets = _load_net_definitions()
    if net_name not in nets:
        raise KeyError(f"USB net '{net_name}' not found")
    info = nets[net_name]
    _controller_for(info).enable(net_name, info["port"])
    print(f"{GREEN}USB port '{net_name}' enabled{RESET}")


def disable(net_name: str) -> None:
    nets = _load_net_definitions()
    if net_name not in nets:
        raise KeyError(f"USB net '{net_name}' not found")
    info = nets[net_name]
    _controller_for(info).disable(net_name, info["port"])
    print(f"{GREEN}USB port '{net_name}' disabled{RESET}")


def toggle(net_name: str) -> bool:
    nets = _load_net_definitions()
    if net_name not in nets:
        raise KeyError(f"USB net '{net_name}' not found")
    info = nets[net_name]
    new_state = _controller_for(info).toggle(net_name, info["port"])
    label = "enabled" if new_state else "disabled"
    print(f"{GREEN}USB port '{net_name}' toggled → {label}{RESET}")
    return new_state


def state(net_name: str) -> bool:
    nets = _load_net_definitions()
    if net_name not in nets:
        raise KeyError(f"USB net '{net_name}' not found")
    info = nets[net_name]
    enabled = _controller_for(info).state(net_name, info["port"])
    label = "enabled" if enabled else "disabled"
    print(f"{GREEN}USB port '{net_name}' is {label}{RESET}")
    return enabled


def states(net_names=None, *, causes=None) -> Dict[str, bool | None]:
    """Read several USB nets, grouping them by physical hub.

    ``state()`` is one net per call and each call is a full hub
    open -> read -> close under that hub's lock, so reading a whole bench with
    it costs one enumerate/connect/disconnect cycle per *port*. Grouping by hub
    turns that into one cycle per *hub*.

    Hubs are grouped by ``_lock_key()`` -- the same key the drivers serialise
    on -- so the grouping can never span two devices that would have contended
    anyway.

    Unlike ``state()`` this is quiet (no stdout) and total: a hub that cannot be
    reached maps all of its nets to None instead of raising, so one absent hub
    does not hide the ones that are present.

    Args:
        net_names: iterable of USB net names, or None for every saved USB net.
        causes: optional dict, filled in place with ``net name -> "Type: msg"``
            for every net reported None because its hub would not open. The
            return value cannot carry this (it is ``bool | None`` and a third
            inhabitant is exactly the ambiguity issue #196 is about), and a
            caller that does not pass it is unaffected.

    Returns:
        dict[str, bool | None]: net name -> enabled, or None if unreadable.
    """
    nets = _load_net_definitions()
    wanted = list(nets) if net_names is None else [n for n in net_names if n in nets]

    # hub lock key -> (controller, {port: net_name})
    by_hub: Dict[str, tuple] = {}
    for name in wanted:
        info = nets[name]
        controller = _controller_for(info)
        try:
            key = controller._lock_key()
        except Exception:
            # A driver that has not implemented _lock_key still works: give it a
            # key of its own so it degrades to one session per net rather than
            # being grouped wrongly (or crashing the whole sweep).
            key = f"unkeyed::{name}"
        if key not in by_hub:
            by_hub[key] = (controller, {})
        by_hub[key][1][info["port"]] = name

    out: Dict[str, bool | None] = {}
    for key, (controller, port_to_net) in by_hub.items():
        try:
            port_states = controller.states(list(port_to_net))
        except Exception as e:
            # Hub absent, SDK missing, lock timeout: report this hub's nets as
            # unknown and move on to the next hub.
            #
            # This is the path that turns a whole hub null, and it used to
            # discard the exception silently -- so a hub that would not open
            # was indistinguishable from one that answered null, with nothing
            # anywhere naming which hub or why. On a multi-hub bench that is
            # what made one hub look intermittently broken (issue #196).
            logger.warning(
                "USB hub %s unreadable, reporting %d net(s) as unknown: %s: %s",
                key, len(port_to_net), type(e).__name__, e,
            )
            # The log names the cause for someone on the box. Hand it to the
            # caller too, so it can reach the user's terminal -- otherwise a
            # hub that would not open and a hub that answered null per port are
            # still indistinguishable over HTTP, which is what made this look
            # like flaky hardware.
            if causes is not None:
                cause = f"{type(e).__name__}: {e}"
                for name in port_to_net.values():
                    causes[name] = cause
            port_states = {}
        for port, name in port_to_net.items():
            value = port_states.get(port)
            out[name] = None if value is None else bool(value)

    return out