# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for Lager CLI defaults (box, net preferences)."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_defaults_show() -> str:
    """Show current Lager CLI default settings.

    Displays the default box, and default nets for each domain
    (power, I2C, SPI, UART, ADC, DAC, GPIO, etc.).
    """
    return run_lager("defaults")


@mcp.tool()
def lager_defaults_set(
    box: str = None,
    serial_port: str = None,
    supply_net: str = None,
    battery_net: str = None,
    solar_net: str = None,
    scope_net: str = None,
    logic_net: str = None,
    adc_net: str = None,
    dac_net: str = None,
    gpio_net: str = None,
    debug_net: str = None,
    eload_net: str = None,
    usb_net: str = None,
    webcam_net: str = None,
    watt_meter_net: str = None,
    thermocouple_net: str = None,
    uart_net: str = None,
    arm_net: str = None,
) -> str:
    """Set Lager CLI default values for box and net names.

    Once set, these defaults are used automatically so you don't have
    to pass --box or net names on every command.

    Args:
        box: Default box name (e.g., 'DEMO')
        serial_port: Default serial port path
        supply_net: Default power supply net
        battery_net: Default battery net
        solar_net: Default solar net
        scope_net: Default oscilloscope net
        logic_net: Default logic analyzer net
        adc_net: Default ADC net
        dac_net: Default DAC net
        gpio_net: Default GPIO net
        debug_net: Default debug net
        eload_net: Default electronic load net
        usb_net: Default USB net
        webcam_net: Default webcam net
        watt_meter_net: Default watt meter net
        thermocouple_net: Default thermocouple net
        uart_net: Default UART net
        arm_net: Default robotic arm net
    """
    args = ["defaults", "add"]
    flag_map = {
        "--box": box,
        "--serial-port": serial_port,
        "--supply-net": supply_net,
        "--battery-net": battery_net,
        "--solar-net": solar_net,
        "--scope-net": scope_net,
        "--logic-net": logic_net,
        "--adc-net": adc_net,
        "--dac-net": dac_net,
        "--gpio-net": gpio_net,
        "--debug-net": debug_net,
        "--eload-net": eload_net,
        "--usb-net": usb_net,
        "--webcam-net": webcam_net,
        "--watt-meter-net": watt_meter_net,
        "--thermocouple-net": thermocouple_net,
        "--uart-net": uart_net,
        "--arm-net": arm_net,
    }
    for flag, value in flag_map.items():
        if value is not None:
            args.extend([flag, value])

    if len(args) == 2:
        return "Error: At least one default must be specified."

    return run_lager(*args)


@mcp.tool()
def lager_defaults_delete(setting: str) -> str:
    """Delete a single Lager CLI default setting.

    Args:
        setting: The default to delete. Valid values:
            'box', 'serial-port', 'supply-net', 'battery-net',
            'solar-net', 'scope-net', 'logic-net', 'adc-net',
            'dac-net', 'gpio-net', 'debug-net', 'eload-net',
            'usb-net', 'webcam-net', 'watt-meter-net',
            'thermocouple-net', 'uart-net', 'arm-net'
    """
    return run_lager("defaults", "delete", setting, "--yes")


@mcp.tool()
def lager_defaults_delete_all() -> str:
    """Delete all Lager CLI default settings.

    Removes every saved default (box, nets, serial port, etc.).
    """
    return run_lager("defaults", "delete-all", "--yes")
