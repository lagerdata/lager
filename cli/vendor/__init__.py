# -*- coding: utf-8 -*-
"""
Vendor directory for third-party libraries bundled with the Lager CLI.

Directory Structure
-------------------
cli/vendor/
    PyCRC/          - CRC calculation library (CRC16, CRC32, CRCCCITT, etc.)

A vendored copy of pyelftools also lived at cli/elftools/ until it was
removed: the tree had been copied in without its construct/lib/ subpackage,
so every module in it raised ModuleNotFoundError in every environment, and
its only consumer was a debug command that was never registered. Nothing
here parses ELF or DWARF today. If that need returns, depend on pyelftools
from PyPI rather than re-vendoring it.

Usage Examples
--------------
    # Import PyCRC modules
    from cli.vendor.PyCRC.CRCCCITT import CRCCCITT
    from cli.vendor.PyCRC.CRC16 import CRC16
    from cli.vendor.PyCRC.CRC32 import CRC32
"""

# Re-export PyCRC for convenient access
# Users can also import directly from cli.vendor.PyCRC.* submodules
from . import PyCRC

__all__ = ["PyCRC"]
