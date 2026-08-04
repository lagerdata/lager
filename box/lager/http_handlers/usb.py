# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
USB HTTP handlers for the Lager Box HTTP+WebSocket Server.

- POST /usb/command — hub-port control (enable/disable/toggle/state).
  Mirrors the supply/battery fast-path: the CLI POSTs here on port 9000
  instead of uploading a Python script to :5000/python, avoiding the
  subprocess+import cost per call. The hub drivers (Acroname BrainStem,
  YKUSH) open the hub fresh per operation and release it immediately
  (see automation/usb_hub/) so no process pins the exclusive USB claim;
  they cache discovery metadata to keep each open cheap.
- GET /usb/devices — lightweight sysfs USB bus enumeration (lsusb-like),
  safe to poll frequently: no exclusive device access, no VISA scan.
- POST /usb/dfu — run dfu-util on the box (list / download / detach),
  so USB-DFU flashing needs no host-side tooling.

Unlike supply/battery, USB drivers do not use pyvisa, so this handler
does not delegate to hardware_service.py.
"""
import base64
import binascii
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading

from flask import Flask, jsonify, request

from lager import (
    DeviceNotFoundError,
    HubOperationTimeout,
    LibraryMissingError,
    PortStateError,
    USBBackendError,
)
from lager.automation import usb_hub
from lager.automation.usb_hub import usb_net
from lager.util import self_restart as _self_restart
from lager.util.device_lock import DeviceLockError

logger = logging.getLogger(__name__)

_VALID_ACTIONS = ("enable", "disable", "toggle", "state")

# Per-service cooldown stamp for the shared self-restart (box_http_server runs
# under start-services.sh's `while true` supervisor, like hardware_service).
_BHS_SELF_RESTART_STAMP = "/tmp/lager-box-http-server-self-restart"


def _usb_net_address(netname):
    """Best-effort VISA-style address for a USB net, for the sysfs-gated
    self-restart check. None if it can't be resolved."""
    try:
        from lager.automation.usb_hub.dispatcher import _load_net_definitions
        return (_load_net_definitions().get(netname) or {}).get("address")
    except Exception:
        return None


def _driver_holds_usb_context(netname):
    """Can this net's driver even HAVE an orphaned USB context?

    Best-effort and fails safe: anything unresolvable answers True, which is
    today's behaviour. Only a driver that positively declares otherwise is
    exempted.
    """
    try:
        from lager.automation.usb_hub.dispatcher import (
            _controller_for, _load_net_definitions,
        )
        info = (_load_net_definitions().get(netname) or {})
        controller = _controller_for(info)
        return bool(getattr(controller, "holds_usb_context_between_ops", True))
    except Exception:
        return True


def _self_restart_if_wedged(netname, action, exc):
    """If a USB op failed because the device is unreachable but the device is
    still on the bus (sysfs), this process's USB/HID context is wedged after a
    re-enumeration — exit so the supervisor respawns box_http_server with a
    clean context. No-op when the device is genuinely absent or in cooldown.

    Skipped entirely for a driver that opens and closes inside every call.
    Restarting repairs exactly one thing: a USB handle this process orphaned
    across a re-enumeration. A driver that keeps no handle has none to orphan,
    so the restart cannot help — it only drops every other in-flight operation
    the service is holding, including UART sessions and running scripts.
    """
    if not _driver_holds_usb_context(netname):
        logger.warning(
            "[HTTP] usb %s %s: not self-restarting — this driver holds no USB "
            "context between operations, so a respawn cannot reach it. Cause: %s",
            action, netname, exc,
        )
        return
    address = _usb_net_address(netname)
    if address:
        _self_restart.maybe_self_restart(
            address, f"usb {action} {netname}", service="box_http_server",
            stamp_path=_BHS_SELF_RESTART_STAMP)


