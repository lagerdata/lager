# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import requests
import json
from lager.instrument_wrappers.visa_enum import EnumEncoder
from lager.instrument_wrappers import rigol_mso5000_defines
from lager.instrument_wrappers import rigol_dm3000_defines
from .constants import HARDWARE_PORT

ALL_ENUMS = (
    rigol_mso5000_defines,
    rigol_dm3000_defines,
)

# Create a session with connection pooling for better concurrent handling
_session = requests.Session()
# Configure connection pool to handle more concurrent requests
adapter = requests.adapters.HTTPAdapter(
    pool_connections=20,  # Number of connection pools to cache
    pool_maxsize=20,      # Maximum connections per pool
    max_retries=0         # No automatic retries (let caller handle)
)
_session.mount('http://', adapter)

class ConnectionFailed(Exception):
    pass

class DeviceError(Exception):
    pass

def enum_decoder(obj):
    if '__enum__' in obj:
        cls, name = obj['__enum__']['type'], obj['__enum__']['value']
        for enum_holder in ALL_ENUMS:
            if hasattr(enum_holder, cls):
                return getattr(enum_holder, cls).from_cmd(name)

    return obj

class Device:
    def __init__(self, device_name, net_info=None):
        self.device_name = device_name
        self.net_info = net_info

    def invoke(self, func, *args, **kwargs):
        data = {
            'device': self.device_name,
            'function': func,
            'args': args,
            'kwargs': kwargs,
            'net_info': self.net_info,
        }
        try:
            # Use session with connection pooling and 10s timeout
            # (thermocouple reads take ~3s, so 10s provides safe margin)
            # Note: Using localhost since all services run in same container
            resp = _session.post(
                f'http://localhost:{HARDWARE_PORT}/invoke',
                headers={'Content-Type': 'application/json'},
                data=json.dumps(data, cls=EnumEncoder),
                timeout=10.0  # 10 second timeout
            )
        except Exception as exc:
            raise ConnectionFailed from exc
        if not resp.ok:
            try:
                raise DeviceError(resp.json())
            except (ValueError, KeyError):
                raise DeviceError(resp.content)
        return json.loads(resp.content, object_hook=enum_decoder)

    def __getattr__(self, func):
        def wrapper(*args, **kwargs):
            return self.invoke(func, *args, **kwargs)
        return wrapper
