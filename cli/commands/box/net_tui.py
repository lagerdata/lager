# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import io
import json
import re
from collections import defaultdict
from contextlib import redirect_stdout
from ...sort_utils import natural_sort_key
from dataclasses import dataclass, field
from typing import Callable

import click
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Click, MouseMove, Leave, Key
from textual.screen import Screen, ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    Static,
    Tree,
)
from textual.widgets.tree import TreeNode

# Handle NoMatches compatibility across textual versions
try:  # textual >= 0.15
    from textual.exceptions import NoMatches
except ModuleNotFoundError:
    try:  # textual 0.12–0.14
        from textual.widget import NoMatches  # type: ignore
    except ModuleNotFoundError:
        class NoMatches(LookupError):
            """Raised when query_one finds no matching node."""
            pass

# ───────────────────────── Lager helpers ──────────────────────────
from ...context import get_impl_path
from ..development.python import run_python_internal

# ──────────────── helpers / model ─────────────────

def _parse_backend_json_tui(raw: str):
    """
    Parse JSON response from backend, handling duplicate output from double execution.
    Same logic as in nets_commands.py but for TUI usage.
    """
    try:
        return json.loads(raw or "[]")
    except json.JSONDecodeError:
        # Handle duplicate JSON output from backend double execution
        if raw and raw.count('[') >= 2:
            # Try to extract the first JSON array
            depth = 0
            first_array_end = -1
            for i, char in enumerate(raw):
                if char == '[':
                    depth += 1
                elif char == ']':
                    depth -= 1
                    if depth == 0:
                        first_array_end = i + 1
                        break

            if first_array_end > 0:
                first_json = raw[:first_array_end]
                return json.loads(first_json)
            else:
                raise json.JSONDecodeError("Could not find complete JSON array", raw, 0)
        else:
            # Handle duplicate JSON objects (e.g., {"ok": true}{"ok": true})
            if raw and raw.count('{') >= 2:
                depth = 0
                first_obj_end = -1
                for i, char in enumerate(raw):
                    if char == '{':
                        depth += 1
                    elif char == '}':
                        depth -= 1
                        if depth == 0:
                            first_obj_end = i + 1
                            break

                if first_obj_end > 0:
                    first_json = raw[:first_obj_end]
                    return json.loads(first_json)

            raise  # Re-raise original exception

def _uid(instr: str, chan: str, role: str, name: str) -> str:
    """Return a row-key that is unique for (instrument, USB0::0x05E6::0x2281::4519728::INSTR channel, type, name)."""
    base = f"{instr}_{chan}_{role}_{name}".replace(" ", "_")
    safe = "".join(c if re.fullmatch(r"[A-Za-z0-9_-]", c) else "_" for c in base)
    return f"_{safe}" if safe and safe[0].isdigit() else safe

_MULTI_HUBS = {"LabJack_T7", "Acroname_8Port", "Acroname_4Port"}
_SINGLE_CHANNEL_INST = {
    "Keithley_2281S": ("batt", "supply"),
    "EA_PSB_10060_60": ("solar", "supply"),
    "EA_PSB_10080_60": ("solar", "supply"),
}

def _first_word(role: str) -> str:
    """Return the first part of a hyphenated role name."""
    # Special case: power-supply nets use 'supply' prefix instead of 'power'
    if role == "power-supply":
        return "supply"
    return role.split("-")[0]

# Common kwargs for run_python_internal calls
_RUN_PYTHON_KWARGS = {
    "env": {},
    "passenv": (),
    "kill": False,
    "download": (),
    "allow_overwrite": False,
    "signum": "SIGTERM",
    "timeout": 30,  # 30 second timeout to prevent infinite hangs (was 0)
    "detach": False,
    "port": (),
    "org": None,
}

