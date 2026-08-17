# Copyright 2024-2026 Lager Data
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
import glob
import json
import tempfile
import shutil
import time
import zipfile
import subprocess
import logging
import uuid
import threading
import signal as signal_module

from lager.exec.process import (
    CLEANUP_GRACE_S,
    CLEANUP_MAX_S,
    TERMINATE_GRACE_S,
    make_output_channel,
    add_cleanup_fn,
    do_cleanup,
    stream_process_output,
    stream_process_output_to_file,
    set_pipe_size,
)
from lager.exec import quiesce
from lager.exec.quiesce import pid_is_alive as _pid_is_alive
from .exceptions import (
    PipInstallError,
    MissingModuleFolderError,
    InvalidSignalError,
    LagerPythonInvalidProcessIdError,
)

logger = logging.getLogger(__name__)

MAX_TIMEOUT = 300
LAGER_PYTHON_IP_ADDR = '172.18.0.10'  # Docker-internal network default; overridden by LOCAL_ADDRESS env var

# How long a starting job waits for the previous one to finish shutting down.
#
# Derived, not chosen. It has to cover the longest reap the box can legitimately
# perform, or the wait ends while the previous job is still being killed and the
# next one starts driving a bench that is still mid-teardown -- exactly the
# overlap the gate exists to prevent. That bound is the full escalation:
# cleanup up to CLEANUP_MAX_S, then TERMINATE_GRACE_S for SIGTERM and again for
# SIGKILL.
#
# It was a flat 15.0 when the escalation was bounded by a fixed 3s cleanup grace
# (7s worst case, comfortably covered). The progress watchdog took the worst case
# to 64s and left this behind, which silently reopened the overlap for any
# cleanup running longer than 15s. Deriving it means that cannot happen again.
#
# Reaching this ceiling still means a job is wedged rather than cleaning up, and
# we proceed with a warning: an indefinite block would turn one stuck script into
# a permanently unusable box.
QUIESCE_WAIT_S = CLEANUP_MAX_S + 2 * TERMINATE_GRACE_S + 5.0


def _release_hardware_service_direct_usb_claims():
    """Best-effort handoff: drop hardware_service's direct-USB claims.

    Tier-1 :9000 net commands route through hardware_service, which keeps each
    device's driver cached (and its USB session open) for warm-path latency.
    ``lager python`` scripts talk to the same physical device directly in a
    child process and fail with an exclusive-claim error (LabJack LJM 1230,
    libusb ``Resource busy``) if we don't yield first. This releases every
    non-VISA USB claim (LabJack/FT232H/Aardvark/USB-202/Joulescope/PPK2) while
    retaining shared pyvisa sessions (supply/battery/eload) — unlike the old
    v0.16.5 ``/cache/clear`` band-aid that tore those down and reintroduced
    ``[Errno 16] Resource busy``.
    """
    try:
        import requests
        from lager.constants import HARDWARE_SERVICE_PORT
        resp = requests.post(
            f'http://127.0.0.1:{HARDWARE_SERVICE_PORT}/cache/release_direct_usb',
            timeout=5.0,
        )
        # requests only raises on transport failures, so an HTTP 500 from the
        # release endpoint would otherwise pass for success and the script
        # would be spawned into a still-claimed device with nothing logged.
        if resp.status_code != 200:
            logger.warning(
                "Direct USB claim release returned HTTP %s; hardware_service may "
                "still hold LabJack/FT232H/Aardvark/Joulescope/PPK2 claims and "
                "this script may fail with an exclusive-claim error "
                "(LJM 1230 / libusb Resource busy). Body: %.200s",
                resp.status_code, resp.text,
            )
        else:
            logger.debug("Released direct USB claims: %.200s", resp.text)
    except Exception as e:
        # Surface this: if the handoff fails, the user's script is likely to
        # hit an opaque exclusive-claim error (LJM 1230 / libusb Resource
        # busy) and this log line is the only clue pointing at the cause.
        logger.warning("Direct USB claim release failed; script may hit "
                       "exclusive-claim errors on LabJack/FT232H/Aardvark/"
                       "Joulescope/PPK2 devices: %s", e)

