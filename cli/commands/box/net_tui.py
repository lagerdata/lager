# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from ...sort_utils import natural_sort_key
from dataclasses import dataclass, field
from typing import Callable

import click
from rich.text import Text
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
    Select,
    Static,
    Tree,
)
from textual.widgets.tree import TreeNode

from . import labjack_pins as _lj

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
from ...core.net_helpers import NET_HTTP_PORT

# ──────────────── helpers / model ─────────────────


class UARTNetSaveValidationError(ValueError):
    """Raised when ``_save_nets_batch`` refuses a batch because a UART
    net was about to be persisted with a non-addressable ``pin`` (e.g.
    a bare interface index like ``"2"``).

    The TUI catches this and surfaces the message via ``show_error``,
    so users get the actionable EEPROM/serial hint instead of the
    silent box-side failure at first use.
    """


def _uid(instr: str, chan: str, role: str, name: str) -> str:
    """Return a row-key that is unique for (instrument, USB0::0x05E6::0x2281::4519728::INSTR channel, type, name)."""
    base = f"{instr}_{chan}_{role}_{name}".replace(" ", "_")
    safe = "".join(c if re.fullmatch(r"[A-Za-z0-9_-]", c) else "_" for c in base)
    return f"_{safe}" if safe and safe[0].isdigit() else safe


def _debug_channel_suffix(value) -> str:
    """Return the lower-cased ``@<channel>`` suffix of a debug net's device.

    Multi-channel FTDIs (FT2232H, FT4232H) encode the interface index in the
    debug net's device field as ``STM32F4x@A``. This helper extracts the
    trailing ``@A``/``@0`` portion (kept lower-case) so two nets on the same
    physical probe can be compared by which interface they own. Returns
    ``""`` for plain devices — that's the implicit channel A.
    """
    if not value:
        return ""
    s = str(value)
    if "@" not in s:
        return ""
    return f"@{s.rpartition('@')[2].lower()}"

# Chips that can only run in one hardware mode at a time across ALL roles.
# FT232H is the canonical example: a single channel hardware-multiplexed
# between MPSSE (spi/i2c/gpio/debug) and async-serial (uart). Once any
# role is saved on one of these chips, the other roles disappear from the
# add list. Multi-channel FTDIs (FT2232H, FT4232H) are NOT in this set —
# they pick a role per channel via the @A/@B/... suffix.
_MODE_EXCLUSIVE_INST = {"FTDI_FT232H"}

_MULTI_HUBS = {"LabJack_T7", "Acroname_8Port", "Acroname_4Port"}
# Role tuples use the canonical saved-role vocabulary (what nets actually
# carry: "power-supply", "battery"), matching the table in nets.py.
_SINGLE_CHANNEL_INST = {
    "Keithley_2281S": ("battery", "power-supply"),
    "EA_PSB_10060_60": ("solar", "power-supply"),
    "EA_PSB_10080_60": ("solar", "power-supply"),
    # Custom serial instrument (DEVICE_CATALOG single_channel=True): only one
    # net may reference the instrument at its serial:// address.
    "Rigol_DP711": ("power-supply",),
}

def _first_word(role: str) -> str:
    """Return the first part of a hyphenated role name."""
    # Special case: power-supply nets use 'supply' prefix instead of 'power'
    if role == "power-supply":
        return "supply"
    return role.split("-")[0]

# ──────────── box :9000 HTTP backend ────────────
# All box round-trips go over the box's long-lived HTTP server (the same
# /nets, /instruments and /custom-devices routes `lager nets` uses); the old
# path uploaded net.py / query_instruments.py / custom_devices.py to the
# :5000 exec service per call. Requests are thread-safe, so run_box_job
# workers can overlap without the old stdout-capture lock.

_HTTP_TIMEOUT = 30  # instrument scans probe hardware; give them headroom