def _self_restart_on_hung_hub(lock_key):
    """Same wedge, other shape: the driver call never returned.

    Wired into the driver layer (``usb_net.set_hang_hook``) rather than called
    from this handler, because the request that notices a hang is usually not
    this one — the 1 Hz ``/nets/state`` sweep gets there first, and it
    deliberately swallows a single hub's failure so one bad hub cannot hide the
    rest. Detecting and responding in the same place covers every caller.

    A hub's lock key IS its VISA address whenever the net has one, which is
    what the sysfs gate needs; a driver falling back to a synthetic key just
    fails that gate and no restart happens. Scheduled rather than run inline so
    the in-flight request can still answer 504 — see
    ``self_restart.schedule_self_restart_for_hang``. Same sysfs gate and
    cooldown stamp as the unreachable path, so a hub that is genuinely
    unplugged still does not send the service into a restart loop."""
    _self_restart.schedule_self_restart_for_hang(
        lock_key, f"usb hub operation on {lock_key}",
        service="box_http_server", stamp_path=_BHS_SELF_RESTART_STAMP)


# Serialize hub calls within this process. The Acroname/YKUSH drivers also
# hold a cross-process flock per physical hub (see automation/usb_hub), and
# bounding the wait here keeps concurrent HTTP requests from queueing on it.
#
# The bound is the point. This lock used to be taken with a plain `with`, so a
# single hub call stuck in native code held it forever and every later request
# — including the 1 Hz state polls — blocked behind it with no timeout, which
# is how one wedged hub took the whole USB endpoint down. The wait has to
# resolve well inside the callers' own HTTP timeouts, or they report a
# transport timeout instead of the real error.
#
# Matched to the drivers' hub lock (usb_net._LOCK_TIMEOUT_S), which this one
# sits in front of. A request that waits here and then waits again inside the
# driver can spend up to the sum before answering; that needs both a wedged
# hub AND a second caller already queued, and both waits end in the same 503,
# so the loser of that race loses only the message, never correctness.
_USB_LOCK_TIMEOUT_S = 10.0

_usb_lock = threading.Lock()

# Serialize dfu-util runs separately from hub-port ops: a DFU download can
# take minutes and must not block enable/disable/state calls.
_dfu_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# sysfs USB bus enumeration (GET /usb/devices)
# --------------------------------------------------------------------------- #

# The implementation moved to lager.util.usb_sysfs so the USB hub drivers can
# use it too: a driver cannot import this module (it would be a cycle), but both
# layers can import util. Re-exported here because /usb/devices and its tests
# have always imported it from this module.
from lager.util.usb_sysfs import enumerate_usb_devices  # noqa: F401

# Kept in this module, not imported, so tests can patch the route's sysfs root
# by patching this attribute. The route passes it explicitly for that reason.
_SYSFS_USB_ROOT = "/sys/bus/usb/devices"


# --------------------------------------------------------------------------- #
# dfu-util (POST /usb/dfu)
# --------------------------------------------------------------------------- #

_DFU_ACTIONS = ("list", "download", "detach")
_DFU_DEFAULT_TIMEOUT_S = 120
_DFU_MAX_TIMEOUT_S = 600
# Soft cap on decoded firmware size. Flask's request body limit is 100MB;
# DFU images are typically far smaller, and a 100MB base64 body would
# pin the DFU lock and /tmp for the whole download window.
_DFU_MAX_FIRMWARE_BYTES = 32 * 1024 * 1024
_DFU_SAFE_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,8}$")

# `dfu-util -l` device lines, e.g.:
# Found DFU: [0483:df11] ver=2200, devnum=42, cfg=1, intf=0, path="1-1.4",
#   alt=1, name="@Option Bytes ...", serial="STM32..."
_DFU_LIST_RE = re.compile(
    r'^Found (?P<mode>DFU|Runtime): \[(?P<vid>[0-9a-fA-F]{4}):'
    r'(?P<pid>[0-9a-fA-F]{4})\] (?P<rest>.*)$'
)
_DFU_KV_RE = re.compile(r'(\w+)=(?:"([^"]*)"|([^,]*))')


