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
import pathlib
import re
import subprocess

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)

# Keys are written here; the host-side lager-ssh-keys.path systemd unit
# watches this directory and syncs any .pub files into authorized_keys.
# start_box.sh installs the units so they're in place before the container starts.
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

        # Write to /etc/lager/authorized_keys.d/<label>.pub.
        # The host-side lager-ssh-keys.path systemd unit (installed by start_box.sh)
        # watches this directory and syncs .pub files into authorized_keys within ~1s.
        added = False
        try:
            _AUTHORIZED_KEYS_D.mkdir(parents=True, exist_ok=True)
            pub_file = _AUTHORIZED_KEYS_D / f'{label}.pub'
            existing_pub = pub_file.read_text().strip() if pub_file.exists() else ''
            if existing_pub != public_key.strip():
                pub_file.write_text(public_key.strip() + '\n')
                added = True
                logger.info('authorize-key: wrote %s to authorized_keys.d', pub_file.name)
            else:
                logger.info('authorize-key: key %r already present in authorized_keys.d', label)
        except OSError as exc:
            logger.warning('authorize-key: could not write authorized_keys.d: %s', exc)
            return jsonify({'error': f'Failed to write key: {exc}'}), 500

        # Best-effort: trigger the sync service immediately via D-Bus so the key
        # lands in authorized_keys before the SSH phase runs, rather than waiting
        # for the path unit's inotify event (which fires within ~1s anyway).
        try:
            subprocess.run(
                ['systemctl', 'start', 'lager-ssh-keys.service'],
                timeout=5, capture_output=True, check=False,
            )
        except Exception as exc:
            logger.debug('authorize-key: systemctl trigger skipped: %s', exc)

        return jsonify({'authorized': True, 'added': added, 'label': label})