def _box_http(dut: str, method: str, path: str, json_body=None, params=None):
    """One :9000 round-trip; parsed JSON on success, RuntimeError otherwise.

    run_box_job forwards raised exceptions to the UI callback, so the
    RuntimeError message (the box's `error` field when present) is what
    lands in show_error.
    """
    import requests

    url = f"http://{dut}:{NET_HTTP_PORT}{path}"
    try:
        resp = requests.request(method, url, json=json_body, params=params,
                                timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise RuntimeError(f"cannot reach box at {dut}:{NET_HTTP_PORT} ({e})")
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not (200 <= resp.status_code < 300):
        error = body.get("error") if isinstance(body, dict) else None
        raise RuntimeError(error or f"HTTP {resp.status_code} from box")
    return body


def _fetch_saved_nets_http(dut: str) -> list:
    """GET /nets/list — the full saved-net records."""
    records = _box_http(dut, "GET", "/nets/list")
    if isinstance(records, dict):
        records = records.get("nets", [])
    return records if isinstance(records, list) else []


def _fetch_instruments_http(dut: str) -> list:
    """GET /instruments/list — detected instruments (incl. custom devices)."""
    result = _box_http(dut, "GET", "/instruments/list")
    return result if isinstance(result, list) else []


def _save_net_http(dut: str, record: dict, old_name: str | None = None) -> None:
    """PUT /nets/<name> — create or replace a net (rename when old_name given)."""
    _box_http(dut, "PUT", f"/nets/{old_name or record.get('name')}",
              json_body=record)


# ──────────── custom-device (cable) assignment helpers ────────────
# TUI twin of ``lager nets assign``, backed by the same /custom-devices/*
# endpoints. Pure helpers live at module level so they can be unit-tested
# without a running Textual app.

def _run_custom_devices(dut: str, action: str, payload: dict | None = None):
    """Call the box's /custom-devices/* endpoints; None on failure.

    Box-reported errors (400 + {"error": ...}) come back as the parsed dict
    so callers can surface the reason; None covers transport failures, non-
    JSON responses, and box images that predate the endpoints (404).
    """
    import requests

    url = f"http://{dut}:{NET_HTTP_PORT}/custom-devices/{action}"
    try:
        if action == "list":
            resp = requests.get(url, timeout=_HTTP_TIMEOUT)
        else:
            resp = requests.post(url, json=payload or {}, timeout=_HTTP_TIMEOUT)
        result = resp.json()
    except Exception:
        return None
    if resp.status_code == 404:
        return None
    return result if isinstance(result, dict) else None


def _set_dialog_busy(screen: Screen, busy: bool) -> None:
    """Disable a dialog's buttons while a run_box_job worker is in flight.

    Prevents double-submits and gives immediate visual feedback that the
    click registered (the box round-trip itself takes seconds).
    """
    for btn in screen.query(Button):
        btn.disabled = busy


def _cable_ident(rec: dict) -> str:
    """Human label for a cable/assignment identity (serial, else port)."""
    if rec.get("serial"):
        return f"serial {rec['serial']}"
    return f"port {rec.get('port_path')}"


def _cable_row_label(c: dict) -> Text:
    """Tree label for an unassigned-cable row.

    Returns a ``Text`` object (not a str) so the content is markup-inert:
    device fields like ``[067b:23a3]`` would otherwise be parsed as markup
    tags and crash rendering with a MarkupError.
    """
    return Text(f"{_cable_ident(c)}  [{c.get('vid')}:{c.get('pid')}]  {c.get('tty')}")


def _assignment_row_label(a: dict) -> Text:
    """Tree label for an assignment row (instrument bolded, data inert)."""
    status = f"→ {a['tty']}" if a.get("tty") else "(cable not connected)"
    baud_note = f"  baud {a['baud']}" if a.get("baud") else ""
    return Text.assemble(
        (str(a.get("instrument", "?")), "bold"),
        f"  cable {_cable_ident(a)}{baud_note}  {status}",
    )


def _assign_payload(cable: dict, device: str, baud: int | None) -> dict:
    """Build the ``/custom-devices/assign`` payload for a picked cable.

    Identity choice mirrors the durable-address rules: prefer the USB serial
    when the cable has one (assignment follows the cable across ports), else
    pin to the USB port path. Clone cables sharing one serial are rejected
    by the backend with a pin-by-port hint — that case needs the CLI.
    """
    payload: dict = {"instrument": device}
    if cable.get("serial"):
        payload["serial"] = cable["serial"]
    else:
        payload["port_path"] = cable.get("port_path")
    if baud is not None:
        payload["baud"] = baud
    return payload


def _default_net_name(assignment: dict) -> str:
    """Default net name for a just-assigned instrument (CLI --as-net parity)."""
    return str(assignment.get("instrument") or "net").lower()


def _net_from_assignment(assignment: dict, name: str) -> Net:
    """Build the saved-net row for a just-assigned instrument (TUI --as-net).

    Mirrors the CLI's --as-net derivation: the first catalog role verbatim
    (saved supply nets must carry the scanner-vocabulary "power-supply" —
    see the nets-add tables) and the role's first catalog channel.
    """
    roles = assignment.get("roles") or ["power-supply"]
    role = roles[0]
    channels = (assignment.get("channels") or {}).get(role) or ["1"]
    return Net(assignment.get("instrument", ""), str(channels[0]), role, name,
               assignment.get("address", ""))


def _net_name_taken(nets: list["Net"], name: str) -> bool:
    """True if a saved net already uses *name* (names are globally unique)."""
    return any(n.saved and n.net == name for n in nets)


def _address_has_saved_net(nets: list["Net"], address: str) -> bool:
    """True if any saved net is already bound to *address*.

    Used to suppress the post-assign Create Net offer on a re-assign (e.g.
    a baud-only update keeps the existing nets): catalog instruments are
    single-channel, and offering the dialog again would let a second net be
    saved for the same instrument, bypassing nets-add's single-channel check.
    """
    return bool(address) and any(n.saved and n.addr == address for n in nets)

_UART_PIN_HINT = (
    "UART nets need either a /dev/tty* path or a non-empty USB serial in "
    "the ``pin`` slot — bare interface indices won't survive the box-side "
    "sysfs lookup. Programme a serial into the adapter's EEPROM (e.g. with "
    "``ftdi_eeprom``) and rescan instruments, then try again."
)


def _validate_uart_pin(pin) -> bool:
    """Return True if *pin* can plausibly address a UART bridge.

    Two shapes are accepted, matching what the box-side dispatcher
    understands (``box/lager/protocols/uart/dispatcher.py``):

    * Direct device path: ``/dev/ttyUSB2``, ``/dev/ttyACM0``.
    * USB serial string: anything else non-empty and longer than two
      characters. We don't try to validate the exact serial format
      because FTDI/CP210x/CDC serials vary widely — we just reject the
      legacy ``"0"/"1"/"2"/"3"`` placeholders that the old FT4232H
      scanner used to emit, which were the silent failure mode that
      motivated this guard in the first place.
    """
    if not isinstance(pin, str):
        return False
    p = pin.strip()
    if not p:
        return False
    if p.startswith("/dev/"):
        return True
    # Anything shorter than 3 chars is almost certainly a bare interface
    # index. Real USB serials are at least 4 characters in practice
    # (FT5XYZAB, 0123-4567, etc.).
    if len(p) < 3:
        return False
    return True


def _validate_nets_before_save(nets: list["Net"]) -> list[tuple["Net", str]]:
    """Return ``[(net, reason)]`` for every net that would round-trip badly.

    Empty list means everything's fine; a non-empty list is what the
    caller surfaces via ``show_error`` before any actual save happens.
    """
    bad: list[tuple["Net", str]] = []
    for n in nets:
        if n.type == "uart" and not _validate_uart_pin(n.chan):
            bad.append((
                n,
                f"net '{n.net}' has invalid UART pin {n.chan!r}; "
                + _UART_PIN_HINT,
            ))
    return bad


def _save_nets_batch(dut: str, nets: list["Net"], custom_names: dict[str, str] | None = None) -> bool:
    """Save multiple nets, one PUT /nets/<name> per record."""
    if not nets:
        return True

    invalid = _validate_nets_before_save(nets)
    if invalid:
        # Refuse the whole batch so the caller's show_error surfaces the
        # actionable reason instead of letting the box reject these later
        # with a cryptic "bridge not found" message at first use.
        raise UARTNetSaveValidationError(
            "Refusing to save invalid UART net(s): "
            + "; ".join(reason for _net, reason in invalid)
        )

    custom_names = custom_names or {}
    nets_data = []
    for n in nets:
        net_name = custom_names.get(n.key(), n.net)
        record = {
            "name": net_name,
            "role": n.type,
            "address": n.addr,
            "instrument": n.instrument,
            "pin": n.chan,
        }
        if n.params:
            record["params"] = n.params
        nets_data.append(record)

    # One PUT per record; keep going on individual failures so one bad net
    # doesn't sink the rest of the batch.
    saved_count = 0
    for record in nets_data:
        try:
            _save_net_http(dut, record)
            saved_count += 1
        except Exception:
            pass  # Continue trying to save other nets

    return saved_count > 0

def is_single_channel_taken(all_nets: list["Net"], inst: str, addr: str, role: str | None = None) -> bool:
    """
    True if a *saved* net already exists for this single-channel
    instrument at this address.

    Single-channel devices like Keithley_2281S (``batt``/``supply``) or
    EA_PSB (``solar``/``supply``) physically hold one channel, so once
    any saved net binds it the other role becomes unsafe to add — the
    underlying hardware can only run one mode at a time. The ``role``
    parameter is kept for backwards compatibility (callers used to ask
    "is THIS role taken?") but is ignored: any saved net on the chip
    counts as taken.
    """
    if inst not in _SINGLE_CHANNEL_INST:
        return False
    return any(n.saved and n.instrument == inst and n.addr == addr for n in all_nets)

@dataclass
class Net:
    instrument: str
    chan: str
    type: str
    net: str
    addr: str
    saved: bool = False
    has_script: bool = False
    # Agent-facing metadata.
    purpose: str = ""
    notes: str = ""
    tags: list[str] = field(default_factory=list)
    # Custom LabJack i2c/spi pin assignment (sda_pin/scl_pin or
    # cs_pin/clk_pin/mosi_pin/miso_pin). None means default pins; the box
    # dispatchers decode the legacy channel string in that case.
    params: dict | None = None
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
    """Dialog for editing net metadata.

    Three fields:

    - **Purpose** -- one sentence describing what this wire does on the
      DUT.  This is the single most important field for an AI agent.
    - **Notes** -- optional markdown for gotchas, jumper positions,
      scope probe points, etc.
    - **Tags** -- optional comma-separated keywords the planning tools
      match on.
    """

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
            yield Label("Purpose (one sentence — what this wire does on the DUT):")
            self.purpose_input = Input(
                placeholder="e.g., DUT debug CLI over UART; primary command/response channel",
                id="purpose_input",
                value=self.net.purpose,
            )
            yield self.purpose_input

            yield Label("Notes (optional — gotchas, jumper positions, scope probe points):")
            self.notes_input = Input(
                placeholder="e.g., Requires JP3 closed; idle level is high.",
                id="notes_input",
                value=self.net.notes,
            )
            yield self.notes_input

            yield Label("Tags (comma-separated, optional):")
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
        self.purpose_input.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        app: NetApp = self.app  # type: ignore[attr-defined]

        if event.button.id == "cancel":
            app.pop_screen()
            return

        purpose = self.purpose_input.value.strip()
        notes = self.notes_input.value.strip()
        tags_raw = self.tags_input.value.strip()
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []

        self.net.purpose = purpose
        self.net.notes = notes
        self.net.tags = tags

        net_data = {
            "name": self.net.net,
            "role": self.net.type,
            "address": self.net.addr,
            "instrument": self.net.instrument,
            "pin": self.net.chan,
            "purpose": purpose,
            "notes": notes,
            "tags": tags,
        }

        _set_dialog_busy(self, True)

        def work() -> dict:
            _save_net_http(app.dut, net_data)
            return {"saved": app._fetch_saved_records()}

        def done(out: object) -> None:
            _set_dialog_busy(self, False)
            if isinstance(out, Exception):
                app.show_error(f"Failed to save details: {str(out)}")
            else:
                app.show_success(f"Updated details for net '{self.net.net}'")
                if out.get("saved") is not None:
                    app._apply_saved_records(out["saved"])
            app._refresh_table()
            app.pop_screen()

        app.run_box_job(work, done)


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
        app.show_loading(f"Deleting net '{self.net.net}'...")

        # Delete this net over the box HTTP API (off the UI thread)
        def work() -> dict:
            _box_http(app.dut, "DELETE", f"/nets/{self.net.net}",
                      params={"role": self.net.type})
            return {"saved": app._fetch_saved_records()}

        def done(out: object) -> None:
            app.hide_loading()
            if isinstance(out, Exception):
                app.show_error(f"Failed to delete net: {str(out)}")
                return
            app.show_success(f"Successfully deleted net '{self.net.net}'")

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
            if out.get("saved") is not None:
                app._apply_saved_records(out["saved"])
            app._refresh_table()

        app.run_box_job(work, done)

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
        app.show_loading(f"Renaming net to '{new_name}'...")
        old_name = self.net.net

        # Rename over the box HTTP API: PUT the stored record (fetched fresh
        # so box-only fields like debug scripts survive) to the old name with
        # the new name in the body (off the UI thread).
        def work() -> dict:
            recs = _fetch_saved_nets_http(app.dut)
            src = next((r for r in recs if r.get("name") == old_name), None)
            if src is None:
                raise RuntimeError(f"net '{old_name}' not found on the box")
            record = {k: v for k, v in src.items() if k != "live_path"}
            record["name"] = new_name
            _save_net_http(app.dut, record, old_name=old_name)
            return {"saved": app._fetch_saved_records()}

        def done(out: object) -> None:
            app.hide_loading()
            if isinstance(out, Exception):
                app.show_error(f"Failed to rename net: {str(out)}")
                return
            app.show_success(f"Successfully renamed net to '{new_name}'")

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

            if out.get("saved") is not None:
                app._apply_saved_records(out["saved"])
            app._refresh_table()

        app.run_box_job(work, done)

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
        callback: Callable[[bool, str | None], None],
        channel_suffix: str = "",
    ):
        super().__init__()
        self.dut = dut
        self.net_name = net_name
        self.address = address
        self.callback = callback
        # ``channel_suffix`` is the FTDI interface tag (``@A`` / ``@B`` / ...)
        # for multi-channel adapters. Empty string for single-channel probes.
        self.channel_suffix = channel_suffix
        self.input = Input(placeholder=f"Enter device type for {address}", id="jlink_type")

    def compose(self):
        # Surface the channel selection so users running an FT2232H or
        # FT4232H know which interface the device they're about to enter
        # corresponds to. Empty for single-channel probes.
        chan_line = ""
        if self.channel_suffix:
            chan_line = f"FTDI channel: {self.channel_suffix[1:]}\n"
        with Vertical(classes="dialog"):
            yield Static("J-Link Device Configuration", classes="dialog-title")
            yield Static(
                f"Net: {self.net_name}\n"
                f"Address: {self.address}\n"
                f"{chan_line}\n"
                f"Please specify the device type for this debugger.",
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

def _labjack_claimed_pin_map(all_nets: list["Net"], target: "Net") -> dict[str, str]:
    """Map DIO pin name -> owning net name for pins claimed by *saved*
    LabJack nets at the same address as ``target``. Used by the pin-picker
    dialog to surface (non-blocking) conflict warnings."""
    claimed: dict[str, str] = {}
    for s in all_nets:
        if not s.saved or s.addr != target.addr:
            continue
        if s.instrument != "LabJack_T7":
            continue
        for pin in _lj.claimed_pins_from_chan(s.type, s.chan):
            claimed.setdefault(pin, s.net)
    return claimed


class LabJackPinDialog(Screen):
    """Pick the LabJack DIO pins for an i2c/spi net before saving it.

    Dropdowns are prefilled with the historical defaults (I2C: SDA=FIO4
    SCL=FIO5; SPI: CS=FIO0 SCK=FIO1 MOSI=FIO2 MISO=FIO3), so accepting the
    dialog unchanged saves exactly what the TUI saved before this dialog
    existed (the legacy channel string, no params). Custom selections are
    written to the net's ``params`` dict — the format the box dispatchers
    already consume — plus a labeled channel summary for display.

    Pins already claimed by saved LabJack nets show a warning but don't
    block: that matches the runtime PinRegistry behavior and the
    ``lager nets add`` CLI.
    """

    def __init__(
        self,
        net: "Net",
        claimed: dict[str, str],
        callback: Callable[[bool], None],
    ):
        super().__init__()
        self.net = net
        self.claimed = claimed
        self.callback = callback
        self.signals = _lj.I2C_SIGNALS if net.type == "i2c" else _lj.SPI_SIGNALS
        self.defaults = (
            _lj.I2C_DEFAULT_PINS if net.type == "i2c" else _lj.SPI_DEFAULT_PINS
        )

    def _select_id(self, signal: str) -> str:
        return f"pin_{signal.lower()}"

    def compose(self) -> ComposeResult:
        pin_options = [(name, name) for name in _lj.ALL_PIN_NAMES]
        with Vertical(classes="dialog"):
            yield Static(
                f"Configure {self.net.type.upper()} Pins", classes="dialog-title"
            )
            yield Static(
                f"Net: {self.net.net}  |  Instrument: LabJack T7\n"
                f"Any DIO pin may be used. Defaults are preselected.",
                classes="dialog-content",
            )
            for signal in self.signals:
                options = list(pin_options)
                if signal == "CS":
                    options.insert(0, ("(none — manual CS)", _lj.NO_CS))
                with Horizontal(classes="pin-row"):
                    yield Label(f"{signal:<5}", classes="pin-label")
                    yield Select(
                        options,
                        value=self.defaults[signal],
                        allow_blank=False,
                        id=self._select_id(signal),
                    )
            yield Static("", id="pin_warn", classes="pin-warn")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="pin-cancel")
                yield Button("Save", id="pin-confirm", variant="success")

    def _chosen(self) -> dict[str, str]:
        return {
            signal: str(self.query_one(f"#{self._select_id(signal)}", Select).value)
            for signal in self.signals
        }

    def _update_warning(self) -> None:
        chosen = self._chosen()
        notes = []
        seen: dict[str, str] = {}
        for signal, pin in chosen.items():
            if pin == _lj.NO_CS:
                continue
            if pin in seen:
                notes.append(f"{seen[pin]} and {signal} both use {pin}")
            seen.setdefault(pin, signal)
            if pin in self.claimed:
                notes.append(f"{pin} is used by net '{self.claimed[pin]}'")
        self.query_one("#pin_warn", Static).update(
            ("Warning: " + "; ".join(notes)) if notes else ""
        )

    @on(Select.Changed)
    def _on_pin_changed(self, event: Select.Changed) -> None:
        self._update_warning()

    @on(Button.Pressed)
    def _on_pin_button(self, event: Button.Pressed) -> None:
        if event.button.id == "pin-cancel":
            self.app.pop_screen()
            self.callback(False)
            return
        if event.button.id != "pin-confirm":
            return

        label, params, error = _lj.resolve_pin_selection(self.net.type, self._chosen())
        if error:
            self.query_one("#pin_warn", Static).update(f"Error: {error}")
            return

        if label is not None:
            # Custom pins: labeled summary for display, params for the
            # box dispatchers. Default selection leaves the net untouched
            # so the saved record is identical to a pre-dialog save.
            self.net.chan = label
            self.net.params = params

        self.app.pop_screen()
        self.callback(True)


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

        # Hide nets for single-channel instruments once ANY role is saved on
        # the same chip+addr. Keithley_2281S is the canonical case: one
        # physical channel that can run as ``batt`` OR ``supply`` but never
        # both. The old per-role check (``s.type == n.type``) let users
        # silently double-book the channel by picking the other role; this
        # tighter check matches the ``add-all`` CLI semantics.
        if n.instrument in _SINGLE_CHANNEL_INST:
            if any(s.saved and s.instrument == n.instrument and s.addr == n.addr for s in self.nets):
                return False

        # Mode-exclusive chips (FT232H): once ANY role is saved on this
        # chip+address, all other role options disappear. The user must
        # delete the existing net to switch modes.
        if n.instrument in _MODE_EXCLUSIVE_INST:
            if any(s.saved and s.instrument == n.instrument and s.addr == n.addr for s in self.nets):
                return False
        # Prevent multiple debug nets with same type/instrument/address —
        # but multi-channel FTDIs encode the interface in the ``@suffix`` of
        # the device field, so we only block when the suffixes also match.
        if n.type == "debug":
            new_suffix = _debug_channel_suffix(n.chan)
            for s in self.nets:
                if (s.saved and s.type == "debug"
                        and s.instrument == n.instrument
                        and s.addr == n.addr
                        and _debug_channel_suffix(s.chan) == new_suffix):
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
            if n.type == "debug" and "DEVICE_TYPE" not in str(n.chan):
                # Already resolved (a real device name); skip the prompt
                # filter. ``DEVICE_TYPE@A`` placeholders still need prompting.
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

        # Check for single-channel device conflicts: at most one net per
        # (instrument, address), regardless of role. Keithley_2281S can be
        # ``batt`` OR ``supply`` but not both; EA_PSB chips similarly choose
        # between ``solar`` and ``supply``. The role tuple metadata in
        # ``_SINGLE_CHANNEL_INST`` is kept for future "available roles"
        # surfacing but doesn't relax the constraint.
        single_cnt: dict[tuple[str, str], int] = defaultdict(int)
        for s in main.nets:
            if s.saved and s.instrument in _SINGLE_CHANNEL_INST:
                single_cnt[(s.instrument, s.addr)] += 1
        for n in selected_nets:
            if n.instrument in _SINGLE_CHANNEL_INST:
                single_cnt[(n.instrument, n.addr)] += 1
        conflicts = [(inst, addr) for (inst, addr), cnt in single_cnt.items() if cnt > 1]
        if conflicts:
            parts = [f"{inst} at {addr}" for inst, addr in conflicts]
            msg = "Only one net may be added per " + ", ".join(parts) + "."
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

        # Mode-exclusive chips: across all roles, only one net per chip+addr
        # can be selected (an FT232H can be SPI OR UART, never both). Detect
        # the case where the user picked two role options for the same chip
        # in this batch and bail out with a hint.
        mode_excl_pairs: dict[tuple[str, str], set[str]] = defaultdict(set)
        for s in main.nets:
            if s.saved and s.instrument in _MODE_EXCLUSIVE_INST:
                mode_excl_pairs[(s.instrument, s.addr)].add(s.type)
        for n in selected_nets:
            if n.instrument in _MODE_EXCLUSIVE_INST:
                mode_excl_pairs[(n.instrument, n.addr)].add(n.type)
        mode_conflicts = [
            (inst, addr, sorted(types))
            for (inst, addr), types in mode_excl_pairs.items()
            if len(types) > 1
        ]
        if mode_conflicts:
            parts = [
                f"{inst} at {addr} ({', '.join(types)})"
                for inst, addr, types in mode_conflicts
            ]
            msg = (
                "These chips only run one mode at a time; pick a single role per "
                + ", ".join(parts) + "."
            )
            try:
                self.query_one("#keithley_hint", Static).update(msg)
            except NoMatches:
                self.mount(Static(msg, id="keithley_hint", classes="warning"))
            return

        # LabJack i2c/spi nets get a pin-picker dialog before saving;
        # defaults are preselected so accepting unchanged behaves exactly
        # like the pre-dialog flow.
        labjack_pin_nets = [
            n for n in selected_nets
            if n.type in ("i2c", "spi") and n.instrument == "LabJack_T7"
        ]

        def after_pin_dialogs():
            self._save_selected(main, selected_nets, debug_nets, normal_nets)

        if labjack_pin_nets:
            self._pin_nets = labjack_pin_nets
            self._pin_idx = 0

            def handle_pin_complete(success: bool):
                if not success:
                    # Cancel aborts the whole add; the Add screen stays up
                    # with the selection intact.
                    return
                self._pin_idx += 1
                if self._pin_idx < len(self._pin_nets):
                    prompt_next_pin()
                else:
                    after_pin_dialogs()

            def prompt_next_pin():
                n = self._pin_nets[self._pin_idx]
                claimed = _labjack_claimed_pin_map(main.nets, n)
                main.push_screen(LabJackPinDialog(n, claimed, handle_pin_complete))

            prompt_next_pin()
            return

        after_pin_dialogs()

    def _batch_save_and_close(
        self,
        main: "NetApp",
        nets_to_save: list[Net],
        pop_on_validation_error: bool = False,
    ) -> None:
        """Batch-save off the UI thread, then refresh and close this screen.

        ``pop_on_validation_error`` preserves the historical difference
        between the two save paths: the plain path keeps the Add screen up
        (selection intact) when a UART net fails validation, while the
        J-Link path abandons it.
        """
        _set_dialog_busy(self, True)

        def work() -> dict:
            ok = _save_nets_batch(main.dut, nets_to_save, self.custom_names)
            return {"ok": ok, "saved": main._fetch_saved_records()}

        def done(out: object) -> None:
            _set_dialog_busy(self, False)
            if isinstance(out, UARTNetSaveValidationError):
                main.show_error(str(out))
                if pop_on_validation_error:
                    main.pop_screen()
                return
            if isinstance(out, Exception):
                main.show_error(f"Failed to save nets: {str(out)}")
                return
            if out["ok"]:
                main.show_success(f"Successfully added {len(nets_to_save)} nets")
            else:
                main.show_error("Failed to save some nets")
            # Refresh saved nets and update UI
            if out.get("saved") is not None:
                main._apply_saved_records(out["saved"])
            main._refresh_table()
            main.pop_screen()

        main.run_box_job(work, done)

    def _save_selected(
        self,
        main: "NetApp",
        selected_nets: list[Net],
        debug_nets: list[Net],
        normal_nets: list[Net],
    ) -> None:
        """Save the selection, prompting for J-Link device types first if
        any debug nets are included. (Tail of the add-confirm flow; split
        out so the LabJack pin dialogs can run before it.)"""
        # If no debug nets selected, save immediately using batch save
        if not debug_nets:
            self._batch_save_and_close(main, selected_nets)
            return

        # If there are debug nets, prompt for each J-Link device type
        self._pending_debug_nets = debug_nets
        self._pending_normal_nets = normal_nets
        self._debug_idx = 0

        def handle_jlink_complete(success: bool, device_type: str | None):
            if not success or not device_type:
                main.pop_screen(to=self)
                return
            # Preserve any ``@<channel>`` suffix that was attached to the
            # DEVICE_TYPE placeholder (multi-channel FTDI). The OpenOCD
            # backend reads the suffix off the saved net to route to the
            # correct FTDI interface.
            target = self._pending_debug_nets[self._debug_idx]
            placeholder = str(target.chan or "")
            suffix = ""
            if "@" in placeholder:
                _, _, after = placeholder.partition("@")
                suffix = f"@{after}"
            target.chan = f"{device_type}{suffix}"
            self._debug_idx += 1
            if self._debug_idx < len(self._pending_debug_nets):
                prompt_next()
                return
            # All debug prompts done – save all pending nets using batch save
            all_nets_to_save = self._pending_normal_nets + self._pending_debug_nets
            self._batch_save_and_close(main, all_nets_to_save,
                                       pop_on_validation_error=True)

        def prompt_next():
            n = self._pending_debug_nets[self._debug_idx]
            suffix = _debug_channel_suffix(n.chan)
            main.push_screen(
                JLinkDeviceTypeDialog(main.dut, n.net, n.addr, handle_jlink_complete, channel_suffix=suffix)
            )

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

        _set_dialog_busy(self, True)

        # Delete all saved nets over the box HTTP API (off the UI thread)
        def work() -> dict:
            _box_http(app.dut, "DELETE", "/nets")
            return {"saved": app._fetch_saved_records()}

        def done(out: object) -> None:
            _set_dialog_busy(self, False)
            if isinstance(out, Exception):
                app.show_error(f"Failed to delete all nets: {str(out)}")
            else:
                app.show_success("Successfully deleted all nets")
                if out.get("saved") is not None:
                    app._apply_saved_records(out["saved"])
            app._refresh_table()
            app.pop_screen()

        app.run_box_job(work, done)


