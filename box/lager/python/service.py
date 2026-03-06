# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
lager.python.service - Python Execution HTTP Service

Provides HTTP endpoints for executing Python scripts on the box.
This service runs on port 5000 alongside the debug service (port 8765).

Migrated from gateway/controller/controller/application/views/run.py (legacy, removed)

Endpoints:
- POST /python - Execute a Python script
- POST /python/kill - Kill a running Python process
- POST /pip - Run pip commands in the container

Migrated from: gateway/controller (legacy, removed)
Now runs in: box/python container (port 5000)
"""

import json
import logging
import signal
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs
import cgi
import io
import warnings

from .executor import PythonExecutor

# Suppress cgi deprecation warning (still works in Python 3.12, removed in 3.13)
warnings.filterwarnings('ignore', category=DeprecationWarning, module='cgi')
from .exceptions import (
    PythonExecutionError,
    PipInstallError,
    MissingModuleFolderError,
    InvalidSignalError,
    LagerPythonInvalidProcessIdError,
)

logger = logging.getLogger(__name__)

# Service configuration
SERVICE_HOST = '0.0.0.0'  # Listen on all interfaces
SERVICE_PORT = 5000
SERVICE_VERSION = '1.0.0'


def is_truthy_string(s):
    """Check if a string represents a truthy value"""
    # Handle file-like objects (BytesIO from cgi.FieldStorage)
    if hasattr(s, 'read'):
        try:
            s = s.read()
        except ValueError as e:
            # File might be closed, try to get value differently
            logger.warning(f"Could not read file-like object: {e}")
            return False
    if isinstance(s, bytes):
        s = s.decode()
    if not isinstance(s, str):
        logger.warning(f"is_truthy_string got unexpected type: {type(s)}")
        return False
    return s.lower() in ('true', '1', 'yes', 'on')


class PythonServiceHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Python execution service"""

    def log_message(self, format, *args):
        """Override to use our logger"""
        logger.info(format % args)

    def send_json_response(self, status_code, data):
        """Send JSON response"""
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        # Note: Do NOT set Lager-Output-Version for JSON responses
        # That header indicates streaming format which JSON responses don't use
        self.end_headers()

        response_json = json.dumps(data, indent=2)
        self.wfile.write(response_json.encode('utf-8'))

    def send_error_response(self, status_code, message):
        """Send error response"""
        self.send_json_response(status_code, {
            'error': message,
            'status': 'error'
        })

    def send_streaming_response(self, generator):
        """Send streaming response from generator"""
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Lager-Output-Version', '1')
        self.end_headers()

        try:
            for chunk in generator:
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    # Client disconnected (e.g., Ctrl+C on client side or pipeline closed)
                    logger.info("Client disconnected during streaming (broken pipe)")
                    break
        except BrokenPipeError:
            logger.info("Client disconnected during streaming")
        except Exception as e:
            logger.exception("Error during streaming", exc_info=e)

    def parse_multipart(self):
        """
        Parse multipart/form-data request.

        Returns:
            dict: Dictionary of field name -> file-like object or string value
        """
        content_type = self.headers.get('Content-Type', '')
        if not content_type.startswith('multipart/form-data'):
            logger.warning(f"Expected multipart/form-data, got: {content_type}")
            return {}

        content_length = self.headers.get('Content-Length', '0')
        logger.info(f"Parsing multipart request: type={content_type}, length={content_length}")

        # Parse the multipart form data
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                'REQUEST_METHOD': 'POST',
                'CONTENT_TYPE': content_type,
                'CONTENT_LENGTH': content_length,
            }
        )

        fields = {}
        for key in form.keys():
            item = form[key]
            if isinstance(item, list):
                # For lists (multiple values), read content immediately
                vals = []
                for i in item:
                    if hasattr(i, 'file') and i.file:
                        vals.append(i.file.read())
                    elif hasattr(i, 'value'):
                        vals.append(i.value)
                    else:
                        vals.append(None)
                fields[key] = vals
                logger.info(f"  Field '{key}': list with {len(vals)} items")
            else:
                # For single values, read content immediately to avoid closed file issues
                # Check if this is a "real" file upload (script.py, module.zip) or a form field
                # by looking at the filename - if it ends with .py or .zip, it's a file
                is_real_file = (hasattr(item, 'filename') and item.filename and
                               (item.filename.endswith('.py') or item.filename.endswith('.zip')))

                if is_real_file:
                    # This is a real file upload (script, module) - wrap in BytesIO
                    # Read content now before cgi closes the file
                    content = item.file.read()
                    fields[key] = io.BytesIO(content)
                    logger.info(f"  Field '{key}': file upload (filename={item.filename}, size={len(content)})")
                else:
                    # This is a form field - read value immediately
                    if hasattr(item, 'file') and item.file:
                        val = item.file.read()
                    elif hasattr(item, 'value'):
                        val = item.value
                    else:
                        val = b''
                    fields[key] = val
                    logger.info(f"  Field '{key}': value={val!r} (type={type(val).__name__})")

        logger.info(f"Parsed fields: {list(fields.keys())}")
        return fields

    def _handle_download_file(self):
        """Handle file download requests"""
        import os
        import gzip
        from urllib.parse import urlparse, parse_qs

        # Parse query parameters
        parsed_url = urlparse(self.path)
        query_params = parse_qs(parsed_url.query)

        # Get filename from query parameter
        if 'filename' not in query_params:
            self.send_error_response(400, 'Missing filename parameter')
            return

        filename = query_params['filename'][0]

        # Security check: prevent path traversal attacks
        if '..' in filename:
            self.send_error_response(400, 'Invalid filename: path traversal not allowed')
            return

        # Get absolute path
        abs_filename = os.path.abspath(filename)

        # Check if file exists
        if not os.path.exists(abs_filename):
            self.send_error_response(404, f'File not found: {filename}')
            return

        # Check if it's a file (not a directory)
        if not os.path.isfile(abs_filename):
            self.send_error_response(400, 'Path is not a file')
            return

        try:
            # Send the file (NOT gzipped - the CLI will handle that if needed)
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Disposition', f'attachment; filename="{os.path.basename(filename)}"')

            # Get file size
            file_size = os.path.getsize(abs_filename)
            self.send_header('Content-Length', str(file_size))
            self.end_headers()

            # Stream file content
            with open(abs_filename, 'rb') as f:
                while True:
                    chunk = f.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

            logger.info(f"Successfully sent file: {filename} ({file_size} bytes)")

        except Exception as e:
            logger.error(f"Error sending file {filename}: {e}")
            # Can't send error response here - headers already sent
            # Client will see incomplete response

    def do_GET(self):
        """Handle GET requests"""
        if self.path == '/binaries/list':
            self._handle_binaries_list()
        elif self.path == '/health':
            self.send_json_response(200, {
                'status': 'healthy',
                'service': 'lager-python-box',
                'version': SERVICE_VERSION,
            })
        elif self.path == '/hello':
            # Return plain text for backwards compatibility with controller
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Hello, world! Your box is connected.\n')
        elif self.path == '/version':
            # Return box version
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            version_info = f'lager-python-box {SERVICE_VERSION}\n'
            self.wfile.write(version_info.encode())
        elif self.path == '/cli-version':
            # Return CLI version stored by lager update command
            import os

            version_data = self._read_box_version()
            if version_data:
                self.send_json_response(200, version_data)
            else:
                self.send_error_response(404, 'Version file not found')
        elif self.path == '/status':
            # Return box status for control plane probing
            import os
            from lager.nets.constants import NetType

            version_data = self._read_box_version()
            version = version_data.get('box_version', 'unknown') if version_data else 'unknown'

            nets = []
            try:
                with open('/etc/lager/saved_nets.json', 'r') as f:
                    saved_nets = json.load(f)
                for net in saved_nets:
                    role = net.get('role', '')
                    try:
                        net_type = NetType.from_role(role).name
                    except (KeyError, ValueError):
                        net_type = role
                    nets.append({'name': net.get('name', ''), 'type': net_type})
            except (FileNotFoundError, json.JSONDecodeError, TypeError):
                pass

            self.send_json_response(200, {
                'healthy': True,
                'version': version,
                'nets': nets,
            })
        elif self.path == '/test-stream':
            # Test endpoint to verify streaming format works
            def test_generator():
                # Send a simple stdout message
                yield b'1 12 Hello world\n'
                # Send exit code 0
                yield b'- 1 0'
            self.send_streaming_response(test_generator())
        elif self.path.startswith('/download-file'):
            self._handle_download_file()
        else:
            self.send_error_response(404, 'Not found')

    def _read_box_version(self):
        """Read box version from /etc/lager/version or fallback location.

        Returns dict with box_version, updater_version, raw keys, or None if not found.
        """
        import os

        version_file = '/etc/lager/version'
        if not os.path.exists(version_file):
            version_file = os.path.expanduser('~/box/.lager/version')

        if not os.path.exists(version_file):
            return None

        try:
            with open(version_file, 'r') as f:
                version_content = f.read().strip()
                if '|' in version_content:
                    box_version, updater_version = version_content.split('|', 1)
                else:
                    box_version = version_content
                    updater_version = None

                return {
                    'box_version': box_version,
                    'updater_version': updater_version,
                    'raw': version_content,
                }
        except Exception as e:
            logger.error(f"Error reading version file: {e}")
            return None

    def do_POST(self):
        """Handle POST requests"""
        try:
            if self.path == '/python':
                self._handle_python_execute()
            elif self.path == '/python/kill':
                self._handle_python_kill()
            elif self.path == '/pip':
                self._handle_pip()
            elif self.path == '/test-execute':
                # Simple test endpoint: just run a hardcoded Python script
                self._handle_test_execute()
            elif self.path == '/binaries/add':
                self._handle_binaries_add()
            elif self.path == '/binaries/remove':
                self._handle_binaries_remove()
            else:
                self.send_error_response(404, 'Not found')
        except PythonExecutionError as e:
            logger.error(f"Python execution error: {e}")
            self.send_error_response(422, str(e))
        except Exception as e:
            logger.exception("Unexpected error handling request", exc_info=e)
            self.send_error_response(500, f"Internal server error: {e}")

    def _handle_python_execute(self):
        """Handle POST /python - Execute Python script"""
        logger.info(f"Handling POST /python from {self.client_address}")

        try:
            fields = self.parse_multipart()
            logger.info(f"Parsed multipart fields: {list(fields.keys())}")
        except Exception as e:
            logger.exception("Failed to parse multipart form data", exc_info=e)
            self.send_error_response(400, f"Failed to parse request: {e}")
            return

        # Helper function to get string value from field (handles BytesIO)
        def get_field_value(field):
            if hasattr(field, 'read'):
                return field.read()
            return field

        # Parse request parameters
        detach = is_truthy_string(fields.get('detach', b'false'))
        stdout_is_stderr = is_truthy_string(fields.get('stdout_is_stderr', b'true'))
        timeout_val = get_field_value(fields.get('timeout', b'300'))
        if isinstance(timeout_val, bytes):
            timeout_val = timeout_val.decode()
        timeout = int(timeout_val)

        # Get script/module files
        script_file = fields.get('script')
        module_zip = fields.get('module')

        # Ensure script_file and module_zip are file-like objects (BytesIO)
        # This handles edge cases where the file detection logic in parse_multipart
        # doesn't properly identify the upload as a "real file"
        if script_file and isinstance(script_file, bytes):
            script_file = io.BytesIO(script_file)
        if module_zip and isinstance(module_zip, bytes):
            module_zip = io.BytesIO(module_zip)

        # Get arguments - need to handle BytesIO objects
        args = fields.get('args', [])
        if not isinstance(args, list):
            args = [args]
        # Convert BytesIO objects to bytes
        args = [get_field_value(a) for a in args]

        # Get environment variables
        env_vars = fields.get('env', [])
        if not isinstance(env_vars, list):
            env_vars = [env_vars]
        # Convert BytesIO objects to strings
        env_vars_processed = []
        for e in env_vars:
            val = get_field_value(e)
            if isinstance(val, bytes):
                val = val.decode()
            env_vars_processed.append(val)
        env_vars = env_vars_processed

        # Get optional configuration
        muxes = fields.get('muxes')
        if muxes and hasattr(muxes, 'read'):
            muxes = muxes.read().decode()
        elif isinstance(muxes, bytes):
            muxes = muxes.decode()

        usb_mapping = fields.get('usb_mapping')
        if usb_mapping and hasattr(usb_mapping, 'read'):
            usb_mapping = usb_mapping.read().decode()
        elif isinstance(usb_mapping, bytes):
            usb_mapping = usb_mapping.decode()

        dut_commands = fields.get('dut_commands')
        if dut_commands and hasattr(dut_commands, 'read'):
            dut_commands = dut_commands.read().decode()
        elif isinstance(dut_commands, bytes):
            dut_commands = dut_commands.decode()

        # Execute
        executor = PythonExecutor()
        output_generator = executor.execute(
            script_file=script_file,
            module_zip=module_zip,
            args=args,
            env_vars=env_vars,
            detach=detach,
            timeout=timeout,
            stdout_is_stderr=stdout_is_stderr,
            client_ip=self.client_address[0],
            muxes=muxes,
            usb_mapping=usb_mapping,
            dut_commands=dut_commands,
        )

        if detach:
            self.send_json_response(200, output_generator or {'status': 'detached'})
        else:
            self.send_streaming_response(output_generator)

    def _handle_python_kill(self):
        """Handle POST /python/kill - Kill Python process"""
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        data = json.loads(body.decode('utf-8'))

        sig = data.get('signal', signal.SIGTERM)
        lager_process_id = data.get('lager_process_id')

        PythonExecutor.kill_process(lager_process_id=lager_process_id, sig=sig)
        self.send_json_response(200, {'status': 'killed'})

    def _handle_test_execute(self):
        """Handle POST /test-execute - Simple test of Python execution"""
        import subprocess
        import tempfile
        from lager.exec.process import make_output_channel, stream_process_output

        logger.info("Handling test-execute request")

        # Create a simple test script
        script_content = b'''
import sys
print("Hello from test script!")
print("Python version:", sys.version)
print("Test execution complete.")
'''

        cleanup_fns = set()
        output_channel = make_output_channel(cleanup_fns)

        # Write script to temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.py', mode='wb') as f:
            f.write(script_content)
            script_path = f.name

        # Execute the script
        proc = subprocess.Popen(
            ['/usr/local/bin/python3', script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            bufsize=0,
        )

        self.send_streaming_response(
            stream_process_output(proc, output_channel, cleanup_fns)
        )

    def _handle_pip(self):
        """Handle POST /pip - Run pip command"""
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        data = json.loads(body.decode('utf-8'))

        pip_args = data.get('args', [])
        if not isinstance(pip_args, list):
            self.send_error_response(400, 'args must be a list')
            return

        # Run pip directly (no docker exec)
        import subprocess
        from lager.exec.process import make_output_channel, stream_process_output

        cleanup_fns = set()
        output_channel = make_output_channel(cleanup_fns)

        base_command = ['pip3']
        base_command.extend(pip_args)

        proc = subprocess.Popen(
            base_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            bufsize=0,
        )

        self.send_streaming_response(
            stream_process_output(proc, output_channel, cleanup_fns)
        )

    # =========================================================================
    # Custom Binaries Endpoints
    # =========================================================================

    # Paths for customer binaries
    # Host path (where files are stored on the box filesystem)
    HOST_BINARIES_DIR = '/home/lagerdata/third_party/customer-binaries'
    # Container path (where files are mounted inside the Docker container)
    CONTAINER_BINARIES_DIR = '/home/www-data/customer-binaries'

    def _handle_binaries_list(self):
        """
        Handle GET /binaries/list - List custom binaries on the box

        Returns JSON:
        {
            "binaries": [
                {"name": "my_tool", "size": 12345, "executable": true},
                ...
            ],
            "host_path": "/home/lagerdata/third_party/customer-binaries",
            "mounted": true
        }
        """
        import os
        import stat

        binaries = []
        host_path = self.HOST_BINARIES_DIR
        container_path = self.CONTAINER_BINARIES_DIR

        # Check if directory exists (on host or in container depending on context)
        # In container, we check the container path
        check_path = container_path if os.path.exists(container_path) else host_path

        if os.path.exists(check_path) and os.path.isdir(check_path):
            for name in os.listdir(check_path):
                file_path = os.path.join(check_path, name)
                if os.path.isfile(file_path):
                    file_stat = os.stat(file_path)
                    binaries.append({
                        'name': name,
                        'size': file_stat.st_size,
                        'executable': bool(file_stat.st_mode & stat.S_IXUSR)
                    })

        # Check if the container path is mounted
        mounted = os.path.exists(container_path) and os.path.isdir(container_path)

        self.send_json_response(200, {
            'binaries': binaries,
            'host_path': host_path,
            'container_path': container_path,
            'mounted': mounted
        })

    def _handle_binaries_add(self):
        """
        Handle POST /binaries/add - Upload a binary to the box

        Expects multipart form data:
        - binary: The binary file content
        - name: The name for the binary

        Returns JSON:
        {
            "success": true,
            "name": "my_tool",
            "path": "/home/www-data/customer-binaries/my_tool",
            "restart_required": false
        }
        """
        import os
        import stat

        try:
            fields = self.parse_multipart()
        except Exception as e:
            logger.exception("Failed to parse multipart form data for binaries/add")
            self.send_error_response(400, f"Failed to parse request: {e}")
            return

        # Get the binary file
        binary_file = fields.get('binary')
        if not binary_file:
            self.send_error_response(400, 'binary file is required')
            return

        # Get the name
        name = fields.get('name')
        if name:
            if hasattr(name, 'read'):
                name = name.read()
            if isinstance(name, bytes):
                name = name.decode('utf-8')
        else:
            self.send_error_response(400, 'name is required')
            return

        # Validate name (no path separators)
        if '/' in name or '\\' in name or '..' in name:
            self.send_error_response(400, 'Invalid binary name')
            return

        # Read binary content
        if hasattr(binary_file, 'read'):
            binary_content = binary_file.read()
        elif isinstance(binary_file, bytes):
            binary_content = binary_file
        else:
            self.send_error_response(400, 'Invalid binary file format')
            return

        # Determine which path to use
        # In container, we write to container path (which is mounted from host)
        # If running outside container (dev mode), we write to host path
        if os.path.exists(self.CONTAINER_BINARIES_DIR):
            binaries_dir = self.CONTAINER_BINARIES_DIR
        else:
            binaries_dir = self.HOST_BINARIES_DIR

        # Ensure directory exists
        os.makedirs(binaries_dir, exist_ok=True)

        # Write the binary
        binary_path = os.path.join(binaries_dir, name)
        try:
            with open(binary_path, 'wb') as f:
                f.write(binary_content)

            # Make executable
            os.chmod(binary_path, os.stat(binary_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

            logger.info(f"Binary '{name}' uploaded to {binary_path} ({len(binary_content)} bytes)")

            # Check if container path exists (i.e., if mount is active)
            restart_required = not os.path.exists(self.CONTAINER_BINARIES_DIR)

            self.send_json_response(200, {
                'success': True,
                'name': name,
                'path': os.path.join(self.CONTAINER_BINARIES_DIR, name),
                'size': len(binary_content),
                'restart_required': restart_required
            })

        except Exception as e:
            logger.exception(f"Failed to write binary '{name}'")
            self.send_error_response(500, f"Failed to write binary: {e}")

    def _handle_binaries_remove(self):
        """
        Handle POST /binaries/remove - Remove a binary from the box

        Expects JSON body:
        {
            "name": "my_tool"
        }

        Returns JSON:
        {
            "success": true,
            "name": "my_tool"
        }
        """
        import os

        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body.decode('utf-8'))
        except json.JSONDecodeError as e:
            self.send_error_response(400, f"Invalid JSON: {e}")
            return

        name = data.get('name')
        if not name:
            self.send_error_response(400, 'name is required')
            return

        # Validate name (no path separators)
        if '/' in name or '\\' in name or '..' in name:
            self.send_error_response(400, 'Invalid binary name')
            return

        # Determine which path to use
        if os.path.exists(self.CONTAINER_BINARIES_DIR):
            binaries_dir = self.CONTAINER_BINARIES_DIR
        else:
            binaries_dir = self.HOST_BINARIES_DIR

        binary_path = os.path.join(binaries_dir, name)

        if not os.path.exists(binary_path):
            self.send_error_response(404, f"Binary '{name}' not found")
            return

        try:
            os.remove(binary_path)
            logger.info(f"Binary '{name}' removed from {binary_path}")

            self.send_json_response(200, {
                'success': True,
                'name': name
            })

        except Exception as e:
            logger.exception(f"Failed to remove binary '{name}'")
            self.send_error_response(500, f"Failed to remove binary: {e}")


def create_python_service():
    """
    Create and return the Python execution HTTP server.

    Returns:
        ThreadingHTTPServer: Server instance
    """
    server = ThreadingHTTPServer((SERVICE_HOST, SERVICE_PORT), PythonServiceHandler)
    logger.info(f"Python execution service initialized on {SERVICE_HOST}:{SERVICE_PORT}")
    return server


def run_python_service():
    """
    Run the Python execution service (blocking).

    This function starts the HTTP server and runs forever.
    """
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.FileHandler('/tmp/lager-python-service.log'),
            logging.StreamHandler()
        ]
    )

    logger.info("Starting Lager Python Execution Service")
    logger.info(f"Listening on {SERVICE_HOST}:{SERVICE_PORT}")

    server = create_python_service()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down Python execution service")
        server.shutdown()


if __name__ == '__main__':
    run_python_service()
