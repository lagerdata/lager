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
    STATUS_STARTING,
    STATUS_RUNNING,
    make_output_channel,
    add_cleanup_fn,
    do_cleanup,
    finalize_meta,
    stream_process_output,
    stream_process_output_to_file,
    set_pipe_size,
    update_meta,
    write_meta,
)
from lager.exec import quiesce
from lager.exec.quiesce import pid_is_alive as _pid_is_alive
from .job_lock import DetachedJobLock
from .exceptions import (
    PipInstallError,
    MissingModuleFolderError,
    InvalidSignalError,
    LagerPythonInvalidProcessIdError,
)

logger = logging.getLogger(__name__)

MAX_TIMEOUT = 300
LAGER_PYTHON_IP_ADDR = '172.18.0.10'  # Docker-internal network default; overridden by LOCAL_ADDRESS env var

# Where a detached job's reattach registry lives: one directory per job holding
# meta.json and output.log (and, when a script pauses, breakpoint.json/resume).
PROCESS_REGISTRY_DIR = '/tmp/lager_processes'

# Exit code reported for a job that never started. 1 is deliberate: it is what
# the attached path already reports for the same failures (the box answers 422,
# the CLI prints the detail and exits 1), so the two agree.
START_FAILURE_EXIT_CODE = 1

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



def _wrap_with_timeout(command, timeout, detach):
    """Wrap a job's argv in /usr/bin/timeout, or return it unchanged.

    Pure: no processes, no filesystem. Extracted from ``execute`` so the
    contract can be asserted without standing up a job -- what the deadline
    is, what happens when it passes, and when the wrapper is absent.

    ``--kill-after`` is what makes the deadline enforceable. GNU timeout sends
    SIGTERM at the deadline and nothing more, so a script that never returns
    from it never dies: one blocked in an uninterruptible call (a
    pyvisa/libusb/serial read -- the normal case on a box) or one that installs
    its own SIGTERM handler runs on until something else kills it.
    ``lager python --timeout 3`` against a 30-second sleep was measured still
    running 17 minutes later, ended by a CI step timeout rather than by the
    timeout it was given. Measured against coreutils 9.1 with the grace in
    place, the same script exits at deadline+grace with 137 --
    ``SIGKILL_EXIT_CODE``, which the CLI has always had a message for and could
    never reach.

    The grace window is ``CLEANUP_GRACE_S``, matching the SIGTERM->SIGKILL
    escalation ``_signal_and_reap`` already applies, so a job is escalated the
    same way however it is being stopped. Longer would let a wedged job hold
    the bench past the point ``QUIESCE_WAIT_S`` assumes it has been reaped.

    What the grace does NOT buy is a script's own ``finally``. Measured against
    coreutils 9.1 and CPython 3: SIGTERM's default disposition terminates the
    interpreter outright, so a ``try/finally`` around the work does not unwind
    on this path -- and did not before this change either. (``_signal_targets``
    reasons carefully about truncating cleanup, but that is about SIGINT, which
    CPython does raise as ``KeyboardInterrupt``; ``timeout`` sends SIGTERM.) So
    the grace is what it looks like -- a wait before force-killing -- and it
    matters for the two cases that outlive SIGTERM: a script that installs its
    own handler, and one wedged in an uninterruptible call where no grace
    length would have helped.

    Making the deadline run bench teardown would mean sending SIGINT instead
    (``timeout --signal=INT``), which changes the exit code a script can report
    and is a larger behavioural change than the deadline not working.

    Args:
        command: the job's argv
        timeout: requested seconds; 0 means no limit. Values above
            MAX_TIMEOUT are capped to it, with a warning -- the cap used to be
            a silent ``min()``, so a job asking for 600s ran 300 and nothing
            said so, which reads as the timeout firing early rather than as a
            ceiling being applied.
        detach: a detached job is wrapped only when a deadline was actually
            asked for, and is NOT subject to MAX_TIMEOUT. See below.

    Returns:
        The argv to execute.

    **Detached jobs.** These used to be left unwrapped unconditionally, on the
    grounds that the wrapper would become the group leader ``_signal_targets``
    reasons about. That is a true statement of fact but it is not a reason, and
    the walk-through says the opposite. ``start_new_session=True`` makes Popen's
    direct child a session and process-group leader; when that child is the
    wrapper, GNU timeout's own ``setpgid(0, 0)`` is a no-op and the script
    inherits the wrapper's group. ``_signal_targets`` then sees
    ``getpgid(script) == wrapper`` -- in the matched set and alive -- so the
    script is *covered* and only the wrapper is signalled, which forwards. That
    is exactly the arrangement ``_signal_targets`` was written and measured for,
    and exactly what the attached path already does. The unwrapped detached job,
    where the script is signalled directly, is the less-exercised shape.

    Two deliberate asymmetries with the attached path:

    * **No MAX_TIMEOUT ceiling.** That ceiling exists because the CLI's
      streaming read timeout is 320s, so an attached job allowed past it would
      have its stream time out client-side before the box could report the
      deadline. Nobody streams a detached job -- ``run_python`` returns
      immediately and ``attach_python`` uses no read timeout at all -- so the
      320s bound has nothing to say about it. Capping ``-d --timeout 3600`` to
      300 would kill the long run ``-d`` exists for.
    * **Wrapped only when a timeout was requested.** The attached path wraps
      unconditionally and relies on duration 0 being inert. Doing that here
      would insert a process into the tree of every detached job that never
      asked for a deadline, changing its recorded pid and returncode for no
      benefit. The default ``-d`` path stays byte-identical to before.
    """
    if not timeout:
        # 0 means no limit. The attached path still gets an inert `timeout 0`
        # wrapper (unchanged); the detached path gets no wrapper at all.
        if detach:
            return command
        return _timeout_argv(0) + command

    if detach:
        return _timeout_argv(timeout) + command

    effective = min(timeout, MAX_TIMEOUT)
    if timeout > MAX_TIMEOUT:
        logger.warning(
            'requested timeout %ss exceeds the box ceiling of %ss; using %ss',
            timeout, MAX_TIMEOUT, effective,
        )
    return _timeout_argv(effective) + command