# ──────────────── custom-device assignment screens ────────────────

class AssignDeviceScreen(Screen):
    """Map a USB-serial cable to a catalog instrument (custom devices).

    Shows the box's unassigned USB-serial cables and current assignments
    (from the box's /custom-devices/list). Selecting a cable opens the
    device-pick dialog; selecting an assignment offers removal. After
    either action the app re-scans instruments so the (un)assigned
    instrument appears in / disappears from the Add Nets flow.
    """

    def __init__(self, data: dict) -> None:
        super().__init__()
        self.data: dict = data or {}

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("Assign Custom Device", classes="dialog-title")
            yield Static(
                "Some instruments (e.g. a Rigol DP711 on RS-232) reach the box "
                "through a generic USB-serial cable the scanner can't identify. "
                "Assign the cable to its instrument once — it then shows up like "
                "any auto-detected device and nets are added the normal way.\n\n"
                "Select a cable to assign it; select an assignment to remove it.",
                classes="dialog-content",
            )
            self.assign_tree = Tree("Cables", id="assign_tree")
            self.assign_tree.show_root = False
            self.assign_tree.root.expand()
            yield self.assign_tree
            self.busy_note = Static("", id="assign_busy", classes="loading")
            self.busy_note.display = False
            yield self.busy_note
            with Horizontal(classes="dialog-buttons"):
                yield Button("Close", id="assign-close", variant="primary")

    def on_mount(self) -> None:
        self._build_tree()
        self.assign_tree.focus()

    def _build_tree(self) -> None:
        tree = self.assign_tree
        tree.clear()

        cables_branch = tree.root.add("Unassigned USB-serial cables", expand=True)
        cables = self.data.get("cables") or []
        for c in cables:
            cables_branch.add_leaf(_cable_row_label(c), data={"kind": "cable", "rec": c})
        if not cables:
            cables_branch.add_leaf("(none — plug the instrument's cable into the box)")

        assigned_branch = tree.root.add("Assignments", expand=True)
        assignments = self.data.get("assignments") or []
        for a in assignments:
            assigned_branch.add_leaf(_assignment_row_label(a),
                                     data={"kind": "assignment", "rec": a})
        if not assignments:
            assigned_branch.add_leaf("(none)")

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        event.stop()
        data = event.node.data
        if not isinstance(data, dict):
            # Branch headers / "(none)" placeholders: just toggle branches.
            if event.node.children:
                event.node.toggle()
            return
        if data["kind"] == "cable":
            catalog = self.data.get("catalog") or []
            self.app.push_screen(DevicePickDialog(data["rec"], catalog, self._do_assign))
        elif data["kind"] == "assignment":
            self.app.push_screen(ConfirmUnassignDialog(data["rec"], self._do_unassign))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "assign-close":
            self.app.pop_screen()

    # ---- backend actions -------------------------------------------------
    # Each action batches its box round-trips (the change itself plus the
    # refreshes the UI needs) into one run_box_job worker: the calls still
    # run sequentially, but off the UI thread, so the app stays responsive
    # instead of freezing for several seconds per click.

    def _set_busy(self, message: str | None) -> None:
        """Show progress and ignore further input while a box job runs."""
        busy = message is not None
        self.busy_note.update(message or "")
        self.busy_note.display = busy
        self.assign_tree.disabled = busy
        self.query_one("#assign-close", Button).disabled = busy

    def _apply_refreshes(self, out: dict) -> None:
        """Apply the refresh data an action's worker fetched (UI thread)."""
        app: NetApp = self.app  # type: ignore[assignment]
        if out.get("saved") is not None:
            app._apply_saved_records(out["saved"])
        if out.get("instruments") is not None:
            app._apply_instruments(out["instruments"])
        app._refresh_table()
        if isinstance(out.get("data"), dict):
            self.data = out["data"]
        self._build_tree()

    def _do_assign(self, cable: dict, device: str, baud: int | None) -> None:
        app: NetApp = self.app  # type: ignore[assignment]
        payload = _assign_payload(cable, device, baud)
        self._set_busy(f"Assigning {device}...")

        def work() -> dict:
            result = _run_custom_devices(app.dut, "assign", payload)
            if not isinstance(result, dict) or not result.get("address"):
                return {"result": result}
            out: dict = {"result": result}
            if result.get("deleted_nets"):
                # Replacing the cable's previous assignment cascaded to its
                # nets — re-sync the saved list.
                out["saved"] = app._fetch_saved_records()
            out["instruments"] = app._fetch_instruments()
            out["data"] = _run_custom_devices(app.dut, "list")
            return out

        def done(out: object) -> None:
            self._set_busy(None)
            result = out.get("result") if isinstance(out, dict) else None
            if not isinstance(result, dict) or not result.get("address"):
                # The box reports validation failures (unplugged cable, ...)
                # as {"error": ...}; show the reason when we have one.
                reason = result.get("error") if isinstance(result, dict) else None
                app.show_error(
                    reason
                    or "Assignment failed — run 'lager nets assign' in a terminal for details."
                )
                return
            msg = f"Assigned {result.get('instrument', device)}"
            deleted = result.get("deleted_nets") or []
            if deleted:
                msg += f" (deleted stale net{'s' if len(deleted) != 1 else ''}: {', '.join(deleted)})"
            app.show_success(msg)
            self._apply_refreshes(out)
            # TUI twin of --as-net: offer to create the instrument's net now —
            # unless one already exists for this address (re-assign/baud
            # update), where a second net would bypass the single-channel
            # constraint.
            if not _address_has_saved_net(app.nets, result.get("address", "")):
                app.push_screen(CreateNetDialog(
                    result, lambda name: self._create_net(result, name)))

        app.run_box_job(work, done)

    def _create_net(self, assignment: dict, name: str) -> None:
        """Save the net offered by CreateNetDialog (TUI --as-net)."""
        app: NetApp = self.app  # type: ignore[assignment]
        net = _net_from_assignment(assignment, name)
        self._set_busy(f"Creating net '{name}'...")

        def work() -> dict:
            ok = _save_nets_batch(app.dut, [net])
            return {"ok": ok, "saved": app._fetch_saved_records()}

        def done(out: object) -> None:
            self._set_busy(None)
            if isinstance(out, UARTNetSaveValidationError):
                app.show_error(str(out))
                return
            if not isinstance(out, dict):
                app.show_error(f"Failed to create net '{name}'")
                return
            if out["ok"]:
                app.show_success(f"Created net '{name}' for {net.instrument}")
            else:
                app.show_error(f"Failed to create net '{name}'")
            if out.get("saved") is not None:
                app._apply_saved_records(out["saved"])
            app._refresh_table()

        app.run_box_job(work, done)

    def _do_unassign(self, assignment: dict) -> None:
        app: NetApp = self.app  # type: ignore[assignment]
        if assignment.get("serial"):
            ident = {"serial": assignment["serial"]}
        else:
            ident = {"port_path": assignment.get("port_path")}
        self._set_busy("Removing assignment...")

        def work() -> dict:
            result = _run_custom_devices(app.dut, "remove", ident)
            if not isinstance(result, dict) or not result.get("removed"):
                return {"result": result}
            return {
                "result": result,
                # The cascade may have changed saved nets on disk — re-sync
                # so the Saved Nets tree drops them, then re-scan for the
                # placeholder list.
                "saved": app._fetch_saved_records(),
                "instruments": app._fetch_instruments(),
                "data": _run_custom_devices(app.dut, "list"),
            }

        def done(out: object) -> None:
            self._set_busy(None)
            result = out.get("result") if isinstance(out, dict) else None
            if not isinstance(result, dict) or not result.get("removed"):
                app.show_error("Could not remove the assignment.")
                return
            msg = f"Removed the {assignment.get('instrument')} assignment"
            deleted = result.get("deleted_nets") or []
            if deleted:
                # Nets live and die with their assignment (backend cascade).
                msg += f" (deleted net{'s' if len(deleted) != 1 else ''}: {', '.join(deleted)})"
            app.show_success(msg)
            self._apply_refreshes(out)

        app.run_box_job(work, done)


