# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Diagnose HTTP endpoints for the lager box.

Surfaces host-side info that `lager diagnose <net>` (added in 0.20.0) needs
to classify a misbehaving instrument net as host-side (usbtmc loaded,
competing process holds the device, etc.) vs instrument-side (firmware
wedged — needs mains power-cycle, software can't recover).

Two endpoints, both on box_http_server (port 5000):

  GET /diagnose/usb?address=<visa>
      Walks /sys/bus/usb/devices to map the VISA address to the kernel
      USB device path, then reports: lsof on that path, lsmod usbtmc
      state, and a tail of dmesg USB events. Pure read-only.

  GET /diagnose/visa?address=<visa>
      Opens a *fresh* pyvisa ResourceManager and queries `*IDN?` with
      a short timeout. Skips the open if hw_service is already holding
      a shared session for this address (we'd collide with ourselves
      and never get an answer). Closes immediately.

Both return JSON; callers (CLI) tolerate per-endpoint 404s from older
boxes that pre-date the diagnose feature.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import subprocess
import time

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)


_USB_VID_RE = re.compile(r'USB\d*::0x?([0-9A-Fa-f]{4})::0x?([0-9A-Fa-f]{4})::([^:]*)::INSTR', re.IGNORECASE)


def _parse_visa_address(addr: str):
    """Return (vid_hex, pid_hex, serial) tuple from a USB VISA address, or None."""
    if not addr:
        return None
    m = _USB_VID_RE.match(addr)
    if not m:
        return None
    return m.group(1).lower(), m.group(2).lower(), m.group(3)


def _find_usb_sysfs(vid_hex, pid_hex):
    """Walk /sys/bus/usb/devices/ for a device matching (vid, pid). Returns
    sysfs path like '/sys/bus/usb/devices/1-4' or None."""
    for dev in glob.glob('/sys/bus/usb/devices/*/'):
        try:
            with open(os.path.join(dev, 'idVendor')) as f:
                if f.read().strip().lower() != vid_hex:
                    continue
            with open(os.path.join(dev, 'idProduct')) as f:
                if f.read().strip().lower() != pid_hex:
                    continue
            return dev.rstrip('/')
        except (FileNotFoundError, OSError):
            continue
    return None


def _usbfs_path_for_sysfs(sysfs):
    """Read busnum + devnum from a sysfs USB device path and return the
    /dev/bus/usb/BBB/DDD path that lsof / fuser operate on, or None."""
    try:
        with open(os.path.join(sysfs, 'busnum')) as f:
            busnum = int(f.read().strip())
        with open(os.path.join(sysfs, 'devnum')) as f:
            devnum = int(f.read().strip())
        return f'/dev/bus/usb/{busnum:03d}/{devnum:03d}'
    except (FileNotFoundError, ValueError, OSError):
        return None