def _parse_dfu_list(stdout):
    """Parse ``dfu-util -l`` output into structured device entries."""
    devices = []
    for line in stdout.splitlines():
        match = _DFU_LIST_RE.match(line.strip())
        if not match:
            continue
        entry = {
            "mode": match.group("mode"),
            "vid": match.group("vid").lower(),
            "pid": match.group("pid").lower(),
        }
        for key, quoted, bare in _DFU_KV_RE.findall(match.group("rest")):
            value = quoted if quoted else bare.strip()
            if key in ("devnum", "cfg", "intf", "alt"):
                try:
                    entry[key] = int(value)
                    continue
                except ValueError:
                    pass
            entry[key] = value
        devices.append(entry)
    return devices


def _build_dfu_args(action, params, firmware_path=None):
    """Build the dfu-util argv for one action. Pure — unit-testable."""
    args = ["dfu-util"]
    vid_pid = params.get("vid_pid")
    if vid_pid:
        args += ["-d", str(vid_pid)]
    serial = params.get("serial")
    if serial:
        args += ["-S", str(serial)]
    if action == "list":
        args += ["-l"]
        return args
    alt = params.get("alt")
    if alt is not None:
        args += ["-a", str(alt)]
    if action == "detach":
        args += ["-e"]
        return args
    # download
    dfuse_address = params.get("dfuse_address")
    if dfuse_address:
        args += ["-s", str(dfuse_address)]
    args += ["-D", firmware_path]
    if params.get("reset"):
        args += ["-R"]
    return args


