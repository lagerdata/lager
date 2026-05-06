# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

class InvalidEnumError(Exception):
    pass

class InstrumentSourceError(Exception):
    pass
    
def to_enum(EnumCls, value):
    if isinstance(EnumCls, list):
        for ecls in EnumCls:
            if isinstance(value, ecls):
                return value

    if isinstance(value, EnumCls):
        return value
    try:
        if isinstance(value, str):
            if "." in value:
                enum_name, value = value.split(".")
                if enum_name != EnumCls.__name__:
                    raise InvalidEnumError("Sent enum name does not match expected enum")
                value = value
            return EnumCls[value]
        elif isinstance(value, dict) and '__enum__' in value:
            enum_name, enum_value = value['__enum__']['type'], value['__enum__']['value']
            if enum_name != EnumCls.__name__:
                raise InvalidEnumError("Sent enum name does not match expected enum")
            return EnumCls.from_cmd(enum_value)

    except KeyError:
        pass
    raise InvalidEnumError()