# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.automation

Automation module for robotic arms, USB hub control, and webcam streaming.

This package groups physical automation-related hardware interfaces:
- arm/: Robotic arm control (Rotrics Dexarm)
- usb_hub/: USB hub port control (Acroname, YKUSH)
- webcam/: Webcam streaming service
"""
from __future__ import annotations

# Re-export from submodules for convenient access
# Note: These are lazy imports to avoid import errors if dependencies are missing

__all__ = [
    # arm exports
    "ArmBase",
    "ArmBackendError",
    "MovementTimeoutError",
    "DeviceNotFoundError",
    "LibraryMissingError",
    "Dexarm",
    "RotricsArm",  # Alias for Dexarm
    # usb_hub exports
    "USBBackendError",
    "PortStateError",
    "USBNet",
    "AcronameUSBNet",
    "AcronameUSB",  # Alias for AcronameUSBNet
    "YKUSHUSBNet",
    "enable",
    "disable",
    "toggle",
    # webcam exports
    "WebcamService",
    "get_active_streams",
    "start_stream",
    "stop_stream",
    "get_stream_info",
    "rename_stream",
]


def __getattr__(name: str):
    """Lazy import to avoid loading all modules at once."""
    # arm exports
    if name in ("ArmBase", "ArmBackendError", "MovementTimeoutError"):
        from .arm.arm_net import ArmBase, ArmBackendError, MovementTimeoutError
        return {"ArmBase": ArmBase, "ArmBackendError": ArmBackendError, "MovementTimeoutError": MovementTimeoutError}[name]
    if name == "Dexarm":
        from .arm.rotrics import Dexarm
        return Dexarm
    if name == "RotricsArm":
        # Alias for Dexarm - Rotrics is the company name, Dexarm is the product
        from .arm.rotrics import Dexarm
        return Dexarm

    # usb_hub exports
    if name in ("USBBackendError", "PortStateError", "USBNet"):
        from .usb_hub.usb_net import USBBackendError, PortStateError, USBNet
        return {"USBBackendError": USBBackendError, "PortStateError": PortStateError, "USBNet": USBNet}[name]
    if name == "AcronameUSBNet":
        from .usb_hub.acroname import AcronameUSBNet
        return AcronameUSBNet
    if name == "AcronameUSB":
        # Alias for AcronameUSBNet - shorter convenience name
        from .usb_hub.acroname import AcronameUSBNet
        return AcronameUSBNet
    if name == "YKUSHUSBNet":
        from .usb_hub.ykush import YKUSHUSBNet
        return YKUSHUSBNet
    if name in ("enable", "disable", "toggle"):
        from . import usb_hub
        return getattr(usb_hub, name)

    # DeviceNotFoundError and LibraryMissingError can come from multiple places
    if name == "DeviceNotFoundError":
        from .arm.arm_net import DeviceNotFoundError
        return DeviceNotFoundError
    if name == "LibraryMissingError":
        from .arm.arm_net import LibraryMissingError
        return LibraryMissingError

    # webcam exports
    if name == "WebcamService":
        from .webcam.service import WebcamService
        return WebcamService
    if name in ("get_active_streams", "start_stream", "stop_stream", "get_stream_info", "rename_stream"):
        from .webcam import service
        return getattr(service, name)

    raise AttributeError(f"module 'lager.automation' has no attribute '{name}'")
