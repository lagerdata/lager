# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The agent's hardware worker.

Serves the Box's existing hardware service -- the same Flask application, the
same routes, the same driver cache and locks -- from a native process, behind
the hostlink token and handshake.

The hardware service module is imported and wrapped, not modified. Its own
``run_service`` binds every interface on a fixed port for use inside a
container; here the application is bound to loopback on the provisioned
port instead.

The Box code names its configuration directory by the literal path
``/etc/lager``. The agent uses that same directory on the host, which the
installer creates and shares into the container, so nothing is translated.
"""
from __future__ import annotations

import logging
import os
import platform
import sys

from flask import Flask, jsonify, request

from lager.hostlink import auth, config, protocol

from . import AGENT_VERSION

logger = logging.getLogger(__name__)

_STATE_KEY = "lager_hostlink"


def _platform_name() -> str:
    return f"{platform.system().lower()}-{platform.machine().lower()}"


def install(app: Flask, endpoint: config.Endpoint) -> Flask:
    """Put authentication and the handshake in front of ``app``.

    Safe to call again with a new endpoint: the hooks are registered once and
    read the current endpoint from the application's state.
    """
    state = app.extensions.get(_STATE_KEY)
    if state is not None:
        state["endpoint"] = endpoint
        return app

    state = {"endpoint": endpoint}
    app.extensions[_STATE_KEY] = state

    def _guard():
        if not auth.is_authorized(request.headers, state["endpoint"].token):
            return jsonify(protocol.error_body(
                protocol.ERR_UNAUTHORIZED,
                "Missing or invalid host agent token.")), 401

        if request.path == protocol.HANDSHAKE_PATH:
            return None
        claimed = request.headers.get(protocol.PROTOCOL_HEADER)
        if claimed is None:
            return jsonify(protocol.error_body(
                protocol.ERR_PROTOCOL_MISMATCH,
                f"Missing {protocol.PROTOCOL_HEADER} header.")), 426
        try:
            protocol.negotiate(int(claimed), int(claimed))
        except (ValueError, protocol.ProtocolMismatch) as exc:
            return jsonify(protocol.error_body(
                protocol.ERR_PROTOCOL_MISMATCH, str(exc))), 426
        return None

    # First in line: nothing the hardware service does may run before the
    # token is checked.
    app.before_request_funcs.setdefault(None, []).insert(0, _guard)

    @app.route(protocol.HANDSHAKE_PATH, methods=["POST"])
    def _hostlink_handshake():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(protocol.error_body(
                protocol.ERR_BAD_REQUEST, "Handshake body must be a JSON object.")), 400
        try:
            agreed = protocol.negotiate(data.get("protocol_min"), data.get("protocol_max"))
        except ValueError as exc:
            return jsonify(protocol.error_body(protocol.ERR_BAD_REQUEST, str(exc))), 400
        except protocol.ProtocolMismatch as exc:
            return jsonify(protocol.error_body(
                protocol.ERR_PROTOCOL_MISMATCH, str(exc))), 426
        logger.info("Handshake from Box %s: protocol %s",
                    data.get("box_version") or "(unknown version)", agreed)
        return jsonify(protocol.handshake_response(agreed, AGENT_VERSION, _platform_name()))

    return app


def create_app(endpoint: config.Endpoint) -> Flask:
    """The hardware service application, wrapped for use as the agent."""
    # Imported here, not at module import, so that importing this module
    # (for its tests, or for --help) does not pull in every driver.
    from lager import hardware_service

    # The agent is where the hardware is. If the container's switch leaked
    # into this environment, the stand-ins would call the agent from itself.
    os.environ.pop("LAGER_HOSTLINK", None)

    if sys.platform == "darwin":
        from . import darwin_usb

        darwin_usb.install()
        try:
            from . import darwin_scan

            darwin_scan.install_serial()
        except Exception:
            logger.exception("serial port support is unavailable")
        # The debug net runs in this process for the container's stand-in.
        try:
            from . import darwin_debug

            darwin_debug.install()
        except Exception:
            logger.exception("debug support is unavailable")
    from . import debug_rpc, net_rpc, serial_rpc, usb_rpc

    debug_rpc.install(hardware_service.app)
    usb_rpc.install(hardware_service.app)
    net_rpc.install(hardware_service.app)
    serial_rpc.install(hardware_service.app, endpoint.bind)
    return install(hardware_service.app, endpoint)


def _log_native_crashes() -> None:
    """Write every thread's Python stack to the log when a driver's native code
    kills the worker. On Apple silicon a native assertion arrives as SIGTRAP,
    which faulthandler does not watch by default; the signal is passed on
    afterwards, so the worker still dies and is restarted as before."""
    import faulthandler
    import signal

    faulthandler.enable(all_threads=True)
    faulthandler.register(signal.SIGTRAP, all_threads=True, chain=True)


def _start_hidapi_on_main_thread() -> None:
    """Make the worker's first hidapi call from the main thread.

    On macOS hidapi schedules its one IOHIDManager on the run loop of the
    thread that first calls it. Requests are served on short-lived threads, so
    without this the manager is left on a run loop that is freed when that
    request ends, and a later hidapi call (a YKUSH open, or hid_exit at exit)
    crashes the worker in CoreFoundation. The main thread lives as long as
    the worker.
    """
    try:
        import hid
    except ImportError:
        return
    try:
        hid.enumerate(0, 0)
    except Exception:
        logger.exception("hidapi did not start; YKUSH hubs are unavailable")


def serve(config_dir: str) -> None:
    """Run the worker until it exits or the hardware service restarts it."""
    _log_native_crashes()
    _start_hidapi_on_main_thread()
    endpoint = config.load_endpoint(config.endpoint_path(config_dir))
    app = create_app(endpoint)
    logger.info("Lager host agent %s (%s) serving hardware on %s:%s, config %s",
                AGENT_VERSION, _platform_name(), endpoint.bind, endpoint.port, config_dir)
    app.run(host=endpoint.bind, port=endpoint.port, debug=False, threaded=True)