class DevicePickDialog(Screen):
    """Pick the catalog instrument (and optional baud) for a cable."""

    def __init__(self, cable: dict, catalog: list[dict],
                 callback: Callable[[dict, str, int | None], None]) -> None:
        super().__init__()
        self.cable = cable
        self.catalog = catalog
        self.callback = callback
        # One shared default-baud hint; per-device defaults still apply on
        # the box (the override is only needed when the front panel differs).
        defaults = sorted({e.get("default_baud") for e in catalog if e.get("default_baud")})
        hint = f"default {defaults[0]}" if len(defaults) == 1 else "instrument default"
        self.baud_input = Input(
            placeholder=f"Baud override (optional, {hint})", id="assign_baud"
        )

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("Assign Cable", classes="dialog-title")
            # markup=False: the interpolated device fields (e.g. "[067b:23a3]")
            # would otherwise be parsed as markup tags and crash rendering.
            yield Static(
                f"Cable: {_cable_ident(self.cable)}  "
                f"[{self.cable.get('vid')}:{self.cable.get('pid')}]  {self.cable.get('tty')}\n\n"
                f"Which instrument is on the other end of this cable?\n"
                f"A baud override must match the instrument's front-panel setting.",
                classes="dialog-content",
                markup=False,
            )
            for i, entry in enumerate(self.catalog):
                roles = ", ".join(entry.get("roles") or [])
                yield Button(
                    f"{entry.get('display_name', entry['name'])} ({roles})",
                    id=f"assign-dev-{i}", variant="success",
                )
            yield self.baud_input
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="assign-dev-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "assign-dev-cancel":
            self.app.pop_screen()
            return
        if not (event.button.id or "").startswith("assign-dev-"):
            return

        baud_raw = self.baud_input.value.strip()
        baud: int | None = None
        if baud_raw:
            try:
                baud = int(baud_raw)
                if baud <= 0:
                    raise ValueError
            except ValueError:
                msg = f"Baud must be a positive integer (got {baud_raw!r})."
                try:
                    self.query_one("#baud_hint", Static).update(msg)
                except NoMatches:
                    # markup=False: user-typed input could contain "[".
                    self.mount(Static(msg, id="baud_hint", classes="warning",
                                      markup=False))
                return

        entry = self.catalog[int(event.button.id.rsplit("-", 1)[1])]
        self.app.pop_screen()
        self.callback(self.cable, entry["name"], baud)


