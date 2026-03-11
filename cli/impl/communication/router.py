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
        # ── Read-only / system ──
        if action == 'connect':
            result = router.connect()
        elif action == 'system_info':
            result = router.get_system_info()
        elif action == 'interfaces':
            result = router.get_interfaces()
        elif action == 'wireless_interfaces':
            result = router.get_wireless_interfaces()
        elif action == 'wireless_clients':
            result = router.get_wireless_clients()
        elif action == 'dhcp_leases':
            result = router.get_dhcp_leases()
        elif action == 'security_profiles':
            result = router.get_security_profiles()
        elif action == 'access_list':
            result = router.get_access_list()

        # ── System actions ──
        elif action == 'reboot':
            result = router.reboot()
        elif action == 'wait_for_ready':
            timeout = args.get('timeout', 120)
            result = {'ready': router.wait_for_ready(timeout=timeout)}

        # ── Interface control ──
        elif action == 'set_interface_disabled':
            result = router.set_interface_disabled(args['interface'], args['disabled'])
        elif action == 'enable_interface':
            router.enable_interface(args['interface'])
            result = {'interface': args['interface'], 'disabled': False}
        elif action == 'disable_interface':
            router.disable_interface(args['interface'])
            result = {'interface': args['interface'], 'disabled': True}
        elif action == 'wait_for_wireless_ready':
            timeout = args.get('timeout', 30)
            result = {'ready': router.wait_for_wireless_ready(args['interface'], timeout=timeout)}

        # ── Wireless configuration ──
        elif action == 'set_wireless_ssid':
            result = router.set_wireless_ssid(args['interface'], args['ssid'])
        elif action == 'configure_wireless':
            kwargs = args.get('kwargs', {})
            result = router.configure_wireless(args['interface'], **kwargs)

        # ── Security profiles ──
        elif action == 'create_security_profile':
            result = router.create_security_profile(
                name=args['name'],
                mode=args.get('mode', 'dynamic-keys'),
                authentication_types=args.get('authentication_types', 'wpa2-psk'),
                unicast_ciphers=args.get('unicast_ciphers', 'aes-ccm'),
                wpa2_pre_shared_key=args.get('wpa2_pre_shared_key', ''),
                wpa_pre_shared_key=args.get('wpa_pre_shared_key', ''),
            )
        elif action == 'create_open_security_profile':
            result = router.create_open_security_profile(args.get('name', 'open'))
        elif action == 'update_security_profile_password':
            result = {'updated': router.update_security_profile_password(
                args['name'], args['new_password']
            )}
        elif action == 'delete_security_profile':
            result = {'deleted': router.delete_security_profile(args['name'])}

        # ── DHCP ──
        elif action == 'enable_dhcp':
            router.enable_dhcp()
            result = {'dhcp': 'enabled'}
        elif action == 'disable_dhcp':
            router.disable_dhcp()
            result = {'dhcp': 'disabled'}
        elif action == 'set_dhcp_lease_time':
            router.set_dhcp_lease_time(args.get('lease_time', '10m'))
            result = {'lease_time': args.get('lease_time', '10m')}

        # ── Bandwidth limits ──
        elif action == 'add_bandwidth_limit':
            result = router.add_bandwidth_limit(
                target=args['target'],
                max_limit=args['max_limit'],
                name=args.get('name'),
            )
        elif action == 'remove_bandwidth_limits':
            router.remove_bandwidth_limits()
            result = {'removed': True}

        # ── Firewall ──
        elif action == 'add_firewall_rule':
            kwargs = {k: v for k, v in args.items()
                      if k not in ('action', 'netname', 'chain', 'rule_action')}
            result = router.add_firewall_rule(
                chain=args.get('chain', 'forward'),
                action=args.get('rule_action', 'drop'),
                **kwargs,
            )
        elif action == 'remove_firewall_rules':
            router.remove_firewall_rules()
            result = {'removed': True}
        elif action == 'block_internet':
            router.block_internet()
            result = {'blocked': 'internet'}
        elif action == 'block_dns':
            router.block_dns()
            result = {'blocked': 'dns'}
        elif action == 'block_port':
            router.block_port(args['port'], args.get('protocol', 'tcp'))
            result = {'blocked': f"{args.get('protocol', 'tcp')}/{args['port']}"}

        # ── Access list ──
        elif action == 'add_access_list_entry':
            result = router.add_access_list_entry(
                mac_address=args['mac_address'],
                authentication=args.get('authentication', True),
                interface=args.get('interface', ''),
                signal_range=args.get('signal_range', ''),
            )
        elif action == 'remove_access_list_entry':
            router.remove_access_list_entry(args['mac_address'])
            result = {'removed': args['mac_address']}
        elif action == 'clear_access_list':
            router.clear_access_list()
            result = {'cleared': True}

        # ── Test reset ──
        elif action == 'reset_to_defaults':
            router.reset_to_defaults(
                baseline_ssid=args.get('baseline_ssid'),
                baseline_pass=args.get('baseline_pass'),
                wireless_interfaces=args.get('wireless_interfaces'),
            )
            result = {'reset': True}

        # ── Raw API ──
        elif action == 'run':
            result = router.run(args.get('path', ''), params=args.get('params'))

        else:
            print(json.dumps({'error': f"Unknown action: '{action}'"}))
            sys.exit(1)

        print(json.dumps(result, indent=2))

    except Exception as e:
        print(json.dumps({'error': str(e)}))
        sys.exit(1)


if __name__ == '__main__':
    main()