def _run_script(ctx: click.Context, script: str, dut: str, *args) -> str:
    """Execute an internal script with given arguments and capture stdout."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            run_python_internal(ctx, get_impl_path(script), dut, **_RUN_PYTHON_KWARGS, args=args)
    except SystemExit:
        pass
    return buf.getvalue()

def _save_nets_batch(ctx: click.Context, dut: str, nets: list["Net"], custom_names: dict[str, str] | None = None) -> bool:
    """Save multiple nets using batch save with fallback to individual saves."""
    if not nets:
        return True

    custom_names = custom_names or {}
    nets_data = []
    for n in nets:
        net_name = custom_names.get(n.key(), n.net)
        nets_data.append({
            "name": net_name,
            "role": n.type,
            "address": n.addr,
            "instrument": n.instrument,
            "pin": n.chan,
        })

    # Try batch save first
    try:
        raw = _run_script(ctx, "net.py", dut, "save-batch", json.dumps(nets_data))

        if raw and raw.strip():
            # Use the same JSON parsing logic as the CLI to handle duplicate output
            response = _parse_backend_json_tui(raw)
            if response.get("ok", False):
                return True
    except (json.JSONDecodeError, Exception):
        pass  # Fall through to individual saves

    # Fallback to individual saves (batch save failed or returned empty)
    saved_count = 0
    for n in nets:
        try:
            net_name = custom_names.get(n.key(), n.net)
            _run_script(ctx, "net.py", dut, "save", json.dumps({
                "name": net_name,
                "role": n.type,
                "address": n.addr,
                "instrument": n.instrument,
                "pin": n.chan,
            }))
            saved_count += 1
        except Exception:
            pass  # Continue trying to save other nets

    return saved_count > 0

def is_single_channel_taken(all_nets: list["Net"], inst: str, addr: str, role: str | None = None) -> bool:
    """
    True if a *saved* net for this instrument+address (and role, if given)
    already exists.

    For multi-role devices like Keithley_2281S (battery + power-supply) or
    EA PSB (solar + power-supply), each role is independent — only block if
    this specific role is already saved.
    """
    if inst not in _SINGLE_CHANNEL_INST:
        return False
    if role is not None:
        return any(n.saved and n.instrument == inst and n.addr == addr and n.type == role for n in all_nets)
    # Fallback (no role given): all allowed roles are already saved
    allowed_prefixes = _SINGLE_CHANNEL_INST[inst]
    saved_roles = {n.type for n in all_nets if n.saved and n.instrument == inst and n.addr == addr}
    return len(saved_roles) >= len(allowed_prefixes)

@dataclass
class Net:
    instrument: str
    chan: str
    type: str
    net: str
    addr: str
    saved: bool = False
    has_script: bool = False
    description: str = ""
    dut_connection: str = ""
    test_hints: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    _uid: str = field(init=False)

    def __post_init__(self) -> None:
        self._uid = _uid(self.instrument, self.chan, self.type, self.net)

    # ───── table rows
    def as_row_main(self) -> list[str]:
        status = "[SAVED]" if self.saved else "[PENDING]"
        return [
            f"{status} {self.net}",
            self.type.upper(),
            self.instrument.replace("_", " "),
            self.chan,
            self.addr,
            "[Rename]",
            "[Delete]",
        ]

    def as_row_add(self, chosen: bool, custom_name: str | None = None) -> list[str]:
        display_name = custom_name if custom_name else self.net
        return [
            "[SELECTED]" if chosen else "[ADD]",
            display_name,
            self.type.upper(),
            self.instrument.replace("_", " "),
            self.chan,
            self.addr,
            "[Rename]",
        ]

    def key(self) -> str:
        return self._uid


@dataclass
class TreeNodeData:
    """Data attached to each tree node."""
    node_type: str  # "instrument" or "net"
    net: Net | None = None
    instrument_key: str | None = None  # "instrument_name|addr" for grouping


class SavedNetsTree(Tree[TreeNodeData]):
    """Tree widget for displaying saved nets, organized by instrument."""

    def __init__(self, **kwargs):
        super().__init__("Saved Nets", **kwargs)
        self.show_root = False
        self.show_guides = True
        self.guide_depth = 6
        self.root.expand()
        self.net_nodes: dict[str, TreeNode] = {}  # key -> node for updates
        self._hover_node_key: str | None = None  # Track which net node is hovered
        self._hover_button: str | None = None  # "rename" or "delete" or None
        self._focus_node_key: str | None = None  # Track which net has keyboard focus
        self._focus_button: str | None = None  # Track keyboard focus: "rename", "delete", or None

    def _get_net_label(self, net: Net, highlight_button: str | None = None) -> str:
        """Generate label for a net with optional button highlighting (hover or focus)."""
        rename_btn = "[reverse][✎][/reverse]" if highlight_button == "rename" else "[✎]"
        edit_btn = "[reverse][⋯][/reverse]" if highlight_button == "edit" else "[⋯]"
        delete_btn = "[reverse][✕][/reverse]" if highlight_button == "delete" else "[✕]"
        script_tag = " [dim](script)[/dim]" if net.has_script else ""
        return f"[bold]{net.net}[/bold] | {net.type.upper()} | Ch: {net.chan}{script_tag}   {rename_btn} {edit_btn} {delete_btn}"

    def _get_highlighted_button(self, net_key: str) -> str | None:
        """Get which button should be highlighted for a given net (hover takes precedence)."""
        if self._hover_node_key == net_key and self._hover_button:
            return self._hover_button
        # Check if this net has keyboard focus
        if self._focus_node_key == net_key and self._focus_button:
            return self._focus_button
        return None

    def _clear_focus(self) -> None:
        """Clear keyboard focus from any button."""
        if self._focus_node_key and self._focus_node_key in self.net_nodes:
            old_node = self.net_nodes[self._focus_node_key]
            if old_node.data and old_node.data.net:
                # Re-render without focus (but keep hover if applicable)
                highlight = self._hover_button if self._hover_node_key == self._focus_node_key else None
                old_node.set_label(self._get_net_label(old_node.data.net, highlight))
        self._focus_node_key = None
        self._focus_button = None

    def _update_focus_display(self) -> None:
        """Update the display of the currently focused net's buttons."""
        if self.cursor_node and self.cursor_node.data:
            data = self.cursor_node.data
            if data.node_type == "net" and data.net:
                net_key = data.net.key()
                # Clear old focus first if it's a different node
                if self._focus_node_key and self._focus_node_key != net_key:
                    self._clear_focus()
                # Set new focus
                self._focus_node_key = net_key
                highlight = self._get_highlighted_button(net_key)
                self.cursor_node.set_label(self._get_net_label(data.net, highlight))

    def _get_button_at_position(self, net: Net, x: int) -> str | None:
        """Determine which button (if any) is at the given x position."""
        content = f"{net.net} | {net.type.upper()} | Ch: {net.chan}"
        if net.has_script:
            content += " (script)"
        content += "   "
        # Buttons: [✎] [⋯] [✕] — each 3 chars, 1 char space between
        content_end = len(content)
        offset = 4

        rename_start = content_end + offset + 3
        rename_end = rename_start + 3
        edit_start = rename_end + 1
        edit_end = edit_start + 3
        delete_start = edit_end + 1
        delete_end = delete_start + 3

        if x >= delete_start and x < delete_end:
            return "delete"
        elif x >= edit_start and x < edit_end:
            return "edit"
        elif x >= rename_start and x < rename_end:
            return "rename"
        return None

    def _update_hover(self, node_key: str | None, button: str | None) -> None:
        """Update hover state and refresh affected labels."""
        if node_key == self._hover_node_key and button == self._hover_button:
            return  # No change

        # Store old values and clear hover state first
        old_node_key = self._hover_node_key
        self._hover_node_key = None
        self._hover_button = None

        # Clear old hover (now _get_highlighted_button won't return hover for old node)
        if old_node_key and old_node_key in self.net_nodes:
            old_node = self.net_nodes[old_node_key]
            if old_node.data and old_node.data.net:
                # Only show focus highlight if this node has keyboard focus
                highlight = self._get_highlighted_button(old_node_key)
                old_node.set_label(self._get_net_label(old_node.data.net, highlight))

        # Set new hover state
        self._hover_node_key = node_key
        self._hover_button = button

        # Update new node's label
        if node_key and node_key in self.net_nodes:
            new_node = self.net_nodes[node_key]
            if new_node.data and new_node.data.net:
                highlight = self._get_highlighted_button(node_key)
                new_node.set_label(self._get_net_label(new_node.data.net, highlight))

    def on_key(self, event: Key) -> None:
        """Handle keyboard navigation for button focus."""
        # Clear focus when navigating up/down to different rows
        if event.key in ("up", "down"):
            self._clear_focus()
            return  # Let default handler process the navigation

        if not self.cursor_node or not self.cursor_node.data:
            return
        data = self.cursor_node.data

        # Toggle instrument nodes on Enter
        if data.node_type == "instrument" and event.key == "enter":
            self.cursor_node.toggle()
            event.stop()
            return

        # Only handle left/right/enter if we're on a net node
        if data.node_type != "net" or not data.net:
            return

        if event.key == "right":
            # Move focus: None -> rename -> edit -> delete
            if self._focus_button is None:
                self._focus_button = "rename"
            elif self._focus_button == "rename":
                self._focus_button = "edit"
            elif self._focus_button == "edit":
                self._focus_button = "delete"
            self._update_focus_display()
            event.stop()
        elif event.key == "left":
            # Move focus: delete -> edit -> rename -> None
            if self._focus_button == "delete":
                self._focus_button = "edit"
            elif self._focus_button == "edit":
                self._focus_button = "rename"
            elif self._focus_button == "rename":
                self._focus_button = None
                self._clear_focus()
            if self._focus_button:
                self._update_focus_display()
            event.stop()
        elif event.key == "enter":
            if self._focus_button == "delete":
                self.app.push_screen(ConfirmDelete(data.net))
                event.stop()
            elif self._focus_button == "edit":
                self.app.push_screen(EditDetailsDialog(data.net))
                event.stop()
            elif self._focus_button == "rename":
                self.app.push_screen(RenameDialog(data.net))
                event.stop()

    def on_mouse_move(self, event: MouseMove) -> None:
        """Track mouse position to highlight buttons on hover."""
        scroll_y = int(self.scroll_offset.y) if hasattr(self, "scroll_offset") else 0
        line = scroll_y + event.y - 1

        if line < 0:
            self._update_hover(None, None)
            return

        # Try to get the node at this line
        node = None
        if hasattr(self, "get_node_at_line"):
            try:
                node = self.get_node_at_line(line)
            except Exception:
                pass

        if node is None or node.data is None or node.data.node_type != "net":
            self._update_hover(None, None)
            return

        net = node.data.net
        if net is None:
            self._update_hover(None, None)
            return

        button = self._get_button_at_position(net, event.x)
        self._update_hover(net.key(), button)

    def on_leave(self, event: Leave) -> None:
        """Clear hover state when mouse leaves the widget."""
        self._update_hover(None, None)

    def on_click(self, event: Click) -> None:
        """Handle click events to toggle instrument nodes or trigger net actions."""
        # Get the line from scroll position + click y coordinate
        scroll_y = int(self.scroll_offset.y) if hasattr(self, "scroll_offset") else 0
        line = scroll_y + event.y - 1

        if line < 0:
            return

        # Try to get the node at this line
        node = None
        if hasattr(self, "get_node_at_line"):
            try:
                node = self.get_node_at_line(line)
            except Exception:
                pass

        if node is None or node.data is None:
            return

        data: TreeNodeData = node.data

        # Prevent default tree behavior and stop propagation
        event.stop()
        event.prevent_default()

        if data.node_type == "instrument":
            # Toggle expand/collapse on click
            node.toggle()
        elif data.node_type == "net" and data.net is not None:
            button = self._get_button_at_position(data.net, event.x)
            if button == "delete":
                self.app.push_screen(ConfirmDelete(data.net))
            elif button == "edit":
                self.app.push_screen(EditDetailsDialog(data.net))
            elif button == "rename":
                self.app.push_screen(RenameDialog(data.net))

    def watch_cursor_node(self, old_node, new_node) -> None:
        """Called when cursor moves to a different node - clear button focus."""
        self._clear_focus()

    def _on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Handle Enter key to toggle instrument nodes or show net action dialog."""
        node = event.node
        data: TreeNodeData | None = node.data

        if data is None:
            return

        if data.node_type == "instrument":
            # Toggle expand/collapse on Enter
            node.toggle()
            event.stop()
        elif data.node_type == "net" and data.net is not None:
            # If a button is focused, on_key handles Enter - don't show dialog
            if self._focus_button is not None:
                return
            # Show action dialog on Enter when no button is focused
            self.app.push_screen(NetActionDialog(data.net))
            event.stop()

    def build(self, nets: list[Net]) -> None:
        """Build tree from saved nets list."""
        self.root.remove_children()
        self.net_nodes.clear()

        # Filter to only saved nets
        saved_nets = [n for n in nets if n.saved]

        if not saved_nets:
            return

        # Group nets by instrument+address
        by_instrument: dict[str, list[Net]] = {}
        for n in saved_nets:
            key = f"{n.instrument}|{n.addr}"
            by_instrument.setdefault(key, []).append(n)

        # Build tree nodes
        for key in sorted(by_instrument.keys(), key=natural_sort_key):
            instrument, addr = key.split("|", 1)
            display_name = instrument.replace("_", " ")
            net_list = by_instrument[key]
            # Show address after instrument name
            if addr and addr != "NA":
                addr_display = addr if len(addr) <= 50 else addr[:45] + "..."
                label = f"[bold]{display_name}[/bold] [{addr_display}]"
            else:
                label = f"[bold]{display_name}[/bold]"

            inst_data = TreeNodeData(node_type="instrument", instrument_key=key)
            inst_node = self.root.add(label, data=inst_data)

            # Sort nets by type, then name
            for net in sorted(net_list, key=lambda x: (x.type, natural_sort_key(x.net))):
                net_key = net.key()
                net_label = self._get_net_label(net)
                net_data = TreeNodeData(node_type="net", net=net)
                node = inst_node.add_leaf(net_label, data=net_data)
                self.net_nodes[net_key] = node

            inst_node.expand()


class AddNetsTree(Tree[TreeNodeData]):
    """Tree widget for adding nets, organized by instrument with multi-select."""

    def __init__(self, **kwargs):
        super().__init__("Available Nets", **kwargs)
        self.show_root = False
        self.show_guides = True
        self.guide_depth = 6
        self.root.expand()
        self.chosen: set[str] = set()  # Track selected net keys
        self.custom_names: dict[str, str] = {}  # key -> custom name
        self.net_nodes: dict[str, TreeNode] = {}  # key -> node for updates
        self._hover_node_key: str | None = None  # Track which net node is hovered
        self._hover_button: bool = False  # Track if rename button is hovered
        self._focus_node_key: str | None = None  # Track which net has keyboard focus
        self._focus_button: str | None = None  # Track keyboard focus: "rename" or None

    def _get_net_label(self, net: Net, highlight_button: str | None = None) -> str:
        """Generate label for a net with optional button highlighting (hover or focus)."""
        net_key = net.key()
        custom_name = self.custom_names.get(net_key, net.net)
        selected = net_key in self.chosen
        status = "[SELECTED]" if selected else "[ADD]"
        rename_btn = "[reverse][✎][/reverse]" if highlight_button == "rename" else "[✎]"
        return f"{status} [bold]{custom_name}[/bold] | {net.type.upper()} | Ch: {net.chan}   {rename_btn}"

    def _get_highlighted_button(self, net_key: str) -> str | None:
        """Get which button should be highlighted for a given net (hover takes precedence)."""
        if self._hover_node_key == net_key and self._hover_button:
            return "rename"
        if self._focus_node_key == net_key and self._focus_button:
            return self._focus_button
        return None

    def _clear_focus(self) -> None:
        """Clear keyboard focus from any button."""
        if self._focus_node_key and self._focus_node_key in self.net_nodes:
            old_node = self.net_nodes[self._focus_node_key]
            if old_node.data and old_node.data.net:
                highlight = "rename" if self._hover_node_key == self._focus_node_key and self._hover_button else None
                old_node.set_label(self._get_net_label(old_node.data.net, highlight))
        self._focus_node_key = None
        self._focus_button = None

    def _update_focus_display(self) -> None:
        """Update the display of the currently focused net's buttons."""
        if self.cursor_node and self.cursor_node.data:
            data = self.cursor_node.data
            if data.node_type == "net" and data.net:
                net_key = data.net.key()
                if self._focus_node_key and self._focus_node_key != net_key:
                    self._clear_focus()
                self._focus_node_key = net_key
                highlight = self._get_highlighted_button(net_key)
                self.cursor_node.set_label(self._get_net_label(data.net, highlight))

    def _get_button_at_position(self, net: Net, x: int) -> bool:
        """Determine if the rename button is at the given x position."""
        # Calculate content length without the button
        net_key = net.key()
        custom_name = self.custom_names.get(net_key, net.net)
        selected = net_key in self.chosen
        status = "[SELECTED]" if selected else "[ADD]"
        content = f"{status} {custom_name} | {net.type.upper()} | Ch: {net.chan}   "
        # Button: [✎] is 3 chars

        content_end = len(content)
        # Add offset to shift clickable area right to match visual button
        offset = 4

        rename_start = content_end + 7
        rename_end = content_end + 3 + 7

        return x >= rename_start and x < rename_end

    def _update_hover(self, node_key: str | None, hover_button: bool) -> None:
        """Update hover state and refresh affected labels."""
        if node_key == self._hover_node_key and hover_button == self._hover_button:
            return  # No change

        # Store old values and clear hover state first
        old_node_key = self._hover_node_key
        self._hover_node_key = None
        self._hover_button = False

        # Clear old hover
        if old_node_key and old_node_key in self.net_nodes:
            old_node = self.net_nodes[old_node_key]
            if old_node.data and old_node.data.net:
                highlight = self._get_highlighted_button(old_node_key)
                old_node.set_label(self._get_net_label(old_node.data.net, highlight))

        # Set new hover state
        self._hover_node_key = node_key
        self._hover_button = hover_button

        # Update new node's label
        if node_key and node_key in self.net_nodes:
            new_node = self.net_nodes[node_key]
            if new_node.data and new_node.data.net:
                highlight = self._get_highlighted_button(node_key)
                new_node.set_label(self._get_net_label(new_node.data.net, highlight))

    def on_mouse_move(self, event: MouseMove) -> None:
        """Track mouse position to highlight button on hover."""
        scroll_y = int(self.scroll_offset.y) if hasattr(self, "scroll_offset") else 0
        line = scroll_y + event.y - 1

        if line < 0:
            self._update_hover(None, False)
            return

        # Try to get the node at this line
        node = None
        if hasattr(self, "get_node_at_line"):
            try:
                node = self.get_node_at_line(line)
            except Exception:
                pass

        if node is None or node.data is None or node.data.node_type != "net":
            self._update_hover(None, False)
            return

        net = node.data.net
        if net is None:
            self._update_hover(None, False)
            return

        hover_button = self._get_button_at_position(net, event.x)
        self._update_hover(net.key(), hover_button)

    def on_leave(self, event: Leave) -> None:
        """Clear hover state when mouse leaves the widget."""
        self._update_hover(None, False)

    def on_key(self, event: Key) -> None:
        """Handle keyboard navigation for button focus."""
        # Clear focus when navigating up/down to different rows
        if event.key in ("up", "down"):
            self._clear_focus()
            return  # Let default handler process the navigation

        if not self.cursor_node or not self.cursor_node.data:
            return
        data = self.cursor_node.data

        # Toggle instrument nodes on Enter
        if data.node_type == "instrument" and event.key == "enter":
            self.cursor_node.toggle()
            event.stop()
            return

        # Only handle left/right/enter if we're on a net node
        if data.node_type != "net" or not data.net:
            return

        if event.key == "right":
            # Move focus: None -> rename (only one button)
            if self._focus_button is None:
                self._focus_button = "rename"
                self._update_focus_display()
            event.stop()
        elif event.key == "left":
            # Move focus: rename -> None
            if self._focus_button == "rename":
                self._focus_button = None
                self._clear_focus()
            event.stop()
        elif event.key == "enter":
            if self._focus_button == "rename":
                # Find the AddScreen and show rename dialog
                for screen in self.app.screen_stack:
                    if hasattr(screen, 'add_tree') and screen.add_tree is self:
                        self.app.push_screen(RenameNewNetDialog(data.net, screen))
                        break
                event.stop()
            # If no button focused, let default handler toggle selection

    def watch_cursor_node(self, old_node, new_node) -> None:
        """Called when cursor moves to a different node - clear button focus."""
        self._clear_focus()

    def on_click(self, event: Click) -> None:
        """Handle click events to toggle nodes."""
        # Get the line from scroll position + click y coordinate
        # Subtract 1 to account for 0-based indexing vs 1-based display
        scroll_y = int(self.scroll_offset.y) if hasattr(self, "scroll_offset") else 0
        line = scroll_y + event.y - 1

        if line < 0:
            return

        # Try to get the node at this line
        node = None
        if hasattr(self, "get_node_at_line"):
            try:
                node = self.get_node_at_line(line)
            except Exception:
                pass

        if node is None or node.data is None:
            return

        data: TreeNodeData = node.data

        # Prevent default tree behavior and stop propagation
        event.stop()
        event.prevent_default()

        if data.node_type == "instrument":
            # Toggle expand/collapse on click
            node.toggle()
        elif data.node_type == "net" and data.net is not None:
            if self._get_button_at_position(data.net, event.x):
                # Find the AddScreen and show rename dialog
                for screen in self.app.screen_stack:
                    if hasattr(screen, 'add_tree') and screen.add_tree is self:
                        self.app.push_screen(RenameNewNetDialog(data.net, screen))
                        return
            else:
                # Toggle selection on click elsewhere
                self.toggle_net(data.net.key())

    def build(self, nets: list[Net]) -> None:
        """Build tree from nets list."""
        self.root.remove_children()
        self.net_nodes.clear()

        # Group nets by instrument+address
        by_instrument: dict[str, list[Net]] = {}
        for n in nets:
            key = f"{n.instrument}|{n.addr}"
            by_instrument.setdefault(key, []).append(n)

        # Build tree nodes
        for key in sorted(by_instrument.keys(), key=natural_sort_key):
            instrument, addr = key.split("|", 1)
            display_name = instrument.replace("_", " ")
            net_list = by_instrument[key]
            # Show address after instrument name
            if addr and addr != "NA":
                addr_display = addr if len(addr) <= 50 else addr[:45] + "..."
                label = f"[bold]{display_name}[/bold] [{addr_display}]"
            else:
                label = f"[bold]{display_name}[/bold]"

            inst_data = TreeNodeData(node_type="instrument", instrument_key=key)
            inst_node = self.root.add(label, data=inst_data)

            # Sort nets by type, then name
            for net in sorted(net_list, key=lambda x: (x.type, natural_sort_key(x.net))):
                net_key = net.key()
                net_label = self._get_net_label(net)
                net_data = TreeNodeData(node_type="net", net=net)
                node = inst_node.add_leaf(net_label, data=net_data)
                self.net_nodes[net_key] = node

            inst_node.expand()

    def toggle_net(self, net_key: str) -> bool:
        """Toggle selection of a net. Returns True if now selected."""
        if net_key in self.chosen:
            self.chosen.remove(net_key)
            selected = False
        else:
            self.chosen.add(net_key)
            selected = True

        # Update the node label
        node = self.net_nodes.get(net_key)
        if node and node.data and node.data.net:
            node.set_label(self._get_net_label(node.data.net))

        return selected

    def select_all(self) -> None:
        """Select all available nets."""
        self.chosen = set(self.net_nodes.keys())
        # Refresh all node labels to show as selected
        for net_key, node in self.net_nodes.items():
            if node.data and node.data.net:
                node.set_label(self._get_net_label(node.data.net))

    def update_net_name(self, net_key: str, new_name: str) -> None:
        """Update the custom name for a net."""
        self.custom_names[net_key] = new_name
        node = self.net_nodes.get(net_key)
        if node and node.data and node.data.net:
            node.set_label(self._get_net_label(node.data.net))