class CreateNetDialog(Screen):
    """Offer to create the net for a just-assigned instrument.

    The TUI twin of ``lager nets assign --as-net``: shown right after a
    successful assignment, pre-filled with the CLI's default name. Skipping
    is always safe — the instrument stays available under + Add Nets.
    """

    def __init__(self, assignment: dict, callback: Callable[[str], None]) -> None:
        super().__init__()
        self.assignment = assignment
        self.callback = callback
        # NOTE: the default value is set in on_mount, not here — Input's
        # value watcher needs an active app, and the constructor may run
        # outside one (e.g. in tests).
        self.name_input = Input(placeholder="Net name", id="asnet_name")

    def compose(self) -> ComposeResult:
        net = _net_from_assignment(self.assignment, "")
        with Vertical(classes="dialog"):
            yield Static("Create Net", classes="dialog-title")
            # markup=False: interpolated device data (see DevicePickDialog).
            yield Static(
                f"{self.assignment.get('instrument')} is assigned. "
                f"Create its net now?\n\n"
                f"Role: {net.type}   Channel: {net.chan}\n"
                f"Address: {self.assignment.get('address')}",
                classes="dialog-content",
                markup=False,
            )
            yield self.name_input
            with Horizontal(classes="dialog-buttons"):
                yield Button("Skip", id="asnet-skip")
                yield Button("Create Net", id="asnet-create", variant="success")

    def on_mount(self) -> None:
        self.name_input.value = _default_net_name(self.assignment)
        self.name_input.focus()

    def _show_hint(self, msg: str) -> None:
        try:
            self.query_one("#asnet_hint", Static).update(msg)
        except NoMatches:
            # markup=False: the message echoes user-typed input.
            self.mount(Static(msg, id="asnet_hint", classes="warning",
                              markup=False))

    def _try_create(self) -> None:
        name = self.name_input.value.strip()
        if not name:
            self._show_hint("Enter a net name (or press Skip).")
            return
        app: NetApp = self.app  # type: ignore[assignment]
        if _net_name_taken(app.nets, name):
            self._show_hint(f"A net named '{name}' already exists — "
                            f"net names are globally unique.")
            return
        self.app.pop_screen()
        self.callback(name)

    @on(Input.Submitted, "#asnet_name")
    def _on_name_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._try_create()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "asnet-skip":
            self.app.pop_screen()
            return
        if event.button.id == "asnet-create":
            self._try_create()


