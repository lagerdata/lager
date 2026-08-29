# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# This file is uploaded and executed on the box; it is never imported by the
# CLI. It still ships inside the lager-cli wheel, because get_impl_path()
# resolves it from the installed package on disk -- so it has to remain
# importable on a host that has no box tree. `lager` lives under box/ and is
# not in the wheel, so every reference to it is imported inside the function
# that needs it. Same pattern as cli/impl/measurement/scope.py.
#
# The net type below must stay equal to NetType.from_role(LOGIC_ROLE), where
# LOGIC_ROLE is what cli/commands/measurement/logic.py validates the net
# against before dispatching here. Net.get matches on type equality in both
# its lookup paths, so a mismatch does not raise -- it returns None, the
# `if target_net:` guard goes false, and every command silently does nothing.
# test/unit/box/test_logic_net_type.py pins the two together.

import os
import json
import sys

RED = '\033[91m'
RESET = '\033[0m'


def _report(action, exc):
    """Turn a box-side failure into one line and a non-zero exit.

    Without this a DeviceError from an unimplemented driver method reaches the
    user as a raw traceback, and the command still looks like it succeeded --
    nothing here returned a status the dispatcher could act on.
    """
    print(f"{RED}Error {action}: {exc}{RESET}", file=sys.stderr)
    return False


def _get_logic_net(netname):
    """Resolve a Logic net, or return None after saying why.

    `Net.get` returns None for a name the box does not have. Every worker below
    used to fall through that case to a bare `return`, so a typo in the net name
    printed nothing and still exited 0.
    """
    from lager.nets.net import Net, NetType

    target_net = Net.get(netname, NetType.Logic)
    if target_net is None:
        print(f"{RED}Net not found: {netname}{RESET}", file=sys.stderr)
    return target_net


def disable_net(netname, mcu=None):
    """Disable scope channel"""
    try:
        target_net = _get_logic_net(netname)
        if target_net is None:
            return False
        target_net.disable(teardown=False)
    except Exception as exc:
        return _report("disabling the net", exc)
    return True

def enable_net(netname, mcu=None):
    """Enable scope channel"""
    try:
        target_net = _get_logic_net(netname)
        if target_net is None:
            return False
        target_net.enable()
    except Exception as exc:
        return _report("enabling the net", exc)
    return True

def start_capture(netname, mcu=None):
    """Start waveform capture"""
    try:
        target_net = _get_logic_net(netname)
        if target_net is None:
            return False
        target_net.device.start_capture()
    except Exception as exc:
        return _report("starting capture", exc)
    return True

def stop_capture(netname, mcu=None):
    """Stop waveform capture"""
    try:
        target_net = _get_logic_net(netname)
        if target_net is None:
            return False
        target_net.device.stop_capture()
    except Exception as exc:
        return _report("stopping capture", exc)
    return True

def start_single(netname, mcu=None):
    """Start single waveform capture"""
    try:
        target_net = _get_logic_net(netname)
        if target_net is None:
            return False
        target_net.device.start_single_capture()
    except Exception as exc:
        return _report("starting single capture", exc)
    return True

def force_trigger(netname, mcu=None):
    """Force trigger"""
    try:
        target_net = _get_logic_net(netname)
        if target_net is None:
            return False
        target_net.device.force_trigger()
    except Exception as exc:
        return _report("forcing trigger", exc)
    return True

def main():
    command = json.loads(os.environ['LAGER_COMMAND_DATA'])
    action = command.get('action')
    params = command.get('params', {})

    logic_actions = {
        'disable_net': disable_net,
        'enable_net': enable_net,
        'start_capture': start_capture,
        'stop_capture': stop_capture,
        'start_single': start_single,
        'force_trigger': force_trigger,
    }

    handler = logic_actions.get(action)
    if handler is None:
        print(f"{RED}Unknown action: {action}{RESET}", file=sys.stderr)
        sys.exit(1)

    # A worker returning False has already explained itself on stderr. Exiting
    # non-zero is what makes the CLI, and any CI step wrapping it, see failure.
    if handler(**params) is False:
        sys.exit(1)


if __name__ == '__main__':
    main()
