# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.drivers.loader - Resolve a custom net to its driver, and import it.

Shared by the HTTP handler (which validates before spawning), the runner
subprocess (which imports and calls) and open_driver() for scripts, so all
three apply the same rules.
"""

import importlib.util
import os
import sys

from lager.drivers import deps, registry
from lager.drivers.errors import DriverFailed
from lager.drivers.manifest import DRIVER_FILE, ManifestError, coerce_params


def _saved_nets():
    from lager.nets.net import Net
    return Net.get_local_nets()


def find_net(netname, nets=None):
    """The saved record for a custom net (one whose role is an installed driver)."""
    nets = _saved_nets() if nets is None else nets
    for rec in nets:
        if rec.get('name') == netname and registry.has(rec.get('role')):
            return rec
    raise ManifestError("Net '%s' is not a custom net on this box" % netname)


def resolve(rec, nets=None):
    """Check a custom net against its driver's manifest.

    Returns (entry, params, base_nets): the registry entry, the net's params
    typed, and the saved record of each built-in net a ``"type": "net"`` param
    names. Raises DriverUnavailable or ManifestError (a ValueError -> 400).
    """
    entry = registry.get_driver(rec.get('role'))
    manifest = entry['manifest']
    params = coerce_params(manifest, rec.get('params'))
    nets = _saved_nets() if nets is None else nets

    base_nets = {}
    for pname, spec in manifest['params'].items():
        if spec['type'] != 'net' or pname not in params:
            continue
        target = params[pname]
        matches = [n for n in nets if n.get('name') == target]
        if not matches:
            raise ManifestError("Net '%s': param '%s' names net '%s', which does not exist"
                                % (rec.get('name'), pname, target))
        base = next((n for n in matches if n.get('role') == spec['role']), None)
        if base is None:
            raise ManifestError("Net '%s': param '%s' needs a %s net, but '%s' is a %s net"
                                % (rec.get('name'), pname, spec['role'], target,
                                   matches[0].get('role')))
        base_nets[pname] = base
    return entry, params, base_nets


def load_driver_class(dtype):
    """Import ``driver.py`` from the installed bundle; return its Driver class."""
    from lager.drivers import CustomDriver

    bundle = registry.driver_dir(dtype)
    path = os.path.join(bundle, DRIVER_FILE)
    # The bundle's own helper modules import by plain name.
    if bundle not in sys.path:
        sys.path.insert(0, bundle)
    # Its declared packages go last, so the box's own packages always win
    # (deps.install refuses a pin that would be hidden by one). A plain
    # entry, not site.addsitedir: a package's .pth files are not run.
    deps_dir = os.path.join(bundle, deps.DEPS_DIR)
    if os.path.isdir(deps_dir) and deps_dir not in sys.path:
        sys.path.append(deps_dir)
    module_name = 'lager_custom_driver_%s' % dtype.replace('-', '_')
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise DriverFailed('Cannot import %s for driver %s' % (DRIVER_FILE, dtype))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise DriverFailed('%s of driver %s failed to import: %s: %s'
                           % (DRIVER_FILE, dtype, type(e).__name__, e))

    cls = getattr(module, 'Driver', None)
    if not (isinstance(cls, type) and issubclass(cls, CustomDriver)):
        raise DriverFailed('%s of driver %s must define class Driver(CustomDriver)'
                           % (DRIVER_FILE, dtype))
    return cls


def bound_action(instance, action):
    """The instance's method for ``action``, only if it is marked @action."""
    method = getattr(instance, action, None)
    if not callable(method) or not getattr(method, '_lager_action', False):
        raise DriverFailed("Driver %s has no @action method '%s'"
                           % (instance.net.get('role'), action))
    return method


def instance_for_net(netname):
    """A ready-to-setup Driver instance for a custom net (used by open_driver)."""
    nets = _saved_nets()
    rec = find_net(netname, nets)
    entry, params, base_nets = resolve(rec, nets)
    cls = load_driver_class(entry['type'])
    return cls(rec, params=params, base_nets=base_nets)