def _run(cmd, timeout=5):
    """Run a shell command, return (rc, stdout, stderr) — never raises."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, '', f'timeout after {timeout}s'
    except Exception as e:  # pragma: no cover
        return -1, '', str(e)


def register_diagnose_routes(app: Flask) -> None:
    """Register `/diagnose/usb` and `/diagnose/visa` on the Flask app."""

    @app.route('/diagnose/usb', methods=['GET'])
    def diagnose_usb():
        addr = (request.args.get('address') or '').strip()
        parts = _parse_visa_address(addr)
        if not parts:
            return jsonify({
                'address': addr,
                'error': 'not a USB VISA address (expected USB0::0xVID::0xPID::SERIAL::INSTR)',
            }), 400

        vid, pid, serial = parts
        sysfs = _find_usb_sysfs(vid, pid)
        if not sysfs:
            return jsonify({
                'address': addr,
                'enumerated': False,
                'sysfs_path': None,
                'device_path': None,
                'is_usbtmc': False,
                'lsof': [],
                'usbtmc_loaded': _usbtmc_loaded(),
                'dmesg_tail': _dmesg_usb_tail(),
                'classification_hint': 'instrument not enumerated on USB — check power and cabling',
            })

        device_path = _usbfs_path_for_sysfs(sysfs)
        lsof_lines = _holders_via_proc(device_path) if device_path else []

        return jsonify({
            'address': addr,
            'enumerated': True,
            'sysfs_path': sysfs,
            'device_path': device_path,
            'vid': vid,
            'pid': pid,
            'serial': serial,
            'is_usbtmc': _device_is_usbtmc(sysfs),
            'lsof': lsof_lines,
            'usbtmc_loaded': _usbtmc_loaded(),
            'dmesg_tail': _dmesg_usb_tail(),
        })

    @app.route('/diagnose/visa', methods=['GET'])
    def diagnose_visa():
        addr = (request.args.get('address') or '').strip()
        if not addr:
            return jsonify({'error': 'address parameter required'}), 400

        # Heads-up: if hw_service already has a shared session pool entry
        # for this address (the common case for Keithley dual-role drivers),
        # we WILL collide with it and almost certainly return EBUSY at the
        # set_configuration call. Detection has to go cross-process —
        # box_http_server (this Flask app, port 9000) is a SEPARATE PROCESS
        # from hardware_service (port 8080), so importing _visa_resources
        # locally would see this process's own empty copy, not the live
        # state. Ask hw_service over HTTP for the canonical answer.
        try:
            import urllib.request as _urlreq
            import urllib.parse as _urlparse
            import json as _json
            disp_url = f'http://127.0.0.1:8080/diagnose/dispatcher?address={_urlparse.quote(addr, safe=":/")}'
            with _urlreq.urlopen(disp_url, timeout=2.0) as resp:
                disp_body = _json.loads(resp.read().decode('utf-8'))
            has_shared = bool(disp_body.get('cached_session'))
        except Exception:
            # If hw_service is unreachable, fall through to a real probe —
            # there's nothing to collide with if it's down.
            has_shared = False

        if has_shared:
            return jsonify({
                'address': addr,
                'skipped': True,
                'reason': 'hw_service already holds a shared session for this address; '
                          'fresh pyvisa open would collide and EBUSY. See '
                          'the Dispatcher section for cached-session state.',
            })

        # Open a fresh session and query IDN. Short timeout so a wedged
        # instrument returns quickly.
        try:
            import pyvisa  # noqa: WPS433 — optional dep
        except Exception as e:
            return jsonify({'address': addr, 'error': f'pyvisa not available: {e}'}), 500

        start = time.time()
        rm = pyvisa.ResourceManager()
        inst = None
        try:
            inst = rm.open_resource(addr)
            inst.timeout = 2000  # ms; wedged firmware should bounce off this
            idn = inst.query('*IDN?').strip()
            elapsed_ms = int((time.time() - start) * 1000)
            return jsonify({
                'address': addr,
                'idn': idn,
                'elapsed_ms': elapsed_ms,
            })
        except Exception as e:
            elapsed_ms = int((time.time() - start) * 1000)
            msg = str(e).lower()
            if 'resource busy' in msg or 'errno 16' in msg:
                klass = 'busy'
            elif (
                'no such device' in msg
                or 'errno 19' in msg
                or 'errno 2' in msg
                or 'entity not found' in msg
            ):
                klass = 'nodev'
            elif 'timed out' in msg or 'errno 110' in msg or 'timeout' in msg:
                klass = 'timeout'
            else:
                klass = 'other'
            return jsonify({
                'address': addr,
                'error': str(e),
                'error_class': klass,
                'elapsed_ms': elapsed_ms,
            })
        finally:
            try:
                if inst is not None:
                    inst.close()
            except Exception:
                pass
            try:
                rm.close()
            except Exception:
                pass


def _device_is_usbtmc(sysfs: str) -> bool:
    """True if any USB interface of the device at sysfs declares
    Application-Specific class (0xFE) + USB-TMC subclass (0x03).
    Used by the classifier to distinguish 'this is a USB-TMC instrument
    that pyvisa should handle' from 'this is a vendor-SDK device (LabJack,
    Picoscope, Acroname) that pyvisa cannot reach by design'."""
    base = os.path.basename(sysfs)
    for iface in glob.glob(os.path.join(sysfs, f'{base}:*')):
        try:
            with open(os.path.join(iface, 'bInterfaceClass')) as f:
                cls = int(f.read().strip(), 16)
            with open(os.path.join(iface, 'bInterfaceSubClass')) as f:
                subcls = int(f.read().strip(), 16)
        except (FileNotFoundError, ValueError, OSError):
            continue
        if cls == 0xFE and subcls == 0x03:
            return True
    return False


def _holders_via_proc(device_path: str) -> list[dict]:
    """Find processes whose /proc/<pid>/fd/* points at device_path.
    PID-namespace-local — sees holders inside the same container as
    box_http_server. Replaces the previous `sudo lsof` shell-out, which
    silently failed on container images that don't ship lsof or sudo
    (lager box image as of 0.20.0)."""
    holders: list[dict] = []
    for pid_dir in glob.glob('/proc/[0-9]*'):
        pid = os.path.basename(pid_dir)
        fd_dir = os.path.join(pid_dir, 'fd')
        try:
            entries = os.listdir(fd_dir)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        for fd_name in entries:
            try:
                target = os.readlink(os.path.join(fd_dir, fd_name))
            except (FileNotFoundError, OSError):
                continue
            if target == device_path:
                try:
                    with open(os.path.join(pid_dir, 'comm')) as f:
                        command = f.read().strip()
                except (FileNotFoundError, OSError):
                    command = '?'
                holders.append({'command': command, 'pid': pid, 'user': ''})
                break
    return holders


def _usbtmc_loaded() -> bool:
    """True if the usbtmc kernel module is currently loaded on the host."""
    try:
        with open('/proc/modules') as f:
            for line in f:
                if line.startswith('usbtmc '):
                    return True
    except OSError:
        pass
    return False


def _dmesg_usb_tail() -> str:
    """Last few lines of dmesg matching USB / usbtmc. Best-effort — needs
    CAP_SYSLOG; if not available, returns an explanatory string instead of
    blowing up."""
    rc, out, err = _run('sudo dmesg 2>&1 | tail -200 | grep -iE "usb|usbtmc" | tail -20', timeout=3)
    if rc != 0:
        return f'(dmesg unavailable: {err.strip() or "permission denied"})'
    return out.strip()
