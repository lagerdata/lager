# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import os
import json
from lager.nets.net import Net, NetType

def disable_net(netname, mcu=None):
    """Disable scope channel"""
    target_net = Net.get(netname, NetType.Analog)
    if target_net:
        target_net.disable(teardown=False)

def enable_net(netname, mcu=None):
    """Enable scope channel"""
    target_net = Net.get(netname, NetType.Analog)
    if target_net:
        target_net.enable()

def start_capture(netname, mcu=None):
    """Start waveform capture"""
    target_net = Net.get(netname, NetType.Analog)
    if target_net and hasattr(target_net.device, 'start_capture'):
        target_net.device.start_capture()

def stop_capture(netname, mcu=None):
    """Stop waveform capture"""
    target_net = Net.get(netname, NetType.Analog)
    if target_net and hasattr(target_net.device, 'stop_capture'):
        target_net.device.stop_capture()

def start_single(netname, mcu=None):
    """Start single waveform capture"""
    target_net = Net.get(netname, NetType.Analog)
    if target_net and hasattr(target_net.device, 'start_single_capture'):
        target_net.device.start_single_capture()

def force_trigger(netname, mcu=None):
    """Force trigger"""
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
