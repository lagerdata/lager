# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
lager.python.executor - Python Script Executor

Handles direct execution of Python scripts within the container.
No longer uses docker exec - executes scripts directly since this service
now runs inside the Python container.

Originally migrated from gateway/controller/controller/application/views/run.py (legacy, removed)
Now performs direct execution to eliminate the controller container dependency.
"""

import os
import json
import tempfile
import shutil
import zipfile
import subprocess
import logging
import uuid
import threading
import signal as signal_module

from lager.exec.process import (
    make_output_channel,
    add_cleanup_fn,
    do_cleanup,
    stream_process_output,
    stream_process_output_to_file,
)
from .exceptions import (
    PipInstallError,
    MissingModuleFolderError,
    InvalidSignalError,
    LagerPythonInvalidProcessIdError,
)

logger = logging.getLogger(__name__)

MAX_TIMEOUT = 300
LAGER_PYTHON_IP_ADDR = '172.18.0.10'  # Docker-internal network default; overridden by LOCAL_ADDRESS env var


def safe_unlink(path):
    """Safely unlink a file, logging errors"""
    try:
        os.unlink(path)
    except Exception as exc:
        logger.exception('Failed to unlink tmpfile', exc_info=exc)


def load_box_secrets():
    """
    Load organization secrets from box filesystem.

    Returns:
        dict: Secrets from /etc/lager/org_secrets.json or empty dict
    """
    secrets_file = '/etc/lager/org_secrets.json'

    if os.path.exists(secrets_file):
        try:
            with open(secrets_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not load secrets from {secrets_file}: {e}")

    return {}


def get_box_id():
    """
    Get box ID from local config.

    Returns:
        str: Box ID from /etc/lager/box_id or 'unknown'
    """
    id_file = '/etc/lager/box_id'

    if os.path.exists(id_file):
        try:
            with open(id_file, 'r') as f:
                return f.read().strip()
        except Exception as e:
            logger.warning(f"Could not load box ID from {id_file}: {e}")

    return 'unknown'


class PythonExecutor:
    """
    Executes Python scripts directly within the container.

    This class handles:
    - Script/module upload and extraction
    - Environment variable setup
    - Pip dependency installation
    - Direct command execution (no docker exec)
    - Output streaming

    Since this service now runs inside the Python container, we execute
    scripts directly without the docker exec wrapper.
    """

    def __init__(self):
        """
        Initialize the executor.
        """
        self.cleanup_fns = set()

    def execute(
        self,
        script_file=None,
        module_zip=None,
        args=None,
        env_vars=None,
        detach=False,
        timeout=MAX_TIMEOUT,
        stdout_is_stderr=True,
        client_ip=None,
        muxes=None,
        usb_mapping=None,
        dut_commands=None,
    ):
        """
        Execute a Python script in the container.

        Args:
            script_file: File object containing the script to execute
            module_zip: Zip file object containing a Python module
            args: List of command-line arguments (bytes)
            env_vars: List of environment variable strings ("KEY=value")
            detach: Run in detached mode (don't wait for completion)
            timeout: Maximum execution time in seconds
            stdout_is_stderr: Redirect stderr to stdout
            client_ip: IP address of the client (for logging)
            muxes: Multiplexer configuration JSON
            usb_mapping: USB device mapping JSON
            dut_commands: DUT command configuration JSON

        Returns:
            Generator yielding output chunks for streaming

        Raises:
            MissingModuleFolderError: If neither script nor module provided
            PipInstallError: If pip install fails
        """
        script = None
        module_folder = None

        try:
            # Get environment info (we're running inside the container now)
            # PIGPIO_ADDR should be set by the container environment
            pigpio_addr = os.environ.get('PIGPIO_ADDR', '172.18.0.2')  # Docker-internal default for pigpio container
            this_host = os.environ.get('LAGER_HOST', '172.17.0.1')  # Docker bridge default; set by start_box.sh

            # Handle module upload
            if module_zip:
                module_folder = tempfile.mkdtemp()
                if not detach:
                    add_cleanup_fn(self.cleanup_fns, shutil.rmtree, module_folder)

                with zipfile.ZipFile(module_zip, 'r') as zip_ref:
                    zip_ref.extractall(module_folder)

                # Install dependencies if requirements.txt exists
                requirements_path = os.path.join(module_folder, 'requirements.txt')
                if os.path.exists(requirements_path):
                    self._install_requirements(module_folder)

            # Handle script upload
            if script_file:
                with tempfile.NamedTemporaryFile(delete=False, suffix='.py') as script:
                    script.write(script_file.read())
                    script.flush()
                    module_folder = os.path.dirname(script.name)
                add_cleanup_fn(self.cleanup_fns, safe_unlink, script.name)

            if module_folder is None:
                raise MissingModuleFolderError()

            # Create output channel
            output_channel = make_output_channel(self.cleanup_fns)

            # Build environment variables
            env_dict = self._build_env_vars(
                env_vars=env_vars,
                pigpio_addr=pigpio_addr,
                this_host=this_host,
                module_folder=module_folder,
                output_channel=output_channel,
                stdout_is_stderr=stdout_is_stderr,
                client_ip=client_ip,
                muxes=muxes,
                usb_mapping=usb_mapping,
                dut_commands=dut_commands,
            )

            # Build command - direct Python execution (no docker exec)
            command = ['/usr/local/bin/python3']
            if script_file:
                command.append(os.path.join(module_folder, os.path.basename(script.name)))
            else:
                command.append(os.path.join(module_folder, 'main.py'))

            # Add arguments
            if args:
                command.extend([arg.decode() if isinstance(arg, bytes) else arg for arg in args])

            # Set up timeout (use timeout command directly, not docker exec)
            if not detach:
                base_command = ['/usr/bin/timeout', str(min(timeout, MAX_TIMEOUT))] + command
            else:
                base_command = command

            # Merge environment variables with current environment
            full_env = os.environ.copy()
            full_env.update(env_dict)

            # Execute directly
            if detach:
                stdin = subprocess.DEVNULL
                stdout = subprocess.PIPE
                stderr = subprocess.STDOUT if stdout_is_stderr else subprocess.PIPE
            else:
                stdin = subprocess.PIPE
                stdout = subprocess.PIPE
                stderr = subprocess.STDOUT if stdout_is_stderr else subprocess.PIPE

            proc = subprocess.Popen(
                base_command,
                stdout=stdout,
                stderr=stderr,
                stdin=stdin,
                cwd=module_folder,  # Set working directory directly
                env=full_env,       # Pass environment directly
                bufsize=0,
                start_new_session=detach,  # detached processes survive independently
            )

            # Handle detached mode — capture output to file, return immediately
            if detach:
                lager_process_id = None
                for var in (env_vars or []):
                    if var.startswith('LAGER_PROCESS_ID='):
                        lager_process_id = var.split('=', 1)[1]
                        break

                # Set up process registry directory for reattach
                process_dir = f'/tmp/lager_processes/{lager_process_id}'
                os.makedirs(process_dir, exist_ok=True)
                log_path = os.path.join(process_dir, 'output.log')
                meta_path = os.path.join(process_dir, 'meta.json')

                meta = {
                    'pid': proc.pid,
                    'lager_process_id': lager_process_id,
                    'started': __import__('time').time(),
                    'status': 'running',
                    'returncode': None,
                }
                with open(meta_path, 'w') as f:
                    json.dump(meta, f)

                # Start daemon thread to capture output to log file
                capture_thread = threading.Thread(
                    target=stream_process_output_to_file,
                    args=(proc, output_channel, self.cleanup_fns, log_path, meta_path),
                    daemon=True,
                )
                capture_thread.start()

                return {
                    'status': 'detached',
                    'pid': proc.pid,
                    'lager_process_id': lager_process_id,
                }

            # Stream output
            return stream_process_output(proc, output_channel, self.cleanup_fns)

        except Exception:
            do_cleanup(self.cleanup_fns)
            raise

    def _install_requirements(self, module_folder):
        """
        Install Python dependencies from requirements.txt.

        Args:
            module_folder: Path to the module containing requirements.txt

        Raises:
            PipInstallError: If pip install fails
        """
        # Direct pip install (no docker exec)
        pip_command = [
            'pip3', 'install', '-r', 'requirements.txt',
        ]
        proc = subprocess.run(
            pip_command,
            cwd=module_folder,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False
        )
        if proc.returncode != 0:
            raise PipInstallError(proc.stdout)

    def _build_env_vars(
        self,
        env_vars,
        pigpio_addr,
        this_host,
        module_folder,
        output_channel,
        stdout_is_stderr,
        client_ip,
        muxes,
        usb_mapping,
        dut_commands,
    ):
        """
        Build environment variable dictionary for the container.

        Args:
            env_vars: List of environment variable strings from client
            pigpio_addr: IP address of pigpio container
            this_host: Host IP address (Docker interface)
            module_folder: Path to the module folder on host
            output_channel: Output channel file object
            stdout_is_stderr: Whether stderr is redirected to stdout
            client_ip: IP address of the client
            muxes: Multiplexer configuration JSON
            usb_mapping: USB device mapping JSON
            dut_commands: DUT command configuration JSON

        Returns:
            dict: Environment variables
        """
        env_dict = {}

        # Parse client-provided env vars
        if env_vars:
            for var in env_vars:
                if '=' in var:
                    key, value = var.split('=', 1)
                    env_dict[key] = value

        # Standard Lager environment variables
        env_dict.update({
            'PIGPIO_ADDR': pigpio_addr,
            'LAGER_HOST': this_host,
            'LAGER_HOST_MODULE_FOLDER': module_folder,
            'LAGER_STDOUT_IS_STDERR': str(stdout_is_stderr),
            'LAGER_OUTPUT_CHANNEL': output_channel.name,
            'PYTHONBREAKPOINT': 'remote_pdb.set_trace',
            'LOCAL_ADDRESS': LAGER_PYTHON_IP_ADDR,
            'REMOTE_PDB_HOST': '0.0.0.0',
            'REMOTE_PDB_PORT': '5555',
        })

        # Box metadata
        box_id = get_box_id()
        env_dict['LAGER_BOX_ID'] = box_id

        # Organization secrets
        box_secrets = load_box_secrets()
        for key, value in box_secrets.items():
            env_dict[f'LAGER_SECRET_{key}'] = value

        # Client info
        if client_ip:
            env_dict['LAGER_CLIENT_IP'] = client_ip

        # Optional configurations
        if muxes:
            env_dict['LAGER_MUXES'] = muxes
        if usb_mapping:
            env_dict['LAGER_USB_MAPPINGS'] = usb_mapping
        if dut_commands:
            env_dict['LAGER_DUT_COMMANDS'] = dut_commands

        return env_dict

    @staticmethod
    def kill_process(lager_process_id=None, sig=signal_module.SIGTERM):
        """
        Kill a running Python process.

        Args:
            lager_process_id: UUID of the process to kill (optional)
            sig: Signal to send (default: SIGTERM)

        Raises:
            InvalidSignalError: If signal number is invalid
            LagerPythonInvalidProcessIdError: If process ID is invalid UUID
        """
        if sig not in range(0, signal_module.NSIG):
            raise InvalidSignalError(sig)

        if lager_process_id:
            try:
                uuid.UUID(lager_process_id)
            except ValueError:
                raise LagerPythonInvalidProcessIdError(lager_process_id)
            # Kill process by searching for it directly
            _kill_by_proc_id(sig, lager_process_id.encode())

            # Clean up log directory
            process_dir = f'/tmp/lager_processes/{lager_process_id}'
            if os.path.isdir(process_dir):
                import shutil as _shutil
                try:
                    _shutil.rmtree(process_dir)
                    logger.info(f"Cleaned up process directory: {process_dir}")
                except Exception as exc:
                    logger.warning(f"Failed to clean up {process_dir}: {exc}")
        else:
            # No process ID — kill ALL lager python processes
            _kill_all_lager_processes(sig)

            # Clean up all log directories
            process_base = '/tmp/lager_processes'
            if os.path.isdir(process_base):
                import shutil as _shutil
                try:
                    _shutil.rmtree(process_base)
                    logger.info(f"Cleaned up all process directories: {process_base}")
                except Exception as exc:
                    logger.warning(f"Failed to clean up {process_base}: {exc}")


def _kill_by_proc_id(sig, proc_id):
    """
    Kill a process by its lager process ID.

    Since LAGER_PROCESS_ID is an environment variable (not visible in ps output),
    we need to search /proc/*/environ files to find the matching process.

    Args:
        sig: Signal number to send
        proc_id: Lager process ID (UUID) to search for (bytes)
    """
    import glob

    proc_id_str = proc_id.decode() if isinstance(proc_id, bytes) else proc_id
    search_str = f'LAGER_PROCESS_ID={proc_id_str}'.encode()

    # Search through /proc/*/environ to find processes with matching LAGER_PROCESS_ID
    for environ_path in glob.glob('/proc/*/environ'):
        try:
            pid = int(environ_path.split('/')[2])

            # Read the environment variables for this process
            with open(environ_path, 'rb') as f:
                environ_data = f.read()

            # Check if this process has the matching LAGER_PROCESS_ID
            if search_str in environ_data:
                # Read cmdline to determine if it's timeout or python
                cmdline_path = f'/proc/{pid}/cmdline'
                try:
                    with open(cmdline_path, 'rb') as f:
                        cmdline = f.read().replace(b'\x00', b' ')
                except FileNotFoundError:
                    continue

                # Log what we're killing
                if b'/usr/bin/timeout' in cmdline:
                    logger.info(f"Killing timeout process {pid} with signal {sig} (cmdline: {cmdline[:100]})")
                elif b'/usr/local/bin/python3' in cmdline:
                    logger.info(f"Killing Python process {pid} with signal {sig} (cmdline: {cmdline[:100]})")
                else:
                    logger.info(f"Killing process {pid} with signal {sig}")

                # Kill the process
                try:
                    os.kill(pid, sig)
                    logger.info(f"Successfully sent signal {sig} to PID {pid}")
                except (ProcessLookupError, PermissionError) as e:
                    logger.warning(f"Failed to kill PID {pid}: {e}")
                    continue

                # If not SIGKILL, wait up to 3s then escalate
                if sig != signal_module.SIGKILL:
                    import time
                    for _ in range(30):
                        try:
                            os.kill(pid, 0)  # check if alive
                        except ProcessLookupError:
                            return  # process exited
                        time.sleep(0.1)
                    # Still alive — escalate to SIGKILL
                    try:
                        logger.warning(f"Process {pid} did not exit after 3s, sending SIGKILL")
                        os.kill(pid, signal_module.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                return

        except (FileNotFoundError, ValueError, PermissionError):
            # Process exited or we can't read it
            continue

    logger.warning(f"Could not find process with LAGER_PROCESS_ID={proc_id_str}")


def _kill_all_lager_processes(sig):
    """
    Kill all processes that have a LAGER_PROCESS_ID environment variable.

    Used when --kill is invoked without a specific process ID.

    Args:
        sig: Signal number to send
    """
    import glob

    search_str = b'LAGER_PROCESS_ID='
    killed = 0

    for environ_path in glob.glob('/proc/*/environ'):
        try:
            pid = int(environ_path.split('/')[2])

            with open(environ_path, 'rb') as f:
                environ_data = f.read()

            if search_str in environ_data:
                logger.info(f"Killing lager process PID {pid} with signal {sig}")
                try:
                    os.kill(pid, sig)
                    killed += 1
                except (ProcessLookupError, PermissionError) as e:
                    logger.warning(f"Failed to kill PID {pid}: {e}")
                    continue

                if sig != signal_module.SIGKILL:
                    import time
                    for _ in range(30):
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(0.1)
                    else:
                        try:
                            logger.warning(f"Process {pid} did not exit after 3s, sending SIGKILL")
                            os.kill(pid, signal_module.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass

        except (FileNotFoundError, ValueError, PermissionError):
            continue

    if killed:
        logger.info(f"Killed {killed} lager process(es)")
    else:
        logger.warning("No running lager processes found")
