# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Custom-driver HTTP handlers for the Lager Box HTTP server (:9000).

Routes:
    GET    /drivers           -> {"drivers": [entry, ...]}
    GET    /drivers/<type>    -> entry
    PUT    /drivers/<type>    -> multipart form: `bundle` (zip);
                                 {"type", "version", "sha256", "replaced"}
    DELETE /drivers/<type>?force=1
                              -> {"type", "removed", "orphaned_nets"}
    POST   /drivers/<type>/deps
                              -> reinstall the driver's Python packages from
                                 its own requirements file; {"type", "packages"}

    entry = {"type", "version", "sha256", "status": "ok"|"error", "error"?,
             "description", "manifest", "nets": [names of nets using it],
             "deps": null | {"status": "ok"|"stale"|"missing", "packages"?, ...}}

Installing a bundle with requirements runs pip, for up to
deps.DEPS_TIMEOUT_S, inside the PUT.

Net commands on a custom net go through the existing POST /net/command:
net_command.py falls back to custom_net_action() for a role that is an
installed driver.
"""

import logging
import threading

from flask import Flask, jsonify, request

from lager.drivers import loader, registry, runner
from lager.drivers.errors import DriverBusy, DriverFailed
from lager.drivers.manifest import ManifestError, coerce_args

logger = logging.getLogger(__name__)

# How long a second call on the same net waits for the first to finish
# before it is told the net is busy.
NET_LOCK_WAIT_S = 5.0

_net_locks = {}
_net_locks_guard = threading.Lock()


def _net_lock(netname):
    with _net_locks_guard:
        lock = _net_locks.get(netname)
        if lock is None:
            lock = _net_locks[netname] = threading.Lock()
        return lock


def _nets_using(dtype, nets=None):
    if nets is None:
        from lager.nets.net import Net
        nets = Net.get_local_nets()
    return sorted({n.get('name') for n in nets if n.get('role') == dtype and n.get('name')})


def _check_uart_free(base_nets):
    """Raise DriverBusy if a live `lager uart` session holds a base UART net.

    Checked here, before spawning, because the session table lives only in
    this process. The runner would otherwise fail on the exclusive serial
    lock with a less helpful errno.
    """
    uart_names = {rec.get('name') for rec in base_nets.values() if rec.get('role') == 'uart'}
    if not uart_names:
        return
    try:
        from lager.http_handlers import uart as uart_handler
    except Exception:
        return
    with uart_handler.active_uart_sessions_lock:
        for sid, sess in list(uart_handler.active_uart_sessions.items()):
            if sess.get('netname') in uart_names and not uart_handler._reclaim_if_stale(sid, sess):
                held = sess.get('netname')
                raise DriverBusy(
                    "UART net '%s' is in use by a `lager uart` session; close it "
                    "first" % held)


def _release_usb_claims():
    try:
        from lager.python.executor import _release_hardware_service_direct_usb_claims
    except Exception:
        return
    _release_hardware_service_direct_usb_claims()


def _secret_env():
    try:
        from lager.python.executor import load_box_secrets
    except Exception:
        return {}
    return {'LAGER_SECRET_%s' % k: str(v) for k, v in load_box_secrets().items()}


def run_action(dtype, rec, params, base_nets, action, args, timeout_s):
    """Run one action in a runner child process; return the action's value."""
    request = {'type': dtype, 'net': rec, 'params': params,
               'base_nets': base_nets, 'action': action, 'args': args}
    env = dict(_secret_env())
    env['LAGER_DRIVERS_DIR'] = registry.DRIVERS_DIR
    result = runner.call(request, env, registry.DRIVERS_DIR,
                         registry.driver_dir(dtype), timeout_s)
    if not result.get('ok'):
        raise DriverFailed('%s: %s' % (result.get('exc_type') or 'Error', result.get('error')))
    return result.get('value')


def _message(action, value):
    if isinstance(value, dict) and isinstance(value.get('message'), str):
        return value['message']
    if value is None or isinstance(value, dict):
        return '%s done' % action
    return str(value)


