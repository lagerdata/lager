# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    cli.context.session

    Session classes for direct communication with Lager boxes.

    Contains:
    - DirectIPSession: SSH + docker exec communication with boxes
    - DirectHTTPSession: Direct HTTP communication with boxes
"""
import os
import signal
from uuid import uuid4

import click
import requests

from .. import __version__


class DirectIPSession:
    """
    Direct session for communicating with box IPs via SSH + docker exec
    """

    def __init__(self, ip_address, box_name=None, *args, **kwargs):
        from ..box_storage import get_box_user

        self.ip_address = ip_address

        # Look up username from box storage if box name is provided
        username = None
        if box_name:
            username = get_box_user(box_name)

        # Default to 'lagerdata' if no username found
        if not username:
            username = 'lagerdata'

        self.ssh_host = f'{username}@{ip_address}'

    def _create_streaming_response(self, process):
        """Create a streaming mock response for a subprocess"""
        class StreamingMockResponse:
            def __init__(self, process):
                self.process = process
                self.headers = {'Lager-Output-Version': '1'}
                self._returncode = None

            def iter_content(self, chunk_size=1024):
                """Stream output in real-time using v1 protocol format"""
                import select
                import os
                import fcntl

                # Set stdout and stderr to non-blocking mode
                flags = fcntl.fcntl(self.process.stdout, fcntl.F_GETFL)
                fcntl.fcntl(self.process.stdout, fcntl.F_SETFL, flags | os.O_NONBLOCK)
                flags = fcntl.fcntl(self.process.stderr, fcntl.F_GETFL)
                fcntl.fcntl(self.process.stderr, fcntl.F_SETFL, flags | os.O_NONBLOCK)

                # Buffers for line-based output
                stdout_buffer = b''
                stderr_buffer = b''

                def flush_buffer(buffer, fileno):
                    """Yield a buffer as a protocol message"""
                    if buffer:
                        header = f"{fileno} {len(buffer)} ".encode()
                        yield header + buffer

                # Stream output until process completes
                while True:
                    # Check if process has terminated
                    poll_result = self.process.poll()

                    # Use select to wait for data with timeout
                    readable, _, _ = select.select(
                        [self.process.stdout, self.process.stderr],
                        [], [],
                        0.1  # 100ms timeout
                    )

                    # Read from stdout if available
                    if self.process.stdout in readable:
                        try:
                            chunk = self.process.stdout.read(chunk_size)
                            if chunk:
                                stdout_buffer += chunk
                                # Flush complete lines
                                while b'\n' in stdout_buffer:
                                    line, stdout_buffer = stdout_buffer.split(b'\n', 1)
                                    line += b'\n'
                                    yield from flush_buffer(line, 1)
                        except BlockingIOError:
                            pass

                    # Read from stderr if available
                    if self.process.stderr in readable:
                        try:
                            chunk = self.process.stderr.read(chunk_size)
                            if chunk:
                                stderr_buffer += chunk
                                # Flush complete lines
                                while b'\n' in stderr_buffer:
                                    line, stderr_buffer = stderr_buffer.split(b'\n', 1)
                                    line += b'\n'
                                    yield from flush_buffer(line, 2)
                        except BlockingIOError:
                            pass

                    # If process has ended, read any remaining data and exit
                    if poll_result is not None:
                        # Read any remaining buffered data
                        while True:
                            try:
                                chunk = self.process.stdout.read(chunk_size)
                                if not chunk:
                                    break
                                stdout_buffer += chunk
                            except (BlockingIOError, ValueError):
                                break

                        while True:
                            try:
                                chunk = self.process.stderr.read(chunk_size)
                                if not chunk:
                                    break
                                stderr_buffer += chunk
                            except (BlockingIOError, ValueError):
                                break

                        # Flush any remaining complete lines
                        while b'\n' in stdout_buffer:
                            line, stdout_buffer = stdout_buffer.split(b'\n', 1)
                            line += b'\n'
                            yield from flush_buffer(line, 1)
                        while b'\n' in stderr_buffer:
                            line, stderr_buffer = stderr_buffer.split(b'\n', 1)
                            line += b'\n'
                            yield from flush_buffer(line, 2)

                        # Flush any remaining partial lines
                        yield from flush_buffer(stdout_buffer, 1)
                        yield from flush_buffer(stderr_buffer, 2)

                        # Send exit code
                        self._returncode = self.process.returncode
                        exit_code_str = str(self._returncode)
                        exit_code_bytes = exit_code_str.encode('utf-8')
                        header = f"- {len(exit_code_bytes)} ".encode()
                        yield header + exit_code_bytes
                        break

        return StreamingMockResponse(process)

    def run_python(self, box, files):
        """
        Run python directly in the container via SSH + docker exec
        """
        import subprocess
        import tempfile
        import os

        # Extract the script content and arguments from files
        script_content = None
        module_content = None
        args = []
        env_vars = []

        for name, content in files:
            if name == 'script':
                if hasattr(content, 'read'):
                    script_content = content.read().decode('utf-8')
                else:
                    script_content = content.decode('utf-8')
            elif name == 'module':
                # Module is a zipped directory
                module_content = content
            elif name == 'args':
                args.append(content)
            elif name == 'env':
                env_vars.append(content)

        if not script_content and not module_content:
            raise ValueError("No script or module content found")

        # Handle module (zipped directory) case
        if module_content:
            # Create temporary local zip file
            with tempfile.NamedTemporaryFile(mode='wb', suffix='.zip', delete=False) as f:
                f.write(module_content)
                temp_zip = f.name

            try:
                # Generate unique remote paths
                import uuid
                remote_id = str(uuid.uuid4())[:8]
                remote_zip = f'/tmp/lager_module_{remote_id}.zip'
                remote_dir = f'/tmp/lager_module_{remote_id}'

                # Transfer zip to box (1st SSH connection via SCP)
                subprocess.run(
                    ['scp', '-q', '-o', 'LogLevel=ERROR', temp_zip, f'{self.ssh_host}:{remote_zip}'],
                    check=True
                )

                # Build the docker exec command
                import shlex
                container_dir = f'/tmp/lager_module_{remote_id}'
                docker_cmd_parts = ['docker', 'exec', '-i']
                for env_var in env_vars:
                    docker_cmd_parts.extend(['-e', shlex.quote(env_var)])

                # Set working directory and add module directory to PYTHONPATH
                # Prepend container_dir to PYTHONPATH so imports from the module work
                # Include /app/box for lager module access
                docker_cmd_parts.extend(['-w', container_dir])
                docker_cmd_parts.extend(['-e', 'PYTHONPATH=.:/app/box:/app'])
                # Set LAGER_HOST_MODULE_FOLDER for error reporting
                docker_cmd_parts.extend(['-e', f'LAGER_HOST_MODULE_FOLDER={container_dir}'])

                # Run main.py (working directory is already set to container_dir)
                docker_cmd_parts.extend(['python', 'python3', 'main.py'])

                # Add script arguments
                for arg in args:
                    docker_cmd_parts.append(shlex.quote(arg))

                docker_cmd = ' '.join(docker_cmd_parts)

                # Consolidate extract, docker cp, and run into single SSH connection (2nd connection)
                # This reduces SSH overhead from 4 connections to 2 connections
                combined_cmd = (
                    f'python3 -c "import zipfile; z = zipfile.ZipFile(\'{remote_zip}\'); '
                    f'z.extractall(\'{remote_dir}\')" && '
                    f'docker cp {remote_dir}/. python:{container_dir} && '
                    f'{docker_cmd}'
                )
                cmd = ['ssh', '-o', 'LogLevel=ERROR', self.ssh_host, combined_cmd]

                # Execute with Popen for streaming output
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=False
                )

                # Get the streaming response
                response = self._create_streaming_response(process)

                # Schedule cleanup after execution (single SSH connection for cleanup)
                import atexit
                def cleanup():
                    # Consolidate cleanup into single SSH connection
                    # Use ; instead of && so both cleanups are attempted even if one fails
                    cleanup_cmd = (
                        f'docker exec python rm -rf {container_dir}; '
                        f'rm -rf {remote_dir} {remote_zip}'
                    )
                    subprocess.run(
                        ['ssh', '-o', 'LogLevel=ERROR', self.ssh_host, cleanup_cmd],
                        capture_output=True
                    )
                atexit.register(cleanup)

                return response

            finally:
                # Clean up local temp file
                os.unlink(temp_zip)

        # Handle script case (existing logic)
        else:
            # Create a temporary file to transfer the script
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                f.write(script_content)
                temp_script = f.name

            try:
                # Build the remote command that will run in the container
                # We need to properly quote arguments for shell execution through SSH
                import shlex

                # Build the docker exec command with environment variables
                docker_cmd_parts = ['docker', 'exec', '-i']
                for env_var in env_vars:
                    docker_cmd_parts.extend(['-e', shlex.quote(env_var)])

                # Add container name and python command
                docker_cmd_parts.extend(['python', 'python3', '-'])

                # Add script arguments with proper quoting
                for arg in args:
                    docker_cmd_parts.append(shlex.quote(arg))

                # Join into a single command string for SSH to execute
                docker_cmd = ' '.join(docker_cmd_parts)

                # Build the SSH command
                cmd = ['ssh', '-o', 'LogLevel=ERROR', self.ssh_host, docker_cmd]

                # Execute script in container by piping it via stdin
                # Use Popen instead of run() to enable real-time streaming output
                process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=False  # Use binary mode for streaming
                )

                # Write the script to stdin and close it
                process.stdin.write(script_content.encode('utf-8'))
                process.stdin.close()

                # Create and return streaming response
                return self._create_streaming_response(process)

            finally:
                # Clean up temporary file
                os.unlink(temp_script)

    def box_hello(self, box):
        """
        Say hello directly to the box via container exec
        """
        import subprocess

        result = subprocess.run([
            'ssh', '-o', 'LogLevel=ERROR', self.ssh_host,
            'docker', 'exec', 'python',
            'python3', '-c', "print('Hello, world! Your box is connected.')"
        ], capture_output=True, text=True)

        class MockResponse:
            def __init__(self, text, status_code=200):
                self.text = text
                self.status_code = status_code
                self.headers = {'Lager-Output-Version': '1'}

        return MockResponse(result.stdout)

    def kill_python(self, box, lager_process_id, sig=signal.SIGTERM):
        """Kill a running Python process on the box"""
        import requests
        import json

        # Use IP address only (not ssh_host which includes username)
        url = f'http://{self.ip_address}:5000/python/kill'
        payload = {
            'lager_process_id': lager_process_id,
            'signal': sig
        }

        try:
            response = requests.post(url, json=payload, timeout=5)
            return response
        except requests.exceptions.RequestException as e:
            # Return a mock response with error
            class ErrorResponse:
                def __init__(self, error):
                    self.text = str(error)
                    self.status_code = 500
                    self.headers = {}
            return ErrorResponse(e)

    def download_file(self, box, filename):
        """Download a file from box using scp"""
        import subprocess
        import tempfile
        import gzip
        import shutil

        class SCPResponse:
            """Response-like object for SCP downloads that supports context manager protocol"""
            def __init__(self, filepath):
                self.filepath = filepath
                self.gzipped_path = filepath + '.gz'
                self._file = None
                self.status_code = 200

            def __enter__(self):
                # Gzip the downloaded file (to match backend behavior)
                try:
                    with open(self.filepath, 'rb') as f_in:
                        with gzip.open(self.gzipped_path, 'wb') as f_out:
                            shutil.copyfileobj(f_in, f_out)

                    # Open the gzipped file and expose it as .raw
                    self._file = open(self.gzipped_path, 'rb')
                    self.raw = self._file
                    return self
                except FileNotFoundError:
                    # File wasn't downloaded successfully
                    raise requests.HTTPError("File not found on box")

            def __exit__(self, exc_type, exc_val, exc_tb):
                if self._file:
                    self._file.close()
                # Clean up temp files
                try:
                    if os.path.exists(self.filepath):
                        os.unlink(self.filepath)
                    if os.path.exists(self.gzipped_path):
                        os.unlink(self.gzipped_path)
                except Exception:
                    pass
                return False

            def raise_for_status(self):
                """Compatibility method for requests-like behavior"""
                if self.status_code >= 400:
                    raise requests.HTTPError(f"HTTP {self.status_code}")

        # Create a temporary file for the download
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='_download')
        temp_file.close()

        # Download file using scp
        scp_cmd = ['scp', '-q', f'{self.ssh_host}:{filename}', temp_file.name]

        try:
            result = subprocess.run(scp_cmd, capture_output=True, check=True, timeout=60)
            return SCPResponse(temp_file.name)
        except subprocess.CalledProcessError as e:
            # Clean up temp file on error
            try:
                os.unlink(temp_file.name)
            except Exception:
                pass

            # Check if it's a file not found error (scp exit code 1)
            error_msg = e.stderr.decode() if e.stderr else str(e)
            if 'No such file' in error_msg or e.returncode == 1:
                # Create a mock response that will raise HTTPError with 404
                class NotFoundResponse:
                    def __init__(self):
                        self.status_code = 404
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        return False
                    def raise_for_status(self):
                        err = requests.HTTPError("File not found")
                        err.response = self
                        raise err
                resp = NotFoundResponse()
                resp.raise_for_status()
            else:
                raise requests.HTTPError(f"Failed to download file: {error_msg}")
        except subprocess.TimeoutExpired:
            # Clean up temp file on timeout
            try:
                os.unlink(temp_file.name)
            except Exception:
                pass
            raise requests.HTTPError("Download timeout")

    def run_pip(self, box, args):
        """
        Run pip commands in the python container via HTTP

        Args:
            box: Box identifier (IP address)
            args: List of pip arguments (e.g., ['install', 'pandas'])

        Returns:
            HTTP response with streaming output
        """
        import requests

        # Make HTTP request to the box's run-pip endpoint
        url = f'http://{self.ip_address}:5001/run/pip'

        try:
            response = requests.post(
                url,
                json={'args': args},
                stream=True,
                timeout=600  # 10 minute timeout for pip operations
            )

            # Check for HTTP errors
            if response.status_code != 200:
                error_msg = f"HTTP {response.status_code}"
                try:
                    error_data = response.json()
                    if 'error' in error_data:
                        error_msg = error_data['error']
                except:
                    pass
                raise requests.HTTPError(error_msg)

            return response

        except requests.exceptions.ConnectionError as e:
            raise requests.HTTPError(f"Failed to connect to box at {self.ip_address}: {e}")
        except requests.exceptions.Timeout:
            raise requests.HTTPError("Pip operation timed out")


class DirectHTTPSession:
    """
    Direct HTTP session to box over any network (VPN, local, etc.).

    Network-agnostic: Works with Tailscale, WireGuard, corporate VPN, local network, or any IP.
    Authorization: Network-level access (can you reach the IP via HTTP?)

    This bypasses the backend proxy for faster execution.

    Uses connection pooling to reuse TCP connections for better performance.
    Sessions are pooled per box IP address.
    """

    _session_pool = {}  # Class-level session pool keyed by box IP

    def __init__(self, box_ip):
        """
        Initialize direct HTTP session to box.

        Args:
            box_ip: IP address of box (Tailscale, VPN, local, or public IP)
        """
        self.box_ip = box_ip
        self.base_url = f'http://{box_ip}:5000'

        # Use pooled session for connection reuse, create if needed
        if box_ip not in DirectHTTPSession._session_pool:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=10,
                pool_maxsize=10,
                max_retries=3
            )
            session.mount('http://', adapter)
            DirectHTTPSession._session_pool[box_ip] = session

        self.session = DirectHTTPSession._session_pool[box_ip]

        # Add version header (updated per invocation)
        self.session.headers.update({
            'Lager-Version': __version__,
            'Lager-Invocation-Id': str(uuid4()),
        })

    def run_python(self, box, files):
        """
        Run python on box via direct HTTP.

        Args:
            box: Box IP (ignored, uses self.box_ip)
            files: List of (name, content) tuples for multipart upload

        Returns:
            requests.Response object with streaming content
        """
        url = f'{self.base_url}/python'

        # Retry logic for multipart upload connection issues
        max_retries = 3
        last_error = None

        for attempt in range(max_retries):
            try:
                # Disable keep-alive to prevent connection reuse issues with multipart uploads
                headers = {'Connection': 'close'}

                # Reset file positions if they are BytesIO objects
                for name, value in files:
                    if hasattr(value, 'seek') and hasattr(value, 'read'):
                        # It's a file-like object, reset position
                        value.seek(0)
                    elif isinstance(value, tuple) and len(value) >= 2:
                        # It's a tuple (filename, file_obj, ...), check the file object
                        if hasattr(value[1], 'seek'):
                            value[1].seek(0)

                response = self.session.post(
                    url,
                    files=files,
                    headers=headers,
                    stream=True,
                    timeout=(7, 320)  # Connect timeout: 7s, Read timeout: 320s
                )
                return response
            except requests.exceptions.ConnectionError as e:
                last_error = e
                if attempt < max_retries - 1:
                    # Wait a bit before retrying
                    import time
                    time.sleep(0.5)
                    continue
                # Last attempt failed - exit cleanly without traceback
                click.secho(f'Could not connect to box at {self.box_ip}', fg='red', err=True)
                click.secho('Ensure the box is online and reachable via your network', fg='yellow', err=True)
                click.secho('(Tailscale, VPN, or local network connection required)', fg='yellow', err=True)
                import sys
                sys.exit(1)
            except requests.exceptions.Timeout:
                click.secho(f'Connection to box at {self.box_ip} timed out', fg='red', err=True)
                import sys
                sys.exit(1)

        # Should not reach here, but just in case
        if last_error:
            click.secho(f'Could not connect to box at {self.box_ip}', fg='red', err=True)
            import sys
            sys.exit(1)

    def kill_python(self, box, lager_process_id, sig=signal.SIGTERM):
        """
        Kill python process on box.

        Args:
            box: Box IP (ignored)
            lager_process_id: Process ID to kill
            sig: Signal to send (default SIGTERM)
        """
        url = f'{self.base_url}/python/kill'
        response = self.session.post(url, json={
            'lager_process_id': lager_process_id,
            'signal': int(sig)
        })
        response.raise_for_status()
        return response

    def box_hello(self, box):
        """Say hello to box to test connectivity"""
        url = f'{self.base_url}/hello'
        return self.session.get(url)

    def list_instruments(self, box):
        """List instruments configured on box"""
        url = f'{self.base_url}/instruments'
        return self.session.get(url)

    def nets(self, box):
        """List nets configured on box"""
        url = f'{self.base_url}/nets'
        return self.session.get(url)

    def attach_python(self, box, lager_process_id):
        """
        Reattach to a detached Python process on box.

        Args:
            box: Box IP (ignored, uses self.box_ip)
            lager_process_id: Process ID to reattach to

        Returns:
            requests.Response object with streaming content
        """
        url = f'{self.base_url}/python/attach'
        response = self.session.post(
            url,
            json={'lager_process_id': lager_process_id},
            stream=True,
            timeout=(7, None),
        )
        return response

    def download_file(self, box, filename):
        """
        Download a file from box via direct HTTP.

        Args:
            box: Box IP (ignored, uses self.box_ip)
            filename: Path to file on box to download

        Returns:
            requests.Response object with streaming content
        """
        url = f'{self.base_url}/download-file'
        return self.session.get(url, params={'filename': filename}, stream=True)