# Nice-value delta we *try* to apply to scripts so they aren't out-scheduled
# by the half-dozen other Python services sharing the lager container
# (python execution / hardware / debug / HTTP / MCP). Critical for tight-
# timing flows like the DA14695 ROM-bootloader recovery handshake, where the
# script must respond within 50–120 ms of each byte from the bootloader.
# Best-effort: silently no-ops if the container/user doesn't hold CAP_SYS_NICE.
_SCRIPT_NICE_DELTA = -10


def _boost_process_priority(pid):
    """
    Best-effort attempt to raise the scheduling priority of ``pid``. Any
    failure is swallowed — the script will still run at the default nice
    value, just with more jitter.

    Called from the *parent* immediately after Popen rather than via a
    preexec_fn: the python execution service is a ThreadingHTTPServer, and
    Python documents preexec_fn as unsafe in a multithreaded process (the
    forked child can deadlock before exec() if another thread held an
    allocator/import lock at fork time). Setting priority from the parent on
    the child's pid has identical permission semantics (still needs
    CAP_SYS_NICE) with no fork/exec window to deadlock in.
    """
    try:
        # Lower the nice value (= higher priority). Requires CAP_SYS_NICE
        # or a permissive RLIMIT_NICE; will raise PermissionError otherwise.
        os.setpriority(os.PRIO_PROCESS, pid, _SCRIPT_NICE_DELTA)
    except (PermissionError, OSError):
        pass


def safe_unlink(path):
    """Safely unlink a file, logging errors"""
    try:
        os.unlink(path)
    except Exception as exc:
        logger.exception('Failed to unlink tmpfile', exc_info=exc)


def load_box_secrets(secrets_file='/etc/lager/org_secrets.json'):
    """
    Load organization secrets from box filesystem.

    The path is a parameter (production callers take the default) so tests
    can hand it a real fixture file instead of patching os.path.exists —
    which is process-global and, on Python >= 3.14, also rewrites every
    pathlib.Path.exists().

    Returns:
        dict: Secrets from /etc/lager/org_secrets.json or empty dict
    """
    if os.path.exists(secrets_file):
        try:
            mode = os.stat(secrets_file).st_mode & 0o777
            if mode & 0o077:
                logger.warning(
                    "%s was group/world-readable (%03o); tightening to 0600",
                    secrets_file, mode
                )
                os.chmod(secrets_file, 0o600)
        except OSError as e:
            logger.warning(f"Could not check/fix permissions on {secrets_file}: {e}")
        try:
            with open(secrets_file, 'r') as f:
                return json.load(f)
        except PermissionError as e:
            # Distinct from a malformed or missing file: the secrets are THERE
            # and we are not allowed to read them. That happens when the file
            # is mode 0600 under an owner other than this process's uid —
            # typically a file copied in by hand and then tightened by a boot
            # script running as the host user.
            #
            # Logged at error, and separately from the generic path, because
            # the consequence is invisible downstream: we return {} either way,
            # so every LAGER_SECRET_* variable simply vanishes and scripts fail
            # far from the cause. This message is the only place the real
            # reason appears.
            logger.error(
                "Cannot read %s: %s. This process runs as uid %d, and the file "
                "is readable only by its owner. Secret injection will be EMPTY. "
                "Fix it on the box with: sudo chown %d:%d %s && sudo chmod 600 %s "
                "(or run `lager update`, which repairs this).",
                secrets_file, e, os.getuid(),
                os.getuid(), os.getuid(), secrets_file, secrets_file,
            )
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
            # '-u' forces unbuffered stdout/stderr at the interpreter level.
            # PYTHONUNBUFFERED=1 is also set in the Dockerfile, but '-u' is the
            # belt-and-suspenders guarantee that block-buffering can't insert
            # latency in scripts that print() between time-critical I/O steps.
            command = ['/usr/local/bin/python3', '-u']
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

            # Execute directly. stdin is always DEVNULL: scripts run by
            # `lager python` never have an interactive stdin, and a dangling
            # subprocess.PIPE that nobody writes to is just a kernel pipe sitting
            # open. (Previously this was PIPE for the non-detached path.)
            stdin = subprocess.DEVNULL
            stdout = subprocess.PIPE
            stderr = subprocess.STDOUT if stdout_is_stderr else subprocess.PIPE

            # Do not start driving the bench while the previous job is still
            # doing so. Its cleanup runs after its client has gone, so the box
            # lock — which the client releases, and which lapses on its own
            # when the client is killed — says nothing about whether the
            # hardware is actually free yet.
            quiesced, still_reaping = quiesce.wait_until_clear(QUIESCE_WAIT_S)
            if not quiesced:
                logger.warning(
                    'starting a new job with pid(s) %s still shutting down '
                    'after %.0fs; their cleanup may not have finished and the '
                    'bench may be in an unexpected state',
                    ', '.join(str(p) for p in still_reaping), QUIESCE_WAIT_S,
                )

            # Yield any direct-USB claims hardware_service holds from prior
            # :9000 CLI net commands (gpo/adc/spi/watt/... on LabJack, FT232H,
            # Aardvark, USB-202, Joulescope, PPK2) so this script can open them.
            _release_hardware_service_direct_usb_claims()

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

            # Raise scheduling priority from the parent (see
            # _boost_process_priority for why this isn't a preexec_fn).
            _boost_process_priority(proc.pid)

            # Enlarge the stdout (and stderr, if separate) pipe buffers so the
            # user's script can never block on a print() while the parent's
            # HTTP socket back to the CLI is slow to drain. Default Linux pipe
            # buffer is 64 KiB; we ask for 1 MiB. Best-effort — falls back to
            # the kernel default if F_SETPIPE_SZ is not permitted.
            if proc.stdout is not None:
                set_pipe_size(proc.stdout.fileno())
            if proc.stderr is not None:
                set_pipe_size(proc.stderr.fileno())

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
            # Route the builtin breakpoint() to lager's interactive pause so a
            # script's `breakpoint()` works like `lager.pause()` instead of
            # crashing on the never-installed remote_pdb.
            'PYTHONBREAKPOINT': 'lager.breakpoint.pause',
            'LOCAL_ADDRESS': LAGER_PYTHON_IP_ADDR,
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


