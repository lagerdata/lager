# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import re
import sys
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Input, Static, RichLog
from textual.containers import Container
from textual.reactive import reactive
from rich.text import Text
from .websocket_client import BatteryWebSocketClient


class BatteryState(Static):
    """Widget to display battery state information"""

    channel = reactive("1")
    terminal_voltage = reactive("--")
    current = reactive("--")
    esr = reactive("--")
    soc = reactive("--")
    voc = reactive("--")
    enabled = reactive("--")
    mode = reactive("--")
    model = reactive("--")
    capacity = reactive("--")
    current_limit = reactive("--")
    ocp_limit = reactive("--")
    ocp_tripped = reactive("--")
    ovp_limit = reactive("--")
    ovp_tripped = reactive("--")
    volt_full = reactive("--")
    volt_empty = reactive("--")

    def render(self) -> str:
        """Render the battery state display in battery simulator style"""
        # Color coding for status
        enabled_color = "black on green" if self.enabled == "ON" else "black on yellow"
        mode_color = "black on cyan" if self.mode in ("Static", "Dynamic") else "black on white"
        ocp_color = "red" if self.ocp_tripped == "YES" else "green"
        ovp_color = "red" if self.ovp_tripped == "YES" else "green"

        # Format values with proper precision
        def fmt_voltage(val):
            try:
                return f"{float(val):06.3f}"
            except (ValueError, TypeError):
                return "00.000"

        def fmt_current(val):
            try:
                return f"{float(val):06.3f}"
            except (ValueError, TypeError):
                return "00.000"

        def fmt_soc(val):
            try:
                return f"{float(val):5.1f}"
            except (ValueError, TypeError):
                return "  0.0"

        def fmt_esr(val):
            try:
                return f"{float(val):06.3f}"
            except (ValueError, TypeError):
                return "00.067"

        def fmt_capacity(val):
            try:
                return f"{float(val):.3g}"
            except (ValueError, TypeError):
                return "0"

        tv_display = fmt_voltage(self.terminal_voltage)
        curr_display = fmt_current(self.current)
        esr_display = fmt_esr(self.esr)
        soc_display = fmt_soc(self.soc)
        voc_display = fmt_voltage(self.voc)
        cap_display = fmt_capacity(self.capacity)
        curr_lim_display = fmt_current(self.current_limit)
        ocp_display = fmt_current(self.ocp_limit)
        ovp_display = fmt_voltage(self.ovp_limit)
        volt_full_display = fmt_voltage(self.volt_full)
        volt_empty_display = fmt_voltage(self.volt_empty)

        # Format model name (truncate if too long)
        model_name = str(self.model) if self.model != "--" else "Custom"
        if len(model_name) > 20:
            model_name = model_name[:17] + "..."

        return f"""
  [{enabled_color}] CH{self.channel}  {self.enabled:3s} [/]   [{mode_color}] {self.mode:8s} [/]   [bold]Model:[/bold] {model_name}
[dim]{'─' * 78}[/dim]
    [bold bright_cyan]{tv_display}[/bold bright_cyan] [cyan]V[/cyan]          [bold bright_green]{curr_display}[/bold bright_green] [green]A[/green]          [bold bright_magenta]{soc_display}[/bold bright_magenta] [magenta]% SOC[/magenta]
    [dim]Voltage[/dim]           [dim]Current[/dim]           [dim]State of Charge[/dim]
[dim]{'─' * 78}[/dim]
  [bold]Battery Parameters:[/bold]
    [cyan]VOC:[/cyan]  {voc_display} V    [cyan]Capacity:[/cyan] {cap_display:>6} Ah    [cyan]ESR:[/cyan] {esr_display} Ohm
    [cyan]Full:[/cyan] {volt_full_display} V    [cyan]Empty:[/cyan]    {volt_empty_display} V
[dim]{'─' * 78}[/dim]
  [bold]Protection:[/bold]
    [yellow]OCP:[/yellow] {ocp_display} A  [{ocp_color}]Tripped: {self.ocp_tripped:3s}[/{ocp_color}]    [yellow]Current Limit:[/yellow] {curr_lim_display} A
    [yellow]OVP:[/yellow] {ovp_display} V  [{ovp_color}]Tripped: {self.ovp_tripped:3s}[/{ovp_color}]
"""


