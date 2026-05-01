# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations
import re
import os
from decimal import Decimal
import pyvisa

from lager.power.supply.supply_net import SupplyNet, SupplyBackendError, LibraryMissingError, DeviceNotFoundError
from lager.instrument_wrappers.instrument_wrap import InstrumentWrap

# ANSI color codes
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'


LAGER_CURRENT_LIMIT = 1

class RigolDP800(SupplyNet):
    """
    SupplyNet implementation for Rigol DP800 series.
    Expects the low-level DP800 class (SCPI driver) to be available in this module.

    Hardware Specifications:
    -------------------------
    DP821: 2 channels
      - Channel 1: 60V/1A max (60W max power)
      - Channel 2: 8V/10A max (80W max power)

    DP832/DP832A: 3 channels
      - Channel 1: 30V/3A max (90W max power)
      - Channel 2: 30V/3A max (90W max power)
      - Channel 3: 5V/3A max (15W max power)

    DP811/DP811A: 1 channel + 1 range channel
      - Channel 1: 20V/10A or 40V/5A (200W max power)

    Important Notes:
    - Always respect hardware limits when setting voltages/currents
    - The instrument will clamp values to its maximum capabilities
    - OVP (Over Voltage Protection) must not exceed channel voltage limits
    - OCP (Over Current Protection) must not exceed channel current limits
    - Test scripts should account for different channel capabilities
    """

    def __init__(self, address: str, channel: int, instrument_hint: str | None = None):
        # Open VISA with fallback to @py backend if usbtmc kernel driver is busy
        last_exc = None
        instr = None

        # Try default backend first, then @py backend (pyvisa-py)
        for backend in (None, "@py"):
            try:
                rm = pyvisa.ResourceManager() if backend is None else pyvisa.ResourceManager(backend)
                instr = rm.open_resource(address)
                break
            except Exception as exc:
                last_exc = exc
                continue

        if instr is None:
            raise DeviceNotFoundError(
                f"Could not open instrument at {address}: {last_exc}"
            ) from last_exc

        # Keep RM alive to prevent GC from invalidating session handles
        self._rm = rm

        # Wrap the raw VISA instrument
        self.instr = InstrumentWrap(instr) if InstrumentWrap else instr
        self.channel = int(channel)

        # Validate model + channel
        self.check_instrument()

        try:
            max_ch = int(self.num_channels())
        except Exception:
            max_ch = 1

        if not (1 <= self.channel <= max_ch):
            raise SupplyBackendError(
                f"Requested channel {self.channel} not available on instrument "
                f"(supports {max_ch} channel{'s' if max_ch != 1 else ''})."
            )
    
    # ---- SupplyNet interface (channel-scoped) ----

    def voltage(self, value: float | None = None,
                ocp: float | None = None,
                ovp: float | None = None) -> None:
        ch = self.channel
        if ocp is not None:
            self.set_overcurrent_protection_value(ocp, channel=ch)
            self.enable_overcurrent_protection(channel=ch)
        if ovp is not None:
            # Validate OVP >= voltage setpoint before setting
            if value is not None:
                if ovp < value:
                    raise SupplyBackendError(f"OVP ({ovp}V) cannot be less than voltage setpoint ({value}V)")
            else:
                # Check against current voltage setpoint
                current_vset = float(self.get_channel_voltage(source=ch))
                if ovp < current_vset:
                    raise SupplyBackendError(f"OVP ({ovp}V) cannot be less than current voltage setpoint ({current_vset}V)")
            self.set_overvoltage_protection_value(ovp, channel=ch)
            self.enable_overvoltage_protection(channel=ch)
            # Clear any existing OVP trip after changing the limit
            try:
                if self.overvoltage_protection_is_tripped(ch):
                    self.clear_overvoltage_protection_trip(channel=ch)
            except Exception:
                pass
            # Add brief delay after setting protection
            try:
                import time
                time.sleep(0.1)
            except Exception:
                pass

        if value is not None:
            # Validate positive voltage
            if value < 0:
                raise SupplyBackendError(f"Voltage must be positive, got {value}V")

            # Get current OVP limit
            current_ovp = float(self.get_overvoltage_protection_value(ch))

            # If voltage is too close to OVP limit (within 5%), temporarily raise OVP
            # to prevent spurious trips during voltage settling
            temp_ovp_raised = False
            if value > current_ovp:
                raise SupplyBackendError(f"Voltage setpoint ({value}V) cannot exceed OVP limit ({current_ovp}V)")
            elif current_ovp > 0 and value >= current_ovp * 0.95:
                # Temporarily raise OVP by 10% to give headroom during voltage change
                try:
                    temp_ovp = current_ovp * 1.1
                    self.set_overvoltage_protection_value(temp_ovp, channel=ch)
                    temp_ovp_raised = True
                    import time
                    time.sleep(0.05)
                except Exception:
                    pass

            # Clear any existing OVP trip before setting voltage
            try:
                if self.overvoltage_protection_is_tripped(ch):
                    self.clear_overvoltage_protection_trip(channel=ch)
                    import time
                    time.sleep(0.05)
            except Exception:
                pass

            # DP800 uses :VOLT and :SOURx for channel-scoped set
            self.set_channel_voltage(value, source=ch)

            # Allow time for instrument to process voltage change
            try:
                import time
                time.sleep(0.15)
            except Exception:
                pass

            # Restore original OVP limit if we raised it
            if temp_ovp_raised:
                try:
                    self.set_overvoltage_protection_value(current_ovp, channel=ch)
                    import time
                    time.sleep(0.05)
                except Exception:
                    pass

            # Final clear of any OVP trip
            try:
                if self.overvoltage_protection_is_tripped(ch):
                    self.clear_overvoltage_protection_trip(channel=ch)
            except Exception:
                pass

            print(f"{GREEN}Voltage set to: {value:.4f}V{RESET}")
            return

        # Add brief delay before reading to ensure stable measurement
        try:
            import time
            time.sleep(0.1)
        except Exception:
            pass
        v = self.measure_voltage(ch)
        # Format consistently as a simple decimal number
        print(f"{GREEN}Voltage: {float(v):.4f}{RESET}")

    def current(self, value: float | None = None,
                ocp: float | None = None,
                ovp: float | None = None) -> None:
        ch = self.channel
        if ocp is not None:
            self.set_overcurrent_protection_value(ocp, channel=ch)
            self.enable_overcurrent_protection(channel=ch)
        if ovp is not None:
            self.set_overvoltage_protection_value(ovp, channel=ch)
            self.enable_overvoltage_protection(channel=ch)

        if value is not None:
            # Validate positive current
            if value < 0:
                raise SupplyBackendError(f"Current must be positive, got {value}A")
            self.set_channel_current(value, source=ch)
            # Allow time for instrument to settle after current change
            try:
                import time
                time.sleep(0.15)
            except Exception:
                pass
            print(f"{GREEN}Current set to: {value:.4f}A{RESET}")
            return

        # Read current limit setpoint (not measurement)
        c_setpoint = self.get_channel_current(source=ch)
        print(f"{GREEN}Current: {float(c_setpoint):.4f}{RESET}")

    def enable(self) -> None:
        self.enable_output(channel=self.channel)

    def disable(self) -> None:
        self.disable_output(channel=self.channel)

    def set_mode(self) -> None:
        # DP800 is a DC supply; nothing special needed here.
        return

    def state(self) -> None:
        """Legacy entry point — kept for direct callers (e.g. tests)."""
        result = self.read_state_fields()
        if result is None:
            return
        from lager.cli_output import print_state
        print_state(
            "supply",
            result["fields"],
            command="supply.state",
            subject={"instrument": result.get("instrument"),
                     "channel": result.get("channel")},
            title_severity=result.get("severity", "ok"),
        )

    def read_state_fields(self):
        """Structured state for cli_output.print_state. See SupplyNet.read_state_fields."""
        from lager.cli_output import Field
        ch = self.channel
        v = self.measure_voltage(ch)
        i = self.measure_current(ch)
        p = self.measure_power(ch)
        v_set = self.get_voltage_set(ch) if hasattr(self, "get_voltage_set") else v
        i_set = self.get_current_set(ch) if hasattr(self, "get_current_set") else i

        ocp_limit = self.get_overcurrent_protection_value(ch)
        ocp_tripped = self.overcurrent_protection_is_tripped(ch)
        ovp_limit = self.get_overvoltage_protection_value(ch)
        ovp_tripped = self.overvoltage_protection_is_tripped(ch)

        enabled = self.output_is_enabled(ch)
        mode = self.get_output_mode(ch)

        # Rigol model is a DP8xx — pull the specific model from the IDN if we can.
        instrument_label = "Rigol DP800"
        try:
            idn = self.get_identification() or ""
            m = re.search(r"DP8\d{2}\w?", idn)
            if m:
                instrument_label = f"Rigol {m.group(0)}"
        except Exception:
            pass

        def _f(x, default=0.0):
            try:
                return float(x)
            except Exception:
                return default

        fields = [
            Field("Output", bool(enabled), severity=("ok" if enabled else "error")),
            Field("Mode",   mode),
            Field("Set",    value=(_f(v_set), _f(i_set)), unit=("V", "A"),
                            json_subkeys=("voltage", "current")),
            Field("Measured", value=(_f(v), _f(i)), unit=("V", "A"),
                              json_subkeys=("voltage", "current")),
            Field("Power",  _f(p), unit="W"),
            Field("OCP",    _f(ocp_limit), unit="A",
                  severity=("error" if ocp_tripped else None)),
            Field("OCP Tripped", bool(ocp_tripped),
                  severity=("error" if ocp_tripped else "ok")),
            Field("OVP",    _f(ovp_limit), unit="V",
                  severity=("error" if ovp_tripped else None)),
            Field("OVP Tripped", bool(ovp_tripped),
                  severity=("error" if ovp_tripped else "ok")),
        ]

        any_trip = bool(ocp_tripped) or bool(ovp_tripped)
        return {
            "instrument": instrument_label,
            "channel": ch,
            "severity": "error" if any_trip else "ok",
            "fields": fields,
        }

    def clear_ocp(self) -> None:
        self.clear_overcurrent_protection_trip(channel=self.channel)
        # Allow time for protection to clear and instrument to stabilize
        try:
            import time
            time.sleep(0.15)
        except Exception:
            pass

    def clear_ovp(self) -> None:
        self.clear_overvoltage_protection_trip(channel=self.channel)
        # Allow time for protection to clear and instrument to stabilize
        try:
            import time
            time.sleep(0.15)
        except Exception:
            pass

    def ocp(self, value: float | None = None) -> None:
        """Set or read over-current protection limit"""
        ch = self.channel
        if value is not None:
            # Validate positive value
            if value < 0:
                raise SupplyBackendError(f"OCP limit must be positive, got {value}A")
            self.set_overcurrent_protection_value(value, channel=ch)
            self.enable_overcurrent_protection(channel=ch)
            return

        # Read current OCP limit
        ocp_limit = self.get_overcurrent_protection_value(ch)
        print(f"{GREEN}OCP Limit: {ocp_limit}{RESET}")

    def ovp(self, value: float | None = None) -> None:
        """Set or read over-voltage protection limit"""
        ch = self.channel
        if value is not None:
            # Validate positive value
            if value < 0:
                raise SupplyBackendError(f"OVP limit must be positive, got {value}V")

            # Check against current voltage setpoint
            current_vset = float(self.get_channel_voltage(source=ch))
            if value < current_vset:
                raise SupplyBackendError(f"OVP limit ({value}V) cannot be less than current voltage setpoint ({current_vset}V)")

            self.set_overvoltage_protection_value(value, channel=ch)
            self.enable_overvoltage_protection(channel=ch)
            # Clear any existing OVP trip after changing the limit
            try:
                if self.overvoltage_protection_is_tripped(ch):
                    self.clear_overvoltage_protection_trip(channel=ch)
            except Exception:
                pass
            return

        # Read current OVP limit
        ovp_limit = self.get_overvoltage_protection_value(ch)
        print(f"{GREEN}OVP Limit: {ovp_limit}{RESET}")

    def get_full_state(self) -> None:
        """Get full state including measurements, setpoints, and limits"""
        ch = self.channel

        # Get measurements
        v = self.measure_voltage(ch)
        i = self.measure_current(ch)
        p = self.measure_power(ch)

        # Get setpoints
        v_set = self.get_channel_voltage(source=ch)
        i_set = self.get_channel_current(source=ch)

        # Get protection limits
        ocp_limit = self.get_overcurrent_protection_value(ch)
        ocp_tripped = self.overcurrent_protection_is_tripped(ch)
        ovp_limit = self.get_overvoltage_protection_value(ch)
        ovp_tripped = self.overvoltage_protection_is_tripped(ch)

        # Get status
        enabled = self.output_is_enabled(ch)
        mode = self.get_output_mode(ch)

        # Get hardware limits from channel data
        channel_data = self.get_channel(ch)
        v_max = channel_data.get("max_voltage", ovp_limit)
        i_max = channel_data.get("max_current", ocp_limit)

        # Print formatted output for parsing
        print(f"{GREEN}Channel: {ch}{RESET}")
        print(f"{GREEN}Enabled: {'ON' if enabled else 'OFF'}{RESET}")
        print(f"{GREEN}Mode: {mode}{RESET}")
        print(f"{GREEN}Voltage: {v}{RESET}")
        print(f"{GREEN}Current: {i}{RESET}")
        print(f"{GREEN}Power: {p}{RESET}")
        print(f"{GREEN}Voltage_Set: {v_set}{RESET}")
        print(f"{GREEN}Current_Set: {i_set}{RESET}")
        print(f"{GREEN}OCP Limit: {ocp_limit}{RESET}")
        ocp_status = f"{RED}YES{RESET}" if ocp_tripped else f"{GREEN}NO{RESET}"
        print(f"    OCP Tripped: {ocp_status}")
        print(f"{GREEN}OVP Limit: {ovp_limit}{RESET}")
        ovp_status = f"{RED}YES{RESET}" if ovp_tripped else f"{GREEN}NO{RESET}"
        print(f"    OVP Tripped: {ovp_status}")
        print(f"{GREEN}Voltage_Max: {v_max}{RESET}")
        print(f"{GREEN}Current_Max: {i_max}{RESET}")

    def check_instrument(self):
        idn = self.get_identification()
        match = re.match("RIGOL TECHNOLOGIES,DP8", idn)
        if not match:
            msg = f"Unknown device identification:\n{idn}\n"
            raise NameError(msg)

    def get_identification(self):
        """
        Query the ID string of the instrument.
        """
        return self.instr.query("*IDN?")

    def set_lager_safety(self):
        # Only act on this net’s channel.
        ch = self.channel
        self.disable_output(channel=ch)
        self.set_overcurrent_protection_value(LAGER_CURRENT_LIMIT, channel=ch)
        self.enable_overcurrent_protection(channel=ch)

    def __str__(self):
        return self.get_identification()

    def _interpret_channel(self, channel):
        """
        Wrapper to allow specifying channels by their name (str) or by their
        number (int)
        """
        if type(channel) == int:
            assert channel <= 3 and channel >= 1
            channel = "CH" + str(channel)
        return channel

    def _interpret_source(self, source):
        """
        Wrapper to allow specifying sources by their name (str) or by their
        number (int)
        """
        if type(source) == int:
            assert source <= 3 and source >= 1
            source = "SOUR" + str(source)
        return source

    def run_analyzer(self):
        """
        When receiving this command, the instrument executes the analysis
        operation according to the current setting.
        """
        self.instr.write(":ANAL:ANAL")

    def get_analyzer_current_time(self):
        """
        Query the current time of the analyzer.
        """
        return int(self.instr.query(":ANAL:CURRT?"))

    def set_analyzer_current_time(self, time=1):
        """
        Set the current time of the analyzer.
        """
        self.instr.write(":ANAL:CURRT {0}".format(time))

    def get_analyzer_end_time(self):
        """
        Query the end time of the analyzer.
        """
        return int(self.instr.query(":ANAL:ENDT?"))

    def set_analyzer_end_time(self, time=2):
        """
        Set the end time of the analyzer.
        """
        self.instr.write(":ANAL:ENDT {0}".format(time))

    def get_analyzer_file(self):
        """
        Query the record file currently opened.
        """
        return self.instr.query(":ANAL:FILE?")

    def set_analyzer_file(self, location):
        """
        Open the specified record file in memory.
        """
        if type(location) is int:
            assert location >= 1 and location <= 10
            self.instr.write(":ANAL:MEM {0}".format(location))
        else:
            assert location.startswith("D:\\")
            self.instr.write(":ANAL:MMEM {0}".format(location))

    def get_analyzer_unit(self):
        """
        Query the analysis object of the analyzer.
        """
        return self.instr.query(":ANAL:OBJ?")

    def set_analyzer_unit(self, unit="V"):
        """
        Set the analysis object of the analyzer to voltage, current or power.
        """
        assert unit in ["V", "C", "P"]
        self.instr.write(":ANAL:OBJ {0}".format(unit))

    def get_analyzer_result(self):
        """
        Query the analysis results, including the number of groups, median,
        mode, average, variance, range, minimum, maximum and mean deviation
        """
        response = self.instr.query(":ANAL:RES?")
        data = dict([attr.split(":") for attr in response.split(",")])
        return data

    def set_analyzer_start_time(self, time=1):
        """
        Set the start time of the analyzer.
        """
        self.instr.write(":ANAL:STARTT {0}".format(time))

    def get_analyzer_start_time(self):
        """
        Query the start time of the analyzer.
        """
        return int(self.instr.query(":ANAL:STARTT?"))

    def get_analyzer_value(self, time=1):
        """
        Query the voltage, current and power at the specified time in the
        record file opened.
        """
        response = self.instr.query(":ANAL:VAL? {0}".format(time))
        data = dict([attr.split(":") for attr in response.split(",")])
        return data

    def get_channel(self, channel=1):
        """
        Query the voltage/current of the specified channel.
        """
        channel = self._interpret_channel(channel)
        response = self.instr.query(":APPL? {0}".format(channel)).strip()
        data = response.split(",")
        data = {
            "max_voltage": float(self.get_overvoltage_protection_value(channel)),
            "max_current": float(self.get_overcurrent_protection_value(channel)),
            "set_voltage": data[1],
            "set_current": data[2],
            "measured": self.measure(channel),
            "has_overvoltage": self.overvoltage_protection_is_tripped(channel),
            "has_overcurrent": self.overcurrent_protection_is_tripped(channel),
        }
        data["enabled"] = self.output_is_enabled(channel)
        data["mode"] = self.get_output_mode(channel)
        return data

    def set_channel(self, voltage, current, channel=1):
        """
        Select the specified channel as the current channel and set the
        voltage/current of this channel.
        """
        channel = self._interpret_channel(channel)
        self.instr.write(":APPL {0},{1},{2}".format(channel, voltage, current))

    def get_channel_limits(self, channel=1):
        """
        Get hardware maximum voltage and current ratings for the specified channel.

        Returns the actual hardware specifications, not protection limits.

        Hardware Specs:
        - DP832/DP832A: Ch1/Ch2: 30V/3A, Ch3: 5V/3A
        - DP821/DP821A: Ch1: 60V/1A, Ch2: 8V/10A
        - DP811/DP811A: Ch1: 20V/10A (low range) or 40V/5A (high range)
        - DP831/DP831A: Ch1: 8V/5A, Ch2/Ch3: 30V/2A
        """
        # Get model ID from instrument identification
        # IDN format: "RIGOL TECHNOLOGIES,DP821A,DP8G235000146,00.01.14"
        try:
            idn = self.get_identification()
            # Extract model from IDN string (e.g., "DP821A")
            parts = idn.split(',')
            if len(parts) >= 2:
                model = parts[1].strip().upper()
            else:
                model = "DP832"  # Default assumption
        except Exception:
            model = "DP832"  # Default assumption

        # Parse channel limits based on model
        if "DP821" in model:
            if channel == 1:
                return {"voltage_max": 60.0, "current_max": 1.0}
            elif channel == 2:
                return {"voltage_max": 8.0, "current_max": 10.0}
        elif "DP832" in model:
            if channel in (1, 2):
                return {"voltage_max": 30.0, "current_max": 3.0}
            elif channel == 3:
                return {"voltage_max": 5.0, "current_max": 3.0}
        elif "DP811" in model:
            # DP811 has range selection, return high range limits
            return {"voltage_max": 40.0, "current_max": 5.0}
        elif "DP831" in model:
            if channel == 1:
                return {"voltage_max": 8.0, "current_max": 5.0}
            elif channel in (2, 3):
                return {"voltage_max": 30.0, "current_max": 2.0}

        # Fallback: assume DP832 specs
        if channel in (1, 2):
            return {"voltage_max": 30.0, "current_max": 3.0}
        else:
            return {"voltage_max": 5.0, "current_max": 3.0}

    def get_delay_cycles(self):
        """
        Query the number of cycles of the delayer.
        """
        response = self.instr.query(":DELAY:CYCLE?")
        if response == "I":
            return response
        else:
            return int(response.split(",")[1])

    def set_delay_cycles(self, cycles=1):
        """
        Set the number of cycles of the delayer
        """
        if cycles == "I":
            self.instr.write(":DELAY:CYCLE {0}".format(cycles))
        else:
            assert cycles >= 1 and cycles <= 99999
            self.instr.write(":DELAY:CYCLE N,{0}".format(cycles))

    def get_delay_end_state(self):
        """
        Query the end state of the delayer.
        """
        return self.instr.query(":DELAY:ENDS?")

    def set_delay_end_state(self, state="OFF"):
        """
        Set the end state of the delayer.
        """
        self.instr.write(":DELAY:ENDS {0}".format(state))

    def get_delay_groups(self):
        """
        Query the number of output groups of the delayer.
        """
        return int(self.instr.query(":DELAY:GROUP?"))

    def set_delay_groups(self, groups=1):
        """
        Set the number of output groups of the delayer.
        """
        assert groups >= 1 and groups <= 2048
        self.instr.write(":DELAY:GROUP {0}".format(groups))

    def get_delay_parameters(self, group=0, num_groups=1):
        """
        Query the delayer parameters of the specified groups.
        """
        response = self.instr.query(":DELAY:PARA? {0},{1}".format(group, num_groups))
        data = [
            dict(zip(["group", "state", "delay"], parameters.split(",")))
            for parameters in response[response.index(",") - 1 : -1].split(";")
        ]
        return data

    def set_delay_parameters(self, group=0, state="OFF", delay=1):
        """
        Set the delayer parameters of the specified group.
        """
        assert delay >= 1 and delay <= 99999
        self.instr.write(":DELAY:PARA {0},{1},{2}".format(group, state, delay))

    def delay_is_enabled(self):
        """
        Query the state of the delay output function of the current channel.
        """
        return self.instr.query(":DELAY?") == "ON"

    def enable_delay(self):
        """
        Enable the state of the delay output function of the current channel.
        """
        self.instr.write(":DELAY ON")

    def disable_delay(self):
        """
        Disable the state of the delay output function of the current channel.
        """
        self.instr.write(":DELAY OFF")

    def get_delay_generation_pattern(self):
        """
        Query the pattern used when generating state automatically.
        """
        return self.instr.query(":DELAY:STAT:GEN?")[:-1]

    def set_delay_generation_pattern(self, pattern="01"):
        """
        Select the pattern used when generating state automatically.
        """
        assert pattern in ["01", "10"]
        self.instr.write(":DELAY:STAT:GEN {0}P".format(pattern))

    def get_delay_stop_condition(self):
        """
        Query the stop condition of the delayer.
        """
        response = self.instr.query(":DELAY:STOP?")
        if response == "NONE":
            return {"condition": "NONE", "value": Decimal("0")}
        else:
            data = dict(list(zip(["condition", "value"], response.split(","))))
            data["value"] = Decimal(data["value"])
            return data

    def set_delay_stop_condition(self, condition="NONE", value=0):
        """
        Set the stop condition of the delayer.
        """
        self.instr.write(":DELAY:STOP {0},{1}".format(condition, value))

    def get_delay_generation_time(self):
        """
        Query the method used to generate time automatically as well as the
        corresponding parameters.
        """
        response = self.instr.query(":DELAY:TIME:GEN?")
        data = dict(zip(["mode", "timebase", "step"], response.split(",")))
        data["timebase"] = int(data["timebase"])
        data["step"] = int(data["step"])
        return data

    def set_delay_generation_time(self, mode="FIX", timebase=None, step=None):
        """
        Set the method used to generate time automatically and the
        corresponding parameters.
        """
        if timebase is not None:
            assert step is not None
            self.instr.write(":DELAY:TIME:GEN {0},{1},{2}".format(mode, timebase, step))
        else:
            self.instr.write(":DELAY:TIME:GEN {0}".format(mode))

    def get_display_mode(self):
        """
        Query the current display mode.
        """
        return self.instr.query(":DISP:MODE?")[:4]

    def set_display_mode(self, mode="NORM"):
        """
        Set the current display mode.
        """
        assert mode in ["NORM", "WAVE", "DIAL", "CLAS"]
        self.instr.write(":DISP:MODE {0}".format(mode))

    def enable_screen_display(self):
        """
        Turn on the screen display.
        """
        self.instr.write(":DISP ON")

    def disable_screen_display(self):
        """
        Turn off the screen display.
        """
        self.instr.write(":DISP OFF")

    def screen_display_is_enabled(self):
        """
        Query the current screen display state.
        """
        return self.instr.query(":DISP?") == "ON"

    def clear_display_text(self):
        """
        Clear the characters displayed on the screen.
        """
        self.instr.write(":DISP:TEXT:CLE")

    def get_display_text(self):
        """
        Query the string currently displayed on the screen.
        """
        return self.instr.query(":DISP:TEXT?")[1:-1]

    def set_display_text(self, text, x=5, y=110):
        """
        Display the specified string from the specified coordinate on the screen.
        """
        self.instr.write(':DISP:TEXT "{0}",{1},{2}'.format(text, x, y))

    def clear_status(self):
        """
        Clear all the event registers in the register set and clear the error
        queue.
        """
        self.instr.write("*CLS")

    def get_event_status_enable(self):
        """
        Query the enable register for the standard event status register set.
        """
        return int(self.instr.query("*ESE?"))

    def set_event_status_enable(self, data=0):
        """
        Set the enable register for the standard event status register set.
        """
        assert data >= 0 and data <= 255
        self.instr.write("*ESE {0}".format(data))

    def get_event_status(self):
        """
        Query and clear the event register for the standard event status
        register.
        """
        return int(self.instr.query("*ESR?"))

    def get_vendor(self):
        return self.get_identification().split(",")[0]

    def get_product(self):
        return self.get_identification().split(",")[1]

    def get_serial_number(self):
        return self.get_identification().split(",")[2]

    def get_firmware(self):
        return self.get_identification().split(",")[3]

    def is_busy(self):
        """
        The *OPC? command is used to query whether the current operation is
        finished. The *OPC command is used to set the Operation Complete bit
        (bit 0) in the standard event status register to 1 after the current
        operation is finished.
        """
        return not bool(int(self.instr.query("*OPC?")))

    def reset(self):
        """
        Restore the instrument to the default state.
        """
        self.instr.write("*RST")

    def get_service_request_enable(self):
        """
        Query the enable register for the status byte register set.
        """
        return int(self.instr.query("*SRE?"))

    def set_service_request_enable(self, data=0):
        """
        Set the enable register for the status byte register set.
        """
        assert data >= 0 and data <= 255
        self.instr.write("*SRE {0}".format(data))

    def get_status_byte(self):
        """
        Query the event register for the status byte register. The
        value of the status byte register is set to 0 after this
        command is executed.
        """
        return int(self.instr.query("*STB?"))

    def self_test_is_passing(self):
        """
        Perform a self-test and then returns the self-test results.
        """
        return not bool(int(self.instr.query("*TST?")))

    def wait(self):
        """
        Wait for the operation to finish.
        """
        self.instr.write("*WAI")

    def initialize_trigger(self):
        """
        Initialize the trigger system.
        """
        self.instr.write(":INIT")

    def get_coupling_channels(self):
        """
        Query the current trigger coupling channels.
        """
        return self.instr.query(":INST:COUP?")

    def set_coupling_channels(self, channels):
        """
        Select the trigger coupling channels.
        """
        self.instr.write(":INST:COUP {0}".format(channels))

    def get_selected_channel(self):
        """
        Query the channel currently selected.
        """
        return int(self.instr.query(":INST:NSEL?"))

    def select_channel(self, channel):
        """
        Select the current channel.
        """
        self.instr.write(":INST:NSEL {0}".format(channel))

    def install_option(self, license):
        """
        Install the options.
        """
        self.instr.write(":LIC:SET {0}".format(license))

    def measure(self, channel):
        """
        Query the voltage, current and power measured on the output terminal of
        the specified channel.
        """
        channel = self._interpret_channel(channel)
        response = self.instr.query(":MEAS:ALL? {0}".format(channel))
        data = dict(
            zip(
                ["voltage", "current", "power"],
                [Decimal(value) for value in response.split(",")],
            )
        )
        return data

    def measure_current(self, channel):
        """
        Query the current measured on the output terminal of the specified
        channel.
        """
        channel = self._interpret_channel(channel)
        return Decimal(self.instr.query(":MEAS:CURR? {0}".format(channel)))

    def measure_power(self, channel):
        """
        Query the power measured on the output terminal of the specified
        channel.
        """
        channel = self._interpret_channel(channel)
        return Decimal(self.instr.query(":MEAS:POWE? {0}".format(channel)))

    def measure_voltage(self, channel):
        """
        Query the voltage measured on the output terminal of the specified
        channel.
        """
        channel = self._interpret_channel(channel)
        response = self.instr.query(":MEAS? {0}".format(channel)).strip()
        # The :MEAS? command should return just voltage, but sometimes returns verbose format
        # Handle both formats: simple "1.234" and verbose "+1.234000E+00A,+3.299153E+00V,+3.369820E+05s"
        if ',' in response:
            # Verbose format: extract voltage value (second field ending with 'V')
            parts = response.split(',')
            for part in parts:
                if part.strip().endswith('V'):
                    # Remove 'V' suffix and convert
                    voltage_str = part.strip()[:-1]
                    return Decimal(voltage_str)
        # Simple format or fallback
        return Decimal(response)

    def get_current_monitor_condition(self):
        """
        Query the current monitor condition of the monitor (the current
        channel).
        """
        return self.instr.query(":MONI:CURR:COND?")

    def set_current_monitor_condition(self, condition="NONE", logic="NONE"):
        """
        Set the current monitor condition of the monitor (the current channel).
        """
        self.instr.write(":MONI:CURR:COND {0},{1}".format(condition, logic))

    def get_power_monitor_condition(self):
        """
        Query the power monitor condition of the monitor (the current channel).
        """
        return self.instr.query(":MONI:POWER:COND?")

    def set_power_monitor_condition(self, condition="NONE", logic="NONE"):
        """
        Set the power monitor condition of the monitor (the current channel).
        """
        self.instr.write(":MONI:POWER:COND {0},{1}".format(condition, logic))

    def enable_monitor(self):
        """
        Enable the monitor (the current channel).
        """
        self.instr.write(":MONI ON")

    def disable_monitor(self):
        """
        Disable the monitor (the current channel).
        """
        self.instr.write(":MONI OFF")

    def monitor_is_enabled(self):
        """
        Query the state of the monitor (the current channel)
        """
        return self.instr.query(":MONI?") == "ON"

    def get_monitor_stop_mode(self):
        """
        Query the stop mode of the monitor (the current channel).
        """
        return self.instr.query(":MONI:STOP?")

    def enable_monitor_outoff(self):
        """
        Enable the "OutpOff" mode of the monitor (the current channel).
        """
        self.instr.write(":MONI:STOP OUTOFF,ON")

    def disable_monitor_outoff(self):
        """
        Disable the "OutpOff" mode of the monitor (the current channel).
        """
        self.instr.write(":MONI:STOP OUTOFF,OFF")

    def enable_monitor_warning(self):
        """
        Enable the "Warning" mode of the monitor (the current channel).
        """
        self.instr.write(":MONI:STOP WARN,ON")

    def disable_monitor_warning(self):
        """
        Disable the "Warning" mode of the monitor (the current channel).
        """
        self.instr.write(":MONI:STOP WARN,OFF")

    def enable_monitor_beeper(self):
        """
        Enable the "Beeper" mode of the monitor (the current channel).
        """
        self.instr.write(":MONI:STOP BEEPER,ON")

    def disable_monitor_beeper(self):
        """
        Disable the "Beeper" mode of the monitor (the current channel).
        """
        self.instr.write(":MONI:STOP BEEPER,OFF")

    def get_voltage_monitor_condition(self):
        """
        Query the voltage monitor condition of the monitor (the current
        channel).
        """
        return self.instr.query(":MONI:VOLT:COND?")

    def set_voltage_monitor_condition(self, condition="NONE", logic="NONE"):
        """
        Set the voltage monitor condition of the monitor (the current channel).
        """
        self.instr.write(":MONI:VOLT:COND {0},{1}".format(condition, logic))

    def get_output_mode(self, channel=None):
        """
        Query the current output mode of the specified channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            return self.instr.query(":OUTP:MODE? {0}".format(channel)).strip()
        else:
            return self.instr.query(":OUTP:MODE?").strip()

    def overcurrent_protection_is_tripped(self, channel=None):
        """
        Query whether OCP occurred on the specified channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            return self.instr.query(":OUTP:OCP:QUES? {0}".format(channel)) == "YES"
        else:
            return self.instr.query(":OUTP:OCP:QUES?").strip()

    def clear_overcurrent_protection_trip(self, channel=None):
        """
        Clear the label of the overcurrent protection occurred on the specified
        channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            self.instr.write(":OUTP:OCP:CLEAR {0}".format(channel))
        else:
            self.instr.write(":OUTP:OCP:CLEAR")


    def enable_overcurrent_protection(self, channel=None):
        """
        Enable the overcurrent protection (OCP) function of the specified
        channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            self.instr.write(":OUTP:OCP {0},ON".format(channel))
        else:
            self.instr.write(":OUTP:OCP ON")

    def disable_overcurrent_protection(self, channel=None):
        """
        Disable the overcurrent protection (OCP) function of the specified
        channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            self.instr.write(":OUTP:OCP {0},OFF".format(channel))
        else:
            self.instr.write(":OUTP:OCP OFF")

    def overcurrent_protection_is_enabled(self, channel=None):
        """
        Query the status of the overcurrent protection (OCP) function of the
        specified channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            return self.instr.query(":OUTP:OCP? {0}".format(channel)) == "ON"
        else:
            return self.instr.query(":OUTP:OCP?") == "ON"

    def get_overcurrent_protection_value(self, channel=None):
        """
        Query the overcurrent protection value of the specified channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            return Decimal(self.instr.query(":OUTP:OCP:VAL? {0}".format(channel)))
        else:
            return Decimal(self.instr.query(":OUTP:OCP:VAL?"))

    def set_overcurrent_protection_value(self, value, channel=None):
        """
        Set the overcurrent protection value of the specified channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            self.instr.write(":OUTP:OCP:VAL {0},{1}".format(channel, value))
        else:
            self.instr.write(":OUTP:OCP:VAL {0}".format(value))

    def overvoltage_protection_is_tripped(self, channel=None):
        """
        Query whether OVP occurred on the specified channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            return self.instr.query(":OUTP:OVP:QUES? {0}".format(channel)) == "YES"
        else:
            return self.instr.query(":OUTP:OVP:QUES?")

    def clear_overvoltage_protection_trip(self, channel=None):
        """
        Clear the label of the overvoltage protection occurred on the specified
        channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            self.instr.write(":OUTP:OVP:CLEAR {0}".format(channel))
        else:
            self.instr.write(":OUTP:OVP:CLEAR")


    def enable_overvoltage_protection(self, channel=None):
        """
        Enable the overvoltage protection (OVP) function of the specified
        channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            self.instr.write(":OUTP:OVP {0},ON".format(channel))
        else:
            self.instr.write(":OUTP:OVP ON")

    def disable_overvoltage_protection(self, channel=None):
        """
        Disable the overvoltage protection (OVP) function of the specified
        channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            self.instr.write(":OUTP:OVP {0},OFF".format(channel))
        else:
            self.instr.write(":OUTP:OVP OFF")

    def overvoltage_protection_is_enabled(self, channel=None):
        """
        Query the status of the overvoltage protection (OVP) function of the
        specified channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            return self.instr.query(":OUTP:OVP? {0}".format(channel)) == "ON"
        else:
            return self.instr.query(":OUTP:OVP?") == "ON"

    def get_overvoltage_protection_value(self, channel=None):
        """
        Query the overvoltage protection value of the specified channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            return Decimal(self.instr.query(":OUTP:OVP:VAL? {0}".format(channel)))
        else:
            return Decimal(self.instr.query(":OUTP:OVP:VAL?"))

    def set_overvoltage_protection_value(self, value, channel=None):
        """
        Set the overvoltage protection value of the specified channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            self.instr.write(":OUTP:OVP:VAL {0},{1}".format(channel, value))
        else:
            self.instr.write(":OUTP:OVP:VAL {0}".format(value))

    def get_output_range(self):
        """
        Query the range currently selected of the channel.
        """
        return self.instr.query(":OUTP:RANG?")

    def set_output_range(self, range="P20V"):
        """
        Select the current range of the channel.
        """
        assert range in ["P20V", "P40V", "LOW", "HIGH"]
        self.instr.write(":OUTP:RANG {0}".format(range))

    def enable_sense(self, channel=None):
        """
        Enable the Sense function of the channel.
        """
        if not os.getenv('LAGER_ENABLE_SENSE'):
            return
        if channel is not None:
            channel = self._interpret_channel(channel)
            self.instr.write(":OUTP:SENS {0},ON".format(channel))
        else:
            self.instr.write(":OUTP:SENS ON")

    def disable_sense(self, channel=None):
        """
        Disable the Sense function of the channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            self.instr.write(":OUTP:SENS {0},OFF".format(channel))
        else:
            self.instr.write(":OUTP:SENS OFF")

    def sense_is_enabled(self, channel=None):
        """
        Query the status of the Sense function of the channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            return self.instr.query(":OUTP:SENS? {0}".format(channel)) == "ON"
        else:
            return self.instr.query(":OUTP:SENS?") == "ON"

    def enable_output(self, channel=None):
        """
        Enable the output of the specified channel.
        """
        if channel is not None:
            channel_str = self._interpret_channel(channel)
            # Before enabling, check if OVP/OCP would trip immediately
            # and clear any existing trip conditions
            try:
                if self.overvoltage_protection_is_tripped(channel):
                    self.clear_overvoltage_protection_trip(channel)
                if self.overcurrent_protection_is_tripped(channel):
                    self.clear_overcurrent_protection_trip(channel)
            except Exception:
                pass
            self.instr.write(":OUTP {0},ON".format(channel_str))
        else:
            # Clear any protection trips before enabling
            try:
                if self.overvoltage_protection_is_tripped():
                    self.clear_overvoltage_protection_trip()
                if self.overcurrent_protection_is_tripped():
                    self.clear_overcurrent_protection_trip()
            except Exception:
                pass
            self.instr.write(":OUTP ON")
        # Allow time for state to propagate
        try:
            import time
            time.sleep(0.2)  # Increased from 0.1 for more reliable state transition
        except Exception:
            pass

    def disable_output(self, channel=None):
        """
        Disable the output of the specified channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            self.instr.write(":OUTP {0},OFF".format(channel))
        else:
            self.instr.write(":OUTP OFF")
        # Allow time for state to propagate
        try:
            import time
            time.sleep(0.2)  # Increased from 0.1 for more reliable state transition
        except Exception:
            pass

    def output_is_enabled(self, channel=None):
        """
        Query the status of the specified channel.
        """
        import time
        try:
            # Interpret channel once before retry loop to avoid re-interpretation
            if channel is not None:
                channel_str = self._interpret_channel(channel)
            else:
                channel_str = None

            # Add settling time before query for more reliable state reading
            time.sleep(0.25)  # Increased from 0.15 for better reliability

            # Try multiple queries with slight delays for robustness
            for attempt in range(3):
                if channel_str is not None:
                    val = self.instr.query(":OUTP? {0}".format(channel_str)).strip()
                else:
                    val = self.instr.query(":OUTP?").strip()

                if val == "ON":
                    return True
                elif val == "OFF":
                    return False
                # If we get an unclear response, wait and retry
                if attempt < 2:
                    time.sleep(0.1)  # Increased from 0.05 for better stability
            # Default to False if all attempts are ambiguous
            return False
        except Exception:
            return False

    def num_channels(self):
        # Prefer product field; e.g., "DP821", "DP832A" etc.
        prod = self.get_product()
        m = re.search(r"DP8(\d)", prod)  # captures 1/2/3 after DP8
        if m:
            return int(m.group(1))

        # Fallback: try full *IDN? string
        idn = self.get_identification()
        m2 = re.search(r"DP8(\d)", idn)
        if m2:
            return int(m2.group(1))

        # If all else fails, assume single-channel
        return 1



    def enable_tracking(self, channel=None):
        """
        Enable the track function of the specified channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            self.instr.write(":OUTP:TRAC {0},ON".format(channel))
        else:
            self.instr.write(":OUTP:TRAC ON")

    def disable_tracking(self, channel=None):
        """
        Disable the track function of the specified channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            self.instr.write(":OUTP:TRAC {0},OFF".format(channel))
        else:
            self.instr.write(":OUTP:TRAC OFF")

    def tracking_is_enabled(self, channel=None):
        """
        Query the status of the track function of the specified channel.
        """
        if channel is not None:
            channel = self._interpret_channel(channel)
            return self.instr.query(":OUTP:TRAC? {0}".format(channel)) == "ON"
        else:
            return self.instr.query(":OUTP:TRAC?") == "ON"

    def get_record_destination(self):
        """
        Query the storage directory of the record file.
        """
        return self.instr.query(":REC:DEST?")

    def set_record_destination(self, file_name="RIGOL.ROF", location=10):
        """
        Store the record file to the specified storage location in the internal
        memory with the specified filename.
        """
        assert file_name.endswith(".ROF")
        assert location >= 1 and location <= 10
        self.instr.write(":REC:MEM {0},{1}".format(location, file_name))

    def set_record_destination_external(self, file_path):
        """
        Store the record file to the specified storage directory in the
        external memory.
        """
        assert file_path.startswith("D:\\") and file_path.endswith(".ROF")
        self.instr.write(":REC:MMEM {0}".format(file_path))

    def get_record_period(self):
        """
        Query the current record period of the recorder.
        """
        return int(self.instr.query(":REC:PERI?"))

    def set_record_period(self, period=1):
        """
        Query the current record period of the recorder.
        """
        self.instr.write(":REC:PERI {0}".format(period))

    def enable_record(self):
        """
        Enable the recorder.
        """
        self.instr.write(":REC ON")

    def disable_record(self):
        """
        Disable the recorder.
        """
        self.instr.write(":REC OFF")

    def record_is_enabled(self):
        """
        Query the status of the recorder.
        """
        return self.instr.query(":REC?") == "ON"

    def get_channel_current(self, source=None):
        """
        Query the current of the specified channel.
        """
        if source is not None:
            source = self._interpret_source(source)
            return Decimal(self.instr.query(":{0}:CURR?".format(source)))
        else:
            return Decimal(self.instr.query(":CURR?"))

    def set_channel_current(self, value, source=None):
        """
        Set the current of the specified channel.
        """
        if source is not None:
            source = self._interpret_source(source)
            self.instr.write(":{0}:CURR {1}".format(source, value))
            # Read back and inform if quantized
            try:
                actual = float(self.instr.query(":{0}:CURR?".format(source)))
                if abs(float(value) - actual) > 1e-6:
                    print(f"NOTE: requested {value} A; instrument accepted {actual} A")
            except Exception:
                pass
        else:
            self.instr.write(":CURR {0}".format(value))
            # Read back and inform if quantized
            try:
                actual = float(self.instr.query(":CURR?"))
                if abs(float(value) - actual) > 1e-6:
                    print(f"NOTE: requested {value} A; instrument accepted {actual} A")
            except Exception:
                pass

    def get_channel_voltage(self, source=None):
        """
        Query the voltage of the specified channel.
        """
        if source is not None:
            source = self._interpret_source(source)
            return Decimal(self.instr.query(":{0}:VOLT?".format(source)))
        else:
            return Decimal(self.instr.query(":VOLT?"))

    def set_channel_voltage(self, value, source=None):
        """
        Set the voltage of the specified channel.
        """
        if source is not None:
            source = self._interpret_source(source)
            self.instr.write(":{0}:VOLT {1}".format(source, value))
            # Read back and inform if quantized
            try:
                actual = float(self.instr.query(":{0}:VOLT?".format(source)))
                if abs(float(value) - actual) > 1e-6:
                    print(f"NOTE: requested {value} V; instrument accepted {actual} V")
            except Exception:
                pass
        else:
            self.instr.write(":VOLT {0}".format(value))
            # Read back and inform if quantized
            try:
                actual = float(self.instr.query(":VOLT?"))
                if abs(float(value) - actual) > 1e-6:
                    print(f"NOTE: requested {value} V; instrument accepted {actual} V")
            except Exception:
                pass

    def get_channel_current_increment(self, source=None):
        """
        Query the step of the current change of the specified channel.
        """
        if source is not None:
            source = self._interpret_source(source)
            return Decimal(self.instr.query(":{0}:CURR:STEP?".format(source))[:-1])
        else:
            return Decimal(self.instr.query(":CURR:STEP?")[:-1])

    def set_channel_current_increment(self, value, source=None):
        """
        Set the step of the current change of the specified channel.
        """
        if source is not None:
            source = self._interpret_source(source)
            self.instr.write(":{0}:CURR:STEP {1}".format(source, value))
        else:
            self.instr.write(":CURR:STEP {0}".format(value))

    def get_channel_current_trigger(self, source=None):
        """
        Query the trigger current of the specified channel.
        """
        if source is not None:
            source = self._interpret_source(source)
            return Decimal(self.instr.query(":{0}:CURR:TRIG?".format(source))[:-1])
        else:
            return Decimal(self.instr.query(":CURR:TRIG?")[:-1])

    def set_channel_current_trigger(self, value, source=None):
        """
        Set the trigger current of the specified channel.
        """
        if source is not None:
            source = self._interpret_source(source)
            self.instr.write(":{0}:CURR:TRIG {1}".format(source, value))
        else:
            self.instr.write(":CURR:TRIG {0}".format(value))

    def beep(self):
        """
        Send this command and the beeper immediately sounds.
        """
        self.instr.write(":SYST:BEEP:IMM")

    def enable_beeper(self):
        """
        Enable the beeper.
        """
        self.instr.write(":SYST:BEEP ON")

    def disable_beeper(self):
        """
        Disable the beeper.
        """
        self.instr.write(":SYST:BEEP OFF")

    def beeper_is_enabled(self):
        """
        Query the status of the beeper.
        """
        return self.instr.query(":SYST:BEEP?") == "ON"

    def get_brightness(self):
        """
        Query the brightness of the screen.
        """
        return int(self.instr.query(":SYST:BRIG?"))

    def set_brightness(self, brightness=50):
        """
        Set the brightness of the screen.
        """
        self.instr.write(":SYST:BRIG {0}".format(brightness))

    def get_gpib_address(self):
        """
        Query the current GPIB address.
        """
        return int(self.instr.query(":SYST:COMM:GPIB:ADDR?"))

    def set_gpib_address(self, address=2):
        """
        Set the current GPIB address.
        """
        self.instr.write(":SYST:COMM:GPIB:ADDR {0}".format(address))

    def apply_lan_settings(self):
        """
        Apply the network parameters currently set.
        """
        self.instr.write(":SYST:COMM:LAN:APPL")

    def enable_auto_ip(self):
        """
        Enable the auto IP configuration mode.
        """
        self.instr.write(":SYST:COMM:LAN:AUTO ON")

    def disable_auto_ip(self):
        """
        Disable the auto IP configuration mode.
        """
        self.instr.write(":SYST:COMM:LAN:AUTO OFF")

    def auto_ip_is_enabled(self):
        """
        Query the status of the auto IP configuration mode.
        """
        return self.instr.query(":SYST:COMM:LAN:AUTO?") == "ON"

    def enable_dhcp(self):
        """
        Enable the DHCP configuration mode.
        """
        self.instr.write(":SYST:COMM:LAN:DHCP ON")

    def disable_dhcp(self):
        """
        Disable the DHCP configuration mode.
        """
        self.instr.write(":SYST:COMM:LAN:DHCP OFF")

    def dhcp_is_enabled(self):
        """
        Query the status of the DHCP configuration mode.
        """
        return self.instr.query(":SYST:COMM:LAN:DHCP?") == "ON"

    def get_dns(self):
        """
        Query the current DNS address.
        """
        return self.instr.query(":SYST:COMM:LAN:DNS?")

    def set_dns(self, address):
        """
        Set the current DNS address.
        """
        self.instr.write(":SYST:COMM:LAN:DNS {0}".format(address))

    def get_gateway(self):
        """
        Query the current default gateway.
        """
        return self.instr.query(":SYST:COMM:LAN:GATE?")

    def set_gateway(self, gateway):
        """
        Set the current default gateway.
        """
        self.instr.write(":SYST:COMM:LAN:GATE {0}".format(gateway))

    def get_ip_address(self):
        """
        Query the current IP address.
        """
        return self.instr.query(":SYST:COMM:LAN:IPAD?")

    def set_ip_address(self, address):
        """
        Set the IP address.
        """
        self.instr.write(":SYST:COMM:LAN:IPAD {0}".format(address))

    def get_mac_address(self):
        """
        Query the MAC address.
        """
        return self.instr.query(":SYST:COMM:LAN:MAC?")

    def enable_manual_ip(self):
        """
        Enable the manual IP configuration mode.
        """
        self.instr.write(":SYST:COMM:LAN:MAN ON")

    def disable_manual_ip(self):
        """
        Disable the manual IP configuration mode.
        """
        self.instr.write(":SYST:COMM:LAN:MAN OFF")

    def manual_ip_is_enabled(self):
        """
        Query the status of the manual IP configuration mode.
        """
        return self.instr.query(":SYST:COMM:LAN:MAN?") == "ON"

    def get_subnet_mask(self):
        """
        Query the current subnet mask.
        """
        return self.instr.query(":SYST:COMM:LAN:SMASK?")

    def set_subnet_mask(self, mask):
        """
        Set the subnet mask.
        """
        self.instr.write(":SYST:COMM:LAN:SMASK {0}".format(mask))

    def get_baud(self):
        """
        Query the baud rate of the RS232 interface.
        """
        return int(self.instr.query(":SYST:COMM:RS232:BAUD?"))

    def set_baud(self, rate):
        """
        Set the baud rate of the RS232 interface and the unit is Baud.
        """
        assert rate in [4800, 7200, 9600, 14400, 19200, 38400, 57600, 115200, 128000]
        self.instr.write(":SYST:COMM:RS232:BAUD {0}".format(rate))

    def get_data_bit(self):
        """
        Query the data bit of the RS232 interface.
        """
        return int(self.instr.query(":SYST:COMM:RS232:DATAB?"))

    def set_data_bit(self, data=8):
        """
        Set the data bit of the RS232 interface.
        """
        assert data in [5, 6, 7, 8]
        self.instr.write(":SYST:COMM:RS232:DATAB {0}".format(data))

    def enable_hardware_flow_control(self):
        """
        Enable the hardware flow control.
        """
        self.instr.write(":SYST:COMM:RS232:FLOWC ON")

    def disable_hardware_flow_control(self):
        """
        Disable the hardware flow control.
        """
        self.instr.write(":SYST:COMM:RS232:FLOWC OFF")

    def hardware_flow_control_is_enabled(self):
        """
        Query the status of the hardware flow control.
        """
        return self.instr.query(":SYST:COMM:RS232:FLOWC?") == "ON"

    def get_parity_mode(self):
        """
        Query the current parity mode.
        """
        return self.instr.query(":SYST:COMM:RS232:PARI?")

    def set_parity_mode(self, mode="NONE"):
        """
        Set the parity mode.
        """
        assert mode in ["NONE", "ODD", "EVEN"]
        self.instr.write(":SYST:COMM:RS232:PARI {0}".format(mode))

    def get_stop_bit(self):
        """
        Query the current stop bit.
        """
        return int(self.instr.query(":SYST:COMM:RS232:STOPB?"))

    def set_stop_bit(self, data=1):
        """
        Set the stop bit.
        """
        assert data in [1, 2]
        self.instr.write(":SYST:COMM:RS232:STOPB {0}".format(data))

    def get_contrast(self):
        """
        Query the contrast of the screen.
        """
        return int(self.instr.query(":SYST:CONT?"))

    def set_contrast(self, contrast=25):
        """
        Set the contrast of the screen.
        """
        assert contrast >= 1 and contrast <= 100
        self.instr.write(":SYST:CONT {0}".format(contrast))

    def get_error(self):
        """
        Query and clear the error messages in the error queue.
        """
        return self.instr.query(":SYST:ERR?")

    def enable_remote_lock(self):
        """
        Enable the remote lock.
        """
        self.instr.write(":SYST:KLOC:STAT ON")

    def disable_remote_lock(self):
        """
        Disable the remote lock.
        """
        self.instr.write(":SYST:KLOC:STAT OFF")

    def remote_lock_is_enabled(self):
        """
        Query the status of the remote lock.
        """
        return self.instr.query(":SYST:KLOC:STAT?") == "ON"

    def get_language(self):
        """
        Query the current system language type.
        """
        return self.instr.query(":SYST:LANG:TYPE?")

    def set_language(self, language="EN"):
        """
        Set the system language.
        """
        assert language in ["EN", "CH", "JAP", "KOR", "GER", "POR", "POL", "CHT", "RUS"]
        self.instr.write(":SYST:LANG:TYPE {0}".format(language))

    def lock_keyboard(self):
        """
        Lock the front panel.
        """
        self.instr.write(":SYST:LOCK ON")

    def unlock_keyboard(self):
        """
        Unlock the front panel.
        """
        self.instr.write(":SYST:LOCK OFF")

    def keyboard_is_locked(self):
        """
        Query whether the front panel is locked.
        """
        return self.instr.query(":SYST:LOCK?") == "ON"

    def enable_sync(self):
        """
        Turn on the on/off sync function.
        """
        self.instr.write(":SYST:ONOFFS ON")

    def disable_sync(self):
        """
        Turn off the on/off sync function.
        """
        self.instr.write(":SYST:ONOFFS OFF")

    def sync_is_enabled(self):
        """
        Query whether the on/off sync function is turned on.
        """
        return self.instr.query(":SYST:ONOFFS?") == "ON"

    def enable_overtemperature_protection(self):
        """
        Enable the over-temperature protection (OTP) function.
        """
        self.instr.write(":SYST:OTP ON")

    def disable_overtemperature_protection(self):
        """
        Disable the over-temperature protection (OTP) function.
        """
        self.instr.write(":SYST:OTP OFF")

    def overtemperature_protection_is_enabled(self):
        """
        Query the status of the over-temperature protection function.
        """
        return self.instr.query(":SYST:OTP?") == "ON"

    def enable_recall(self):
        """
        The instrument uses the system configuration (including all the system
        parameters and states except the channel output on/off states) before
        the last power-off at power-on.
        """
        self.instr.write(":SYST:POWE LAST")

    def disable_recall(self):
        """
        The instrument uses the factory default values at power-on (except
        those parameters that will not be affected by reset.
        """
        self.instr.write(":SYST:POWE DEF")

    def recall_is_enabled(self):
        """
        Query the status of the power-on mode.
        """
        return self.instr.query(":SYST:POWE?") == "LAST"

    def get_luminosity(self):
        """
        Query the RGB brightness of the screen.
        """
        return int(self.instr.query(":SYST:RGBB?"))

    def set_luminosity(self, luminosity=50):
        """
        Set the RGB brightness of the screen.
        """
        assert luminosity >= 1 and luminosity <= 100
        self.instr.write(":SYST:RGBB {0}".format(luminosity))

    def enable_screen_saver(self):
        """
        Enable the screen saver function.
        """
        self.instr.write(":SYST:SAV ON")

    def disable_screen_saver(self):
        """
        Disable the screen saver function.
        """
        self.instr.write(":SYST:SAV OFF")

    def screen_saver_is_enabled(self):
        """
        Query the status of the screen saver function.
        """
        return self.instr.query(":SYST:SAV?") == "ON"

    def top_board_is_passing(self):
        """
        Query the self-test results of TopBoard.
        """
        return self.instr.query(":SYST:SELF:TEST:BOARD?").split(",")[0] == "PASS"

    def bottom_board_is_passing(self):
        """
        Query the self-test results of BottomBoard.
        """
        return self.instr.query(":SYST:SELF:TEST:BOARD?").split(",")[1] == "PASS"

    def fan_is_passing(self):
        """
        Query the self-test results of the fan.
        """
        return self.instr.query(":SYST:SELF:TEST:FAN?") == "PASS"

    def get_temperature(self):
        """
        Query the self-test result of the temperature.
        """
        return Decimal(self.instr.query(":SYST:SELF:TEST:TEMP?"))

    def get_track_mode(self):
        """
        Query the current track mode.
        """
        return self.instr.query(":SYST:TRACKM?")

    def set_track_mode(self, mode="SYNC"):
        """
        Set the track mode.
        """
        assert mode in ["SYNC", "INDE"]
        self.instr.write(":SYST:TRACKM {0}".format(mode))

    def get_system_version(self):
        """
        Query the SCPI version number of the system
        """
        return self.instr.query(":SYST:VERS?")

    def get_timer_cycles(self):
        """
        Query the current number of cycles of the timer.
        """
        response = self.instr.query(":TIME:CYCLE?")
        if response.startswith("N,"):
            return int(response[2:])
        else:
            return response

    def set_timer_cycles(self, cycles="I"):
        """
        Set the number of cycles of the timer.
        """
        if cycles == "I":
            self.instr.write(":TIME:CYCLE {0}".format(cycles))
        else:
            assert cycles >= 1 and cycles <= 99999
            self.instr.write(":TIME:CYCLE N,{0}".format(cycles))

    def get_timer_end_state(self):
        """
        Query the current end state of the timer.
        """
        return self.instr.query(":TIME:ENDS?")

    def set_timer_end_state(self, state="OFF"):
        """
        Set the end state of the timer.
        """
        assert state in ["OFF", "LAST"]
        self.instr.write(":TIME:ENDS {0}".format(state))

    def get_timer_groups(self):
        """
        Query the current number of output groups of the timer.
        """
        return int(self.instr.query(":TIME:GROUP?"))

    def set_timer_groups(self, num_groups=1):
        """
        Set the number of output groups of the timer.
        """
        assert num_groups >= 1 and num_groups <= 2048
        self.instr.write(":TIME:GROUP {0}".format(num_groups))

    def get_timer_parameters(self, group=None, num_groups=1):
        """
        Query the timer parameters of the specified groups.
        """
        assert group >= 0 and group <= 2047
        assert num_groups >= 1 and num_groups <= 2048
        return self.instr.query(":TIME:PARA? {0},{1}".format(group, num_groups))

    def set_timer_parameters(self, group, voltage, current=1, delay=1):
        """
        Set the timer parameters of the specified group.
        """
        assert group >= 0 and group <= 2047
        assert delay >= 1 and delay <= 99999
        self.instr.write(":TIME:PARA {0},{1},{2},{3}".format(group, voltage, current, delay))

    def enable_timer(self):
        """
        Enable the timing output function.
        """
        self.instr.write(":TIME ON")

    def disable_timer(self):
        """
        Disable the timing output function.
        """
        self.instr.write(":TIME OFF")

    def timer_is_enabled(self):
        """
        Query the status of the timing output function.
        """
        return self.instr.query(":TIME?") == "ON"

    def reconstruct_timer(self):
        """
        Send this command and the instrument will create the timer parameters
        according to the templet currently selected and the parameters set.
        """
        self.instr.write(":TIME:TEMP:CONST")

    def get_timer_exp_fall_rate(self):
        """
        Query the fall index of ExpFall.
        """
        return int(self.instr.query(":TIME:TEMP:FALLR?"))

    def set_timer_exp_fall_rate(self, rate=0):
        """
        Set the fall index of ExpFall.
        """
        assert rate >= 0 and rate <= 10
        self.instr.write(":TIME:TEMP:FALLR {0}".format(rate))

    def get_timer_interval(self):
        """
        Query the current time interval.
        """
        return int(self.instr.query(":TIME:TEMP:INTE?"))

    def set_timer_interval(self, interval=1):
        """
        Set the time interval.
        """
        assert interval >= 1 and interval <= 99999
        self.instr.write(":TIME:TEMP:INTE {0}".format(interval))

    def enable_timer_invert(self):
        """
        Enable the invert function of the templet currently selected.
        """
        self.instr.write(":TIME:TEMP:INVE ON")

    def disable_timer_invert(self):
        """
        Disable the invert function of the templet currently selected.
        """
        self.instr.write(":TIME:TEMP:INVE OFF")

    def timer_is_inverted(self):
        """
        Query whether the invert function of the templet currently selected is
        enabled.
        """
        return self.instr.query(":TIME:TEMP:INVE?") == "ON"

    def get_timer_max_value(self):
        """
        Query the maximum voltage or current of the templet currently selected.
        """
        return Decimal(self.instr.query(":TIME:TEMP:MAXV?"))

    def set_timer_max_value(self, value):
        """
        Set the maximum voltage or current of the templet currently selected.
        """
        self.instr.write(":TIME:TEMP:MAXV {0}".format(value))

    def get_timer_min_value(self):
        """
        Query the minimum voltage or current of the templet currently selected.
        """
        return Decimal(self.instr.query(":TIME:TEMP:MINV?"))

    def set_timer_min_value(self, value=0):
        """
        Set the minimum voltage or current of the templet currently selected.
        """
        self.instr.write(":TIME:TEMP:MINV {0}".format(value))

    def get_timer_unit(self):
        """
        Query the editing object of the templet currently selected as well as
        the corresponding current or voltage.
        """
        return self.instr.query(":TIME:TEMP:OBJ?")

    def set_timer_unit(self, unit="V", value=0):
        """
        Select the editing object of the templet and set the current or
        voltage.
        """
        assert unit in ["V", "C"]
        self.instr.write(":TIME:TEMP:OBJ {0},{1}".format(unit, value))

    def get_timer_pulse_period(self):
        """
        Query the period of Pulse.
        """
        return int(self.instr.query(":TIME:TEMP:PERI?"))

    def set_timer_pulse_period(self, value=10):
        """
        Set the period of Pulse.
        """
        assert value >= 2 and value <= 99999
        self.instr.write(":TIME:TEMP:PERI {0}".format(value))

    def get_timer_points(self):
        """
        Query the total number of points
        """
        return int(self.instr.query(":TIME:TEMP:POINT?"))

    def set_timer_points(self, value=10):
        """
        Set the total number of points.
        """
        assert value >= 10 and value <= 2048
        self.instr.write(":TIME:TEMP:POINT {0}".format(value))

    def get_timer_exp_rise_rate(self):
        """
        Query the rise index of ExpRise.
        """
        return int(self.instr.query(":TIME:TEMP:RISER?"))

    def set_timer_exp_rise_rate(self, rate=0):
        """
        Set the rise index of ExpRise.
        """
        assert rate >= 0 and rate <= 10
        self.instr.write(":TIME:TEMP:RISER {0}".format(rate))

    def get_timer_template(self):
        """
        Query the templet type currently selected
        """
        return self.instr.query(":TIME:TEMP:SEL?")

    def set_timer_template(self, mode="SINE"):
        """
        Select the desired templet type.
        """
        assert mode in ["SINE", "SQUARE", "RAMP", "UP", "DN", "UPDN", "RISE", "FALL"]
        self.instr.write(":TIME:TEMP:SEL {0}".format(mode))

    def get_timer_ramp_symmetry(self):
        """
        Query the symmetry of RAMP.
        """
        return int(self.instr.query(":TIME:TEMP:SYMM?"))

    def set_timer_ramp_symmetry(self, symmetry=50):
        """
        Set the symmetry of RAMP.
        """
        assert symmetry >= 0 and symmetry <= 100
        self.instr.write(":TIME:TEMP:SYMM {0}".format(symmetry))

    def get_timer_pulse_width(self):
        """
        Query the positive pulse width of Pulse.
        """
        return int(self.instr.query(":TIME:TEMP:WIDT?"))

    def set_timer_pulse_width(self, width=5):
        """
        Set the positive pulse width of Pulse.
        """
        assert width >= 1 and width <= 99998
        self.instr.write(":TIME:TEMP:WIDT {0}".format(width))

    def get_trigger_source_type(self):
        """
        Query the trigger source type currently selected.
        """
        return self.instr.query(":TRIG:IN:CHTY?")

    def set_trigger_source_type(self, mode="BUS"):
        """
        Select the trigger source type
        """
        assert mode in ["BUS", "IMM"]
        self.instr.write(":TRIG:IN:CHTY {0}".format(mode))

    def set_trigger_current(self, current=0.1, channel=1):
        """
        Set the trigger current of the specified channel.
        """
        channel = self._interpret_channel(channel)
        self.instr.write(":TRIG:IN:CURR {0},{1}".format(channel, current))

    def enable_trigger_input(self, data_line=None):
        """
        Enable the trigger input function of the specified data line.
        """
        if data_line is not None:
            self.instr.write(":TRIG:IN {0},ON".format(data_line))
        else:
            self.instr.write(":TRIG:IN ON")

    def disable_trigger_input(self, data_line=None):
        """
        Disable the trigger input function of the specified data line.
        """
        if data_line is not None:
            self.instr.write(":TRIG:IN {0},OFF".format(data_line))
        else:
            self.instr.write(":TRIG:IN OFF")

    def trigger_input_is_enabled(self, data_line="D0"):
        """
        Query the status of the trigger input function of the specified data line.
        """
        return self.instr.query(":TRIG:IN? {0}".format(data_line)) == "{0},ON".format(data_line)

    def trigger(self):
        """
        Initialize the trigger system.
        """
        self.instr.write(":TRIG:IN:IMME")

    def get_trigger_response(self, data_line=None):
        """
        Query the output response of the trigger input of the specified data line
        """
        if data_line is not None:
            return self.instr.query(":TRIG:IN:RESP? {0}".format(data_line))
        else:
            return self.instr.query(":TRIG:IN:RESP?")

    def set_trigger_response(self, mode="OFF", data_line=None):
        """
        Set the output response of the trigger input of the specified data line.
        """
        assert mode in ["ON", "OFF", "ALTER"]
        if data_line is not None:
            self.instr.write(":TRIG:IN:RESP {0},{1}".format(data_line, mode))
        else:
            self.instr.write(":TRIG:IN:RESP {0}".format(mode))

    def get_trigger_sensitivity(self, data_line=None):
        """
        Query the trigger sensitivity of the trigger input of the specified data line.
        """
        if data_line is not None:
            return self.instr.query(":TRIG:IN:SENS? {0}".format(data_line))
        else:
            return self.instr.query(":TRIG:IN:SENS?")

    def set_trigger_sensitivity(self, sensitivity="LOW", data_line=None):
        """
        Set the trigger sensitivity of the trigger input of the specified data line.
        """
        assert sensitivity in ["LOW", "MID", "HIGH"]
        if data_line is not None:
            self.instr.write(":TRIG:IN:SENS {0},{1}".format(data_line, sensitivity))
        else:
            self.instr.write(":TRIG:IN:SENS {0}".format(sensitivity))

    def get_trigger_input_source(self, data_line=None):
        """
        Query the source under control of the trigger input of the specified data line.
        """
        if data_line is not None:
            return self.instr.query(":TRIG:IN:SOUR? {0}".format(data_line))
        else:
            return self.instr.query(":TRIG:IN:SOUR?")

    def set_trigger_input_source(self, channel=1, data_line=None):
        """
        Set the source under control of the trigger input of the specified data line.
        """
        channel = self._interpret_channel(channel)
        if data_line is not None:
            self.instr.write(":TRIG:IN:SOUR {0},{1}".format(data_line, channel))
        else:
            self.instr.write(":TRIG:IN:SOUR {0}".format(channel))

    def get_trigger_type(self, data_line=None):
        """
        Query the trigger type of the trigger input of the specified data line.
        """
        if data_line is not None:
            return self.instr.query(":TRIG:IN:TYPE? {0}".format(data_line))
        else:
            return self.instr.query(":TRIG:IN:TYPE?")

    def set_trigger_type(self, mode="RISE", data_line=None):
        """
        Set the trigger type of the trigger input of the specified data line.
        """
        assert mode in ["RISE", "FALL", "HIGH", "LOW"]
        if data_line is not None:
            self.instr.write(":TRIG:IN:TYPE {0},{1}".format(data_line, mode))
        else:
            self.instr.write(":TRIG:IN:TYPE {0}".format(mode))

    def set_trigger_voltage(self, voltage=0, channel=1):
        """
        Set the trigger voltage of the specified channel.
        """
        channel = self._interpret_channel(channel)
        self.instr.write(":TRIG:IN:VOLT {0},{1}".format(channel, voltage))

    def get_trigger_condition(self, data_line=None):
        """
        Query the trigger condition of the trigger output of the specified data line.
        """
        if data_line is not None:
            return self.instr.query(":TRIG:OUT:COND? {0}".format(data_line))
        else:
            return self.instr.query(":TRIG:OUT:COND?")

    def set_trigger_condition(self, condition="OUTOFF", value=0, data_line=None):
        """
        Set the trigger condition of the trigger output of the specified data line.
        """
        assert condition in [
            "OUTOFF",
            "OUTON",
            ">V",
            "<V",
            "=V",
            ">C",
            "<C",
            "=C",
            ">P",
            "<P",
            "=P",
            "AUTO",
        ]
        if data_line is not None:
            self.instr.write(":TRIG:OUT:COND {0},{1},{2}".format(data_line, condition, value))
        else:
            self.instr.write(":TRIG:OUT:COND {0},{1}".format(condition, value))

    def get_trigger_duty_cycle(self, data_line=None):
        """
        Query the duty cycle of the square waveform of the trigger output on the
        specified data line.
        """
        if data_line is not None:
            return int(self.instr.query(":TRIG:OUT:DUTY? {0}".format(data_line)))
        else:
            return int(self.instr.query(":TRIG:OUT:DUTY?"))

    def set_trigger_duty_cycle(self, duty_cycle=50, data_line=None):
        """
        Set the duty cycle of the square waveform of the trigger output on the
        specified data line.
        """
        assert duty_cycle >= 10 and duty_cycle <= 90
        if data_line is not None:
            self.instr.write(":TRIG:OUT:DUTY {0},{1}".format(data_line, duty_cycle))
        else:
            self.instr.write(":TRIG:OUT:DUTY {0}".format(duty_cycle))

    def enable_trigger_output(self, data_line=None):
        """
        Enable the trigger output function of the specified data line.
        """
        if data_line is not None:
            self.instr.write(":TRIG:OUT {0},ON".format(data_line))
        else:
            self.instr.write(":TRIG:OUT ON")

    def disable_trigger_output(self, data_line=None):
        """
        Disable the trigger output function of the specified data line.
        """
        if data_line is not None:
            self.instr.write(":TRIG:OUT {0},OFF".format(data_line))
        else:
            self.instr.write(":TRIG:OUT OFF")

    def trigger_output_is_enabled(self, data_line="D0"):
        """
        Query the status of the trigger output function of the specified data line.
        """
        return self.instr.query(":TRIG:OUT? {0}".format(data_line)) == "{0},ON".format(data_line)

    def get_trigger_period(self, data_line=None):
        """
        Query the period of the square waveform of the trigger output on the
        specified data line.
        """
        if data_line is not None:
            return Decimal(self.instr.query(":TRIG:OUT:PERI? {0}".format(data_line)))
        else:
            return Decimal(self.instr.query(":TRIG:OUT:PERI?"))

    def set_trigger_period(self, period=1, data_line=None):
        """
        Set the period of the square waveform of the trigger output on the
        specified data line.
        """
        assert period >= 1e-4 and period <= 2.5
        if data_line is not None:
            self.instr.write(":TRIG:OUT:PERI {0},{1}".format(data_line, period))
        else:
            self.instr.write(":TRIG:OUT:PERI {0}".format(period))

    def get_trigger_polarity(self, data_line=None):
        """
        Query the polarity of the trigger output signal of the specified data
        line.
        """
        if data_line is not None:
            return self.instr.query(":TRIG:OUT:POLA? {0}".format(data_line))
        else:
            return self.instr.query(":TRIG:OUT:POLA?")

    def set_trigger_polarity(self, polarity="POSI", data_line=None):
        """
        Set the polarity of the trigger output signal of the specified data
        line.
        """
        assert polarity in ["POSI", "NEGA"]
        if data_line is not None:
            self.instr.write(":TRIG:OUT:POLA {0},{1}".format(data_line, polarity))
        else:
            self.instr.write(":TRIG:OUT:POLA {0}".format(polarity))

    def get_trigger_signal(self, data_line=None):
        """
        Query the type of the trigger output signal of the specified data line.
        """
        if data_line is not None:
            return self.instr.query(":TRIG:OUT:SIGN? {0}".format(data_line))
        else:
            return self.instr.query(":TRIG:OUT:SIGN?")

    def set_trigger_signal(self, signal="LEVEL", data_line=None):
        """
        Set the type of the trigger output signal of the specified data line.
        """
        assert signal in ["LEVEL", "SQUARE"]
        if data_line is not None:
            self.instr.write(":TRIG:OUT:SIGN {0},{1}".format(data_line, signal))
        else:
            self.instr.write(":TRIG:OUT:SIGN {0}".format(signal))

    def get_trigger_output_source(self, data_line=None):
        """
        Query the control source of the trigger output of the specified data
        line.
        """
        if data_line is not None:
            return self.instr.query(":TRIG:OUT:SOUR? {0}".format(data_line))
        else:
            return self.instr.query(":TRIG:OUT:SOUR?")

    def set_trigger_output_source(self, channel=1, data_line=None):
        """
        Set the control source of the trigger output of the specified data
        line.
        """
        channel = self._interpret_channel(channel)
        if data_line is not None:
            self.instr.write(":TRIG:OUT:SOUR {0},{1}".format(data_line, channel))
        else:
            self.instr.write(":TRIG:OUT:SOUR {0}".format(channel))

    def get_trigger_delay(self):
        """
        Query the current trigger delay.
        """
        return int(self.instr.query(":TRIG:DEL?"))

    def set_trigger_delay(self, delay=0):
        """
        Set the trigger delay.
        """
        assert delay >= 0 and delay <= 3600
        self.instr.write(":TRIG:DEL {0}".format(delay))

    def get_trigger_source(self):
        """
        Query the trigger source currently selected.
        """
        return self.instr.query(":TRIG:SOUR?")

    def set_trigger_source(self, source="BUS"):
        """
        Select the trigger source.
        """
        assert source in ["BUS", "IMM"]
        self.instr.write(":TRIG:SOUR {0}".format(source))

    def get_all_channels_state(self):
        output = {}
        for ch in range(1, self.num_channels() + 1):
            output[f"channel_{ch}"] = self.get_channel(ch)
        return output

    def close(self) -> None:
        """Close the VISA connection and release resources."""
        if hasattr(self, 'instr') and self.instr is not None:
            try:
                if hasattr(self.instr, 'instr') and hasattr(self.instr.instr, 'close'):
                    # InstrumentWrap pattern: self.instr is InstrumentWrap, self.instr.instr is VISA resource
                    self.instr.instr.close()
                elif hasattr(self.instr, 'close'):
                    # Direct VISA resource
                    self.instr.close()
            except Exception:
                pass
            finally:
                self.instr = None

    def __del__(self) -> None:
        """Cleanup when instance is garbage collected."""
        self.close()


def create_device(net_info):
    """Factory function for hardware_service.

    Extracts the required parameters from net_info dict and creates a RigolDP800 instance.
    This allows hardware_service to instantiate the device without knowing the constructor signature.
    """
    address = net_info.get('address')
    channel = net_info.get('channel') or net_info.get('pin') or 1
    instrument_hint = net_info.get('instrument')
    return RigolDP800(address=address, channel=int(channel), instrument_hint=instrument_hint)