KILL_POLL_INTERVAL_S = 0.1


def _scan_lager_pids(search_str):
    """
    PIDs whose environment block contains ``search_str``.

    LAGER_PROCESS_ID is an environment variable rather than something visible
    in ps output, so identifying a job means reading /proc/*/environ.

    Args:
        search_str: bytes to look for in each process's environment

    Returns:
        list[int]: matching PIDs, in no particular order
    """
    pids = []
    for environ_path in glob.glob('/proc/*/environ'):
        try:
            pid = int(environ_path.split('/')[2])
            with open(environ_path, 'rb') as f:
                environ_data = f.read()
        except (FileNotFoundError, ProcessLookupError, ValueError, PermissionError):
            # Process exited between the glob and the read, or belongs to
            # another user.
            continue
        if search_str in environ_data:
            pids.append(pid)
    return pids


def _describe_pid(pid):
    """Truncated cmdline for log messages, or '<gone>' if the process exited."""
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            cmdline = f.read().replace(b'\x00', b' ').strip()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return '<gone>'
    return cmdline[:100].decode('utf-8', errors='replace')


def _signal_targets(pids, sig):
    """
    Reduce a job's PIDs to the ones that must be signalled directly.

    A non-detached job runs under /usr/bin/timeout, which puts itself and the
    script in a fresh process group and forwards SIGINT/SIGTERM/SIGHUP to it.
    Signalling the group leader therefore already reaches every member.

    Signalling the members as well does not merely duplicate harmlessly.
    Measured against GNU coreutils 9.7: signalling wrapper and script together
    delivers SIGINT to the script twice, and the second one lands inside the
    `finally` that is unwinding from the first, raises out of it, and
    truncates the cleanup after roughly one step. Signalling either alone
    delivers exactly once and the cleanup completes. One signal per process
    group is the whole point of this function.

    Two things are deliberately still signalled directly:

    * Everything, when ``sig`` is SIGKILL. It cannot be caught, so nothing
      forwards it and every PID has to be named. There is no cleanup to
      truncate in that case either.
    * Any member whose group leader is not part of this job, or is no longer
      alive to do the forwarding — a grandchild that called setsid(), or a
      wrapper that has already exited.

    Args:
        pids: the job's PIDs
        sig: signal number about to be sent

    Returns:
        list[int]: the subset to signal
    """
    if sig == signal_module.SIGKILL:
        return list(pids)

    matched = set(pids)
    targets = []
    for pid in pids:
        try:
            leader = os.getpgid(pid)
        except (ProcessLookupError, PermissionError):
            continue
        covered = leader != pid and leader in matched and _pid_is_alive(leader)
        if covered:
            logger.info(f"PID {pid} will be signalled by its group leader {leader}")
            continue
        targets.append(pid)
    return targets