class CommandLog(RichLog):
    """Widget to display command execution log with auto-scroll to bottom"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.auto_scroll = True  # Always scroll to bottom for newest content


class BatteryTUI(App):
    """Interactive TUI for battery simulator control"""

    CSS = """
    Screen {
        background: $surface;
    }

    #state_container {
        height: auto;
        min-height: 22;
        max-height: 27;
        margin: 1 2;
        padding: 1;
        border: heavy $primary;
        background: $panel;
    }

    BatteryState {
        height: auto;
        padding: 0 1;
        background: $panel;
    }

    #command_log {
        border: solid $secondary;
        height: 1fr;
        min-height: 25;
        padding: 1;
        margin: 1 2;
        background: $panel;
    }

    #command_input {
        border: solid $accent;
        margin: 1 2;
        dock: bottom;
        background: $surface;
    }

    Input {
        border: solid $accent;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("ctrl+c", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
    ]

    def __init__(self, ctx, netname, box_ip, dut):
        super().__init__()
        self.ctx = ctx
        self.netname = netname
        self.box_ip = box_ip
        self.dut = dut
        self.command_history = []
        self.history_index = -1
        self.title = f"BatteryTUI - {netname}"
        self.ws_client = None  # WebSocket client

    def compose(self) -> ComposeResult:
        """Create child widgets"""
        yield Header()
        yield Container(
            BatteryState(id="state_widget"),
            id="state_container"
        )
        yield CommandLog(id="command_log")
        yield Input(
            placeholder=f"Enter command for '{self.netname}' (e.g., 'soc 80', 'enable', 'state') or 'help'",
            id="command_input"
        )
        yield Footer()

    async def on_mount(self) -> None:
        """Initialize the app"""
        try:
            self.query_one("#command_input", Input).focus()

            # Initialize WebSocket connection
            try:
                # Determine box URL (use port 9000 for WebSocket)
                box_url = f"http://{self.box_ip}:9000"

                # Create WebSocket client
                self.ws_client = BatteryWebSocketClient(
                    box_url=box_url,
                    netname=self.netname,
                    update_interval=1.0  # Update every 1 second
                )

                # Set up callbacks
                self.ws_client.on_state_update = self._handle_state_update
                self.ws_client.on_error = self._handle_ws_error
                self.ws_client.on_connected = self._handle_ws_connected
                self.ws_client.on_disconnected = self._handle_ws_disconnected

                # Connect in background worker
                self.run_worker(self._connect_websocket(), exclusive=False)
            except Exception as e:
                # If WebSocket initialization fails, log and exit
                self._add_log_entry("Error", f"[red]Failed to initialize WebSocket: {e}[/red]")
                self.exit(1)
        except Exception as e:
            import traceback
            print(f"CRITICAL ERROR in on_mount: {e}", file=sys.stderr)
            print(traceback.format_exc(), file=sys.stderr)
            self.exit(1)

    async def _connect_websocket(self):
        """Connect to WebSocket server and start monitoring"""
        try:
            # Connect (silently)
            if not self.ws_client.connect(timeout=10.0):
                raise Exception("Failed to connect to WebSocket server")

            # Start monitoring (silently)
            if not self.ws_client.start_monitoring():
                raise Exception("Failed to start battery monitoring")

            # Connection successful - no log message needed
        except Exception as e:
            # Diagnose-then-report (added in 0.20.0): probe /health on port
            # 9000 to differentiate box-unreachable from box-up-but-WS-missing,
            # so the log entry tells the user what to do next instead of just
            # "WebSocket connection failed".
            from cli.core.ws_diagnose import make_ws_failure_message
            actionable = make_ws_failure_message(self.box_ip, original_error=e)
            self._add_log_entry("Error", f"[red]{actionable}[/red]")
            self.exit(1)

    def _handle_state_update(self, state):
        """Handle WebSocket state update"""
        # Update the state widget
        state_widget = self.query_one("#state_widget", BatteryState)

        # Extract channel number (default to 1)
        channel = state.get("channel", "1")
        # Remove any non-numeric characters
        channel = ''.join(c for c in str(channel) if c.isdigit()) or "1"

        # Format values
        def format_value(val, default="--"):
            if val is None:
                return default
            if isinstance(val, bool):
                return "ON" if val else "OFF"
            if isinstance(val, (int, float)):
                return str(val)
            return str(val)

        state_widget.channel = channel
        state_widget.terminal_voltage = format_value(state.get("terminal_voltage"))
        state_widget.current = format_value(state.get("current"))
        state_widget.esr = format_value(state.get("esr"))
        state_widget.soc = format_value(state.get("soc"))
        state_widget.voc = format_value(state.get("voc"))
        state_widget.enabled = "ON" if state.get("enabled") else "OFF"
        state_widget.mode = format_value(state.get("mode"))
        state_widget.model = format_value(state.get("model"))
        state_widget.capacity = format_value(state.get("capacity"))
        state_widget.current_limit = format_value(state.get("current_limit"))
        state_widget.ocp_limit = format_value(state.get("ocp_limit"))
        state_widget.ocp_tripped = "YES" if state.get("ocp_tripped") else "NO"
        state_widget.ovp_limit = format_value(state.get("ovp_limit"))
        state_widget.ovp_tripped = "YES" if state.get("ovp_tripped") else "NO"
        state_widget.volt_full = format_value(state.get("volt_full"))
        state_widget.volt_empty = format_value(state.get("volt_empty"))

    def _handle_ws_error(self, message):
        """Handle WebSocket error"""
        self._add_log_entry("Error", f"[red]{message}[/red]")

    def _handle_ws_connected(self):
        """Handle WebSocket connected"""
        pass  # Already logged in _connect_websocket

    def _handle_ws_disconnected(self):
        """Handle WebSocket disconnected"""
        self._add_log_entry("WebSocket", "[red]Disconnected from WebSocket server[/red]")
        self.exit(1)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle command input"""
        command_text = event.value.strip()
        if not command_text:
            return

        # Add to history
        self.command_history.append(command_text)
        self.history_index = len(self.command_history)

        # Clear input
        event.input.value = ""

        # Handle special commands
        if command_text.lower() in ("q", "quit", "exit"):
            self.exit()
            return

        if command_text.lower() == "help":
            self._show_help()
            return

        if command_text.lower() == "clear":
            log_widget = self.query_one("#command_log", CommandLog)
            log_widget.clear()
            return

        # Parse and execute command
        await self._execute_command(command_text)

    def _show_help(self) -> None:
        """Display help information"""
        log_widget = self.query_one("#command_log", CommandLog)

        # Create rich formatted help text
        help_lines = [
            ("Battery TUI - Interactive Battery Simulator Control", "bold cyan"),
            ("", ""),
            ("Available Commands:", "bold white"),
            ("soc [VALUE]              ", "bold yellow", " - Set or read state of charge (e.g., 'soc 80')", ""),
            ("voc [VALUE]              ", "bold yellow", " - Set or read open circuit voltage (e.g., 'voc 3.7')", ""),
            ("batt-full [VALUE]        ", "bold yellow", " - Set or read fully charged voltage (e.g., 'batt-full 4.2')", ""),
            ("batt-empty [VALUE]       ", "bold yellow", " - Set or read fully discharged voltage (e.g., 'batt-empty 3.0')", ""),
            ("capacity [VALUE]         ", "bold yellow", " - Set or read capacity in Ah (e.g., 'capacity 2.5')", ""),
            ("current-limit [VALUE]    ", "bold yellow", " - Set or read max current (e.g., 'current-limit 1.0')", ""),
            ("ocp [VALUE]              ", "bold yellow", " - Set or read over-current protection (e.g., 'ocp 2.0')", ""),
            ("ovp [VALUE]              ", "bold yellow", " - Set or read over-voltage protection (e.g., 'ovp 4.5')", ""),
            ("mode [static|dynamic]    ", "bold yellow", " - Set or read simulation mode", ""),
            ("model [NAME]             ", "bold yellow", " - Set or read battery model (e.g., 'model 18650')", ""),
            ("models                   ", "bold yellow", " - List battery models saved on the instrument", ""),
            ("enable                   ", "bold yellow", " - Enable battery output", ""),
            ("disable                  ", "bold yellow", " - Disable battery output", ""),
            ("state                    ", "bold yellow", " - Display current state", ""),
            ("clear-ocp                ", "bold yellow", " - Clear over-current protection trip", ""),
            ("clear-ovp                ", "bold yellow", " - Clear over-voltage protection trip", ""),
            ("clear-protection         ", "bold yellow", " - Clear all protection trips", ""),
            ("set                      ", "bold yellow", " - Initialize battery simulator mode", ""),
            ("help                     ", "bold yellow", " - Show this help message", ""),
            ("clear                    ", "bold yellow", " - Clear command log", ""),
            ("q/quit/exit              ", "bold yellow", " - Exit TUI", ""),
            ("", ""),
            ("Display Legend:", "bold white"),
            ("  - Large values show actual measurements (Terminal Voltage, Current, SOC)", ""),
            ("  - Battery Parameters show configured settings", ""),
            ("  - Protection shows OCP/OVP limits and trip status", ""),
            ("", ""),
            ("Note: The display updates automatically via WebSocket every second.", "dim"),
        ]

        for line_parts in help_lines:
            if len(line_parts) == 2:
                # Simple line with one style
                text = Text(line_parts[0], style=line_parts[1])
            elif len(line_parts) == 4:
                # Command line with command and description
                text = Text()
                text.append(line_parts[0], style=line_parts[1])
                text.append(line_parts[2], style=line_parts[3])
            else:
                text = Text()
            log_widget.write(text)

    async def _execute_command(self, command_text: str) -> None:
        """Execute a battery command in background to avoid blocking UI"""
        parts = command_text.split()
        if not parts:
            return

        # Make command case-insensitive
        action = parts[0].lower()

        try:
            # Parse and validate command parameters first (fast, non-blocking)
            backend_action, backend_params, value_set = self._parse_command(action, parts)

            if backend_action is None:
                # Validation error occurred, message already logged
                return

            # Execute command via WebSocket
            if not self.ws_client or not self.ws_client.connected:
                self._add_log_entry(command_text, "[red]WebSocket not connected[/red]")
                return

            # Run WebSocket command in worker (non-blocking!)
            self.run_worker(
                self._run_ws_command_worker(command_text, backend_action, backend_params, value_set),
                exclusive=False
            )

        except Exception as e:
            self._add_log_entry(command_text, f"[red]Unexpected error: {e}[/red]")

    def _parse_command(self, action: str, parts: list) -> tuple:
        """Parse command and return (backend_action, params, value_set) or (None, None, None) on error"""
        value_set = None

        if action == "soc":
            if len(parts) > 1:
                try:
                    value = float(parts[1])
                    if value < 0 or value > 100:
                        self._add_log_entry(" ".join(parts), f"[red]SOC must be between 0 and 100%[/red]")
                        return (None, None, None)
                    value_set = value
                except ValueError:
                    self._add_log_entry(" ".join(parts), f"[red]Invalid SOC value: '{parts[1]}'\nUsage: soc [VALUE] (e.g., 'soc 80')[/red]")
                    return (None, None, None)
            else:
                value = None
            return ("soc", {"value": value}, value_set)

        elif action == "voc":
            if len(parts) > 1:
                try:
                    value = float(parts[1])
                    value_set = value
                except ValueError:
                    self._add_log_entry(" ".join(parts), f"[red]Invalid VOC value: '{parts[1]}'\nUsage: voc [VALUE] (e.g., 'voc 3.7')[/red]")
                    return (None, None, None)
            else:
                value = None
            return ("voc", {"value": value}, value_set)

        elif action == "batt-full":
            if len(parts) > 1:
                try:
                    value = float(parts[1])
                    value_set = value
                except ValueError:
                    self._add_log_entry(" ".join(parts), f"[red]Invalid voltage value: '{parts[1]}'\nUsage: batt-full [VALUE] (e.g., 'batt-full 4.2')[/red]")
                    return (None, None, None)
            else:
                value = None
            return ("batt_full", {"value": value}, value_set)

        elif action == "batt-empty":
            if len(parts) > 1:
                try:
                    value = float(parts[1])
                    value_set = value
                except ValueError:
                    self._add_log_entry(" ".join(parts), f"[red]Invalid voltage value: '{parts[1]}'\nUsage: batt-empty [VALUE] (e.g., 'batt-empty 3.0')[/red]")
                    return (None, None, None)
            else:
                value = None
            return ("batt_empty", {"value": value}, value_set)

        elif action == "capacity":
            if len(parts) > 1:
                try:
                    value = float(parts[1])
                    value_set = value
                except ValueError:
                    self._add_log_entry(" ".join(parts), f"[red]Invalid capacity value: '{parts[1]}'\nUsage: capacity [VALUE] (e.g., 'capacity 2.5')[/red]")
                    return (None, None, None)
            else:
                value = None
            return ("capacity", {"value": value}, value_set)

        elif action == "current-limit":
            if len(parts) > 1:
                try:
                    value = float(parts[1])
                    value_set = value
                except ValueError:
                    self._add_log_entry(" ".join(parts), f"[red]Invalid current value: '{parts[1]}'\nUsage: current-limit [VALUE] (e.g., 'current-limit 1.0')[/red]")
                    return (None, None, None)
            else:
                value = None
            return ("current_limit", {"value": value}, value_set)

        elif action == "ocp":
            if len(parts) > 1:
                try:
                    value = float(parts[1])
                    value_set = value
                except ValueError:
                    self._add_log_entry(" ".join(parts), f"[red]Invalid OCP value: '{parts[1]}'\nUsage: ocp [VALUE] (e.g., 'ocp 2.0')[/red]")
                    return (None, None, None)
            else:
                value = None
            return ("ocp", {"value": value}, value_set)

        elif action == "ovp":
            if len(parts) > 1:
                try:
                    value = float(parts[1])
                    value_set = value
                except ValueError:
                    self._add_log_entry(" ".join(parts), f"[red]Invalid OVP value: '{parts[1]}'\nUsage: ovp [VALUE] (e.g., 'ovp 4.5')[/red]")
                    return (None, None, None)
            else:
                value = None
            return ("ovp", {"value": value}, value_set)

        elif action == "mode":
            if len(parts) > 1:
                mode_type = parts[1].lower()
                if mode_type not in ("static", "dynamic"):
                    self._add_log_entry(" ".join(parts), f"[red]Invalid mode: '{parts[1]}'\nUsage: mode [static|dynamic][/red]")
                    return (None, None, None)
                value_set = mode_type
            else:
                mode_type = None
            return ("mode", {"mode_type": mode_type}, value_set)

        elif action == "model":
            if len(parts) > 1:
                partnumber = parts[1]
                value_set = partnumber
            else:
                partnumber = None
            return ("model", {"partnumber": partnumber}, value_set)

        elif action == "models":
            return ("models", {}, None)

        elif action == "enable":
            return ("enable", {}, None)

        elif action == "disable":
            return ("disable", {}, None)

        elif action == "state":
            return ("state", {}, None)

        elif action == "set":
            return ("set", {}, None)

        elif action == "clear-ocp":
            return ("clear_ocp", {}, None)

        elif action == "clear-ovp":
            return ("clear_ovp", {}, None)

        elif action in ("clear-protection", "clear-prot"):
            return ("clear", {}, None)

        else:
            self._add_log_entry(" ".join(parts), f"[red]Unknown command: '{action}'[/red]\nType 'help' for available commands.")
            return (None, None, None)

    async def _run_ws_command_worker(self, command_text: str, backend_action: str, backend_params: dict, value_set) -> None:
        """Worker that runs command via WebSocket and updates log when complete"""
        try:
            # Send command via WebSocket
            response = self.ws_client.send_command(backend_action, backend_params, timeout=5.0)

            if response is None:
                output = "[red]Command timeout or connection error[/red]"
            elif not response.get('success', False):
                error = response.get('error', 'Unknown error')
                output = f"[red]{error}[/red]"
            else:
                # Success - format message
                message = response.get('message', '')
                if message:
                    output = f"[green]{message}[/green]"
                else:
                    # Generate default success message
                    if backend_action == "soc" and value_set is not None:
                        output = f"[green]SOC set to {value_set}%[/green]"
                    elif backend_action == "voc" and value_set is not None:
                        output = f"[green]VOC set to {value_set}V[/green]"
                    elif backend_action == "batt_full" and value_set is not None:
                        output = f"[green]Battery full voltage set to {value_set}V[/green]"
                    elif backend_action == "batt_empty" and value_set is not None:
                        output = f"[green]Battery empty voltage set to {value_set}V[/green]"
                    elif backend_action == "capacity" and value_set is not None:
                        output = f"[green]Capacity set to {value_set}Ah[/green]"
                    elif backend_action == "current_limit" and value_set is not None:
                        output = f"[green]Current limit set to {value_set}A[/green]"
                    elif backend_action == "ocp" and value_set is not None:
                        output = f"[green]OCP limit set to {value_set}A[/green]"
                    elif backend_action == "ovp" and value_set is not None:
                        output = f"[green]OVP limit set to {value_set}V[/green]"
                    elif backend_action == "mode" and value_set is not None:
                        output = f"[green]Mode set to {value_set}[/green]"
                    elif backend_action == "model" and value_set is not None:
                        output = f"[green]Model set to {value_set}[/green]"
                    elif backend_action == "enable":
                        output = "[green]Battery output enabled[/green]"
                    elif backend_action == "disable":
                        output = "[green]Battery output disabled[/green]"
                    elif backend_action == "set":
                        output = "[green]Battery simulator mode initialized[/green]"
                    elif backend_action == "clear_ocp":
                        output = "[green]OCP trip cleared[/green]"
                    elif backend_action == "clear_ovp":
                        output = "[green]OVP trip cleared[/green]"
                    elif backend_action == "clear":
                        output = "[green]Protection trips cleared[/green]"
                    else:
                        output = "[green]Command completed[/green]"

            # Add to command log history
            self._add_log_entry(command_text, output)

            # State updates happen automatically via WebSocket monitoring
            # No need to manually trigger update_state()

        except Exception as e:
            self._add_log_entry(command_text, f"[red]Error: {e}[/red]")


    def _add_log_entry(self, command_text: str, output: str) -> None:
        """Add a command and its output to the log history"""
        log_widget = self.query_one("#command_log", CommandLog)

        # Create rich text with proper styling
        command_line = Text()
        command_line.append("> ", style="bold cyan")
        command_line.append(command_text, style="bold white")

        # Parse output for color styling
        output_text = self._parse_output_markup(output)

        # Write to log - RichLog automatically scrolls to bottom
        log_widget.write(command_line)
        log_widget.write(output_text)
        log_widget.write("")  # Blank line for separation

    def _parse_output_markup(self, output: str) -> Text:
        """Parse markup tags in output and convert to Rich Text with proper styling"""
        text = Text()

        # Simple markup parser for common patterns
        import re

        # Remove markup tags and apply styles
        if output.startswith("[green]"):
            # Success messages
            clean = re.sub(r'\[/?green\]', '', output)
            text.append(clean, style="green")
        elif output.startswith("[red]"):
            # Error messages
            clean = re.sub(r'\[/?red\]', '', output)
            text.append(clean, style="red")
        elif output.startswith("[dim]"):
            # Dim messages
            clean = re.sub(r'\[/?dim\]', '', output)
            text.append(clean, style="dim")
        else:
            # Plain text output (like state command)
            text.append(output)

        return text


    async def on_key(self, event) -> None:
        """Handle key presses"""
        if event.key == "up" and self.command_history:
            # Navigate backward through command history
            self.history_index = max(0, self.history_index - 1)
            command_input = self.query_one("#command_input", Input)
            command_input.value = self.command_history[self.history_index]
        elif event.key == "down" and self.command_history:
            # Navigate forward through command history
            command_input = self.query_one("#command_input", Input)
            if self.history_index < len(self.command_history) - 1:
                self.history_index += 1
                command_input.value = self.command_history[self.history_index]
            else:
                # Allow moving past history to clear input
                self.history_index = len(self.command_history)
                command_input.value = ""


    def action_quit(self) -> None:
        """Clean up and exit"""
        # Disconnect WebSocket if connected
        if self.ws_client:
            try:
                self.ws_client.disconnect()
            except Exception:
                pass
        # Call parent quit
        self.exit()
