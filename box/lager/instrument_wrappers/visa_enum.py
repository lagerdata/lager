# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from enum import Enum
import simplejson as json

class EnumEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Enum):
            if isinstance(obj.value, str):
                return {'__enum__': {'type': type(obj).__name__, 'value': obj.value}}
            elif isinstance(obj.value, tuple):
                return {'__enum__': {'type': type(obj).__name__, 'value': obj.value[1]}}
        return json.JSONEncoder.default(self, obj)

class VisaEnum(Enum):
    @classmethod
    def from_cmd(cls, cmd):
        for value in cls.__members__.values():
            if isinstance(value.value, str):
                if value.value == cmd:
                    return value
            elif isinstance(value.value, tuple):
                if value.value[1] == cmd:
                    return value
        raise AttributeError(f"Got an unexpected value from the instrument: {cmd}")

    def to_cmd(self):
        if isinstance(self.value, str):
            return self.value
        elif isinstance(self.value, tuple):
            return self.value[0]

    @classmethod
    def is_cmd(cls, cmd):
        try:
            cls.from_cmd(cmd)
        except AttributeError:
            return False

    def __format__(self, specifier):
        return self.to_cmd()