def _dfu_timeout(params):
    try:
        timeout = float(params.get("timeout_seconds") or _DFU_DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout = _DFU_DEFAULT_TIMEOUT_S
    return max(1.0, min(timeout, _DFU_MAX_TIMEOUT_S))


def _run_dfu_util(args, timeout):
    """Run dfu-util, returning (exit_code, stdout, stderr)."""
    proc = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def register_usb_routes(app: Flask) -> None:
    """Register USB HTTP routes with the Flask app."""

    # This process runs under start-services.sh's supervisor, so it is one of
    # the few that may answer a hung hub by exiting. Claiming that here, next
    # to the cooldown stamp it uses, keeps the whole self-restart policy for
    # box_http_server in one file.
    usb_net.set_hang_hook(_self_restart_on_hung_hub)

    @app.route('/usb/command', methods=['POST'])
    def usb_command_http():
        """
        Execute a USB hub command against a configured USB net.

        Request body:
        {
            "netname": "usb1",
            "action": "enable" | "disable" | "toggle"
        }

        Returns:
        {
            "success": true,
            "action": "toggle",
            "state": "enabled" | "disabled",   # resulting port state
            "message": "USB port 'usb1' toggled → disabled"
        }
        """
        try:
            data = request.get_json() or {}
            netname = data.get('netname')
            action = data.get('action')

            if not netname or action not in _VALID_ACTIONS:
                return jsonify({
                    'success': False,
                    'error': 'netname and action (enable|disable|toggle|state) are required',
                }), 400

            # Bounded acquire, not `with`: a hub call wedged in native code
            # holds this lock for the life of the process, and queueing behind
            # it forever is the failure this endpoint is being protected from.
            if not _usb_lock.acquire(timeout=_USB_LOCK_TIMEOUT_S):
                logger.warning(
                    "[HTTP] /usb/command %s %s: hub lock still held after %.0fs",
                    action, netname, _USB_LOCK_TIMEOUT_S)
                return jsonify({
                    'success': False,
                    'error': 'hub-busy: another USB hub operation is still running',
                }), 503

            try:
                result = getattr(usb_hub, action)(netname)
            # HubOperationTimeout is a USBBackendError; it must be matched
            # first or a hang reports as an ordinary backend error (502).
            except HubOperationTimeout as e:
                # The self-restart was already scheduled by the driver layer's
                # hang hook (see _self_restart_on_hung_hub); this request's job
                # is just to say what happened before the respawn lands.
                logger.error("[HTTP] /usb/command %s %s hung: %s",
                             action, netname, e)
                return jsonify({
                    'success': False, 'error': f'hub-op-timeout: {e}',
                }), 504
            except DeviceLockError as e:
                # Neither lock layer could claim the hub in time — another
                # thread or another box process still has it.
                logger.warning("[HTTP] /usb/command hub unavailable: %s", e)
                return jsonify({'success': False, 'error': f'hub-busy: {e}'}), 503
            except LibraryMissingError as e:
                logger.warning("[HTTP] /usb/command library missing: %s", e)
                return jsonify({'success': False, 'error': f'library-missing: {e}'}), 500
            except DeviceNotFoundError as e:
                logger.warning("[HTTP] /usb/command device not found: %s", e)
                # "device not found" + still in sysfs = wedged USB context.
                _self_restart_if_wedged(netname, action, e)
                return jsonify({'success': False, 'error': f'device-not-found: {e}'}), 404
            except PortStateError as e:
                logger.warning("[HTTP] /usb/command port-state error: %s", e)
                return jsonify({'success': False, 'error': f'port-state: {e}'}), 409
            except USBBackendError as e:
                logger.exception("[HTTP] /usb/command backend error")
                return jsonify({'success': False, 'error': f'backend: {e}'}), 502
            except KeyError as e:
                # Net not found in saved_nets.json (raised by dispatcher).
                logger.warning("[HTTP] /usb/command unknown net: %s", e)
                return jsonify({'success': False, 'error': f"USB net not found: {e}"}), 404
            except (RuntimeError, FileNotFoundError) as e:
                logger.exception("[HTTP] /usb/command dispatcher error")
                return jsonify({'success': False, 'error': str(e)}), 502
            finally:
                _usb_lock.release()

            # toggle and state both return the live port state from the
            # dispatcher; enable/disable are unambiguous from the action itself.
            if action == "toggle":
                state = "enabled" if result else "disabled"
                message = f"USB port '{netname}' toggled → {state}"
            elif action == "state":
                state = "enabled" if result else "disabled"
                message = f"USB port '{netname}' is {state}"
            else:
                state = "enabled" if action == "enable" else "disabled"
                message = f"USB port '{netname}' {action}d"

            return jsonify({
                'success': True,
                'action': action,
                'state': state,
                'message': message,
            })
        except Exception as e:
            logger.exception("[HTTP] /usb/command unexpected error")
            # YKUSH/pykush raises a plain "device not found" (not lager's
            # DeviceNotFoundError), so an unreachable wedge lands here. Only
            # self-restart when the error means the device is unreachable (not
            # a device that responded with an error).
            if _self_restart.looks_like_device_unreachable(e):
                _self_restart_if_wedged(
                    locals().get('netname'), locals().get('action'), e)
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/usb/devices', methods=['GET'])
    def usb_devices_http():
        """
        Enumerate USB devices on the box's bus from sysfs.

        Optional query parameters: vid, pid (hex, with or without 0x),
        serial (exact iSerial match).

        Returns:
        {
            "success": true,
            "devices": [
                {"sysfs_name": "1-1.4", "vid": "0483", "pid": "df11",
                 "serial": "...", "product": "...", "manufacturer": "...",
                 "busnum": "1", "devnum": "42", "devpath": "1.4",
                 "device_class": "00", "speed": "12"},
                ...
            ]
        }
        """
        try:
            devices = enumerate_usb_devices(
                # Passed explicitly rather than defaulted inside the helper so
                # this module's _SYSFS_USB_ROOT stays the thing tests patch.
                sysfs_root=_SYSFS_USB_ROOT,
                vid=request.args.get('vid'),
                pid=request.args.get('pid'),
                serial=request.args.get('serial'),
            )
            return jsonify({'success': True, 'devices': devices})
        except Exception as e:
            logger.exception("[HTTP] /usb/devices unexpected error")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/usb/dfu', methods=['POST'])
    def usb_dfu_http():
        """
        Run dfu-util on the box.

        Request body (the box-level {action, params} envelope):
        {
            "action": "list" | "download" | "detach",
            "params": {
                "vid_pid": "0483:df11",         # optional -d filter
                "serial": "STM32...",           # optional -S filter
                "alt": 0,                       # optional -a (download/detach)
                "dfuse_address": "0x08000000",  # optional -s (download, DfuSe)
                "reset": true,                  # optional -R (download)
                "firmware": "<base64>",         # required for download
                "filename": "fw.bin",           # optional, temp-file suffix
                "timeout_seconds": 120          # optional, clamped to 600
            }
        }

        Returns the command envelope; `value` carries `devices` for list,
        and `exit_code` / `stdout` / `stderr` for every action.
        """
        firmware_path = None
        try:
            data = request.get_json() or {}
            action = data.get('action')
            params = data.get('params') or {}
            if not isinstance(params, dict):
                return jsonify({
                    'success': False,
                    'error': 'params must be a JSON object',
                }), 400
            if action not in _DFU_ACTIONS:
                return jsonify({
                    'success': False,
                    'error': 'action (list|download|detach) is required',
                }), 400

            dfu_util = shutil.which('dfu-util')
            if dfu_util is None:
                return jsonify({
                    'success': False,
                    'error': ('dfu-util-missing: dfu-util is not installed on '
                              'this box. Install it with '
                              "'lager box-config apt add dfu-util'"),
                }), 500

            if action == 'download':
                firmware_b64 = params.get('firmware')
                if not firmware_b64:
                    return jsonify({
                        'success': False,
                        'error': "download requires base64 'firmware' in params",
                    }), 400
                try:
                    firmware = base64.b64decode(firmware_b64, validate=True)
                except (binascii.Error, ValueError) as e:
                    return jsonify({
                        'success': False,
                        'error': f'invalid base64 firmware: {e}',
                    }), 400
                if len(firmware) > _DFU_MAX_FIRMWARE_BYTES:
                    return jsonify({
                        'success': False,
                        'error': (f'firmware exceeds '
                                  f'{_DFU_MAX_FIRMWARE_BYTES} byte limit'),
                    }), 400
                suffix = os.path.splitext(params.get('filename') or '')[1] or '.bin'
                if not _DFU_SAFE_SUFFIX_RE.match(suffix):
                    suffix = '.bin'
                fd, firmware_path = tempfile.mkstemp(
                    prefix='lager-dfu-', suffix=suffix)
                with os.fdopen(fd, 'wb') as fh:
                    fh.write(firmware)

            args = _build_dfu_args(action, params, firmware_path)
            # Use the resolved binary path so PATH cannot change between
            # the which() check and the subprocess.
            args[0] = dfu_util
            timeout = _dfu_timeout(params)
            try:
                with _dfu_lock:
                    exit_code, stdout, stderr = _run_dfu_util(args, timeout)
            except subprocess.TimeoutExpired:
                return jsonify({
                    'success': False,
                    'error': f'dfu-util timed out after {timeout:.0f}s',
                }), 504

            value = {'exit_code': exit_code, 'stdout': stdout, 'stderr': stderr}
            if exit_code != 0:
                tail = (stderr or stdout).strip().splitlines()[-3:]
                return jsonify({
                    'success': False,
                    'action': action,
                    'value': value,
                    'error': 'dfu-util exited with code '
                             f"{exit_code}: {' / '.join(tail) or 'no output'}",
                }), 502

            if action == 'list':
                value['devices'] = _parse_dfu_list(stdout)
                message = f"Found {len(value['devices'])} DFU device(s)"
            else:
                message = f'dfu-util {action} completed'
            return jsonify({
                'success': True,
                'action': action,
                'value': value,
                'message': message,
            })
        except Exception as e:
            logger.exception("[HTTP] /usb/dfu unexpected error")
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            if firmware_path:
                try:
                    os.remove(firmware_path)
                except OSError:
                    pass