def _signal_and_reap(pids, sig, grace_s=None):
    """
    Signal a job, then wait for all of it to exit.

    Two properties matter here, and the old implementation had neither.

    Signalling is reduced to one process per group (see ``_signal_targets``)
    so an interrupt is delivered exactly once and the script's cleanup is not
    cut short by its own duplicate.

    Whatever is signalled, the wait covers every PID in the job and runs once
    for the whole set. The previous shape resolved each PID fully — signal,
    poll for 3s, escalate — before touching the next, so a job with a wrapper,
    a script and a grandchild could hold the request open for 9s while the
    client's POST timed out underneath it.

    Args:
        pids: the job's PIDs
        sig: signal number to send
        grace_s: how long to wait for exit before escalating to SIGKILL.
            Defaults to CLEANUP_GRACE_S, read at call time so the caller can
            size the window to the workload.

    Returns:
        int: number of processes successfully signalled
    """
    if grace_s is None:
        grace_s = CLEANUP_GRACE_S

    # Registered before the first signal so there is no gap in which the job
    # is unwinding its cleanup but the box believes the bench is free.
    with quiesce.reaping_job(pids):
        signalled = []
        for pid in _signal_targets(pids, sig):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError) as exc:
                logger.warning(f"Failed to signal PID {pid}: {exc}")
                continue
            signalled.append(pid)
            logger.info(f"Sent signal {sig} to PID {pid}")

        # Nothing to escalate to, so there is nothing to wait for.
        if not signalled or sig == signal_module.SIGKILL:
            return len(signalled)

        # Waits on the whole job, not just what was signalled: a member covered
        # by its group leader's forwarding still has to be seen to exit.
        deadline = time.monotonic() + grace_s
        remaining = [pid for pid in pids if _pid_is_alive(pid)]
        while remaining and time.monotonic() < deadline:
            time.sleep(KILL_POLL_INTERVAL_S)
            remaining = [pid for pid in remaining if _pid_is_alive(pid)]

        for pid in remaining:
            logger.warning(f"Process {pid} did not exit after {grace_s}s, sending SIGKILL")
            try:
                os.kill(pid, signal_module.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        return len(signalled)


def _kill_by_proc_id(sig, proc_id):
    """
    Kill every process belonging to one lager process ID.

    All of a job's processes carry LAGER_PROCESS_ID — the /usr/bin/timeout
    wrapper, the python3 script it wraps, and anything the script spawned — so
    all of them have to be collected before any of them is signalled. This
    used to signal whichever match glob() returned first and then return, and
    glob() yields readdir order: a job whose script had spawned a child could
    have that child killed and reported as a successful kill while the script
    itself ran on untouched.

    Args:
        sig: Signal number to send
        proc_id: Lager process ID (UUID) to search for (bytes or str)
    """
    proc_id_str = proc_id.decode() if isinstance(proc_id, bytes) else proc_id
    pids = _scan_lager_pids(f'LAGER_PROCESS_ID={proc_id_str}'.encode())

    if not pids:
        logger.warning(f"Could not find process with LAGER_PROCESS_ID={proc_id_str}")
        return

    for pid in pids:
        logger.info(f"Killing PID {pid} with signal {sig} (cmdline: {_describe_pid(pid)})")
    _signal_and_reap(pids, sig)


def _kill_all_lager_processes(sig):
    """
    Kill all processes that have a LAGER_PROCESS_ID environment variable.

    Used when --kill is invoked without a specific process ID. Note that this
    sweeps every lager python job on the box, not just the caller's.

    Args:
        sig: Signal number to send
    """
    pids = _scan_lager_pids(b'LAGER_PROCESS_ID=')

    if not pids:
        logger.warning("No running lager processes found")
        return

    for pid in pids:
        logger.info(f"Killing lager process PID {pid} with signal {sig} (cmdline: {_describe_pid(pid)})")
    killed = _signal_and_reap(pids, sig)
    logger.info(f"Killed {killed} lager process(es)")
