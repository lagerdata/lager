# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Custom-device framework.

Declarative catalog of instruments that the box cannot auto-detect from a
USB VID:PID alone — typically serial (RS-232) instruments reached through a
generic USB-serial adapter. A user manually associates an enumerated cable
with one of these catalog entries; the rest of the stack (scanner surfacing,
driver dispatch, net validation) is then driven from the catalog rather than
from the hand-edited per-instrument tables used for auto-detected USB-TMC gear.
"""
