# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

__all__ = [
    # Connection and testing exceptions
    'LagerDeviceConnectionError',
    'LagerDeviceNotSupportedError',
    'LagerBoxConnectionError',
    'LagerTestingFailure',
    'LagerTestingSuccess',
    # Base backend exceptions
    'LagerBackendError',
    'LibraryMissingError',
    'DeviceNotFoundError',
    'DeviceLockError',
    'PortStateError',
    # Domain-specific backend exceptions
    'SupplyBackendError',
    'BatteryBackendError',
    'SolarBackendError',
    'ELoadBackendError',
    'USBBackendError',
    'ThermocoupleBackendError',
    'WattBackendError',
    'ADCBackendError',
    'DACBackendError',
    'GPIOBackendError',
    'UARTBackendError',
    'SPIBackendError',
    'I2CBackendError',
    'EnergyAnalyzerBackendError',
]


class LagerDeviceConnectionError(Exception):
    def __init__(self, message="unspecified error"):
        super().__init__(f"Failed to connect to Device: {message}")

class LagerDeviceNotSupportedError(Exception):
    def __init__(self, device):
        super().__init__(f"Device {device} not supported")

class LagerBoxConnectionError(Exception):
    def __init__(self, message=None):
        output = "Unable to connect to Lager box"
        if message:
            output += f": {message}"
        super().__init__(output)


class LagerTestingFailure(Exception):
    def __init__(self, message=None):
        super().__init__(f"Test failure: {message}")

class LagerTestingSuccess(Exception):
    def __init__(self, message=None):
        super().__init__(f"Test success: {message}")


# =============================================================================
# Unified Hardware Backend Exception Hierarchy
# =============================================================================

class LagerBackendError(Exception):
    """Base exception for all hardware backend errors.

    All domain-specific backend errors should inherit from this class
    to allow unified error handling across different hardware types.
    """
    def __init__(self, message: str, device: str = None, backend: str = None):
        self.device = device
        self.backend = backend
        parts = []
        if backend:
            parts.append(f"[{backend}]")
        if device:
            parts.append(f"Device '{device}':")
        parts.append(message)
        super().__init__(" ".join(parts))


class LibraryMissingError(LagerBackendError):
    """Raised when a required library/driver is not installed."""
    def __init__(self, library: str, install_hint: str = None):
        self.library = library
        self.install_hint = install_hint
        message = f"Required library '{library}' is not installed"
        if install_hint:
            message += f". Install with: {install_hint}"
        super().__init__(message)


class DeviceNotFoundError(LagerBackendError):
    """Raised when a hardware device cannot be found or connected to."""
    def __init__(self, device: str, address: str = None, backend: str = None):
        self.address = address
        message = "Device not found"
        if address:
            message += f" at address '{address}'"
        super().__init__(message, device=device, backend=backend)


class DeviceLockError(LagerBackendError):
    """Raised when a device is locked or in use by another process."""
    def __init__(self, device: str, backend: str = None):
        super().__init__("Device is locked or in use by another process",
                        device=device, backend=backend)


class PortStateError(LagerBackendError):
    """Raised when a USB hub port state operation fails."""
    def __init__(self, port: int = None, device: str = None, backend: str = None):
        self.port = port
        message = "Error reading or changing port state"
        if port is not None:
            message = f"Error reading or changing state of port {port}"
        super().__init__(message, device=device, backend=backend or "USB")


# =============================================================================
# Domain-Specific Backend Exceptions
# =============================================================================

class SupplyBackendError(LagerBackendError):
    """Exception for power supply backend errors."""
    def __init__(self, message: str, device: str = None, backend: str = None):
        super().__init__(message, device=device, backend=backend or "Supply")


class BatteryBackendError(LagerBackendError):
    """Exception for battery simulator backend errors."""
    def __init__(self, message: str, device: str = None, backend: str = None):
        super().__init__(message, device=device, backend=backend or "Battery")


class SolarBackendError(LagerBackendError):
    """Exception for solar simulator backend errors."""
    def __init__(self, message: str, device: str = None, backend: str = None):
        super().__init__(message, device=device, backend=backend or "Solar")


class ELoadBackendError(LagerBackendError):
    """Exception for electronic load backend errors."""
    def __init__(self, message: str, device: str = None, backend: str = None):
        super().__init__(message, device=device, backend=backend or "ELoad")


class USBBackendError(LagerBackendError):
    """Exception for USB hub backend errors."""
    def __init__(self, message: str, device: str = None, backend: str = None):
        super().__init__(message, device=device, backend=backend or "USB")


class ThermocoupleBackendError(LagerBackendError):
    """Exception for thermocouple/temperature sensor backend errors."""
    def __init__(self, message: str, device: str = None, backend: str = None):
        super().__init__(message, device=device, backend=backend or "Thermocouple")


class WattBackendError(LagerBackendError):
    """Exception for wattmeter backend errors."""
    def __init__(self, message: str, device: str = None, backend: str = None):
        super().__init__(message, device=device, backend=backend or "Watt")


class ADCBackendError(LagerBackendError):
    """Exception for ADC (analog-to-digital converter) backend errors."""
    def __init__(self, message: str, device: str = None, backend: str = None):
        super().__init__(message, device=device, backend=backend or "ADC")


class DACBackendError(LagerBackendError):
    """Exception for DAC (digital-to-analog converter) backend errors."""
    def __init__(self, message: str, device: str = None, backend: str = None):
        super().__init__(message, device=device, backend=backend or "DAC")


class GPIOBackendError(LagerBackendError):
    """Exception for GPIO (general purpose I/O) backend errors."""
    def __init__(self, message: str, device: str = None, backend: str = None):
        super().__init__(message, device=device, backend=backend or "GPIO")


class UARTBackendError(LagerBackendError):
    """Exception for UART (serial communication) backend errors."""
    def __init__(self, message: str, device: str = None, backend: str = None):
        super().__init__(message, device=device, backend=backend or "UART")


class SPIBackendError(LagerBackendError):
    """Exception for SPI (serial peripheral interface) backend errors."""
    def __init__(self, message: str, device: str = None, backend: str = None):
        super().__init__(message, device=device, backend=backend or "SPI")


class I2CBackendError(LagerBackendError):
    """Exception for I2C (inter-integrated circuit) backend errors."""
    def __init__(self, message: str, device: str = None, backend: str = None):
        super().__init__(message, device=device, backend=backend or "I2C")


class EnergyAnalyzerBackendError(LagerBackendError):
    """Exception for energy analyzer backend errors."""
    def __init__(self, message: str, device: str = None, backend: str = None):
        super().__init__(message, device=device, backend=backend or "EnergyAnalyzer")