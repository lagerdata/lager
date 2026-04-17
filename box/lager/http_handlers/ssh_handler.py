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

import hmac
import json
import logging
import pathlib
import re

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)

# Best-effort immediate path: on Linux (Docker container) the host's
# /home/lagerdata/.ssh is bind-mounted at /home/www-data/.ssh. On macOS
# (native host mode) we write directly to ~lagerdata/.ssh/authorized_keys.
import sys as _sys
if _sys.platform == 'darwin':
    _HOST_AUTHORIZED_KEYS = pathlib.Path('/Users/lagerdata/.ssh/authorized_keys')
else:
    _HOST_AUTHORIZED_KEYS = pathlib.Path('/home/www-data/.ssh/authorized_keys')

# Staging area for pub keys when the immediate path is not writable.
_AUTHORIZED_KEYS_D = pathlib.Path('/tmp/lager-authorized-keys.d')

_LABEL_RE = re.compile(r'^[a-zA-Z0-9._-]{1,64}$')

from ..constants import CONTROL_PLANE_CONFIG_PATH as _CONTROL_PLANE_CONFIG_PATH_STR

_CONTROL_PLANE_CONFIG_PATH = pathlib.Path(_CONTROL_PLANE_CONFIG_PATH_STR)


def _get_authorize_token() -> str | None:
    """Read authorize_token from control_plane.json. Returns None if missing or unreadable."""
    try:
        data = json.loads(_CONTROL_PLANE_CONFIG_PATH.read_text())
        token = data.get('authorize_token')
        return token if isinstance(token, str) and token else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


# Matches a valid OpenSSH public key:
#   <key-type> <base64-blob> [optional comment with no newlines]
# Key types: the four standard types plus sk- (FIDO2) variants.
_SSH_KEY_RE = re.compile(
    r'^(?:ssh-(?:rsa|dss|ed25519|ed25519-sk)|'
    r'ecdsa-sha2-nistp(?:256|384|521)(?:-sk)?)'
    r'\s+[A-Za-z0-9+/]+=*'
    r'(?:\s+[^\r\n]*)?$'
)


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
        # Authenticate: require a Bearer token matching the authorize_token in
        # control_plane.json. Use hmac.compare_digest for timing-safe comparison.
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Authorization required'}), 401

        provided_token = auth_header[len('Bearer '):]
        expected_token = _get_authorize_token()

        if not expected_token:
            logger.warning('authorize-key: control_plane.json missing or has no authorize_token')
            return jsonify({'error': 'Box not configured for authenticated key authorization'}), 503

        if not hmac.compare_digest(provided_token, expected_token):
            logger.warning('authorize-key: Bearer token mismatch — rejecting request')
            return jsonify({'error': 'Invalid authorization token'}), 401

        data = request.get_json(silent=True) or {}
        public_key = (data.get('public_key') or '').strip()
        label_raw = (data.get('label') or 'remote-key').strip()

        if not public_key:
            return jsonify({'error': 'public_key is required'}), 400

        if not _SSH_KEY_RE.match(public_key):
            return jsonify({'error': 'public_key must be a valid OpenSSH public key'}), 400

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
