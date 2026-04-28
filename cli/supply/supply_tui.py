# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

import re
import sys
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Input, Static, RichLog
from textual.containers import Container
from textual.reactive import reactive
from rich.text import Text
from .websocket_client import SupplyWebSocketClient


class SupplyState(Static):
    """Widget to display supply state information"""

    channel = reactive("1")
    voltage = reactive("--")
    current = reactive("--")
    power = reactive("--")
    enabled = reactive("--")
    mode = reactive("--")
    ocp_limit = reactive("--")
    ocp_tripped = reactive("--")
    ovp_limit = reactive("--")
    ovp_tripped = reactive("--")
    voltage_set = reactive("--")
    current_set = reactive("--")
    voltage_max = reactive("--")
    current_max = reactive("--")

    def render(self) -> str:
        """Render the supply state display in power supply style"""
        # Color coding for status
        enabled_color = "black on green" if self.enabled == "ON" else "black on yellow"
        mode_color = "black on yellow" if self.mode in ("CC", "CV") else "black on white"
        ocp_color = "red" if self.ocp_tripped == "YES" else "green"
        ovp_color = "red" if self.ovp_tripped == "YES" else "green"

        # If supply is disabled, force measurements to 0
        if self.enabled == "OFF" or self.mode == "OFF":
            v = "0.00"
            i = "0.000"
            p = "0.00"
        else:
            # Format values with proper precision
            v = self.voltage if self.voltage != "--" else "0.00"
            i = self.current if self.current != "--" else "0.000"
            p = self.power if self.power != "--" else "0.00"

        # Parse and format values with consistent precision (2 leading digits + 3 decimals)
        try:
            v_val = float(v.replace("V", "").strip())
            v_display = f"{v_val:06.3f}"
        except (ValueError, AttributeError):
            v_display = "00.000"

        try:
            i_val = float(i.replace("A", "").strip())
            i_display = f"{i_val:06.3f}"
        except (ValueError, AttributeError):
            i_display = "00.000"

        try:
            p_val = float(p.replace("W", "").strip())
            p_display = f"{p_val:06.3f}"
        except (ValueError, AttributeError):
            p_display = "00.000"

        # Format setpoints (2 leading digits + 3 decimals)
        try:
            v_set = f"{float(self.voltage_set):06.3f}" if self.voltage_set != "--" else "00.000"
        except (ValueError, TypeError):
            v_set = "00.000"

        try:
            i_set = f"{float(self.current_set):06.3f}" if self.current_set != "--" else "00.000"
        except (ValueError, TypeError):
            i_set = "00.000"

        # Format limits (2 leading digits + 3 decimals)
        try:
            v_max = f"{float(self.voltage_max):06.3f}" if self.voltage_max != "--" else "00.000"
        except (ValueError, TypeError):
            v_max = "00.000"

        try:
            i_max = f"{float(self.current_max):06.3f}" if self.current_max != "--" else "00.000"
        except (ValueError, TypeError):
            i_max = "00.000"

        # Format protection values (2 leading digits + 3 decimals)
        try:
            ocp_val = f"{float(self.ocp_limit):06.3f}" if self.ocp_limit != "--" else "00.000"
        except (ValueError, TypeError):
            ocp_val = "00.000"

        try:
            ovp_val = f"{float(self.ovp_limit):06.3f}" if self.ovp_limit != "--" else "00.000"
        except (ValueError, TypeError):
            ovp_val = "00.000"

        return f"""
  [{enabled_color}] CH{self.channel}  {self.enabled:3s} [/]   [{mode_color}] {self.mode:2s} [/]
[dim]{'─' * 78}[/dim]
   [bold reverse bright_cyan]{v_display}[/bold reverse bright_cyan] [bold cyan]V[/bold cyan]         [bold reverse bright_green]{i_display}[/bold reverse bright_green] [bold green]A[/bold green]         [bold reverse bright_yellow]{p_display}[/bold reverse bright_yellow] [bold yellow]W[/bold yellow]
[dim]{'─' * 78}[/dim]
  [bold]Protection:[/bold]
    OCP: {ocp_val} A  [{ocp_color}]Tripped: {self.ocp_tripped:3s}[/{ocp_color}]
    OVP: {ovp_val} V  [{ovp_color}]Tripped: {self.ovp_tripped:3s}[/{ovp_color}]
[dim]{'─' * 78}[/dim]
  [bold]Set[/bold]                        [bold]Hardware Max[/bold]
   {v_set} V                    {v_max} V
   {i_set} A                    {i_max} A
"""