class ConfirmUnassignDialog(Screen):
    """Are-you-sure overlay for removing a custom-device assignment."""

    def __init__(self, assignment: dict, callback: Callable[[dict], None]) -> None:
        super().__init__()
        self.assignment = assignment
        self.callback = callback

    def compose(self) -> ComposeResult:
        a = self.assignment
        addr_note = (f"\nSaved nets pointing at {a['address']} will be deleted."
                     if a.get("address") else "")
        with Vertical(classes="dialog"):
            yield Static("Remove Assignment", classes="dialog-title")
            # markup=False: defensive — interpolated device data must never be
            # parsed as markup (see DevicePickDialog).
            yield Static(
                f"Remove the {a.get('instrument')} assignment from the cable at "
                f"{_cable_ident(a)}?\n\n"
                f"The cable will be offered as a generic UART device again."
                f"{addr_note}",
                classes="dialog-content warning",
                markup=False,
            )
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="unassign-cancel")
                yield Button("Remove", id="unassign-confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.app.pop_screen()
        if event.button.id == "unassign-confirm":
            self.callback(self.assignment)


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

    /* LabJack pin-picker dialog */
    .pin-row {
        height: 3;
        margin: 0 0;
    }

    .pin-label {
        width: 8;
        padding: 1 1;
        text-style: bold;
        color: $accent;
    }

    .pin-row Select {
        width: 32;
    }

    .pin-warn {
        color: $warning;
        text-style: bold;
        margin: 1 0;
        height: auto;
    }

    /* Action buttons specific styling */
    #add_btn {
        background: mediumaquamarine;
        color: black;
    }

    #assign_btn {
        background: khaki;
        color: black;
    }

    #del_all_btn {
        background: coral;
        color: black;
    }

    /* Custom-device assignment screen */
    #assign_tree {
        background: $surface;
        border: solid hotpink;
        height: 1fr;
        max-height: 25;
        margin: 1 0;
    }

    #assign_tree > .tree--guides {
        color: hotpink;
    }

    #assign_tree > .tree--cursor {
        background: $accent 30%;
    }

    #assign_tree > .tree--highlight {
        background: hotpink 20%;
    }

    #assign_baud {
        max-width: 60;
    }

    #assign-dev-cancel {
        background: coral;
        color: black;
    }

    #assign-close {
        min-width: 12;
    }

    #unassign-cancel {
        background: mediumaquamarine;
        color: black;
    }

    #unassign-confirm {
        background: coral;
        color: black;
    }

    #asnet_name {
        max-width: 60;
    }

    #asnet-skip {
        background: coral;
        color: black;
    }

    #asnet-create {
        background: mediumaquamarine;
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
                self.assign_btn = Button("Assign Device", id="assign_btn")
                self.del_all_btn = Button("Delete All Nets", id="del_all_btn", variant="error")
                self.del_all_btn.display = False  # shown only when nets exist
                yield self.add_btn
                yield self.assign_btn
                yield self.del_all_btn
            # Right button container
            self.right_container = Horizontal(classes="button-container-right")
            with self.right_container:
                self.exit_btn = Button("Exit", id="exit_btn", variant="success")
                yield self.exit_btn

        yield Footer()

    def on_mount(self) -> None:
        # launch_tui fetched instruments and saved nets moments before
        # constructing the app — re-fetching here blocked the UI thread for
        # another box round-trip, so the freshly painted screen ignored
        # clicks for its duration.
        self._refresh_table()

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
            del self.loading_msg

    def run_box_job(self, work, on_done) -> None:
        """Run blocking box I/O on a worker thread, then update the UI.

        Every box round-trip (HTTP to the box's :9000 API) can take seconds; doing
        one synchronously inside an event handler freezes the whole event
        loop — buttons stop responding until it returns, which reads as
        "I have to click multiple times". ``work`` runs on a thread and must
        not touch widgets; ``on_done`` receives its return value (or the
        raised exception) back on the UI thread.
        """
        def job() -> None:
            try:
                result = work()
            except Exception as exc:
                result = exc
            self.call_from_thread(on_done, result)
        self.run_worker(job, thread=True, exclusive=False)

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

    def _fetch_saved_records(self) -> list | None:
        """Blocking ``GET /nets/list`` round-trip; None on failure.

        No widget access — safe to call from a ``run_box_job`` thread.
        """
        try:
            return _fetch_saved_nets_http(self.dut)
        except Exception:
            return None

    def _apply_saved_records(self, saved_from_disk: list) -> None:
        """Replace saved nets with the given backend records (UI thread)."""
        self.nets = [n for n in self.nets if not n.saved] + [
            Net(
                instrument=rec.get("instrument", "NA"),
                chan=rec.get("pin", "NA"),
                type=rec.get("role", "NA"),
                net=rec.get("name"),
                addr=rec.get("address", "NA"),
                saved=True,
                has_script=bool(rec.get("jlink_script") or rec.get("openocd_config")),
                purpose=rec.get("purpose", ""),
                notes=rec.get("notes", ""),
                tags=rec.get("tags", []),
            ) for rec in saved_from_disk
        ]
        self._ensure_autogen_unsaved()

    def _fetch_instruments(self) -> list | None:
        """Blocking instrument re-scan; None on failure.

        No widget access — safe to call from a ``run_box_job`` thread.
        """
        try:
            return _fetch_instruments_http(self.dut)
        except Exception:
            # Keep the previous scan rather than wiping the add list.
            return None

    def _apply_instruments(self, inst_list: list) -> None:
        """Rebuild the unsaved placeholder nets from a fresh scan (UI thread).

        Called after a custom-device (un)assignment changes what the scanner
        reports: the assigned instrument appears (or disappears) and its
        generic UART cable record does the inverse. Saved nets are left
        untouched; only the auto-generated placeholders are rebuilt.
        """
        self.inst_list = inst_list
        self.nets = [n for n in self.nets if n.saved]
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
        """Handle *Add Nets*, *Assign Device*, *Delete All Nets*, and *Exit* buttons."""
        if event.button.id == "add_btn":
            self.push_screen(AddScreen(self.nets, self.multi_labjack))
            return
        if event.button.id == "assign_btn":
            # The /custom-devices/list round-trip takes seconds — run it off the
            # UI thread so the app keeps responding, and disable the button
            # so the click visibly registered.
            self.assign_btn.disabled = True
            self.show_loading("Contacting box...")

            def _open_assign(data: object) -> None:
                self.hide_loading()
                self.assign_btn.disabled = False
                if not isinstance(data, dict):
                    self.show_error(
                        "Custom-device assignment needs newer box software — "
                        "run 'lager update' and retry."
                    )
                    return
                self.push_screen(AssignDeviceScreen(data))

            self.run_box_job(
                lambda: _run_custom_devices(self.dut, "list"),
                _open_assign,
            )
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
        if self.assign_btn.visible:
            buttons.append(self.assign_btn)
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
        inst_list = _fetch_instruments_http(dut)
    except Exception:
        inst_list = []

    try:
        saved_list = _fetch_saved_nets_http(dut)
    except Exception:
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
            has_script=bool(rec.get("jlink_script") or rec.get("openocd_config")),
            purpose=rec.get("purpose", ""),
            notes=rec.get("notes", ""),
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