# ─────────────────── dialogs ────────────────────
class NetActionDialog(Screen):
    """Dialog to choose an action (Rename/Delete) for a saved net."""

    def __init__(self, net: Net) -> None:
        super().__init__()
        self.net = net

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("Net Actions", classes="dialog-title")
            yield Static(
                f"Name: {self.net.net}\n"
                f"Type: {self.net.type.upper()}\n"
                f"Instrument: {self.net.instrument.replace('_', ' ')}\n"
                f"Channel: {self.net.chan}\n"
                f"Address: {self.net.addr}",
                classes="dialog-content"
            )
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Rename", id="rename", variant="primary")
                yield Button("Edit Details", id="edit_details", variant="primary")
                yield Button("Delete", id="delete", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        app: NetApp = self.app  # type: ignore[attr-defined]

        if event.button.id == "cancel":
            app.pop_screen()
            return

        if event.button.id == "rename":
            app.pop_screen()
            app.push_screen(RenameDialog(self.net))
            return

        if event.button.id == "edit_details":
            app.pop_screen()
            app.push_screen(EditDetailsDialog(self.net))
            return

        if event.button.id == "delete":
            app.pop_screen()
            app.push_screen(ConfirmDelete(self.net))
            return


class EditDetailsDialog(Screen):
    """Dialog for editing net metadata (description, DUT connection, hints, tags)."""

    def __init__(self, net: Net) -> None:
        super().__init__()
        self.net = net

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("Edit Net Details", classes="dialog-title")
            yield Static(
                f"Net: {self.net.net}  ({self.net.type.upper()})",
                classes="dialog-content",
            )
            yield Label("Description:")
            self.desc_input = Input(
                placeholder="e.g., SPI flash (W25Q128) on DUT main board",
                id="desc_input",
                value=self.net.description,
            )
            yield self.desc_input

            yield Label("DUT Connection:")
            self.dut_input = Input(
                placeholder="e.g., MCU SPI1 peripheral (PA5-PA7, CS on PA4)",
                id="dut_input",
                value=self.net.dut_connection,
            )
            yield self.dut_input

            yield Label("Test Hints (one per line, comma-separated):")
            self.hints_input = Input(
                placeholder="e.g., Read JEDEC ID, Write/readback pattern",
                id="hints_input",
                value=", ".join(self.net.test_hints),
            )
            yield self.hints_input

            yield Label("Tags (comma-separated):")
            self.tags_input = Input(
                placeholder="e.g., flash, storage, boot-critical",
                id="tags_input",
                value=", ".join(self.net.tags),
            )
            yield self.tags_input

            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save_details", variant="success")

    def on_mount(self) -> None:
        self.desc_input.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        app: NetApp = self.app  # type: ignore[attr-defined]

        if event.button.id == "cancel":
            app.pop_screen()
            return

        description = self.desc_input.value.strip()
        dut_connection = self.dut_input.value.strip()
        hints_raw = self.hints_input.value.strip()
        tags_raw = self.tags_input.value.strip()

        test_hints = [h.strip() for h in hints_raw.split(",") if h.strip()] if hints_raw else []
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []

        self.net.description = description
        self.net.dut_connection = dut_connection
        self.net.test_hints = test_hints
        self.net.tags = tags

        net_data = {
            "name": self.net.net,
            "role": self.net.type,
            "address": self.net.addr,
            "instrument": self.net.instrument,
            "pin": self.net.chan,
            "description": description,
            "dut_connection": dut_connection,
            "test_hints": test_hints,
            "tags": tags,
        }

        try:
            _run_script(app.ctx, "net.py", app.dut, "save", json.dumps(net_data))
            app.show_success(f"Updated details for net '{self.net.net}'")
        except Exception as e:
            app.show_error(f"Failed to save details: {str(e)}")

        app._sync_saved_from_disk()
        app._refresh_table()
        app.pop_screen()


class ConfirmDelete(Screen):
    """Are-you-sure overlay for Delete."""

    def __init__(self, net: Net) -> None:
        super().__init__()
        self.net = net

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("Confirm Deletion", classes="dialog-title")
            yield Static(
                f"Are you sure you want to delete the saved net:\n\n"
                f"Name: {self.net.net}\n"
                f"Type: {self.net.type}\n"
                f"Instrument: {self.net.instrument}\n\n"
                f"This action cannot be undone.",
                classes="dialog-content"
            )
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Delete", id="confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        app: NetApp = self.app  # type: ignore[attr-defined]

        if event.button.id == "cancel":
            app.pop_screen()
            return

        app.pop_screen()
        # Delete this net via net.py script
        try:
            _run_script(app.ctx, "net.py", app.dut, "delete", self.net.net, self.net.type)
            app.show_success(f"Successfully deleted net '{self.net.net}'")
        except Exception as e:
            app.show_error(f"Failed to delete net: {str(e)}")
            return

        if self.net in app.nets:
            app.nets.remove(self.net)
        auto_name = f"{self.net.type}{self.net.chan}"
        duplicate = next(
            (n for n in app.nets if (n.type, n.instrument, n.chan, n.addr) ==
             (self.net.type, self.net.instrument, self.net.chan, self.net.addr)),
            None,
        )
        if duplicate is None:
            app.nets.append(Net(
                instrument=self.net.instrument,
                chan=self.net.chan,
                type=self.net.type,
                net=auto_name,
                addr=self.net.addr,
                saved=False,
            ))
        app._sync_saved_from_disk()
        app._refresh_table()

class RenameDialog(Screen):
    """Prompt + text box to enter a new name."""

    def __init__(self, net: Net) -> None:
        super().__init__()
        self.net = net
        self.input: Input

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("Rename Net", classes="dialog-title")
            yield Static(
                f"Current name: {self.net.net}\n"
                f"Type: {self.net.type}\n"
                f"Instrument: {self.net.instrument}",
                classes="dialog-content"
            )
            self.input = Input(
                placeholder="Enter new net name...",
                id="rename_input",
                value=self.net.net
            )
            yield self.input
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Rename", id="confirm", variant="success")

    def on_mount(self) -> None:
        self.input.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle Cancel / Confirm in the rename dialog."""
        app: NetApp = self.app  # type: ignore[attr-defined]

        if event.button.id == "cancel":
            app.pop_screen()
            return

        new_name = self.input.value.strip()
        if not new_name or new_name == self.net.net:
            app.pop_screen()
            return

        if any(n is not self.net and n.saved and n.net.lower() == new_name.lower() for n in app.nets):
            self.input.placeholder = "That name is already used!"
            self.input.value = ""
            self.input.focus()
            return

        app.pop_screen()
        # Rename the net via net.py script
        try:
            _run_script(
                app.ctx,
                "net.py",
                app.dut,
                "rename",
                self.net.net,
                new_name
            )
            app.show_success(f"Successfully renamed net to '{new_name}'")
        except Exception as e:
            app.show_error(f"Failed to rename net: {str(e)}")
            return

        # Update the net name locally
        self.net.net = new_name
        self.net._uid = _uid(self.net.instrument, self.net.chan, self.net.type, new_name)

        placeholder = next(
            (n for n in app.nets if not n.saved and
             (n.type, n.instrument, n.chan, n.addr) ==
             (self.net.type, self.net.instrument, self.net.chan, self.net.addr)),
            None,
        )
        if placeholder is not None:
            app.nets.remove(placeholder)

        app._sync_saved_from_disk()
        app._refresh_table()

class RenameNewNetDialog(Screen):
    """Prompt + text box to enter a new name for an unsaved net in AddScreen."""

    def __init__(self, net: Net, add_screen: "AddScreen") -> None:
        super().__init__()
        self.net = net
        self.add_screen = add_screen
        self.input: Input

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("Rename Net Before Adding", classes="dialog-title")
            # Get current name from tree's custom_names if available
            if self.add_screen.add_tree:
                current_name = self.add_screen.add_tree.custom_names.get(self.net.key(), self.net.net)
            else:
                current_name = self.add_screen.custom_names.get(self.net.key(), self.net.net)
            yield Static(
                f"Current name: {current_name}\n"
                f"Type: {self.net.type}\n"
                f"Instrument: {self.net.instrument}",
                classes="dialog-content"
            )
            self.input = Input(
                placeholder="Enter new net name...",
                id="rename_new_input",
                value=current_name
            )
            yield self.input
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Rename", id="confirm", variant="success")

    def on_mount(self) -> None:
        self.input.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle Cancel / Confirm in the rename dialog."""
        app: NetApp = self.app  # type: ignore[attr-defined]

        if event.button.id == "cancel":
            app.pop_screen()
            return

        new_name = self.input.value.strip()
        # Get current name from tree's custom_names if available
        if self.add_screen.add_tree:
            current_name = self.add_screen.add_tree.custom_names.get(self.net.key(), self.net.net)
        else:
            current_name = self.add_screen.custom_names.get(self.net.key(), self.net.net)

        if not new_name or new_name == current_name:
            app.pop_screen()
            return

        # Check if the name conflicts with any saved net
        if any(n.saved and n.net.lower() == new_name.lower() for n in app.nets):
            self.input.placeholder = "That name is already used by a saved net!"
            self.input.value = ""
            self.input.focus()
            return

        # Check if the name conflicts with other custom names in the add screen
        custom_names = self.add_screen.add_tree.custom_names if self.add_screen.add_tree else self.add_screen.custom_names
        for key, custom in custom_names.items():
            if key != self.net.key() and custom.lower() == new_name.lower():
                self.input.placeholder = "That name is already used in add list!"
                self.input.value = ""
                self.input.focus()
                return

        # Update the tree display
        if self.add_screen.add_tree:
            self.add_screen.add_tree.update_net_name(self.net.key(), new_name)

        app.pop_screen()

class JLinkDeviceTypeDialog(Screen):
    def __init__(
        self,
        dut: str,
        net_name: str,
        address: str,
        callback: Callable[[bool, str | None], None]
    ):
        super().__init__()
        self.dut = dut
        self.net_name = net_name
        self.address = address
        self.callback = callback
        self.input = Input(placeholder=f"Enter device type for {address}", id="jlink_type")

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static("J-Link Device Configuration", classes="dialog-title")
            yield Static(
                f"Net: {self.net_name}\n"
                f"Address: {self.address}\n\n"
                f"Please specify the device type for this J-Link debugger.",
                classes="dialog-content"
            )
            yield self.input
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Configure", id="confirm", variant="success")

    @on(Button.Pressed)
    def _on_jlink_type_entered(self, event: Button.Pressed):
        if event.button.id == "cancel":
            self.app.pop_screen()
            self.callback(False, None)
            return

        jlink_device_type = self.input.value.strip()
        if not jlink_device_type:
            return  # Prevent empty submission

        self.app.pop_screen()
        self.callback(True, jlink_device_type)

class AddScreen(Screen):
    """Dialog that lets the user multi-select nets to add (unsaved only)."""

    def __init__(self, nets: list[Net], multi_labjack: bool = False) -> None:
        super().__init__()
        self.nets: list[Net] = nets
        self.multi_labjack: bool = multi_labjack
        self.chosen: set[str] = set()
        self.custom_names: dict[str, str] = {}  # key -> custom_name

    def _row_allowed(self, n: Net) -> bool:
        """
        True ⇒ show row in *Add Nets* list (apply various filters).
        """
        if n.saved:
            return False

        # Hide unsaved nets if a saved net already exists for the same physical channel
        # Key: (type, instrument, channel, address)
        if any(s.saved and s.type == n.type and s.instrument == n.instrument and s.chan == n.chan and s.addr == n.addr for s in self.nets):
            return False

        # Hide nets for single-channel instruments if this role is already saved
        if n.instrument in _SINGLE_CHANNEL_INST:
            if any(s.saved and s.instrument == n.instrument and s.addr == n.addr and s.type == n.type for s in self.nets):
                return False
        # Prevent multiple debug nets with same type/instrument/address
        if n.type == "debug":
            if any(s.saved and s.type == "debug" and s.instrument == n.instrument and s.addr == n.addr for s in self.nets):
                return False
        return True

    def _get_addable_nets(self) -> tuple[list["Net"], list[str]]:
        """
        Build (rows, warnings) for the *Add Nets* screen.

        Rules:
        1. If >1 LabJack_T7 or >1 Acroname_8Port or 4Port is plugged in,
           no nets from that family are shown (with a warning).
        2. Otherwise, nets for the first hub only are listed.
        3. Single-channel instruments may have only one net per address – duplicates are hidden and warned.
        """
        warnings: list[str] = []

        # Deduplicate nets by (instrument, channel, address) - NOT by name
        # This ensures saved "hahahah" on channel 2 deduplicates with unsaved "usb2" on channel 2
        # Prioritize unsaved nets over saved ones when deduplicating
        uniq: dict[tuple[str, str, str, str], Net] = {}
        for n in self.nets:
            # Key by (type, instrument, channel, address) - excludes name
            dedup_key = (n.type, n.instrument, n.chan, n.addr)
            # Only add if key doesn't exist, OR if this net is unsaved and existing is saved
            if dedup_key not in uniq or (not n.saved and uniq[dedup_key].saved):
                uniq[dedup_key] = n
        nets: list[Net] = list(uniq.values())

        # Detect multiple physical hubs of same type (LabJack, Acroname)
        # Key by (instrument, address) to distinguish different physical devices
        chan_seen: dict[tuple[str, str], set[str]] = defaultdict(set)
        duplicate_hubs: set[tuple[str, str]] = set()
        for n in nets:
            if n.instrument in _MULTI_HUBS:
                device_key = (n.instrument, n.addr)
                if n.chan in chan_seen[device_key]:
                    # Same channel seen twice on same device - this is expected (saved + unsaved)
                    # Only block if we see the SAME channel on DIFFERENT devices
                    pass
                chan_seen[device_key].add(n.chan)

        # Check if we have multiple devices of the same type (different addresses)
        device_counts: dict[str, set[str]] = defaultdict(set)
        for n in nets:
            if n.instrument in _MULTI_HUBS:
                device_counts[n.instrument].add(n.addr)

        # Block instrument families that have multiple physical devices
        blocked_families: set[str] = set()
        for inst, addrs in device_counts.items():
            if len(addrs) > 1:
                blocked_families.add(inst)

        remaining: list[Net] = []
        dup_single: set[tuple[str, str]] = set()

        for n in nets:
            if n.instrument in blocked_families:
                continue
            if n.instrument in _SINGLE_CHANNEL_INST and is_single_channel_taken(self.nets, n.instrument, n.addr, n.type):
                dup_single.add((n.instrument, n.addr))
                continue
            if n.type == "debug" and n.chan != "DEVICE_TYPE":
                continue
            if not self._row_allowed(n):
                continue
            remaining.append(n)

        for inst in sorted(blocked_families, key=natural_sort_key):
            warnings.append(f"Multiple {inst} devices detected - unplug extras before adding nets.")
        for inst, addr in sorted(dup_single, key=lambda x: natural_sort_key(x[0])):
            warnings.append(f"{inst} at {addr} already has a net.")

        return remaining, warnings

    def compose(self) -> ComposeResult:
        remaining, warnings = self._get_addable_nets()

        # Store the nets that are actually displayed for rename lookups
        # CRITICAL: Only store unsaved nets to prevent delete dialogs
        self.displayed_nets = {n.key(): n for n in remaining if not n.saved}

        # CRITICAL: Filter to only unsaved nets - multiple safety checks
        unsaved_only = [n for n in remaining if not n.saved]

        with Vertical(classes="dialog"):
            yield Static("Add Available Nets", classes="dialog-title")

            if warnings:
                yield Static("Warnings:", classes="dialog-content")
                for w in warnings:
                    yield Static(f"• {w}", classes="warning")

            if unsaved_only:
                yield Static(f"Found {len(unsaved_only)} available nets.", classes="dialog-content")
                self.add_tree = AddNetsTree(id="add_tree")
                self.add_tree.build(unsaved_only)
                yield self.add_tree
            else:
                self.add_tree = None
                yield Static("No Available Nets to Add", classes="placeholder")
                yield Static("All compatible nets are already saved or unavailable.", classes="info")

            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="add-cancel")
                if unsaved_only:
                    yield Button("Add Selected", id="add-confirm")
                    yield Button("Add All Available", id="select-all")
                else:
                    yield Button("Close", id="close", variant="primary")

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Handle tree node selection for toggling nets."""
        node = event.node
        data: TreeNodeData | None = node.data

        if data is None:
            return

        # Stop event propagation
        event.stop()

        if data.node_type == "instrument":
            # Toggle expand/collapse
            node.toggle()
            return

        if data.node_type == "net" and data.net is not None:
            # If a button is focused, on_key handles Enter - don't toggle
            if self.add_tree and self.add_tree._focus_button is not None:
                return
            # Toggle selection
            net_key = data.net.key()
            if self.add_tree:
                self.add_tree.toggle_net(net_key)
                # Sync chosen set with tree's chosen set
                self.chosen = self.add_tree.chosen

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle *Cancel* / *Confirm* / *Rename* buttons in the Add-dialog."""
        main: NetApp = self.app

        if event.button.id == "select-all":
            if self.add_tree:
                self.add_tree.select_all()
                self.chosen = self.add_tree.chosen
            return

        # Sync chosen and custom_names from tree before any action
        if self.add_tree:
            self.chosen = self.add_tree.chosen
            self.custom_names = self.add_tree.custom_names

        if event.button.id in ("cancel", "close", "add-cancel"):
            main.pop_screen()
            return

        if not self.chosen:
            main.pop_screen()
            return

        # Collect all selected Net objects
        selected_nets = [next(n for n in main.nets if n.key() == k) for k in self.chosen]

        # Separate debug and normal nets
        debug_nets = [n for n in selected_nets if n.type == "debug"]
        normal_nets = [n for n in selected_nets if n.type != "debug"]

        # Check for single-channel device conflicts (one net per instrument+address+role)
        single_cnt: dict[tuple[str, str, str], int] = defaultdict(int)
        for s in main.nets:
            if s.saved and s.instrument in _SINGLE_CHANNEL_INST:
                single_cnt[(s.instrument, s.addr, s.type)] += 1
        for n in selected_nets:
            if n.instrument in _SINGLE_CHANNEL_INST:
                single_cnt[(n.instrument, n.addr, n.type)] += 1
        conflicts = [(inst, addr) for (inst, addr, _role), cnt in single_cnt.items() if cnt > 1]
        if conflicts:
            parts = [f"{inst} at {addr}" for inst, addr in conflicts]
            msg = "Only one net per role may be added per " + ", ".join(parts) + "."
            try:
                self.query_one("#keithley_hint", Static).update(msg)
            except NoMatches:
                self.mount(Static(msg, id="keithley_hint", classes="warning"))
            return
        else:
            try:
                self.query_one("#keithley_hint", Static).update("")
            except NoMatches:
                pass

        # If no debug nets selected, save immediately using batch save
        if not debug_nets:
            if _save_nets_batch(main.ctx, main.dut, selected_nets, self.custom_names):
                main.show_success(f"Successfully added {len(selected_nets)} nets")
            else:
                main.show_error("Failed to save some nets")
            # Refresh saved nets and update UI
            main._sync_saved_from_disk()
            main._refresh_table()
            main.pop_screen()
            return

        # If there are debug nets, prompt for each J-Link device type
        self._pending_debug_nets = debug_nets
        self._pending_normal_nets = normal_nets
        self._debug_idx = 0

        def handle_jlink_complete(success: bool, device_type: str | None):
            if not success or not device_type:
                main.pop_screen(to=self)
                return
            # Set the J-Link device type as the "channel"
            self._pending_debug_nets[self._debug_idx].chan = device_type
            self._debug_idx += 1
            if self._debug_idx < len(self._pending_debug_nets):
                prompt_next()
                return
            # All debug prompts done – save all pending nets using batch save
            all_nets_to_save = self._pending_normal_nets + self._pending_debug_nets
            if _save_nets_batch(main.ctx, main.dut, all_nets_to_save, self.custom_names):
                main.show_success(f"Successfully added {len(all_nets_to_save)} nets")
            else:
                main.show_error("Failed to save some nets")
            main._sync_saved_from_disk()
            main._refresh_table()
            main.pop_screen()

        def prompt_next():
            n = self._pending_debug_nets[self._debug_idx]
            main.push_screen(JLinkDeviceTypeDialog(main.dut, n.net, n.addr, handle_jlink_complete))

        prompt_next()