def custom_net_action(netname, role, action, params):
    """The /net/command handler for a net whose role is a custom driver.

    Same contract as a ROLE_ACTIONS entry: returns {"message", "value"?}.
    """
    from lager.http_handlers.net_command import UnknownAction

    rec = next((n for n in _saved_nets() if n.get('name') == netname
                and n.get('role') == role), None)
    if rec is None:
        raise ManifestError("Net '%s' (role %s) not found" % (netname, role))
    entry, net_params, base_nets = loader.resolve(rec)
    manifest = entry['manifest']
    if action not in manifest['actions']:
        raise UnknownAction(action)
    args = coerce_args(manifest, action, params)
    timeout_s = manifest['actions'][action]['timeout_s']

    _check_uart_free(base_nets)
    lock = _net_lock(netname)
    if not lock.acquire(timeout=NET_LOCK_WAIT_S):
        raise DriverBusy("Net '%s' is busy with another command" % netname)
    try:
        _release_usb_claims()
        value = run_action(role, rec, net_params, base_nets, action, args, timeout_s)
    finally:
        lock.release()

    out = {'message': _message(action, value)}
    if value is not None:
        out['value'] = value
    return out


def _saved_nets():
    from lager.nets.net import Net
    return Net.get_local_nets()


def _entry_with_nets(entry, nets):
    entry = dict(entry)
    entry['nets'] = _nets_using(entry['type'], nets)
    return entry


def register_drivers_routes(app: Flask) -> None:
    """Register custom-driver REST routes with the Flask app."""

    @app.route('/drivers', methods=['GET'])
    def drivers_list():
        try:
            nets = _saved_nets()
            return jsonify({'drivers': [_entry_with_nets(e, nets)
                                        for e in registry.list_drivers()]})
        except Exception:
            logger.exception("Failed to list custom drivers")
            return jsonify({'error': 'Failed to list drivers; see the box log'}), 500

    @app.route('/drivers/<dtype>', methods=['GET'])
    def drivers_get(dtype):
        try:
            if not registry.has(dtype):
                return jsonify({'error': "No custom driver '%s' is installed" % dtype}), 404
            return jsonify(_entry_with_nets(registry._describe(dtype), _saved_nets()))
        except Exception:
            logger.exception("Failed to read custom driver %s", dtype)
            return jsonify({'error': 'Failed to read the driver; see the box log'}), 500

    @app.route('/drivers/<dtype>', methods=['PUT'])
    def drivers_install(dtype):
        try:
            bundle = request.files.get('bundle')
            if bundle is None:
                return jsonify({'error': 'bundle file is required'}), 400
            data = bundle.read(registry.MAX_BUNDLE_BYTES + 1)
            try:
                result = registry.install(dtype, data)
            except registry.DriverStoreError as e:
                return jsonify({'error': e.message}), e.status
            logger.info("[drivers] installed %s %s (sha256 %s)%s",
                        dtype, result['version'], result['sha256'][:12],
                        ' with ' + ', '.join(result['packages']) if result['packages'] else '')
            return jsonify(result)
        except Exception:
            logger.exception("Failed to install custom driver %s", dtype)
            return jsonify({'error': 'Failed to install the driver; see the box log'}), 500

    @app.route('/drivers/<dtype>/deps', methods=['POST'])
    def drivers_reinstall_deps(dtype):
        try:
            try:
                result = registry.reinstall_deps(dtype)
            except registry.DriverStoreError as e:
                return jsonify({'error': e.message}), e.status
            logger.info("[drivers] reinstalled packages of %s: %s",
                        dtype, ', '.join(result['packages']) or 'none')
            return jsonify(result)
        except Exception:
            logger.exception("Failed to reinstall packages of custom driver %s", dtype)
            return jsonify({'error': 'Failed to reinstall the packages; see the box log'}), 500

    @app.route('/drivers/<dtype>', methods=['DELETE'])
    def drivers_remove(dtype):
        try:
            force = request.args.get('force', '').lower() in ('1', 'true', 'yes')
            try:
                result = registry.remove(dtype, _nets_using(dtype), force=force)
            except registry.DriverStoreError as e:
                return jsonify({'error': e.message}), e.status
            logger.info("[drivers] removed %s", dtype)
            return jsonify(result)
        except Exception:
            logger.exception("Failed to remove custom driver %s", dtype)
            return jsonify({'error': 'Failed to remove the driver; see the box log'}), 500
