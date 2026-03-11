# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
lager.control_plane_client - Control Plane Heartbeat & Jobs Client

Connects to a control plane (e.g., Stout) via WebSocket and sends
periodic heartbeat messages with box status and device information.
Also connects to the jobs WebSocket endpoint to receive and execute
job dispatch commands.

Configuration is read from /etc/lager/control_plane.json:
{
    "url": "http://stout:3001",
    "api_key": "stout_abc123...",
    "heartbeat_interval_seconds": 30,
    "enabled": true
}
"""

import io
import json
import logging
import socket
import threading
import time
import urllib.request

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
                devices.append({'name': net.get('name', ''), 'type': net_type, 'netName': net.get('name', '')})
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

        self._jobs_client = JobsWebSocketClient(self._config, self._stop_event)
        self._jobs_client.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        if hasattr(self, '_jobs_client') and self._jobs_client:
            self._jobs_client.join(timeout=5)


class JobsWebSocketClient:
    """WebSocket client that connects to the control plane jobs endpoint
    to receive and execute job dispatch commands."""

    def __init__(self, config, stop_event):
        self._config = config
        self._stop_event = stop_event
        self._thread = None

    def _build_ws_url(self):
        url = self._config['url'].rstrip('/')
        if url.startswith('http://'):
            url = 'ws://' + url[7:]
        elif url.startswith('https://'):
            url = 'wss://' + url[8:]
        return f"{url}/ws/box/jobs"

    def _build_multipart(self, script, args, timeout_seconds):
        """Build multipart/form-data body using stdlib only."""
        boundary = '----LagerJobBoundary'
        body = io.BytesIO()

        def write_field(name, value, filename=None):
            body.write(f'--{boundary}\r\n'.encode())
            if filename:
                body.write(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
                body.write(b'Content-Type: application/octet-stream\r\n')
            else:
                body.write(f'Content-Disposition: form-data; name="{name}"\r\n'.encode())
            body.write(b'\r\n')
            if isinstance(value, bytes):
                body.write(value)
            else:
                body.write(str(value).encode())
            body.write(b'\r\n')

        write_field('script', script.encode(), filename='job.py')
        for arg in args:
            write_field('args', arg)
        write_field('timeout', str(timeout_seconds))
        write_field('stdout_is_stderr', 'false')

        body.write(f'--{boundary}--\r\n'.encode())
        return body.getvalue(), f'multipart/form-data; boundary={boundary}'

    def _parse_wire_format(self, response):
        """Parse streaming wire format from the /python endpoint.

        Each chunk: "<fileno> <length> <data>"
        Exit marker: "- <length> <returncode>"
        fileno: 0=keepalive, 1=stdout, 2=stderr
        """
        buf = b''
        for chunk in iter(lambda: response.read(4096), b''):
            buf += chunk
            while buf:
                # Need at least "X Y " to parse header
                first_space = buf.find(b' ')
                if first_space == -1:
                    break
                second_space = buf.find(b' ', first_space + 1)
                if second_space == -1:
                    break

                fileno_str = buf[:first_space].decode('ascii', errors='replace')
                length_str = buf[first_space + 1:second_space].decode('ascii', errors='replace')

                try:
                    length = int(length_str)
                except ValueError:
                    # Malformed, skip a byte
                    buf = buf[1:]
                    continue

                data_start = second_space + 1
                data_end = data_start + length
                if len(buf) < data_end:
                    break  # Need more data

                data = buf[data_start:data_end]
                buf = buf[data_end:]
                # Strip trailing newline if present
                if buf.startswith(b'\n'):
                    buf = buf[1:]

                if fileno_str == '-':
                    # Exit marker — data is the return code
                    try:
                        exit_code = int(data.decode('ascii', errors='replace').strip())
                    except ValueError:
                        exit_code = -1
                    yield ('exit', exit_code, '')
                    return
                elif fileno_str == '0':
                    # Keepalive, ignore
                    continue
                elif fileno_str in ('1', '2'):
                    stream = 'stdout' if fileno_str == '1' else 'stderr'
                    yield (stream, 0, data.decode('utf-8', errors='replace'))
                else:
                    # Unknown fileno, skip
                    continue

    def _execute_job(self, ws, job_id, command, args, timeout_seconds):
        """Execute a job by POSTing to the local Lager box and streaming results."""
        sequence_num = 0

        try:
            # Send ack
            ws.send(json.dumps({
                'type': 'job:ack',
                'jobId': job_id,
            }))

            # Build wrapper script that runs the lager CLI command
            script = (
                'import subprocess, sys, shlex\n'
                f'sys.exit(subprocess.call(["lager"] + shlex.split({command!r}) + sys.argv[1:]))\n'
            )

            body, content_type = self._build_multipart(script, args, timeout_seconds)

            req = urllib.request.Request(
                'http://localhost:5000/python',
                data=body,
                headers={'Content-Type': content_type},
                method='POST',
            )

            response = urllib.request.urlopen(req, timeout=timeout_seconds + 30)

            exit_code = -1
            for event_type, code_or_zero, data in self._parse_wire_format(response):
                if event_type == 'exit':
                    exit_code = code_or_zero
                    break

                # Split data into lines and send each
                lines = data.splitlines()
                if not lines:
                    lines = [data]
                for line in lines:
                    ws.send(json.dumps({
                        'type': 'job:log',
                        'jobId': job_id,
                        'line': line,
                        'stream': event_type,
                        'sequenceNum': sequence_num,
                    }))
                    sequence_num += 1

            ws.send(json.dumps({
                'type': 'job:result',
                'jobId': job_id,
                'exitCode': exit_code,
            }))

        except Exception as e:
            logger.error(f"Job {job_id} execution error: {e}")
            try:
                ws.send(json.dumps({
                    'type': 'job:result',
                    'jobId': job_id,
                    'exitCode': -1,
                }))
            except Exception:
                pass

    def _run_loop(self):
        import websocket

        backoff = 1
        max_backoff = 60

        while not self._stop_event.is_set():
            ws = None
            try:
                ws_url = self._build_ws_url()
                logger.info(f"Connecting to control plane jobs: {ws_url}")

                ws = websocket.create_connection(ws_url, timeout=10)

                # Authenticate
                ws.send(json.dumps({
                    'type': 'auth',
                    'apiKey': self._config['api_key'],
                }))

                auth_resp = json.loads(ws.recv())
                if auth_resp.get('type') != 'auth_ok':
                    logger.error(f"Jobs auth failed: {auth_resp}")
                    raise Exception("Authentication failed")

                box_id = auth_resp.get('boxId')
                logger.info(f"Jobs client authenticated as box {box_id}")
                backoff = 1

                # Receive loop — wait for job:execute messages
                ws.settimeout(60)
                while not self._stop_event.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        # Send ping to keep connection alive
                        try:
                            ws.ping()
                        except Exception:
                            break
                        continue

                    msg = json.loads(raw)
                    msg_type = msg.get('type')

                    if msg_type == 'job:execute':
                        job_id = msg['jobId']
                        command = msg['command']
                        job_args = msg.get('args', [])
                        timeout_secs = msg.get('timeoutSeconds', 300)
                        logger.info(f"Received job:execute for {job_id}: {command}")

                        # Execute in a thread so we can still receive messages
                        job_thread = threading.Thread(
                            target=self._execute_job,
                            args=(ws, job_id, command, job_args, timeout_secs),
                            daemon=True,
                        )
                        job_thread.start()
                    elif msg_type == 'ack':
                        # Ack from control plane, ignore
                        pass
                    else:
                        logger.debug(f"Jobs client received: {msg_type}")

            except Exception as e:
                logger.warning(f"Jobs WebSocket connection error: {e}")
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, max_backoff)

    def start(self):
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Control plane jobs thread started")

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout=timeout)


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
