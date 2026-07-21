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
    """Raised when hardware_service (:8080/invoke) can't be reached.

    Usually raised bare (``raise ConnectionFailed from exc``); ``__str__``
    falls back to the cause so messages built with ``f'... {e}'`` never
    end empty.
    """
    def __str__(self):
        text = super().__str__().strip()
        if text:
            return text
        cause = self.__cause__ or self.__context__
        if cause is not None:
            cause_text = str(cause).strip() or type(cause).__name__
            return f"hardware service did not respond ({cause_text})"
        return "hardware service did not respond"


class DeviceError(Exception):
    """Error response from hardware_service's /invoke.

    Stringifies to the response's one-line 'error' field so user-facing
    messages stay readable; the box-side traceback stays available on
    ``.details`` for logs instead of being dumped at the user.
    """
    def __init__(self, payload):
        super().__init__(payload)
        self.payload = payload

    @property
    def details(self):
        if isinstance(self.payload, dict):
            return self.payload.get('details')
        return None

    def __str__(self):
        payload = self.payload
        if isinstance(payload, dict):
            error = payload.get('error')
            if error:
                return str(error)
        if isinstance(payload, bytes):
            payload = payload.decode('utf-8', 'replace')
        return str(payload)


def describe_error(exc: Exception) -> str:
    """Human-readable description of an exception that may stringify empty.

    ``ConnectionFailed`` is raised bare (``raise ConnectionFailed from exc``),
    so ``str(exc)`` is '' and messages built with ``f'... {e}'`` end with
    nothing — e.g. the supply TUI's 'Hardware service unreachable: '. Fall
    back to the cause chain, then to the exception class names, so the user
    always sees WHAT failed (timeout vs refused vs device error).
    """
    text = str(exc).strip()
    cause = exc.__cause__ or exc.__context__
    if not text and cause is not None:
        text = str(cause).strip() or type(cause).__name__
    name = type(exc).__name__
    return f"{name}: {text}" if text else name


def enum_decoder(obj):
    if '__enum__' in obj:
        cls, name = obj['__enum__']['type'], obj['__enum__']['value']
        for enum_holder in ALL_ENUMS:
            if hasattr(enum_holder, cls):
                return getattr(enum_holder, cls).from_cmd(name)

    return obj

class Device:
    #: Default per-request HTTP timeout for /invoke (seconds). Thermocouple
    #: reads take ~3s, so 10s is a safe margin for typical calls.
    DEFAULT_TIMEOUT = 10.0

    def __init__(self, device_name, net_info=None, timeout=None):
        self.device_name = device_name
        self.net_info = net_info
        # Callers that drive long-running device methods (gpio wait_for_level,
        # watt/energy-analyzer integration windows up to ~30s) must widen this
        # so the /invoke POST doesn't time out before the device method returns.
        self.timeout = timeout if timeout is not None else self.DEFAULT_TIMEOUT

    def invoke(self, func, *args, **kwargs):
        data = {
            'device': self.device_name,
            'function': func,
            'args': args,
            'kwargs': kwargs,
            'net_info': self.net_info,
        }
        try:
            # Note: Using localhost since all services run in same container
            resp = _session.post(
                f'http://localhost:{HARDWARE_PORT}/invoke',
                headers={'Content-Type': 'application/json'},
                data=json.dumps(data, cls=EnumEncoder),
                timeout=self.timeout
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