class ConfirmDeleteAll(Screen):
    """Are-you-sure overlay for *Delete All Nets*."""

    def compose(self) -> ComposeResult:
        dut_id = getattr(self.app, "dut", "this DUT")  # type: ignore[attr-defined]
        with Vertical(classes="dialog"):
            yield Static("Confirm Delete All", classes="dialog-title")
            yield Static(
                f"WARNING: This will delete ALL saved nets on device {dut_id}.\n\n"
                f"This action is permanent and cannot be undone.\n\n"
                f"Are you sure you want to continue?",
                classes="dialog-content warning"
            )
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Delete All", id="confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        app: NetApp = self.app  # type: ignore[attr-defined]

        if event.button.id == "cancel":
            app.pop_screen()
            return

        # Delete all saved nets via net.py script
        try:
            _run_script(app.ctx, "net.py", app.dut, "delete-all")
            app.show_success("Successfully deleted all nets")
        except Exception as e:
            app.show_error(f"Failed to delete all nets: {str(e)}")
        app._sync_saved_from_disk()
        app._refresh_table()
        app.pop_screen()

# ────────────────────────── main app ───────────────────────────────
class NetApp(App):
    TITLE = "Lager Nets TUI"

    CSS = """
    /* Global App Styling */
    App {
        background: $surface-darken-2;
        color: $text;
    }

    /* Header Styling */
    Header {
        background: hotpink;
        color: black;
        text-style: bold;
        height: 5;
        text-align: center;
        content-align: center middle;
    }

    /* Main Title */
    .title {
        text-style: bold;
        color: $accent;
        background: $surface;
        height: auto;
        text-align: left;
        margin: 0;
        border: solid hotpink;
        padding: 0 1;
    }

    /* Add Nets Tree Styling */
    AddNetsTree {
        background: $surface;
        border: solid hotpink;
        height: 1fr;
        max-height: 25;
        margin: 1 0;
    }

    AddNetsTree > .tree--guides {
        color: hotpink;
    }

    AddNetsTree > .tree--cursor {
        background: $accent 30%;
    }

    AddNetsTree > .tree--highlight {
        background: hotpink 20%;
    }

    /* Saved Nets Tree Styling */
    SavedNetsTree {
        background: $surface;
        border: solid hotpink;
        height: 1fr;
        margin: 0 0 1 0;
    }

    SavedNetsTree > .tree--guides {
        color: hotpink;
    }

    SavedNetsTree > .tree--cursor {
        background: $accent 30%;
    }

    SavedNetsTree > .tree--highlight {
        background: hotpink 20%;
    }

    /* Zebra striping for tree rows */
    SavedNetsTree > .tree--row:odd {
        background: $surface-lighten-1;
    }

    SavedNetsTree > .tree--row:even {
        background: $surface;
    }

    AddNetsTree > .tree--row:odd {
        background: $surface-lighten-1;
    }

    AddNetsTree > .tree--row:even {
        background: $surface;
    }

    /* Button Styling */
    Button {
        margin: 0 1;
        min-width: 16;
        height: 3;
        text-style: bold;
        border: none;
        padding: 0 1;
    }

    Button:hover {
        background: white !important;
        color: black !important;
    }

    #exit_btn {
        background: hotpink;
        color: black;
    }

    /* Button Container Row */
    .button-row {
        height: 5;
        width: 100%;
        margin: 1 0;
        dock: bottom;
    }

    .button-container-left {
        width: 1fr;
    }

    .button-container-center {
        width: auto;
        align: center middle;
    }

    .button-container-right {
        width: 1fr;
        align: right middle;
        margin-right: 2;
    }

    /* Input Styling */
    Input {
        background: $surface;
        border: solid hotpink;
        margin: 1 0;
        height: 3;
    }

    Input:focus {
        border: solid $accent;
    }

    /* Static Text Styling */
    .placeholder {
        text-align: center;
        color: $text-muted;
        text-style: italic;
        height: 3;
        content-align: center middle;
    }

    .empty-state {
        height: 1fr;
        margin: 0 0 1 0;
        border: solid hotpink;
        align: center middle;
    }

    .placeholder-message {
        text-align: center;
        color: $text-muted;
        text-style: italic;
        content-align: center middle;
        height: auto;
    }

    .placeholder-arrow {
        text-align: center;
        color: $text-muted;
        height: auto;
        margin-bottom: 1;
    }

    .warning {
        background: $warning;
        color: $text;
        text-style: bold;
        padding: 1;
        margin: 1 0;
        border-left: thick $error;
    }

    .error {
        background: $error;
        color: $text;
        text-style: bold;
        padding: 1;
        margin: 1 0;
        border-left: thick $error-darken-1;
    }

    .info {
        background: hotpink 20%;
        color: $text;
        padding: 1;
        margin: 1 0;
        border-left: thick hotpink;
    }

    .success {
        background: $success 20%;
        color: $text;
        padding: 1;
        margin: 1 0;
        border-left: thick $success;
    }

    /* Footer Styling */
    Footer {
        background: $surface-darken-1;
        color: $text-muted;
        height: 1;
    }

    /* Modal/Dialog Styling */
    Screen {
        align: center middle;
    }

    .dialog {
        background: $surface;
        border: solid hotpink;
        padding: 1;
        margin: 1;
        width: 90%;
        height: 90%;
        max-height: 90%;
    }

    .dialog-title {
        text-style: bold;
        color: hotpink;
        text-align: center;
        margin-bottom: 1;
    }

    .dialog-content {
        margin: 1 0;
    }

    .dialog-buttons {
        height: 5;
        align: center middle;
        margin-top: 1;
        dock: bottom;
        padding: 1 0;
    }

    /* Action buttons specific styling */
    #add_btn {
        background: mediumaquamarine;
        color: black;
    }

    #del_all_btn {
        background: coral;
        color: black;
    }

    #exit_btn {
        min-width: 12;
    }

    #cancel {
        background: mediumaquamarine;
        color: black;
    }

    #confirm {
        background: coral;
        color: black;
    }

    #add-cancel {
        background: coral;
        color: black;
    }

    #add-confirm {
        background: mediumaquamarine;
        color: black;
    }

    #select-all {
        background: mediumaquamarine;
        color: black;
        margin-left: 3;
    }

    #rename {
        background: khaki;
        color: black;
    }

    #delete {
        background: coral;
        color: black;
    }

    /* Add Nets Table Styling */
    #add_tbl {
        height: 1fr;
        max-height: 25;
        overflow-y: auto;
        margin: 1 0;
    }

    /* Loading/Progress Indicators */
    .loading {
        text-align: center;
        color: $accent;
        text-style: bold;
    }

    .status-indicator {
        width: 3;
        height: 1;
        margin: 0 1;
    }

    .status-saved {
        background: $success;
        color: $text;
    }

    .status-pending {
        background: $warning;
        color: $text;
    }

    .status-error {
        background: $error;
        color: $text;
    }


    /* Zebra striping for tables */
    .zebra-even {
        background: $surface-lighten-1;
    }

    .zebra-odd {
        background: $surface;
    }
    """

    BINDINGS = [("q", "quit", "Quit"), ("ctrl+c", "quit", "Exit")]

    def __init__(self, ctx: click.Context, dut: str,
                 inst_list: list[dict[str, str]],
                 nets: list[Net], multi_labjack: bool = False):
        super().__init__()
        self.ctx, self.dut, self.nets = ctx, dut, nets
        self.inst_list = inst_list
        self.multi_labjack = multi_labjack

    def compose(self) -> ComposeResult:
        yield Header()

        # Title for saved nets section
        yield Static("Saved Nets", classes="title")

        # Main tree view for saved nets (organized by instrument)
        self.saved_tree = SavedNetsTree(id="saved_tree")
        yield self.saved_tree

        with Vertical(classes="empty-state") as self.no_saved:
            yield Static("No Saved Nets Available\n\nPress + Add Nets below to get started\n", classes="placeholder-message")
            yield Static("│\n│\n│\n│\n▼", classes="placeholder-arrow")
        self.no_saved.display = False

        # action buttons layout
        with Horizontal(classes="button-row"):
            # Left spacer
            self.left_spacer = Horizontal(classes="button-container-left")
            with self.left_spacer:
                pass
            # Center buttons container
            with Horizontal(classes="button-container-center"):
                self.add_btn = Button("+ Add Nets", id="add_btn", variant="primary")
                self.del_all_btn = Button("Delete All Nets", id="del_all_btn", variant="error")
                self.del_all_btn.display = False  # shown only when nets exist
                yield self.add_btn
                yield self.del_all_btn
            # Right button container
            self.right_container = Horizontal(classes="button-container-right")
            with self.right_container:
                self.exit_btn = Button("Exit", id="exit_btn", variant="success")
                yield self.exit_btn

        yield Footer()

    def on_mount(self) -> None:
        self.show_loading("Loading saved nets...")
        self._sync_saved_from_disk()
        self._refresh_table()
        self.hide_loading()

    def show_loading(self, message: str) -> None:
        """Show loading message."""
        if hasattr(self, 'loading_msg'):
            self.loading_msg.update(message)
        else:
            self.loading_msg = Static(message, classes="loading")
            self.mount(self.loading_msg)

    def hide_loading(self) -> None:
        """Hide loading message."""
        if hasattr(self, 'loading_msg'):
            self.loading_msg.remove()

    def _refresh_table(self) -> None:
        """Re-populate the saved nets tree & toggle *Delete All* visibility."""
        saved = [n for n in self.nets if n.saved]
        if saved:
            self.saved_tree.display = True
            self.no_saved.display = False
            self.del_all_btn.display = True
            self.saved_tree.build(self.nets)
        else:
            self.saved_tree.display = False
            self.no_saved.display = True
            self.del_all_btn.display = False

    def _sync_saved_from_disk(self) -> None:
        # Retrieve saved nets from disk via net.py list
        try:
            output = _run_script(self.ctx, "net.py", self.dut, "list")
            saved_from_disk = _parse_backend_json_tui(output) if output.strip() else []
        except (json.JSONDecodeError, AttributeError) as e:
            # Show error message to user but continue with empty list
            self.show_error(f"Error loading saved nets: {str(e)}")
            saved_from_disk = []
        except Exception as e:
            # Handle any other unexpected errors
            self.show_error(f"Unexpected error: {str(e)}")
            saved_from_disk = []

        # Keep unsaved nets and replace saved nets with those from disk
        self.nets = [n for n in self.nets if not n.saved] + [
            Net(
                instrument=rec.get("instrument", "NA"),
                chan=rec.get("pin", "NA"),
                type=rec.get("role", "NA"),
                net=rec.get("name"),
                addr=rec.get("address", "NA"),
                saved=True,
                has_script=bool(rec.get("jlink_script")),
                description=rec.get("description", ""),
                dut_connection=rec.get("dut_connection", ""),
                test_hints=rec.get("test_hints", []),
                tags=rec.get("tags", []),
            ) for rec in saved_from_disk
        ]
        self._ensure_autogen_unsaved()

    def show_error(self, message: str) -> None:
        """Show error message to user."""
        error_msg = Static(f"Error: {message}", classes="error")
        self.mount(error_msg)
        # Auto-remove error after 5 seconds
        self.set_timer(5.0, lambda: error_msg.remove())

    def show_success(self, message: str) -> None:
        """Show success message to user."""
        success_msg = Static(message, classes="success")
        self.mount(success_msg)
        # Auto-remove success after 3 seconds
        self.set_timer(3.0, lambda: success_msg.remove())

    def _ensure_autogen_unsaved(self) -> None:
        # Track which channels have unsaved placeholders
        unsaved_keys = {(n.type, n.instrument, n.chan, n.addr) for n in self.nets if not n.saved}
        role_counter: dict[str, int] = defaultdict(int)
        idx_re = re.compile(r"^([A-Za-z]+)(\d+)$")

        # Track highest auto-index for each role already present
        for n in self.nets:
            m = idx_re.match(n.net)
            if m and _first_word(n.type) == m.group(1):
                role_counter[n.type] = max(role_counter[n.type], int(m.group(2)))

        # Add placeholder unsaved nets for ALL channels (even if saved versions exist)
        for dev in self.inst_list:
            instr = dev.get("name", "Unknown")
            addr = dev.get("address", "NA")
            channel_map = dev.get("channels", {})
            for role, channels in (channel_map or {}).items():
                # Sort channels to ensure consistent ordering
                sorted_channels = sorted(channels, key=lambda ch: str(ch))
                for ch in sorted_channels:
                    key = (role, instr, ch, addr)
                    # Only skip if an UNSAVED placeholder already exists
                    if key in unsaved_keys:
                        continue
                    role_counter[role] += 1
                    auto_name = f"{_first_word(role)}{role_counter[role]}"
                    self.nets.append(Net(instr, ch, role, auto_name, addr, saved=False))
                    unsaved_keys.add(key)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Handle tree node selection for saved nets - show action dialog."""
        node = event.node
        data: TreeNodeData | None = node.data

        if data is None:
            return

        # Only handle net nodes from the saved tree (not AddNetsTree)
        if not isinstance(event.tree, SavedNetsTree):
            return

        event.stop()

        if data.node_type == "instrument":
            # Toggle expand/collapse
            node.toggle()
            return

        if data.node_type == "net" and data.net is not None:
            # Show action dialog for this net
            self.push_screen(NetActionDialog(data.net))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle *Add Nets*, *Delete All Nets*, and *Save & Exit* buttons."""
        if event.button.id == "add_btn":
            self.push_screen(AddScreen(self.nets, self.multi_labjack))
            return
        if event.button.id == "del_all_btn":
            self.push_screen(ConfirmDeleteAll())
            return
        if event.button.id == "exit_btn":
            self.action_quit()
            return

    def _get_visible_buttons(self) -> list[Button]:
        """Get list of visible action buttons in order."""
        buttons = []
        if self.add_btn.visible:
            buttons.append(self.add_btn)
        if self.del_all_btn.visible:
            buttons.append(self.del_all_btn)
        if self.exit_btn.visible:
            buttons.append(self.exit_btn)
        return buttons

    def on_key(self, event: Key) -> None:
        """Handle arrow key navigation between tree and buttons."""
        focused = self.focused

        # If a button is focused, handle left/right/up navigation
        if isinstance(focused, Button):
            buttons = self._get_visible_buttons()
            if not buttons:
                return

            try:
                idx = buttons.index(focused)
            except ValueError:
                return

            if event.key == "left":
                if idx > 0:
                    buttons[idx - 1].focus()
                    event.stop()
            elif event.key == "right":
                if idx < len(buttons) - 1:
                    buttons[idx + 1].focus()
                    event.stop()
            elif event.key == "up":
                # Move focus back to the tree
                self.saved_tree.focus()
                event.stop()

        # If tree is focused and down is pressed, check if we should move to buttons
        elif isinstance(focused, SavedNetsTree):
            if event.key == "down":
                # Check if we're at the last visible node in the tree
                tree = self.saved_tree
                if tree.cursor_node:
                    # Check if current node is a leaf with no next sibling
                    # and its parent has no next sibling (i.e., we're at the bottom)
                    node = tree.cursor_node
                    at_bottom = False

                    # If it's a leaf node, check if it's the last child of its parent
                    if not node.children or not node.is_expanded:
                        # Walk up to find if we're at the very end
                        current = node
                        at_bottom = True
                        while current.parent:
                            siblings = list(current.parent.children)
                            if current != siblings[-1]:
                                at_bottom = False
                                break
                            current = current.parent

                    if at_bottom:
                        buttons = self._get_visible_buttons()
                        if buttons:
                            buttons[0].focus()
                            event.stop()

    def action_quit(self) -> None:
        self.exit()

