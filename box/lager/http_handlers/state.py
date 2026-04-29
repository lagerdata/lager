# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Global state management for HTTP/WebSocket sessions.

This module provides thread-safe storage for active sessions across
UART, supply, and battery handlers.
"""
import threading

# Global dictionary to track active UART sessions
# Format: {session_id: {'driver': driver_obj, 'thread': thread_obj, 'stop_event': event_obj}}
active_uart_sessions = {}
active_uart_sessions_lock = threading.Lock()

# Global dictionary to track active supply monitoring sessions
# Format: {session_id: {'netname': str, 'stop_event': event_obj, 'thread': thread_obj, 'instrument_lock': Lock}}
active_supply_sessions = {}
active_supply_sessions_lock = threading.Lock()

# Global dictionary to track active battery monitoring sessions
# Format: {session_id: {'netname': str, 'stop_event': event_obj, 'thread': thread_obj, 'instrument_lock': Lock}}
active_battery_sessions = {}
active_battery_sessions_lock = threading.Lock()

# Global dictionary to track instrument locks (one lock per netname to prevent concurrent SCPI queries)
# Format: {netname: threading.Lock()}
instrument_locks = {}
instrument_locks_lock = threading.Lock()


def get_instrument_lock(netname):
    """Get or create an instrument lock for a given netname."""
    with instrument_locks_lock:
        if netname not in instrument_locks:
            instrument_locks[netname] = threading.Lock()
        return instrument_locks[netname]


def conflicting_other_role_session(target_role, target_address):
    """Return ``(other_role, other_netname)`` if the VISA address
    ``target_address`` is currently held by an active monitoring session
    in the *other* role; otherwise None.

    Used to fail-fast on the Keithley 2281S cross-role concurrent-use
    pattern. The 2281S has two mutually-exclusive entry functions
    (``:ENTR:FUNC POW`` for power-supply mode, ``:ENTR:FUNC BATT`` for
    battery-simulator mode); each Lager driver flips the entry function
    to its preferred mode on every SCPI command. If a supply TUI and a
    battery CLI/TUI both target the same physical Keithley, they fight
    over the entry function on every poll, producing intermittent SCPI
    errors and ``[Errno 16] Resource busy`` events that aren't
    actionable from the user's perspective.

    This helper is conservative: it ONLY checks the active monitoring
    sessions stored in ``active_{supply,battery}_sessions``. Sequential
    CLI calls (no TUI) don't populate those dicts, so V.4-style
    sequential cross-role workflows remain unaffected — they continue
    to work because hardware_service.py serializes per-VISA-address
    SCPI calls and switches the entry function cleanly between calls.

    Args:
        target_role: ``'power-supply'`` or ``'battery'`` — the role the
            incoming request is *trying* to use.
        target_address: VISA address of the physical instrument the
            request will touch.

    Returns:
        ``(other_role, other_netname)`` if a session in the opposite
        role is already monitoring the same address; ``None`` otherwise
        (including when ``target_address`` is None/empty).
    """
    if not target_address:
        return None
    if target_role == 'power-supply':
        with active_battery_sessions_lock:
            for session_info in active_battery_sessions.values():
                if session_info.get('address') == target_address:
                    return ('battery', session_info.get('netname'))
    elif target_role == 'battery':
        with active_supply_sessions_lock:
            for session_info in active_supply_sessions.values():
                if session_info.get('address') == target_address:
                    return ('power-supply', session_info.get('netname'))
    return None


def format_cross_role_conflict_message(target_role, target_netname,
                                       target_address, other_role,
                                       other_netname):
    """Compose the user-facing error message for a Keithley 2281S
    cross-role conflict. Centralised so the wording stays identical
    across the HTTP and WebSocket entry points."""
    return (
        f"Cannot run a {target_role} command on '{target_netname}': the "
        f"same physical instrument (VISA address {target_address}) is "
        f"currently active as a {other_role} net '{other_netname}'. "
        f"The Keithley 2281S can only operate as a power supply OR a "
        f"battery simulator at one time — its Power Supply "
        f"(:ENTR:FUNC POW) and Battery Simulator (:ENTR:FUNC BATT) "
        f"entry functions are mutually exclusive in the instrument's "
        f"firmware. Close the {other_role} session first, or configure "
        f"only one role on this Keithley."
    )


def cleanup_all_sessions():
    """Clean up all active sessions. Called during graceful shutdown."""
    # Cleanup UART sessions
    with active_uart_sessions_lock:
        for session_id, session in list(active_uart_sessions.items()):
            try:
                if 'stop_event' in session:
                    session['stop_event'].set()
                if 'driver' in session:
                    session['driver']._cleanup()
            except Exception:
                pass
        active_uart_sessions.clear()

    # Cleanup supply sessions
    with active_supply_sessions_lock:
        for session_id, session in list(active_supply_sessions.items()):
            try:
                if 'stop_event' in session:
                    session['stop_event'].set()
            except Exception:
                pass
        active_supply_sessions.clear()

    # Cleanup battery sessions
    with active_battery_sessions_lock:
        for session_id, session in list(active_battery_sessions.items()):
            try:
                if 'stop_event' in session:
                    session['stop_event'].set()
            except Exception:
                pass
        active_battery_sessions.clear()