def resolve_lager_process_id(env_vars):
    """
    The job's id, minted if the client did not supply one.

    Pure. Returns ``(process_id, env_vars)`` with ``LAGER_PROCESS_ID``
    guaranteed present in the environment the child will inherit.

    Minting one is not merely so the registry directory has a name. A job is
    found by reading LAGER_PROCESS_ID out of ``/proc/*/environ``
    (see ``_scan_lager_pids``), so an id the child does not carry is an id
    ``lager python --kill <id>`` can never resolve. Previously a request with no
    LAGER_PROCESS_ID left this as None: the registry became the literal path
    ``/tmp/lager_processes/None``, the response reported ``null``, and the job
    was unkillable by id.

    Args:
        env_vars: list of "KEY=value" strings, or None

    Returns:
        (str, list): the process id, and the env list carrying it
    """
    env_vars = list(env_vars or [])
    for var in env_vars:
        if var.startswith('LAGER_PROCESS_ID='):
            value = var.split('=', 1)[1]
            if value:
                return value, env_vars

    process_id = str(uuid.uuid4())
    logger.info(f"Request carried no LAGER_PROCESS_ID; minted {process_id}")
    env_vars.append(f'LAGER_PROCESS_ID={process_id}')
    return process_id, env_vars


def process_dir_for(lager_process_id):
    """The registry directory for one job."""
    return os.path.join(PROCESS_REGISTRY_DIR, lager_process_id)


def register_detached_job(lager_process_id):
    """
    Create the reattach registry entry for a job that has not started yet.

    Called on the REQUEST thread, before the response. The empty output.log is
    the load-bearing part: ``stream_log_file`` opens it directly, so a
    ``lager python --reattach`` racing the launch would otherwise hit
    FileNotFoundError and be answered with a 500 for a job that is fine.

    Deliberately allowed to raise. If the box cannot even record the job -- a
    full or unwritable /tmp -- this is the last moment at which anything can be
    reported to the client, and answering 200 would be a lie.

    Args:
        lager_process_id: the job's id

    Returns:
        (str, str): log_path, meta_path
    """
    process_dir = process_dir_for(lager_process_id)
    os.makedirs(process_dir, exist_ok=True)
    log_path = os.path.join(process_dir, 'output.log')
    meta_path = os.path.join(process_dir, 'meta.json')

    with open(log_path, 'wb'):
        pass

    write_meta(meta_path, {
        'lager_process_id': lager_process_id,
        'pid': None,
        # When the job was REGISTERED, not when it started. With a pip install
        # in front of Popen those can be minutes apart.
        'started': time.time(),
        'status': STATUS_STARTING,
        'returncode': None,
    })
    return log_path, meta_path


