# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
lager.control_plane_client - Control Plane Heartbeat Client

Connects to a control plane (e.g., Stout) via WebSocket and sends
periodic heartbeat messages with box status and device information.

Configuration is read from /etc/lager/control_plane.json:
{
    "url": "http://stout:3001",
    "api_key": "stout_abc123...",
    "heartbeat_interval_seconds": 30,
    "enabled": true
}
"""

import json
import logging
import socket
import threading
import time

logger = logging.getLogger(__name__)

CONFIG_PATH = '/etc/lager/control_plane.json'
VERSION_PATH = '/etc/lager/version'
NETS_PATH = '/etc/lager/saved_nets.json'


class ControlPlaneClient:
    """WebSocket client that sends heartbeats to a control plane."""

    def __init__(self, config_path=CONFIG_PATH):
        self._config_path = config_path
        self._config = None
        self._box_id = None
        self._stop_event = threading.Event()
        self._thread = None

    def _load_config(self):
        try:
            with open(self._config_path, 'r') as f:
                self._config = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.warning(f"Cannot load control plane config: {e}")
            self._config = None
        return self._config

    @property
    def enabled(self):
        if self._config is None:
            self._load_config()
        return self._config is not None and self._config.get('enabled', False)

    def _get_status(self):
        version = 'unknown'
        try:
            with open(VERSION_PATH, 'r') as f:
                content = f.read().strip()
                version = content.split('|', 1)[0] if '|' in content else content
        except (FileNotFoundError, IOError):
            pass

        ip = 'unknown'
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass

        return {'version': version, 'ip': ip}

    def _get_devices(self):
        devices = []
        try:
            from lager.nets.constants import NetType
            with open(NETS_PATH, 'r') as f:
                saved_nets = json.load(f)
            for net in saved_nets:
                role = net.get('role', '')
                try:
                    net_type = NetType.from_role(role).name
                except (KeyError, ValueError):
                    net_type = role
                devices.append({'name': net.get('name', ''), 'type': net_type})
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ImportError):
            pass
        return devices

    def _build_ws_url(self):
        url = self._config['url'].rstrip('/')
        # Convert http(s) to ws(s)
        if url.startswith('http://'):
            url = 'ws://' + url[7:]
        elif url.startswith('https://'):
            url = 'wss://' + url[8:]
        return f"{url}/ws/box/heartbeat"

    def _run_loop(self):
        import websocket

        backoff = 1
        max_backoff = 60

        while not self._stop_event.is_set():
            ws = None
            try:
                ws_url = self._build_ws_url()
                logger.info(f"Connecting to control plane: {ws_url}")

                ws = websocket.create_connection(ws_url, timeout=10)

                # Authenticate
                ws.send(json.dumps({
                    'type': 'auth',
                    'apiKey': self._config['api_key'],
                }))

                auth_resp = json.loads(ws.recv())
                if auth_resp.get('type') != 'auth_ok':
                    logger.error(f"Auth failed: {auth_resp}")
                    raise Exception("Authentication failed")

                self._box_id = auth_resp.get('boxId')
                logger.info(f"Authenticated as box {self._box_id}")
                backoff = 1  # Reset on successful connection

                interval = self._config.get('heartbeat_interval_seconds', 30)

                while not self._stop_event.is_set():
                    heartbeat = {
                        'type': 'heartbeat',
                        'status': self._get_status(),
                        'devices': self._get_devices(),
                    }
                    ws.send(json.dumps(heartbeat))

                    ack = json.loads(ws.recv())
                    if ack.get('type') != 'ack':
                        logger.warning(f"Unexpected response: {ack}")

                    self._stop_event.wait(interval)

            except Exception as e:
                logger.warning(f"Control plane connection error: {e}")
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, max_backoff)

    def start(self):
        if not self.enabled:
            logger.info("Control plane heartbeat disabled or not configured")
            return

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Control plane heartbeat thread started")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )

    client = ControlPlaneClient()
    if not client.enabled:
        logger.info("Control plane heartbeat not enabled. Exiting.")
        return

    logger.info("Starting control plane heartbeat client...")
    client.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down control plane heartbeat client")
        client.stop()


if __name__ == '__main__':
    main()
