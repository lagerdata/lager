# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""SSH key authorization handler for the Lager Box HTTP server.

Provides an endpoint to authorize an SSH public key on the box host so that
remote systems (e.g. a control plane) can connect via SSH without a manual
ssh-copy-id step.

Keys are written to two places:
  1. ~/.ssh/authorized_keys  — via the /home/www-data/.ssh bind-mount from
     /home/lagerdata/.ssh on the host (immediate effect; mount added by start_box.sh)
  2. /etc/lager/authorized_keys.d/<label>.pub  — durable record; the background
     poller in start_box.sh re-syncs this on each restart

Both writes are idempotent: if the key already exists it is not duplicated.
"""

import logging
import pathlib
import re

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)

# Primary path: host's /home/lagerdata/.ssh is bind-mounted here by start_box.sh.
# Writing here takes effect immediately for SSH connections.
_HOST_AUTHORIZED_KEYS = pathlib.Path('/home/www-data/.ssh/authorized_keys')

# Durable record: keys written here survive container restarts.
# start_box.sh syncs these back into authorized_keys on each start.
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

        # Durable record: write to authorized_keys.d so the key survives a container
        # restart. start_box.sh syncs this directory into ~/.ssh/authorized_keys on
        # each boot and via a background poller every 5 seconds while running.
        # authorized_keys.d is chmod 777 by start_box.sh so the container user can
        # always write here regardless of the host UID.
        try:
            _AUTHORIZED_KEYS_D.mkdir(parents=True, exist_ok=True)
            pub_file = _AUTHORIZED_KEYS_D / f'{label}.pub'
            existing_pub = pub_file.read_text().strip() if pub_file.exists() else ''
            if existing_pub != public_key.strip():
                pub_file.write_text(public_key.strip() + '\n')
                logger.info('authorize-key: wrote %s to authorized_keys.d', pub_file.name)
        except OSError as exc:
            logger.warning('authorize-key: could not write authorized_keys.d: %s', exc)
            return jsonify({'error': f'Failed to write key to authorized_keys.d: {exc}'}), 500

        # Best-effort immediate write: /home/www-data/.ssh is bind-mounted from the
        # host's /home/lagerdata/.ssh. This takes effect instantly for SSH but may
        # fail if the host directory is owned by a different UID (the poller above
        # handles that case within ~5 seconds).
        added = False
        try:
            _HOST_AUTHORIZED_KEYS.parent.mkdir(parents=True, exist_ok=True)
            _HOST_AUTHORIZED_KEYS.parent.chmod(0o700)
            added = _append_key_idempotent(_HOST_AUTHORIZED_KEYS, public_key)
            if added:
                _HOST_AUTHORIZED_KEYS.chmod(0o600)
                logger.info('authorize-key: wrote key to authorized_keys (immediate)')
            else:
                logger.info('authorize-key: key already present in authorized_keys')
        except OSError as exc:
            logger.warning(
                'authorize-key: could not write authorized_keys directly (will sync via poller): %s', exc
            )

        return jsonify({'authorized': True, 'added': added, 'label': label})
