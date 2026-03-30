# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""SSH key authorization handler for the Lager Box HTTP server.

Provides an endpoint to authorize an SSH public key on the box host so that
remote systems (e.g. a control plane) can connect via SSH without a manual
ssh-copy-id step.

Keys are written to two places:
  1. ~/.ssh/authorized_keys  — via the /home/www-data/.ssh mount (immediate effect)
  2. /etc/lager/authorized_keys.d/<label>.pub  — for the systemd path-unit sync mechanism

Both writes are idempotent: if the key already exists it is not duplicated.
"""

import logging
import os
import pathlib
import re

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)

# In a --privileged container, /proc/1/root is the host's root filesystem.
# We use HOST_HOME (passed from start_box.sh) to locate the SSH directory of
# whoever started the box, which is the user whose authorized_keys we want.
_HOST_HOME = os.environ.get('HOST_HOME', '/home/lagerdata')
_HOST_ROOT = pathlib.Path('/proc/1/root')
_SSH_DIR = _HOST_ROOT / _HOST_HOME.lstrip('/') / '.ssh'
_AUTHORIZED_KEYS = _SSH_DIR / 'authorized_keys'

# Secondary location for the systemd-based sync mechanism
_AUTHORIZED_KEYS_D = pathlib.Path('/etc/lager/authorized_keys.d')

_LABEL_RE = re.compile(r'^[a-zA-Z0-9._-]{1,64}$')


def _safe_label(label: str) -> str:
    """Return label if valid, else raise ValueError."""
    if not _LABEL_RE.match(label):
        raise ValueError(f'Invalid label: {label!r}')
    return label


def _append_key_idempotent(path: pathlib.Path, public_key: str) -> bool:
    """Append public_key to path if not already present. Returns True if added."""
    try:
        existing = path.read_text()
    except FileNotFoundError:
        existing = ''
    if public_key in existing:
        return False
    with path.open('a') as f:
        if existing and not existing.endswith('\n'):
            f.write('\n')
        f.write(public_key.strip() + '\n')
    return True


def register_ssh_routes(app: Flask) -> None:
    """Register SSH authorization REST routes with the Flask app."""

    @app.route('/authorize-key', methods=['POST'])
    def authorize_key():
        """Authorize an SSH public key on this box.

        Request body (JSON):
          public_key  (str, required) — the full OpenSSH public key string
          label       (str, optional) — identifier used for the .pub file in
                                        authorized_keys.d (default: "remote-key")

        Responses:
          200  { "authorized": true,  "added": false, "label": "..." }  — key already present
          200  { "authorized": true,  "added": true,  "label": "..." }  — key added
          400  { "error": "..." }  — bad request
          500  { "error": "..." }  — filesystem error
        """
        data = request.get_json(silent=True) or {}
        public_key = (data.get('public_key') or '').strip()
        label_raw = (data.get('label') or 'remote-key').strip()

        if not public_key:
            return jsonify({'error': 'public_key is required'}), 400

        try:
            label = _safe_label(label_raw)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400

        added_to_auth_keys = False

        # --- Write 1: ~/.ssh/authorized_keys via host filesystem ---
        # The container runs --privileged so /proc/1/root exposes the host FS as root,
        # bypassing uid/permission issues between www-data and the host user.
        try:
            _SSH_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            _AUTHORIZED_KEYS.touch(mode=0o600)
            added_to_auth_keys = _append_key_idempotent(_AUTHORIZED_KEYS, public_key)
            logger.info('authorize-key: %s key %r to authorized_keys',
                        'added' if added_to_auth_keys else 'key already present in', label)
        except OSError as exc:
            logger.warning('authorize-key: could not write authorized_keys via /proc/1/root: %s', exc)

        # --- Write 2: /etc/lager/authorized_keys.d/<label>.pub (systemd-sync fallback) ---
        added_to_keys_d = False
        try:
            _AUTHORIZED_KEYS_D.mkdir(parents=True, exist_ok=True)
            pub_file = _AUTHORIZED_KEYS_D / f'{label}.pub'
            existing_pub = pub_file.read_text().strip() if pub_file.exists() else ''
            if existing_pub != public_key.strip():
                pub_file.write_text(public_key.strip() + '\n')
                added_to_keys_d = True
                logger.info('authorize-key: wrote %s to authorized_keys.d', pub_file.name)
        except OSError as exc:
            logger.warning('authorize-key: could not write authorized_keys.d: %s', exc)

        added = added_to_auth_keys or added_to_keys_d
        return jsonify({'authorized': True, 'added': added, 'label': label})
