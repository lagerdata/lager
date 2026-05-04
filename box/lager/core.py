# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

import os
import sys
import pickle
import enum
import traceback
import itertools
import shutil
import re
import json
import yaml

from lager.log import log
from lager.exceptions import (
    LagerDeviceConnectionError,
    LagerDeviceNotSupportedError,
    LagerBoxConnectionError,
    LagerTestingFailure,
    LagerTestingSuccess,
)

__all__ = [
    # Enums
    'Interface',
    'Transport',
    'OutputEncoders',
    # Exception handling
    'lager_excepthook',
    'restore_excepthook',
    'install_excepthook',
    # Output
    'output',
    # File classes
    'Hexfile',
    'Binfile',
    # Utility functions
    'read_adc',
    'get_available_instruments',
    'get_saved_nets',
    # Constants
    'LAGER_HOST',
]

Interface = enum.Enum(
    value='Interface',
    names=[
        ('ftdi', 1),
        ('cmsisdap', 2),
        ('jlink', 3),
        ('xds110', 4),
        ('stlink', 5),
        ('stlink-dap', 6),
        ('stlink_dap', 6),
    ]
)

class Transport(enum.Enum):
    swd = 1
    jtag = 2
    hla_swd = 3

def lager_excepthook(etype, value, tb):
    """
        Custom exception printer so users get nice tracebacks without
        weird temporary filenames and paths
    """
    error_lines = traceback.format_exception(etype, value, tb)
    module_folder = os.environ.get('LAGER_HOST_MODULE_FOLDER')
    if not module_folder:
        for line in error_lines:
            print(line, end='')
        return
    if module_folder == '/tmp':
        # We are just running a standalone script
        regex = re.compile(r'\A(\s*File \")tmp[a-zA-Z0-9_-]+.py(\",.*)')
        for line in error_lines:
            print(regex.sub(r'\1script.py\2', line, 1), end='')
    else:
        # We are running a module
        regex = re.compile(rf'\A(\s*File \"){module_folder}/(.*)(\",.*)')
        for line in error_lines:
            print(regex.sub(r'\1\2\3', line, 1), end='')

def restore_excepthook():
    sys.excepthook = sys.__excepthook__

def install_excepthook():
    sys.excepthook = lager_excepthook

if not os.getenv('LAGER_PRESERVE_EXCEPTHOOK'):
    install_excepthook()

class OutputEncoders(enum.IntEnum):
    Raw = enum.auto()
    Pickle = enum.auto()
    JSON = enum.auto()
    YAML = enum.auto()


def output(obj, encoder=OutputEncoders.Pickle):
    """
        Output an arbitrary object
    """
    if encoder == OutputEncoders.Raw:
        try:
            memoryview(obj).release()
        except TypeError as exc:
            raise RuntimeError('Raw encoding requires a byteslike object') from exc
        encoded = obj
    elif encoder == OutputEncoders.Pickle:
        encoded = pickle.dumps(obj, protocol=4, fix_imports=False)
    elif encoder == OutputEncoders.JSON:
        encoded = json.dumps(obj).encode()
    elif encoder == OutputEncoders.YAML:
        encoded = yaml.dump(obj, encoding='utf-8')

    with open(os.getenv('LAGER_OUTPUT_CHANNEL'), 'ab') as file:
        file.write(f"{encoder} {len(encoded)} ".encode())
        file.write(encoded)

LAGER_HOST = os.environ.get("LAGER_HOST")

class Hexfile:
    def __init__(self, path):
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        self.path = path

class Binfile:
    def __init__(self, path, address):
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        self.path = path
        self.address = address



def read_adc(kind="T7", interface="AIN0"):
    from labjack import ljm
    handle = None
    try:
        handle = ljm.openS(kind, "ANY", "ANY")
        return ljm.eReadName(handle, interface)
    finally:
        if handle is not None:
            try:
                ljm.close(handle)
            except Exception:
                pass

def get_available_instruments():
    with open('/etc/lager/available_instruments.json') as f:
        return json.load(f)

def get_saved_nets():
    with open('/etc/lager/saved_nets.json') as f:
        return json.load(f)
