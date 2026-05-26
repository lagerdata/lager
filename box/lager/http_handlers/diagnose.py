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


_USB_VID_RE = re.compile(r'USB\d*::0x?([0-9A-Fa-f]{4})::0x?([0-9A-Fa-f]{4})::([^:]+)::INSTR', re.IGNORECASE)


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
                'lsof': [],
                'usbtmc_loaded': _usbtmc_loaded(),
                'dmesg_tail': _dmesg_usb_tail(),
                'classification_hint': 'instrument not enumerated on USB — check power and cabling',
            })

        device_path = _usbfs_path_for_sysfs(sysfs)
        lsof_lines = []
        if device_path:
            # Use lsof to find processes holding the USB device file. Needs
            # sudo for the kernel to see other users' fds; on the box,
            # juultest has lsof access to /dev/bus/usb already.
            rc, out, _ = _run(f'sudo lsof {device_path} 2>/dev/null', timeout=3)
            if rc == 0 and out:
                # First line is header; keep PID + COMMAND for each holder.
                for line in out.strip().splitlines()[1:]:
                    fields = line.split(None, 3)
                    if len(fields) >= 2:
                        lsof_lines.append({
                            'command': fields[0],
                            'pid': fields[1],
                            'user': fields[2] if len(fields) > 2 else '',
                        })

        return jsonify({
            'address': addr,
            'enumerated': True,
            'sysfs_path': sysfs,
            'device_path': device_path,
            'vid': vid,
            'pid': pid,
            'serial': serial,
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
        # we WILL collide with it and almost certainly hang or return
        # garbage. Detect that via the hardware_service module — if the
        # address is in `_visa_resources` we skip the open and report.
        try:
            from lager.hardware_service import _visa_resources, _visa_resources_meta_lock
            with _visa_resources_meta_lock:
                has_shared = addr in _visa_resources
        except Exception:
            has_shared = False

        if has_shared:
            return jsonify({
                'address': addr,
                'skipped': True,
                'reason': 'hw_service already holds a shared session for this address; '
                          'would collide. Probe /diagnose/dispatcher (port 8080) for the '
                          'cached session state instead.',
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
            elif 'no such device' in msg or 'errno 19' in msg:
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
