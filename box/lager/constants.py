# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

__all__ = [
    # GPIO states
    'HIGH',
    'LOW',
    # Paths
    'LAGER_CONFIG_DIR',
    'SAVED_NETS_PATH',
    'AVAILABLE_INSTRUMENTS_PATH',
    'ORG_SECRETS_PATH',
    'BOX_ID_PATH',
    'VERSION_FILE_PATH',
    'WEBCAM_STREAMS_PATH',
    # Port numbers
    'HARDWARE_SERVICE_PORT',
    'BOX_HTTP_PORT',
    'DEBUG_SERVICE_PORT',
    # Timeouts
    'DEFAULT_VISA_TIMEOUT',
    'DEFAULT_SERIAL_TIMEOUT',
    'DEFAULT_HTTP_TIMEOUT',
    'GDB_TIMEOUT',
    'MAX_SCRIPT_TIMEOUT',
]

# GPIO states
HIGH = 1
LOW = 0

# Configuration paths
LAGER_CONFIG_DIR = "/etc/lager"
SAVED_NETS_PATH = "/etc/lager/saved_nets.json"
AVAILABLE_INSTRUMENTS_PATH = "/etc/lager/available_instruments.json"
ORG_SECRETS_PATH = "/etc/lager/org_secrets.json"
BOX_ID_PATH = "/etc/lager/box_id"
VERSION_FILE_PATH = "/etc/lager/version"
WEBCAM_STREAMS_PATH = "/etc/lager/webcam_streams.json"

# Service port numbers
HARDWARE_SERVICE_PORT = 8080  # Hardware service for instrument control
BOX_HTTP_PORT = 5000          # Python execution service
DEBUG_SERVICE_PORT = 8765     # Debug service for embedded debugging

# Timeouts (milliseconds for VISA, seconds for others)
DEFAULT_VISA_TIMEOUT = 5000   # 5 seconds for VISA instrument communication (ms)
DEFAULT_SERIAL_TIMEOUT = 10   # 10 seconds for serial communication
DEFAULT_HTTP_TIMEOUT = 10.0   # 10 seconds for HTTP requests
GDB_TIMEOUT = 10.0            # 10 seconds for GDB operations
MAX_SCRIPT_TIMEOUT = 300      # 5 minutes max for script execution