# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The request each `lager usb|ble|blufi|wifi|router` subcommand sends to the box.

These commands had no test that ran them: `test_net_9000_migration.py` only
imports the modules to check what they import. The bench suites that drive
them are not run by any workflow either, so a renamed action, a dropped
parameter or a confirmation that stopped guarding a destructive call would
reach users with every check green.

Each test runs the real root group (`cli.main.cli`) against the in-process
`fake_box` from conftest.py and asserts the exact payload, the exit code, and
that a refused or unconfirmed command sends nothing at all.
"""

import json

import pytest

ADDR = "AA:BB:CC:DD:EE:FF"


# ---------------------------------------------------------------------------
# lager usb <net> enable|disable|toggle|state
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", ["enable", "disable", "toggle", "state"])
def test_usb_action_reaches_the_usb_endpoint(lager, fake_box, action):
    fake_box.net("usb1", "usb")
    result = lager("usb", "usb1", action)
    assert result.exit_code == 0, result.output
    url, body = fake_box.posts[-1]
    assert url == f"http://{fake_box.IP}:9000/usb/command"
    assert body == {"netname": "usb1", "action": action}


def test_usb_on_a_net_that_does_not_exist_sends_nothing(lager, fake_box):
    fake_box.net("usb1", "usb")
    result = lager("usb", "usb9", "enable")
    assert result.exit_code != 0
    assert "usb9" in result.output
    assert fake_box.posts == []


def test_usb_failure_from_the_box_is_a_non_zero_exit(lager, fake_box):
    fake_box.net("usb1", "usb")
    fake_box.status = 500
    fake_box.answer = lambda url, body: {"success": False, "error": "hub did not answer"}
    result = lager("usb", "usb1", "disable")
    assert result.exit_code != 0
    assert "hub did not answer" in result.output


# ---------------------------------------------------------------------------
# lager ble
# ---------------------------------------------------------------------------

def test_ble_scan_sends_the_timeout_and_name_filters(lager, fake_box):
    result = lager("ble", "scan", "--timeout", "2", "--name-contains", "DK")
    assert result.exit_code == 0, result.output
    url, body = fake_box.posts[-1]
    assert url.endswith(":9000/ble/command")
    assert body == {"action": "scan",
                    "params": {"timeout": 2.0, "name_contains": "DK", "name_exact": None}}


def test_ble_scan_lists_what_the_box_found(lager, fake_box):
    fake_box.answer = lambda url, body: {
        "success": True,
        "value": {"devices": [{"name": "Bench-DK", "address": ADDR, "rssi": -40}]},
    }
    result = lager("ble", "scan", "--timeout", "1")
    assert result.exit_code == 0, result.output
    assert "Found 1 device(s)" in result.output
    assert "Bench-DK" in result.output and ADDR in result.output


@pytest.mark.parametrize("verb", ["info", "connect", "disconnect"])
def test_ble_address_commands_send_the_address(lager, fake_box, verb):
    result = lager("ble", verb, ADDR)
    assert result.exit_code == 0, result.output
    assert fake_box.last == {"action": verb, "params": {"address": ADDR}}


@pytest.mark.parametrize("verb", ["info", "connect", "disconnect"])
@pytest.mark.parametrize("bad", ["nothex", "AA:BB:CC:DD:EE", "GG:BB:CC:DD:EE:FF"])
def test_ble_rejects_a_malformed_address_before_the_box(lager, fake_box, verb, bad):
    result = lager("ble", verb, bad)
    assert result.exit_code != 0
    assert "Invalid BLE address format" in result.output
    assert fake_box.posts == []


def test_ble_dash_separated_address_is_accepted(lager, fake_box):
    result = lager("ble", "info", "AA-BB-CC-DD-EE-FF")
    assert result.exit_code == 0, result.output
    assert fake_box.last["params"] == {"address": "AA-BB-CC-DD-EE-FF"}


def test_ble_connect_failure_is_a_non_zero_exit(lager, fake_box):
    fake_box.status = 500
    fake_box.answer = lambda url, body: {"success": False, "error": "device not found"}
    result = lager("ble", "connect", ADDR)
    assert result.exit_code != 0
    assert "device not found" in result.output


# ---------------------------------------------------------------------------
# lager blufi
# ---------------------------------------------------------------------------

def test_blufi_scan_payload(lager, fake_box):
    result = lager("blufi", "scan")
    assert result.exit_code == 0, result.output
    url, body = fake_box.posts[-1]
    assert url.endswith(":9000/blufi/command")
    assert body == {"action": "scan", "params": {"timeout": 10.0, "name_contains": None}}


@pytest.mark.parametrize("verb, action", [
    ("status", "status"),
    ("version", "version"),
    ("connect", "connect"),
])
def test_blufi_device_commands_name_the_device(lager, fake_box, verb, action):
    result = lager("blufi", verb, "esp-dev")
    assert result.exit_code == 0, result.output
    assert fake_box.last == {"action": action,
                             "params": {"device_name": "esp-dev", "timeout": 20.0}}


def test_blufi_wifi_scan_uses_the_wire_action_name(lager, fake_box):
    result = lager("blufi", "wifi-scan", "esp-dev")
    assert result.exit_code == 0, result.output
    assert fake_box.last["action"] == "wifi_scan"
    assert fake_box.last["params"]["device_name"] == "esp-dev"
    assert fake_box.last["params"]["scan_timeout"] == 15.0


def test_blufi_provision_sends_the_credentials(lager, fake_box):
    result = lager("blufi", "provision", "esp-dev", "--ssid", "benchnet",
                   "--password", "benchpass")
    assert result.exit_code == 0, result.output
    assert fake_box.last["action"] == "provision"
    params = fake_box.last["params"]
    assert params["device_name"] == "esp-dev"
    assert params["ssid"] == "benchnet"
    assert params["password"] == "benchpass"


def test_blufi_failure_is_a_non_zero_exit(lager, fake_box):
    fake_box.status = 500
    fake_box.answer = lambda url, body: {"success": False, "error": "no such device"}
    result = lager("blufi", "status", "esp-dev")
    assert result.exit_code != 0
    assert "no such device" in result.output


# ---------------------------------------------------------------------------
# lager wifi (hidden group)
# ---------------------------------------------------------------------------

# `status` and `access-points` read the box host over SSH, not the container,
# which cannot see the host's wireless interface. Each test fakes the SSH
# runner with what the host would print.

@pytest.fixture
def host(monkeypatch):
    """The box host as seen over SSH: canned output, and the commands sent."""
    class Host:
        rc, out, err = 0, "", ""
        cmds = []

        def __call__(self, box_ip, cmd, **kw):
            self.cmds.append(cmd)
            return self.rc, self.out, self.err

    fake = Host()
    fake.cmds = []
    monkeypatch.setattr("cli.commands.communication.wifi.default_ssh_runner", fake)
    return fake


IW_CONNECTED = """phy#0
\tInterface wlan0
\t\tifindex 3
\t\ttype managed
\t\tssid bench net
"""


def _status(wireless="wlan0 up", networkctl="!missing", nmcli="!missing", iw="!missing"):
    """The status script's output: one section per source, as the host prints it."""
    return (f"--wireless--\n{wireless}\n--networkctl--\n{networkctl}\n"
            f"--nmcli--\n{nmcli}\n--iw--\n{iw}\n")


