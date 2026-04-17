# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

from .state_dir import get_state_dir

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
    'LOCK_FILE_PATH',
    'CONTROL_PLANE_CONFIG_PATH',
    'AUTHORIZED_KEYS_DIR',
    'BENCH_JSON_PATH',
    'MCP_AUDIT_LOG_PATH',
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

# Configuration paths — all derived from the platform-aware state directory.
# On Linux this is /etc/lager; on macOS this is /Library/Application Support/Lager.
# Override with the LAGER_STATE_DIR environment variable.
_STATE_DIR = get_state_dir()
LAGER_CONFIG_DIR = str(_STATE_DIR)
SAVED_NETS_PATH = str(_STATE_DIR / "saved_nets.json")
AVAILABLE_INSTRUMENTS_PATH = str(_STATE_DIR / "available_instruments.json")
ORG_SECRETS_PATH = str(_STATE_DIR / "org_secrets.json")
BOX_ID_PATH = str(_STATE_DIR / "box_id")
VERSION_FILE_PATH = str(_STATE_DIR / "version")
WEBCAM_STREAMS_PATH = str(_STATE_DIR / "webcam_streams.json")
LOCK_FILE_PATH = str(_STATE_DIR / "lock.json")
CONTROL_PLANE_CONFIG_PATH = str(_STATE_DIR / "control_plane.json")
AUTHORIZED_KEYS_DIR = str(_STATE_DIR / "authorized_keys.d")
BENCH_JSON_PATH = str(_STATE_DIR / "bench.json")
MCP_AUDIT_LOG_PATH = str(_STATE_DIR / "mcp_audit.log")

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
