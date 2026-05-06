# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    cli.core.param_types

    Custom click parameter types for the CLI.
"""
import collections
import os
import re
import itertools
import click

class MemoryAddressType(click.ParamType):
    """
        Memory address integer parameter
    """
    name = 'memory address'

    def convert(self, value, param, ctx):
        value = value.strip().lower()
        if value.lower().startswith('0x'):
            try:
                return int(value, 16)
            except ValueError:
                self.fail(f"{value} is not a valid hex integer", param, ctx)

        try:
            return int(value, 10)
        except ValueError:
            self.fail(f"{value} is not a valid integer", param, ctx)

    def __repr__(self):
        return 'ADDR'

class HexParamType(click.ParamType):
    """
        Hexadecimal integer parameter
    """
    name = 'hex'

    def convert(self, value, param, ctx):
        try:
            return int(value, 16)
        except ValueError:
            self.fail(f"{value} is not a valid hex integer", param, ctx)

    def __repr__(self):
        return 'HEX'

def grouper(iterator, n):
    while chunk := list(itertools.islice(iterator, n)):
        yield chunk


class HexArrayType(click.ParamType):
    """
        Array of hexadecimal integers
    """
    name = 'hexarray'

    def convert(self, value, param, ctx):
        if len(value) % 2 != 0:
            self.fail(f"Value must contain an even number of hex digits", param, ctx)

        out = []
        for chunk in grouper(iter(value), 2):
            try:
                out.append(int(''.join(chunk), 16))
            except ValueError:
                self.fail(f"{value} is not a valid hex integer", param, ctx)

        return out

    def __repr__(self):
        return 'HEXARRAY'

class VarAssignmentType(click.ParamType):
    """
        Openocd variable parameter
    """
    name = 'FOO=BAR'

    def convert(self, value, param, ctx):
        parts = value.split('=')
        if len(parts) != 2:
            self.fail('Invalid assignment', param, ctx)

        return parts

    def __repr__(self):
        return 'VAR ASSIGNMENT'

class EnvVarType(click.ParamType):
    """
        Environment variable
    """
    name = 'FOO=BAR'
    regex = re.compile(r'\A[a-zA-Z_]{1,}[a-zA-Z0-9_]{0,}\Z')

    def convert(self, value, param, ctx):
        parts = value.split('=', maxsplit=1)
        if len(parts) != 2:
            self.fail('Invalid assignment', param, ctx)

        name = parts[0]
        if not self.regex.match(name):
            self.fail(f'Invalid environment variable name "{name}". Names must begin with a letter or underscore, and may only contain letters, underscores, and digits', param, ctx)

        return value

    def __repr__(self):
        return 'ENV VAR'

Binfile = collections.namedtuple('Binfile', ['path', 'address'])
class BinfileType(click.ParamType):
    """
        Type to represent a command line argument for a binfile (<path>,<address>)
    """
    envvar_list_splitter = os.path.pathsep
    name = 'binfile'

    def __init__(self, *args, exists=False, **kwargs):
        self.exists = exists
        super().__init__(*args, **kwargs)

    def convert(self, value, param, ctx):
        parts = value.rsplit(',', 1)
        if len(parts) != 2:
            self.fail(f'{value}. Syntax: --binfile <filename>,<address>', param, ctx)
        filename, address = parts
        path = click.Path(exists=self.exists).convert(filename, param, ctx)
        address = HexParamType().convert(address, param, ctx)

        return Binfile(path=path, address=address)

    def __repr__(self):
        return 'BINFILE'

CanFrame = collections.namedtuple('CanFrame', [
    'arbitration_id',
    'is_fd',
    'is_error_frame',
    'is_remote_frame',
    'is_extended_id',
    'data',
])

CanFilter = collections.namedtuple('CanFilter', [
    'can_id',
    'can_mask',
    'extended',
])

PortForwardSpecifier = collections.namedtuple('PortForwardSpecifier', [
    'src',
    'dst',
    'proto',
])

def parse_can_data(data_str):
    parts = data_str.split('.')
    return list(b''.join([bytes.fromhex(part) for part in parts]))

def parse_can2(value):
    arbitration_id, rest = value.split('#')
    arbitration_id = int(arbitration_id, 16)
    if rest == 'R':
        is_remote_frame = True
        data = None
    else:
        is_remote_frame = False
        data = parse_can_data(rest)
    return CanFrame(
        arbitration_id=arbitration_id,
        is_fd=False,
        is_error_frame=False,
        is_remote_frame=is_remote_frame,
        is_extended_id=False,
        data=data,
    )


def parse_canfd(value):
    arbitration_id, rest = value.split('##')
    arbitration_id = int(arbitration_id, 16)
    flags = int(rest[0:1], 16)
    data = parse_can_data(rest[1:])
    return CanFrame(
        arbitration_id=arbitration_id,
        is_fd=True,
        is_error_frame=False,
        is_remote_frame=False,
        is_extended_id=False,
        data=data,
        flags=flags,
    )

class CanFrameType(click.ParamType):
    """
        Type to represent a command line argument for a CAN frame
    """
    name = 'CANFrame'

    def convert(self, value, param, ctx):
        if '#' in value:
            return parse_can2(value)
        if '##' in value:
            return parse_canfd(value)
        raise ValueError('Invalid CAN frame.\nSee `lager canbus send --help` for format and examples.')

    def __repr__(self):
        return 'CAN_FRAME'


class CanFilterType(click.ParamType):
    """
        Type to represent a command line argument for a CAN filter
    """
    name = 'CANFilter'

    def convert(self, value, param, ctx):
        try:
            can_id, can_mask = value.split(':')
            if len(can_id) not in (3, 8):
                self.fail('Filter can_id must be 3 or 8 hexadecimal digits')
            extended = len(can_id) == 8
            can_id = int(can_id, 16)
            can_mask = int(can_mask, 16)
        except ValueError:
            self.fail('Invalid filter format.\nSee lager canbus dump --help')

        return CanFilter(can_id=can_id, can_mask=can_mask, extended=extended)

    def __repr__(self):
        return 'CAN_FILTER'

class ADCChannelType(click.ParamType):
    """
        Type to represent a command line argument for an ADC channel
    """
    name = 'CHANNEL'

    SPECIAL = ('VTREF', 'VIO')
    def convert(self, value, param, ctx):
        if value in self.SPECIAL:
            return value
        if '-' in value:
            start, end = value.split('-', 1)
            start = int(start, 10)
            end = int(end, 10)
            if start < 0 or start > 5:
                self.fail('Range start must be 0-5')
            if end < 0 or end > 5:
                self.fail('Range end must be 0-5')
            if end <= start:
                self.fail('Range start must be before range end')
            return {'start': start, 'end': end}
        value = int(value, 10)
        if value < 0 or value > 5:
            self.fail('Read channel must be 0-5')
        return {'channel': value}

    def __repr__(self):
        return 'ADC_CHANNEL'


class CanbusRange(click.ParamType):
    """
        Type to represent a command line argument for a CAN interface range
    """
    name = 'CANRange'

    def convert(self, value, param, ctx):
        output = []
        for part in value.split(','):
            rangevals = part.split('-')
            if len(rangevals) == 1:
                output.append(int(rangevals[0]))
            elif len(rangevals) == 2:
                start = int(rangevals[0], 10)
                end = int(rangevals[1], 10) + 1
                output.extend(range(start, end))
            else:
                self.fail(f'Invalid range {part}')

        return sorted(set(output))

    def __repr__(self):
        return 'CAN_RANGE'


class PortForwardType(click.ParamType):
    """
        Port forward specifier
    """
    name = 'PORT'
    regex = re.compile(r'\A([0-9]+)(:[0-9]+)?(/[a-z]+)?\Z')
    RESERVED = [2331, 3333, 4444, 8081, 5555, 8888]

    def convert(self, value, param, ctx):
        match = self.regex.search(value)
        if not match:
            self.fail(f'Invalid port specifier "{value}".', param, ctx)

        source = int(match[1], 10)
        if source in self.RESERVED:
            self.fail(f'Port {source} is reserved for internal use on the box.', param, ctx)

        if match[2]:
            dest = int(match[2][1:], 10)
        else:
            dest = source

        if match[3]:
            protocol = match[3][1:]
        else:
            protocol = None

        return PortForwardSpecifier(source, dest, protocol)

    def __repr__(self):
        return 'PORT FORWARD'
