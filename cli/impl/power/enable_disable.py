# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# This file is uploaded and executed on the box; it is never imported by the
# CLI. It still ships inside the lager-cli wheel, because get_impl_path()
# resolves it from the installed package on disk -- so it has to remain
# importable on a host that has no box tree. `lager` lives under box/ and is
# not in the wheel, so every reference to it is imported inside the function
# that needs it. Same pattern as cli/impl/measurement/scope.py.

import os
import json

def disable_net(netname, mcu=None):
    """Disable scope channel"""
    from lager.nets.net import Net, NetType

    target_net = Net.get(netname, NetType.Analog)
    if target_net:
        target_net.disable(teardown=False)

def enable_net(netname, mcu=None):
    """Enable scope channel"""
    from lager.nets.net import Net, NetType

    target_net = Net.get(netname, NetType.Analog)
    if target_net:
        target_net.enable()

def start_capture(netname, mcu=None):
    """Start waveform capture"""
    from lager.nets.net import Net, NetType

    target_net = Net.get(netname, NetType.Analog)
    if target_net and hasattr(target_net.device, 'start_capture'):
        target_net.device.start_capture()

def stop_capture(netname, mcu=None):
    """Stop waveform capture"""
    from lager.nets.net import Net, NetType

    target_net = Net.get(netname, NetType.Analog)
    if target_net and hasattr(target_net.device, 'stop_capture'):
        target_net.device.stop_capture()

def start_single(netname, mcu=None):
    """Start single waveform capture"""
    from lager.nets.net import Net, NetType

    target_net = Net.get(netname, NetType.Analog)
    if target_net and hasattr(target_net.device, 'start_single_capture'):
        target_net.device.start_single_capture()

def force_trigger(netname, mcu=None):
    """Force trigger"""
    from lager.nets.net import Net, NetType

    target_net = Net.get(netname, NetType.Analog)
    if target_net and hasattr(target_net.device, 'force_trigger'):
        target_net.device.force_trigger()

def main():
    command = json.loads(os.environ['LAGER_COMMAND_DATA'])
    action = command.get('action')
    params = command.get('params', {})

    if action == 'disable_net':
        disable_net(**params)
    elif action == 'enable_net':
        enable_net(**params)
    elif action == 'start_capture':
        start_capture(**params)
    elif action == 'stop_capture':
        stop_capture(**params)
    elif action == 'start_single':
        start_single(**params)
    elif action == 'force_trigger':
        force_trigger(**params)

if __name__ == '__main__':
    main()
