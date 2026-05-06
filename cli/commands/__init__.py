# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
CLI commands package.

This package contains grouped command modules organized by domain:
- measurement/: ADC, DAC, GPI, GPO, scope, logic, thermocouple, watt commands
- power/: Power supply, battery, solar, eload commands
- communication/: UART, BLE, WiFi, USB commands
- development/: Debug, ARM, Python commands
- box/: Hello, boxes, instruments, nets, SSH commands
- utility/: Defaults, binaries, update, exec, logs, webcam commands
"""

from .measurement import (
    adc,
    dac,
    gpi,
    gpo,
    scope,
    logic,
    thermocouple,
    watt,
)

from .communication import (
    uart,
)

from .box import (
    hello,
    boxes,
    instruments,
    nets,
    ssh,
)

from .utility import (
    defaults,
    binaries,
    update,
    exec_,
    logs,
    webcam,
)

__all__ = [
    # Measurement commands
    "adc",
    "dac",
    "gpi",
    "gpo",
    "scope",
    "logic",
    "thermocouple",
    "watt",
    # Communication commands
    "uart",
    # Box commands
    "hello",
    "boxes",
    "instruments",
    "nets",
    "ssh",
    # Utility commands
    "defaults",
    "binaries",
    "update",
    "exec_",
    "logs",
    "webcam",
]