def test_wifi_status_reads_the_host_and_sends_nothing_to_the_box(lager, fake_box, host):
    host.out = _status(nmcli="wlan0:wifi:connected:preconfigured\neth0:ethernet:connected:Wired",
                       iw=IW_CONNECTED)
    result = lager("wifi", "status")
    assert result.exit_code == 0, result.output
    assert "SSID:  bench net" in result.output
    assert "State: Connected" in result.output
    assert "eth0" not in result.output
    assert fake_box.posts == []


@pytest.mark.parametrize("label", ["WiFi access point", "Wi-Fi access point"])
def test_wifi_status_from_networkctl_alone(lager, fake_box, host, label):
    # A box on systemd-networkd with no NetworkManager and no iw. systemd 249
    # and 255 spell the access-point label differently.
    host.out = _status(wireless="wlp2s0 up", networkctl=(
        f"@wlp2s0\n                       State: routable (configured)\n"
        f"          {label}: bench net (aa:bb:cc:dd:ee:ff)"))
    result = lager("wifi", "status")
    assert result.exit_code == 0, result.output
    assert "Interface: wlp2s0" in result.output
    assert "SSID:  bench net\n" in result.output
    assert "State: Connected" in result.output


def test_wifi_status_networkctl_with_no_carrier_is_disconnected(lager, fake_box, host):
    host.out = _status(wireless="wlp2s0 dormant",
                       networkctl="@wlp2s0\n   State: no-carrier (configured)")
    result = lager("wifi", "status")
    assert result.exit_code == 0, result.output
    assert "State: Disconnected" in result.output


def test_wifi_status_with_only_iw_on_the_host(lager, fake_box, host):
    host.out = _status(wireless="wlan0 down",
                       iw=IW_CONNECTED.replace("\t\tssid bench net\n", ""))
    result = lager("wifi", "status")
    assert result.exit_code == 0, result.output
    assert "State: Disconnected" in result.output


def test_wifi_status_up_with_no_source_for_the_ssid(lager, fake_box, host):
    host.out = _status(wireless="wlan0 up")
    result = lager("wifi", "status")
    assert result.exit_code == 0, result.output
    assert "SSID:  Unknown" in result.output
    assert "State: Up" in result.output


def test_wifi_status_fails_when_the_host_script_did_not_run(lager, fake_box, host):
    host.rc, host.out, host.err = 127, "", "sh: 1: syntax error"
    result = lager("wifi", "status")
    assert result.exit_code == 1
    assert "Cannot read the box's Wi-Fi state" in result.output


def test_wifi_status_fails_with_no_wireless_interface(lager, fake_box, host):
    host.out = _status(wireless="", nmcli="eth0:ethernet:connected:Wired")
    result = lager("wifi", "status")
    assert result.exit_code == 1
    assert "no wireless interface" in result.output


