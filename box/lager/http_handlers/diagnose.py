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

    @app.route('/diagnose/jlink', methods=['GET'])
    def diagnose_jlink():
        """Diagnose a debug-probe net (J-Link, or basic OpenOCD/ST-Link).

        Localises a fault across the J-Link stack and reports it as structured
        JSON for the CLI's ``_classify_jlink`` to turn into a one-line action:

          - probe USB enumeration + competing-process holders (reuses the
            instrument-agnostic USB helpers below),
          - J-Link software presence on the box,
          - probe visibility to ``JLinkExe`` via ``ShowEmuList`` (harmless —
            never touches a target),
          - the per-probe gdbserver's run state + logfile health, and
          - when the probe is *idle* (no gdbserver holding it), a real SWD
            ``connect`` that reads VTref (target power) and confirms comms.

        The intrusive connect is gated exactly like ``/diagnose/visa`` skips a
        fresh pyvisa open when hw_service already holds a session: if a
        gdbserver is live for this probe we report from its log instead of
        colliding with it. OpenOCD/ST-Link nets get a basic (backend +
        enumeration + gdbserver) report; deep connect-probing is J-Link-only.
        """
        net_name = (request.args.get('net') or '').strip()
        address = (request.args.get('address') or '').strip()
        device = (request.args.get('device') or '').strip()

        # Resolve the net from the box's saved-nets cache when a name is given;
        # fall back to explicit address/device query args otherwise.
        net = _lookup_debug_net(net_name) if net_name else None
        if net:
            address = address or (net.get('address') or '')
            device = device or (net.get('channel') or net.get('pin') or '')

        if not address:
            return jsonify({'error': 'net or address parameter required'}), 400

        try:
            from lager.debug.probes import (
                parse_probe_address, resolve_backend, gdb_port_for_slot,
            )
        except Exception as e:  # pragma: no cover — debug pkg ships on every box
            return jsonify({'address': address, 'error': f'debug package unavailable: {e}'}), 500

        vid, pid, serial = parse_probe_address(address)
        backend = resolve_backend(net if isinstance(net, dict) else {'address': address})

        # USB enumeration + competing holders. The USB-TMC helpers are keyed
        # only on VID/PID, so they work for a J-Link probe unchanged.
        sysfs = _find_usb_sysfs(vid, pid) if vid and pid else None
        device_path = _usbfs_path_for_sysfs(sysfs) if sysfs else None
        holders = _holders_via_proc(device_path) if device_path else []

        # Deterministic slot/port so we read the *right* gdbserver pid/log when
        # several probes share the box (mirrors debug.service._resolve_probe).
        slot = _compute_debug_slot(serial)
        gdb_port = gdb_port_for_slot(slot)

        result = {
            'address': address,
            'device': device or None,
            'backend': backend,
            'serial': serial,
            'probe_enumerated': bool(sysfs),
            'sysfs_path': sysfs,
            'device_path': device_path,
            'holders': holders,
        }

        if backend == 'openocd':
            result['mode'] = 'openocd-basic'
            try:
                from lager.debug.openocd import get_openocd_status
                from lager.debug.probes import openocd_logfile
                from lager.debug.mappings import readfile
                st = get_openocd_status(serial=serial)
                result['openocd_gdbserver'] = {
                    'running': bool(st.get('running')), 'pid': st.get('pid'),
                }
                result['logfile_tail'] = _tail(readfile(openocd_logfile(serial)), 25)
            except Exception as e:
                result['openocd_error'] = str(e)
            return jsonify(result)

        result['mode'] = 'jlink'

        try:
            from lager.debug.jlink import get_jlink_exe_path, commander
        except Exception as e:
            return jsonify({**result, 'error': f'jlink module unavailable: {e}'}), 500
        jlink_exe = get_jlink_exe_path()
        result['jlink_installed'] = bool(jlink_exe)
        result['jlink_exe_path'] = jlink_exe

        # gdbserver state for this probe (+ logfile health when running).
        gdbserver = {'running': False, 'pid': None}
        try:
            from lager.debug.gdbserver import get_jlink_gdbserver_status
            from lager.debug.probes import jlink_gdbserver_logfile
            from lager.debug.mappings import check_logfile
            gstat = get_jlink_gdbserver_status(serial=serial)
            gdbserver = {'running': bool(gstat.get('running')), 'pid': gstat.get('pid')}
            if gdbserver['running']:
                ok, log = check_logfile(
                    jlink_gdbserver_logfile(serial), max_tries=1, target_port=gdb_port
                )
                gdbserver['logfile_ok'] = bool(ok)
                gdbserver['logfile_tail'] = _tail(log, 25)
        except Exception as e:
            gdbserver = {'running': False, 'error': str(e)}
        result['gdbserver'] = gdbserver

        if not jlink_exe:
            # No J-Link tools — can't enumerate or connect. The CLI flags this
            # as the root cause (install via `lager box update`).
            return jsonify(result)

        # ShowEmuList — harmless probe enumeration. Don't bind a serial so we
        # see every probe on the box (and can tell "wrong/no probe" apart).
        emu_list = []
        try:
            with commander([]) as jl:
                out = jl.run_command('ShowEmuList', timeout=10)
            emu_list = _parse_emu_list(out)
        except Exception as e:
            result['emu_list_error'] = str(e)
        result['emu_list'] = emu_list
        result['probe_visible'] = _serial_in_emu_list(serial, emu_list)

        # Intrusive connect — only when the probe is idle, visible, and we know
        # which device to connect as. Same "don't collide with a live session"
        # rule the visa endpoint uses.
        if gdbserver.get('running'):
            result['connect_skipped'] = True
            result['connect_skip_reason'] = (
                'gdbserver running for this probe; not disturbing the live session'
            )
        elif not result['probe_visible']:
            result['connect_skipped'] = True
            result['connect_skip_reason'] = 'probe not visible to JLinkExe; connect would fail'
        elif not device:
            result['connect_skipped'] = True
            result['connect_skip_reason'] = 'no device/MCU configured on net; cannot connect'
        else:
            from lager.debug.probes import parse_device_field
            jl_device, _channel = parse_device_field(device)  # strip @channel (FTDI-only)
            try:
                args = ['-device', jl_device, '-if', 'SWD', '-speed', '4000']
                with commander(args, serial=serial) as jl:
                    out = jl.run_command('connect', timeout=20)
                    # Best-effort resume so we leave the core running, not halted.
                    try:
                        jl.run_command('g', timeout=5)
                    except Exception:
                        pass
                result['connect'] = _parse_connect_output(out)
                result['connect_output'] = (out or '')[-2000:]
            except Exception as e:
                result['connect'] = {'connect_ok': False, 'connect_error_class': 'other'}
                result['connect_error'] = str(e)

        return jsonify(result)


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
    blowing up. Uses `sudo -n` so a missing passwordless-sudo grant fails
    fast instead of blocking on a password prompt, and filters in Python
    rather than via a shell pipeline so the dmesg rc isn't masked by a
    downstream `tail` succeeding and so stderr text isn't filtered away by
    `grep`."""
    rc, out, err = _run('sudo -n dmesg', timeout=3)
    if rc != 0:
        return f'(dmesg unavailable: {err.strip() or "permission denied"})'
    matches = [
        line for line in out.splitlines()[-200:]
        if 'usb' in line.lower() or 'usbtmc' in line.lower()
    ]
    return '\n'.join(matches[-20:])


# ---- J-Link diagnose helpers --------------------------------------------------
#
# Pure parsers and small cache lookups for `/diagnose/jlink`. The parsers are
# deliberately free of any hardware/import dependency so they can be unit-tested
# against captured `ShowEmuList` / `connect` text without a box.

def _tail(text, n):
    """Last *n* lines of *text* (empty string for None/empty)."""
    if not text:
        return ''
    return '\n'.join(text.splitlines()[-n:])


def _lookup_debug_net(name):
    """Return the saved debug net dict named *name*, or None.

    Reads the box's nets cache; swallows any cache error so diagnose still
    works from an explicit ?address=/&device= when the cache is unavailable.
    """
    try:
        from lager.cache import get_nets_cache
        for n in get_nets_cache().get_nets():
            if n.get('name') == name and n.get('role') == 'debug':
                return n
    except Exception:
        return None
    return None


def _compute_debug_slot(serial):
    """Deterministic slot for *serial* across all debug probes in the cache.

    Mirrors ``debug.service._resolve_probe`` so we read the gdbserver pid/log
    for the correct slot when multiple probes share the box. Falls back to slot
    0 (legacy single-probe paths) on any failure.
    """
    if not serial:
        return 0
    try:
        from lager.cache import get_nets_cache
        from lager.debug.probes import parse_probe_serial, compute_slot
        all_serials = []
        for n in get_nets_cache().get_nets():
            if n.get('role') != 'debug':
                continue
            s = parse_probe_serial(n.get('address'))
            if s:
                all_serials.append(s)
        return compute_slot(serial, all_serials)
    except Exception:
        return 0


_EMU_RE = re.compile(
    r'Serial number:\s*(\w+).*?ProductName:\s*(.+?)\s*$', re.IGNORECASE
)


def _parse_emu_list(text):
    """Parse ``ShowEmuList`` output into ``[{'serial', 'product'}, ...]``.

    JLinkExe prints one line per connected probe, e.g.::

        J-Link[0]: Connection: USB, Serial number: 000504402175, ProductName: J-Link Plus

    Serial numbers may or may not carry leading zeros depending on firmware;
    ``_serial_in_emu_list`` normalises before comparing.
    """
    probes = []
    if not text:
        return probes
    for line in text.splitlines():
        m = _EMU_RE.search(line)
        if m:
            probes.append({'serial': m.group(1).strip(), 'product': m.group(2).strip()})
    return probes


def _norm_serial(s):
    """Normalise a probe serial for comparison (drop leading zeros, lowercase)."""
    return (s or '').lstrip('0').lower()


def _serial_in_emu_list(serial, emu_list):
    """True if *serial* matches a probe in *emu_list* (zero-pad tolerant)."""
    if not emu_list:
        return False
    if not serial:
        # No serial parseable from the address (e.g. unprogrammed EEPROM): a
        # single visible probe is, by elimination, the one this net points at.
        return len(emu_list) >= 1
    target = _norm_serial(serial)
    for p in emu_list:
        es = _norm_serial(p.get('serial'))
        if es and (es == target or es.endswith(target) or target.endswith(es)):
            return True
    return False


_VTREF_RE = re.compile(r'VTref[=:]\s*([0-9]+\.?[0-9]*)\s*V', re.IGNORECASE)
_CORE_RE = re.compile(r'(Cortex-\S+)\s+identified', re.IGNORECASE)


def _parse_connect_output(text):
    """Classify JLinkExe ``connect`` output.

    Returns ``{vtref_mv, connect_ok, connect_error_class, core}`` where
    ``connect_error_class`` is one of ``ok``, ``no_target_power``,
    ``no_target_comms``, ``locked``, ``wrong_device``, ``other``. The signal
    vocabulary matches the error text J-Link/the existing ``api.py`` produce.
    """
    out = {'vtref_mv': None, 'connect_ok': False, 'connect_error_class': 'other', 'core': None}
    if not text:
        return out
    low = text.lower()

    m = _VTREF_RE.search(text)
    if m:
        try:
            out['vtref_mv'] = int(round(float(m.group(1)) * 1000))
        except ValueError:
            pass

    cm = _CORE_RE.search(text)
    if cm:
        out['core'] = cm.group(1).strip()

    wrong_dev_signs = (
        'unknown device selected', 'is not supported',
        'not in the list of supported', 'no valid device',
    )
    locked_signs = (
        'is locked', 'device is locked', 'idcode', 'access port protection',
        'approtect', 'read protection', 'readout protection', 'is protected',
    )
    comms_fail_signs = (
        'cannot connect to target', 'could not connect to target',
        'could not find core', 'could not find coresight',
        'communication timed out', 'failed to connect',
    )
    success_signs = (
        'connected to target', 'j-link is connected', 'found sw-dp',
        'found swd-dp', 'identified',
    )

    # Order matters — most specific failure first; VTref≈0 outranks a generic
    # comms failure so "target unpowered" wins over "can't connect".
    if any(s in low for s in wrong_dev_signs):
        out['connect_error_class'] = 'wrong_device'
    elif any(s in low for s in locked_signs):
        out['connect_error_class'] = 'locked'
    elif out['vtref_mv'] is not None and out['vtref_mv'] < 300:
        out['connect_error_class'] = 'no_target_power'
    elif any(s in low for s in comms_fail_signs):
        out['connect_error_class'] = 'no_target_comms'
    elif out['core'] or any(s in low for s in success_signs):
        out['connect_ok'] = True
        out['connect_error_class'] = 'ok'
    return out
