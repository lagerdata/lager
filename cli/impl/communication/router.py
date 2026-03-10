#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
MikroTik implementation script - runs on the box inside Docker.

Dispatches actions to MikroTikRouter based on JSON args in sys.argv[1].
"""

import json
import sys


def main():
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'No arguments provided'}))
        sys.exit(1)

    try:
        args = json.loads(sys.argv[1])
    except json.JSONDecodeError as e:
        print(json.dumps({'error': f'Invalid JSON arguments: {e}'}))
        sys.exit(1)

    action = args.get('action')
    if not action:
        print(json.dumps({'error': 'No action specified'}))
        sys.exit(1)

    # add-net saves directly to saved_nets.json without needing a router instance
    if action == 'add_net':
        from lager.nets.net import Net
        net_data = args['net_data']
        Net.save_local_net(net_data)
        print(f"Net '{net_data['name']}' saved successfully.")
        return

    # All other actions require loading an existing router net
    netname = args.get('netname')
    if not netname:
        print(json.dumps({'error': 'No netname specified'}))
        sys.exit(1)

    from lager.nets.net import Net
    from lager.nets.constants import NetType

    router = Net.get_from_saved_json(netname, NetType.Router)
    if router is None:
        print(json.dumps({'error': f"Router net '{netname}' not found"}))
        sys.exit(1)

    try:
        if action == 'connect':
            result = router.connect()
        elif action == 'interfaces':
            result = router.get_interfaces()
        elif action == 'wireless_clients':
            result = router.get_wireless_clients()
        elif action == 'wireless_interfaces':
            result = router.get_wireless_interfaces()
        elif action == 'dhcp_leases':
            result = router.get_dhcp_leases()
        elif action == 'system_info':
            result = router.get_system_info()
        elif action == 'reboot':
            result = router.reboot()
        elif action == 'run':
            path = args.get('path', '')
            params = args.get('params')
            result = router.run(path, params=params)
        elif action == 'set_interface_disabled':
            result = router.set_interface_disabled(
                args['interface'], args['disabled']
            )
        elif action == 'set_wireless_ssid':
            result = router.set_wireless_ssid(args['interface'], args['ssid'])
        else:
            print(json.dumps({'error': f"Unknown action: '{action}'"}))
            sys.exit(1)

        print(json.dumps(result, indent=2))

    except Exception as e:
        print(json.dumps({'error': str(e)}))
        sys.exit(1)


if __name__ == '__main__':
    main()