def test_wifi_status_names_an_ssh_failure(lager, fake_box, host):
    host.rc, host.err = 255, "Permission denied (publickey)."
    result = lager("wifi", "status")
    assert result.exit_code == 1
    assert "SSH key authentication failed" in result.output


def test_wifi_access_points_scans_the_named_interface(lager, fake_box, host):
    host.out = "@wlan1\nbench net:WPA2:40\nbench net:WPA2:71\nlab\\:guest::12\n:WPA2:90\n"
    result = lager("wifi", "access-points", "--interface", "wlan1")
    assert result.exit_code == 0, result.output
    assert "IF=wlan1;" in host.cmds[-1]
    assert "on wlan1" in result.output
    assert fake_box.posts == []
    payload = json.loads(result.output.split("JSON Output:", 1)[1])
    assert payload == {"access_points": [
        {"ssid": "bench net", "security": "WPA2", "strength": 71},
        {"ssid": "lab:guest", "security": "Open", "strength": 12},
    ]}


def test_wifi_access_points_defaults_to_the_box_wireless_interface(lager, fake_box, host):
    host.out = "@wlp2s0\nbench net:WPA2:40\n"
    result = lager("wifi", "access-points")
    assert result.exit_code == 0, result.output
    assert "IF='';" in host.cmds[-1]
    assert "on wlp2s0" in result.output


def test_wifi_access_points_without_networkmanager_says_why(lager, fake_box, host):
    host.out = "@wlp2s0\n!missing\n"
    result = lager("wifi", "access-points")
    assert result.exit_code == 1
    assert "Cannot list the access points on wlp2s0" in result.output
    assert "does not run NetworkManager" in result.output


def test_wifi_access_points_with_no_wireless_interface(lager, fake_box, host):
    host.out = "@\n"
    result = lager("wifi", "access-points")
    assert result.exit_code == 1
    assert "no wireless interface" in result.output


def test_wifi_connect_sends_ssid_password_and_interface(lager, fake_box):
    result = lager("wifi", "connect", "--ssid", "benchnet", "--password", "longenough")
    assert result.exit_code == 0, result.output
    assert fake_box.last == {"action": "connect",
                             "params": {"ssid": "benchnet", "password": "longenough",
                                        "interface": "wlan0"}}


def test_wifi_connect_refuses_a_short_wpa_password(lager, fake_box):
    result = lager("wifi", "connect", "--ssid", "benchnet", "--password", "short")
    assert result.exit_code != 0
    assert "Password too short" in result.output
    assert fake_box.posts == []


def test_wifi_delete_connection_is_not_sent_without_confirmation(lager, fake_box):
    # Deleting the box's Wi-Fi connection can take it off the network, so the
    # prompt is the only thing between a typo and a box that needs a cable.
    result = lager("wifi", "delete-connection", "benchnet", input="n\n")
    assert "ethernet connection will be required" in result.output
    assert fake_box.posts == []


def test_wifi_delete_connection_with_yes(lager, fake_box):
    result = lager("wifi", "delete-connection", "benchnet", "--yes")
    assert result.exit_code == 0, result.output
    assert fake_box.last == {"action": "delete",
                             "params": {"ssid": "benchnet", "connection_name": "benchnet"}}


# ---------------------------------------------------------------------------
# lager router
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv, action, params", [
    (["connect"], "connect", {}),
    (["interfaces"], "interfaces", {}),
    (["wireless-interfaces"], "wireless_interfaces", {}),
    (["wireless-clients"], "wireless_clients", {}),
    (["dhcp-leases"], "dhcp_leases", {}),
    (["system-info"], "system_info", {}),
    (["block-internet"], "block_internet", {}),
    (["enable-interface", None, "wlan1"], "enable_interface", {"interface": "wlan1"}),
    (["disable-interface", None, "wlan1"], "disable_interface", {"interface": "wlan1"}),
    (["reboot", None, "--yes"], "reboot", {}),
    (["reset", None, "--yes"], "reset_to_defaults",
     {"baseline_ssid": None, "baseline_pass": None}),
    (["reset", None, "--yes", "--ssid", "lab", "--password", "labpass1"],
     "reset_to_defaults", {"baseline_ssid": "lab", "baseline_pass": "labpass1"}),
])
def test_router_subcommand_payload(lager, fake_box, argv, action, params):
    fake_box.net("router1", "router")
    argv = [argv[0], "router1"] + [a for a in argv[1:] if a is not None]
    result = lager("router", *argv)
    assert result.exit_code == 0, result.output
    url, body = fake_box.posts[-1]
    assert url.endswith(":9000/net/command")
    assert body == {"netname": "router1", "action": action, "params": params,
                    "role": "router"}


@pytest.mark.parametrize("verb", ["reboot", "reset"])
def test_router_destructive_commands_need_confirmation(lager, fake_box, verb):
    fake_box.net("router1", "router")
    result = lager("router", verb, "router1", input="n\n")
    assert fake_box.posts == [], result.output
