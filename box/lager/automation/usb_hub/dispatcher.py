# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
from typing import Dict

from .usb_net import USBNet
from .acroname import AcronameUSBNet
from .ykush import YKUSHUSBNet

# ANSI color codes
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'

# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
from ...constants import SAVED_NETS_PATH as _DEFAULT_SAVED_NETS_PATH
NET_DEFS_PATH = os.environ.get(
    "LAGER_USB_NETS_FILE",
    _DEFAULT_SAVED_NETS_PATH,
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


def toggle(net_name: str) -> None:
    nets = _load_net_definitions()
    if net_name not in nets:
        raise KeyError(f"USB net '{net_name}' not found")
    info = nets[net_name]
    _controller_for(info).toggle(net_name, info["port"])
    print(f"{GREEN}USB port '{net_name}' toggled{RESET}")