def _timeout_argv(seconds):
    """The /usr/bin/timeout prefix for a given deadline.

    A duration of 0 disables the timeout (coreutils), so the wrapper is inert
    on the attached default path and --kill-after never comes into play.
    Verified against coreutils 9.1 rather than assumed.
    """
    return ['/usr/bin/timeout', '--kill-after', str(CLEANUP_GRACE_S), str(seconds)]

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
            timeout: Maximum execution time in seconds. 0 means no limit.
                Attached jobs are capped at MAX_TIMEOUT, with a warning;
                detached jobs are not (see _wrap_with_timeout). Enforced by
                /usr/bin/timeout, which sends SIGTERM at the deadline and
                SIGKILL CLEANUP_GRACE_S later.
            stdout_is_stderr: Redirect stderr to stdout
            client_ip: IP address of the client (for logging)
            muxes: Multiplexer configuration JSON
            usb_mapping: USB device mapping JSON
            dut_commands: DUT command configuration JSON

        Returns:
            Generator yielding output chunks for streaming, or -- when
            ``detach`` is set -- the response dict from ``start_detached``.

        Raises:
            MissingModuleFolderError: If neither script nor module provided
            PipInstallError: If pip install fails (attached jobs only; a
                detached job reports it through its own log)
        """
        if detach:
            return self.start_detached(
                script_file=script_file,
                module_zip=module_zip,
                args=args,
                env_vars=env_vars,
                timeout=timeout,
                stdout_is_stderr=stdout_is_stderr,
                client_ip=client_ip,
                muxes=muxes,
                usb_mapping=usb_mapping,
                dut_commands=dut_commands,
            )

        proc, output_channel = self._prepare_and_spawn(
            script_file=script_file,
            module_zip=module_zip,
            args=args,
            env_vars=env_vars,
            detach=False,
            timeout=timeout,
            stdout_is_stderr=stdout_is_stderr,
            client_ip=client_ip,
            muxes=muxes,
            usb_mapping=usb_mapping,
            dut_commands=dut_commands,
        )
        return stream_process_output(proc, output_channel, self.cleanup_fns)

    @staticmethod
    def validate_request(script_file, module_zip):
        """
        Reject a request that has nothing to run.

        The one check cheap enough to stay on the HTTP request thread for a
        detached launch, so a client mistake is still answered with today's 422
        rather than accepted as a job that could never have existed.

        Truthiness, not ``is None``, to match the ``if module_zip:`` /
        ``if script_file:`` branches in _prepare_and_spawn -- an empty
        non-file ``script`` part arrives as b'' and must keep failing here.

        Raises:
            MissingModuleFolderError: if neither was supplied
        """
        if not script_file and not module_zip:
            raise MissingModuleFolderError()

    def _prepare_and_spawn(
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
        Everything from upload to Popen. Returns ``(proc, output_channel)``.

        Slow by nature, and that is the point of it being its own method: the
        module unpack, ``pip install -r requirements.txt`` with no bound on it,
        the QUIESCE_WAIT_S gate (69s worst case) and the direct-USB handoff all
        live here. The attached path runs it inside the HTTP request because
        the client is waiting for the stream either way. The detached path runs
        it on its own thread, because a client that asked not to wait should
        not be made to.

        Args:
            see execute()

        Returns:
            (subprocess.Popen, file): the spawned process and its output channel

        Raises:
            MissingModuleFolderError, PipInstallError, and anything Popen or
            the unpack can raise. Cleanup runs before the exception leaves.
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

            base_command = _wrap_with_timeout(command, timeout, detach)

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

            return proc, output_channel

        except Exception:
            do_cleanup(self.cleanup_fns)
            raise

    def start_detached(
        self,
        script_file=None,
        module_zip=None,
        args=None,
        env_vars=None,
        timeout=MAX_TIMEOUT,
        stdout_is_stderr=True,
        client_ip=None,
        muxes=None,
        usb_mapping=None,
        dut_commands=None,
        lock_holder=None,
    ):
        """
        Register a detached job and hand it to a background thread.

        Returns as soon as the job is recorded on disk, BEFORE any of the work
        that makes a job slow to start. That ordering is the entire fix: this
        all used to run inside the HTTP request, so a `-d` launch of a module
        with a requirements.txt sat on the client's 320s read timeout waiting
        for a pip install it had explicitly asked not to wait for.

        Everything that can go wrong after this point is reported through the
        job's own log and meta.json rather than to a caller that has already
        been answered. See _supervise_detached.

        Args:
            see execute()
            lock_holder: the box-lock holder string the CLI acquired with, when
                it wants the box to own the lock for this job's lifetime.
                Absent for a run that did not auto-lock.

        Returns:
            dict: the response body

        Raises:
            MissingModuleFolderError: nothing to run (422)
            OSError: the registry could not be written (500) -- the last point
                at which a failure can still reach the client
        """
        self.validate_request(script_file, module_zip)
        lager_process_id, env_vars = resolve_lager_process_id(env_vars)
        log_path, meta_path = register_detached_job(lager_process_id)

        job = {
            'script_file': script_file,
            'module_zip': module_zip,
            'args': args,
            'env_vars': env_vars,
            'detach': True,
            'timeout': timeout,
            'stdout_is_stderr': stdout_is_stderr,
            'client_ip': client_ip,
            'muxes': muxes,
            'usb_mapping': usb_mapping,
            'dut_commands': dut_commands,
        }

        threading.Thread(
            target=self._supervise_detached,
            args=(job, lager_process_id, log_path, meta_path, lock_holder),
            name=f'lager-detached-{lager_process_id[:8]}',
            daemon=True,
        ).start()

        return {
            'status': 'detached',
            # Null, not absent. No process exists yet -- that is the point --
            # but dropping the key would turn any consumer's data['pid'] into a
            # KeyError. The real pid lands in meta.json once there is one.
            'pid': None,
            'lager_process_id': lager_process_id,
            # Tells the CLI the box has taken over the lock's lifetime. Its
            # absence is what an older box looks like, and the CLI must keep
            # its eternal hold in that case rather than arm a TTL nothing on
            # the box will refresh.
            'lock_held_by_box': bool(lock_holder),
        }

    def _supervise_detached(self, job, lager_process_id, log_path, meta_path,
                            lock_holder=None):
        """
        Run one detached job to completion on this thread. Never raises.

        One thread for the whole lifecycle -- setup, spawn, capture -- rather
        than a supervisor that hands off to a capture thread. One try/finally
        then owns every way the job can end, so there is no window in which a
        job has been abandoned but nothing has recorded that.

        Args:
            job: kwargs for _prepare_and_spawn
            lager_process_id: the job's id, for logging
            log_path: the job's output.log
            meta_path: the job's meta.json
            lock_holder: box-lock holder to keep alive while the job runs
        """
        failure = None
        job_lock = DetachedJobLock(lock_holder)
        try:
            proc, output_channel = self._prepare_and_spawn(**job)
            update_meta(meta_path, pid=proc.pid, status=STATUS_RUNNING)
            job_lock.start()
            stream_process_output_to_file(
                proc, output_channel, self.cleanup_fns, log_path, meta_path,
            )
        except Exception as exc:
            logger.exception(
                'detached job %s failed to start', lager_process_id, exc_info=exc,
            )
            failure = (
                b'lager python: the detached job failed to start on the box.\n'
                + f'{type(exc).__name__}: {exc}\n'.encode('utf-8', errors='replace')
            )
        finally:
            job_lock.stop()
            # Backstop. A no-op when the capture loop already wrote a real exit
            # code; the guarantee is that nothing leaves a job at 'starting' or
            # 'running' with nobody working on it, which would make a reattach
            # tail an ended job forever.
            finalize_meta(
                meta_path, log_path,
                message=failure if failure is not None else (
                    b'lager python: the detached job ended without reporting an '
                    b'exit code.\n'
                ),
                returncode=START_FAILURE_EXIT_CODE,
            )
            do_cleanup(self.cleanup_fns)

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
            process_dir = process_dir_for(lager_process_id)
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
            process_base = PROCESS_REGISTRY_DIR
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
