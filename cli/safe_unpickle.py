# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import importlib
import io
import pickle

defaults = {
    'builtins': {
        'range',
        'complex',
        'set',
        'frozenset',
        'slice',
    },
    'datetime': {
        'date',
        'time',
        'datetime',
    },
}


class RestrictedUnpickler(pickle.Unpickler):
    def __init__(self, safe, *args, **kwargs):
        self.safe = safe
        super().__init__(*args, **kwargs)

    def find_class(self, module, name):
        allowed = self.safe.get(module, {})
        if name not in allowed:
            raise pickle.UnpicklingError("global '%s.%s' is forbidden" %
                                         (module, name))
        mod = importlib.import_module(module)
        return getattr(mod, name)

def restricted_loads(s, safe=None):
    """Helper function analogous to pickle.loads()."""
    if safe is None:
        safe = defaults
    return RestrictedUnpickler(safe, io.BytesIO(s), fix_imports=False).load()