class CommandLog(RichLog):
    """Widget to display command execution log with auto-scroll to bottom"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.auto_scroll = True  # Always scroll to bottom for newest content


class SupplyTUI(App):
    """Interactive TUI for power supply control"""

    CSS = """
    Screen {
        background: $surface;
    }

    #state_container {
        height: auto;
        min-height: 20;
        max-height: 25;
        margin: 1 2;
        padding: 1;
        border: heavy $primary;
        background: $panel;
    }

    SupplyState {
        height: auto;
        padding: 0 1;
        background: $panel;
    }

    #command_log {
        border: solid $secondary;
        height: 1fr;
        min-height: 30;
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

    def __init__(self, ctx, netname, box, box_ip):
        super().__init__()
        self.ctx = ctx
        self.netname = netname
        self.box = box
        self.box_ip = box_ip
        self.command_history = []
        self.history_index = -1
        self.title = f"SupplyTUI - {netname}"
        self.ws_client = None  # WebSocket client
        # Captured by the click wrapper after run_async() returns; if set it
        # gets printed to stderr so failures don't disappear with the alt-screen.
        self.exit_error: str | None = None

    def compose(self) -> ComposeResult:
        """Create child widgets"""
        yield Header()
        yield Container(
            SupplyState(id="state_widget"),
            id="state_container"
        )
        yield CommandLog(id="command_log")
        yield Input(
            placeholder=f"Enter command for '{self.netname}' (e.g., 'voltage 3.3', 'enable', 'state') or 'help'",
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
                self.ws_client = SupplyWebSocketClient(
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
                import traceback
                # If WebSocket initialization fails, log and exit
                self._add_log_entry("Error", f"[red]Failed to initialize WebSocket: {e}[/red]")
                self.exit(1)
        except Exception as e:
            import traceback
            print(f"CRITICAL ERROR in on_mount: {e}")
            print(traceback.format_exc())
            self.exit(1)

    async def _connect_websocket(self):
        """Connect to WebSocket server and start monitoring"""
        try:
            # Connect (silently)
            if not self.ws_client.connect(timeout=10.0):
                raise Exception("Failed to connect to WebSocket server")

            # Start monitoring (silently)
            if not self.ws_client.start_monitoring():
                raise Exception("Failed to start supply monitoring")

            # Connection successful - no log message needed
        except Exception as e:
            self._add_log_entry("Error", f"[red]WebSocket connection failed: {e}[/red]")
            self.exit_error = f"WebSocket connection failed: {e}"
            self.exit(1)

    def _handle_state_update(self, state):
        """Handle WebSocket state update"""
        # Update the state widget
        state_widget = self.query_one("#state_widget", SupplyState)

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
        state_widget.voltage = format_value(state.get("voltage"))
        state_widget.current = format_value(state.get("current"))
        state_widget.power = format_value(state.get("power"))
        state_widget.enabled = "ON" if state.get("enabled") else "OFF"
        state_widget.mode = format_value(state.get("mode"))
        state_widget.ocp_limit = format_value(state.get("ocp_limit"))
        state_widget.ocp_tripped = "YES" if state.get("ocp_tripped") else "NO"
        state_widget.ovp_limit = format_value(state.get("ovp_limit"))
        state_widget.ovp_tripped = "YES" if state.get("ovp_tripped") else "NO"
        state_widget.voltage_set = format_value(state.get("voltage_set"))
        state_widget.current_set = format_value(state.get("current_set"))
        state_widget.voltage_max = format_value(state.get("voltage_max"))
        state_widget.current_max = format_value(state.get("current_max"))

    def _handle_ws_error(self, message):
        """Handle WebSocket error"""
        self._add_log_entry("Error", f"[red]{message}[/red]")
        # Remember the most recent server-side error; if the TUI exits because
        # start_monitoring() failed, this is the message we want surfaced on stderr.
        self.exit_error = message

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
            ("Supply TUI - Interactive Power Supply Control", "bold cyan"),
            ("", ""),
            ("Available Commands:", "bold white"),
            ("voltage [VALUE]          ", "bold yellow", " - Set or read voltage (e.g., 'voltage 3.3')", ""),
            ("current [VALUE]          ", "bold yellow", " - Set or read current limit (e.g., 'current 0.5')", ""),
            ("ocp [VALUE]              ", "bold yellow", " - Set or read over-current protection limit (e.g., 'ocp 1.0')", ""),
            ("ovp [VALUE]              ", "bold yellow", " - Set or read over-voltage protection limit (e.g., 'ovp 5.5')", ""),
            ("enable                   ", "bold yellow", " - Enable supply output", ""),
            ("disable                  ", "bold yellow", " - Disable supply output", ""),
            ("state                    ", "bold yellow", " - Display current state", ""),
            ("clear-ocp                ", "bold yellow", " - Clear over-current protection trip", ""),
            ("clear-ovp                ", "bold yellow", " - Clear over-voltage protection trip", ""),
            ("help                     ", "bold yellow", " - Show this help message", ""),
            ("clear                    ", "bold yellow", " - Clear command log", ""),
            ("q/quit/exit              ", "bold yellow", " - Exit TUI", ""),
            ("", ""),
            ("Display Legend:", "bold white"),
            ("  - Large values show actual measurements (Voltage, Current, Power)", ""),
            ("  - Set values show configured setpoints", ""),
            ("  - Limit values show hardware maximum ratings", ""),
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
        """Execute a supply command in background to avoid blocking UI"""
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

        if action == "voltage":
            if len(parts) > 1:
                try:
                    value = float(parts[1])
                    value_set = value
                except ValueError:
                    self._add_log_entry(" ".join(parts), f"[red]Invalid voltage value: '{parts[1]}'\nUsage: voltage [VALUE] (e.g., 'voltage 3.3')[/red]")
                    return (None, None, None)
            else:
                value = None
            return ("voltage", {"value": value}, value_set)

        elif action == "current":
            if len(parts) > 1:
                try:
                    value = float(parts[1])
                    value_set = value
                except ValueError:
                    self._add_log_entry(" ".join(parts), f"[red]Invalid current value: '{parts[1]}'\nUsage: current [VALUE] (e.g., 'current 0.5')[/red]")
                    return (None, None, None)
            else:
                value = None
            return ("current", {"value": value}, value_set)

        elif action == "enable":
            return ("enable", {}, None)

        elif action == "disable":
            return ("disable", {}, None)

        elif action == "state":
            return ("state", {}, None)

        elif action == "ocp":
            if len(parts) > 1:
                try:
                    value = float(parts[1])
                    value_set = value
                except ValueError:
                    self._add_log_entry(" ".join(parts), f"[red]Invalid OCP value: '{parts[1]}'\nUsage: ocp [VALUE] (e.g., 'ocp 1.0')[/red]")
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
                    self._add_log_entry(" ".join(parts), f"[red]Invalid OVP value: '{parts[1]}'\nUsage: ovp [VALUE] (e.g., 'ovp 5.5')[/red]")
                    return (None, None, None)
            else:
                value = None
            return ("ovp", {"value": value}, value_set)

        elif action == "clear-ocp":
            return ("clear_ocp", {}, None)

        elif action == "clear-ovp":
            return ("clear_ovp", {}, None)

        else:
            self._add_log_entry(" ".join(parts), f"[red]Unknown command: '{action}'[/red]\nType 'help' for available commands.")
            return (None, None, None)

    async def _run_ws_command_worker(self, command_text: str, backend_action: str, backend_params: dict, value_set: float | None) -> None:
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
                    if backend_action == "voltage" and value_set is not None:
                        output = f"[green]Voltage set to {value_set}V[/green]"
                    elif backend_action == "current" and value_set is not None:
                        output = f"[green]Current limit set to {value_set}A[/green]"
                    elif backend_action == "ocp" and value_set is not None:
                        output = f"[green]OCP limit set to {value_set}A[/green]"
                    elif backend_action == "ovp" and value_set is not None:
                        output = f"[green]OVP limit set to {value_set}V[/green]"
                    elif backend_action == "enable":
                        output = "[green]Supply output enabled[/green]"
                    elif backend_action == "disable":
                        output = "[green]Supply output disabled[/green]"
                    elif backend_action == "clear_ocp":
                        output = "[green]OCP trip cleared[/green]"
                    elif backend_action == "clear_ovp":
                        output = "[green]OVP trip cleared[/green]"
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