def launch_tui(ctx: click.Context, dut: str) -> None:
    # Query connected instruments and saved nets
    try:
        inst_result = _run_script(ctx, "query_instruments.py", dut)
        inst_list = json.loads(inst_result) if inst_result.strip() else []
    except (json.JSONDecodeError, AttributeError):
        inst_list = []

    try:
        saved_result = _run_script(ctx, "net.py", dut, "list")
        saved_list = _parse_backend_json_tui(saved_result) if saved_result.strip() else []
    except (json.JSONDecodeError, AttributeError):
        saved_list = []

    # Sort instruments by their first channel to ensure consistent ordering
    # This ensures UART devices are processed in order (ttyUSB0, ttyUSB1, ttyUSB2, etc.)
    def sort_key(dev):
        channels = dev.get("channels", {})
        # Get the first channel from any role, or empty string if none
        for role_channels in channels.values():
            if role_channels:
                return str(sorted(role_channels)[0])
        return ""

    inst_list.sort(key=sort_key)

    role_counter: dict[str, int] = defaultdict(int)
    nets: list[Net] = []
    idx_re = re.compile(r"^([A-Za-z]+)(\d+)$")

    # First, load saved nets and track highest number for each role
    for rec in saved_list:
        net_name = rec.get("name", "")
        role = rec.get("role", "NA")

        # Track highest auto-index for each role already present in saved nets
        m = idx_re.match(net_name)
        if m and _first_word(role) == m.group(1):
            role_counter[role] = max(role_counter[role], int(m.group(2)))

        nets.append(Net(
            instrument=rec.get("instrument", "NA"),
            chan=rec.get("pin", "NA"),
            type=role,
            net=net_name,
            addr=rec.get("address", "NA"),
            saved=True,
            has_script=bool(rec.get("jlink_script")),
            description=rec.get("description", ""),
            dut_connection=rec.get("dut_connection", ""),
            test_hints=rec.get("test_hints", []),
            tags=rec.get("tags", []),
        ))

    # Now generate auto-names for new devices, continuing from highest saved number
    for dev in inst_list:
        instr = dev.get("name", "Unknown")
        addr = dev.get("address", "NA")
        channel_map = dev.get("channels", {})
        for role, channels in (channel_map or {}).items():
            # Sort channels to ensure consistent ordering (e.g., /dev/ttyUSB0 before /dev/ttyUSB1)
            sorted_channels = sorted(channels, key=lambda ch: str(ch))
            for ch in sorted_channels:
                # Check if this exact net already exists - skip counter increment if so
                already_exists = any(
                    n.type == role
                    and n.instrument == instr
                    and str(n.chan) == str(ch)
                    and n.addr == addr
                    and n.saved
                    for n in nets
                )

                if not already_exists:
                    role_counter[role] += 1

                auto_name = f"{_first_word(role)}{role_counter[role]}"
                nets.append(Net(instr, ch, role, auto_name, addr, saved=False))

    # Launch the Textual TUI
    NetApp(ctx, dut, inst_list, nets).run()
