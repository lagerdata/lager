# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Shared SSH transport for `lager box-config` host-side operations.

A single default runner (with optional stdin), and a parameterized
sudo-error helper. Callers (mount prep, apply-time host ops, container
bounce) inject custom runners for unit tests; production code uses the
default. Keeps the BatchMode + user-resolution policy in one place so
fixes don't have to be made three times.
"""

from __future__ import annotations

import getpass
import os
import re
import shlex
import shutil
import socket
import subprocess
from typing import Callable, List, Optional, Sequence, Tuple

from ...errors import LagerError

SshRunner = Callable[..., Tuple[int, str, str]]

# Dedicated key used by `lager install` / `lager update`. When present,
# it's tried first for all `lager box-config` SSH calls — on boxes
# provisioned by lager it's the only key authorized for the box user.
# Passing `-i` replaces ssh's default identity list, so when this key
# is NOT authorized for the box's user (customer-managed users), a key
# the user installed themselves with ssh-copy-id would never be
# offered. On auth failure the runner therefore retries once without
# the key, letting the default identities through.
_LAGER_BOX_KEY = os.path.expanduser("~/.ssh/lager_box")

# Destinations (user@ip) where the lager_box key was already rejected
# this process; skip the doomed keyed attempt on subsequent calls. One
# `apply` makes several SSH calls (per-mount prep, apt, sysctl, udev,
# bounce) and shouldn't pay a failed auth round-trip for each.
_KEY_FALLBACK_DESTS: set = set()

_AUTH_FAILURE_MARKERS = ("permission denied", "too many authentication failures")


def is_auth_failure(stderr: Optional[str]) -> bool:
    """True when ssh's stderr says the *server* rejected our credentials.

    This is the one predicate that licenses retrying a keyed attempt
    without the key: an authentication failure means the remote command
    never ran, so the retry cannot double-execute anything. Transport
    failures (timeout, no route, refused) are deliberately excluded — a
    second attempt fails the same way, only slower.
    """
    return any(m in (stderr or "").lower() for m in _AUTH_FAILURE_MARKERS)


def lager_box_key_if_present(key_path: str = _LAGER_BOX_KEY) -> Optional[str]:
    """The lager_box key path when the file exists, else ``None``.

    The shape every caller wants: an identity to offer, or nothing. A box
    that has never had `lager ssh-setup` (or `lager install`) run against
    this machine has no such key, and must keep working on whatever
    identity the operator already uses.
    """
    return key_path if os.path.exists(key_path) else None


def ssh_identity_args(identity: Optional[str]) -> List[str]:
    """``['-i', identity]`` when an identity was chosen, else ``[]``.

    Every command that shells out to ssh builds its argv differently
    (``-t`` here, BatchMode there, multiplexing options from the pool), so
    what is shared between them is this fragment rather than a whole
    command builder. Callers get the identity from
    :func:`probe_box_identity`; ``None`` means "offer ssh's own defaults".
    """
    return ["-i", identity] if identity else []


def probe_box_identity(
    dest: str,
    *,
    key_path: str = _LAGER_BOX_KEY,
    connect_timeout: int = 10,
    timeout: int = 15,
    extra_args: Sequence[str] = (),
) -> Tuple[Optional[str], subprocess.CompletedProcess]:
    """Answer "which identity should this command offer ``dest``?".

    Returns ``(identity, proc)``. ``identity`` is ``key_path`` when the
    lager_box key authenticated, and ``None`` when it did not — meaning
    the caller should let ssh offer its own default identities. ``proc``
    is the attempt whose result the caller should classify, so the
    existing "connection refused" / "host key verification failed" /
    "could not resolve hostname" branches keep working unchanged.

    Why a probe rather than an unconditional ``-i``: passing ``-i``
    *replaces* ssh's built-in default identity list (id_rsa, id_ed25519,
    …) rather than adding to it, so an unconditional ``-i`` would lock
    out a box authorized only by a key the operator installed themselves
    with ssh-copy-id. At most two connections are made, and the second
    only when the server actually rejected our credentials.

    BatchMode=yes on both attempts: a box that authorizes neither must
    fail fast with an actionable error, not block on a password prompt
    that a hardened box would refuse to honor anyway.

    Exceptions (``TimeoutExpired``, ``FileNotFoundError``) propagate —
    callers already classify those around their connectivity check.
    """
    def _probe(identity: Optional[str]) -> subprocess.CompletedProcess:
        argv = ["ssh"]
        argv.extend(ssh_identity_args(identity))
        if identity is not None:
            # IdentitiesOnly=yes ONLY on the keyed attempt, and only here.
            # Without it, `-i` adds the key to a list that still holds the
            # agent's keys and any ~/.ssh/config identity for the host, so
            # this reports "the key works" whenever ANY credential works —
            # and the keyless fallback below can never fire, because the
            # first attempt already succeeded on something else.
            #
            # Deliberately not applied to the commands themselves: there,
            # offering the key first while leaving the operator's own
            # identities available as backup is exactly what is wanted. The
            # probe is the one place that has to isolate the question.
            argv.extend(["-o", "IdentitiesOnly=yes"])
        argv.extend(["-o", f"ConnectTimeout={connect_timeout}"])
        argv.extend(extra_args)
        argv.extend(["-o", "BatchMode=yes", dest, "echo ok"])
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

    identity = lager_box_key_if_present(key_path)
    proc = _probe(identity)
    if identity is not None and proc.returncode != 0 and is_auth_failure(proc.stderr):
        identity = None
        proc = _probe(None)
    return identity, proc


# Where box/start_box.sh's authorized-keys sync reads managed keys from.
#
# Installing the key and REGISTERING it are not the same thing. ssh-copy-id
# (and the `>> ~/.ssh/authorized_keys` equivalents in `lager update` and the
# deploy script) append a line OUTSIDE every manager's marker block, and
# start_box.sh's sync preserves such lines byte-for-byte — but only against
# itself. Any other key manager on the box that rebuilds authorized_keys from
# its own source, keeping only its own block, takes that loose line with it,
# and start_box.sh then re-creates its block from this directory alone. A key
# that is not here does not come back.
#
# So every path that installs the lager_box key also drops the public half
# here. A `.pub` in this directory is durable by construction: it is the
# sync's source of truth, and it is what makes the key survive a rebuild
# rather than depend on nobody having done one.
BOX_KEYS_DIR = "/etc/lager/authorized_keys.d"


def registered_key_name(user: Optional[str] = None, host: Optional[str] = None) -> str:
    """Filename this machine's lager_box public key is registered under.

    One file per operator machine, overwritten by every ssh-setup, so
    re-running after regenerating the keypair replaces the entry instead of
    leaving the superseded key authorized forever. The `lager-box-` prefix
    keeps it clear of names other key managers claim.
    """
    if user is None:
        try:
            user = getpass.getuser()
        except Exception:
            user = "user"
    if host is None:
        host = (socket.gethostname() or "host").split(".")[0]
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", f"{user}-{host}")[:48].strip("-._")
    return f"lager-box-{slug or 'unknown'}.pub"


def register_key_command(pub_key: str, filename: str) -> str:
    """Remote shell command that publishes `pub_key` into the box's key dir.

    Unprivileged first, then `sudo -n`. On a plain Lager box the key
    directory belongs to the box and no sudo is wanted. A key manager that
    hardens the box makes it root-owned deliberately — a writable key
    directory lets any user on the box authorize any key — so the answer
    there is not to widen the directory but to use the narrow NOPASSWD grant
    `lager install` writes for exactly this path shape.

    Failure still has to be reportable, so the `|| true` is confined to the
    trailing chmod (which cannot succeed for the login user when the sudo
    path was taken, and is redundant there: root's umask already gives 0644).
    A failing mkdir or write short-circuits and surfaces its own status.
    """
    directory = shlex.quote(BOX_KEYS_DIR)
    path = shlex.quote(f"{BOX_KEYS_DIR}/{filename}")
    line = shlex.quote(pub_key.strip())
    return (
        f"{{ mkdir -p {directory} 2>/dev/null || sudo -n mkdir -p {directory}; }} && "
        f"{{ printf '%s\\n' {line} > {path} 2>/dev/null || "
        f"printf '%s\\n' {line} | sudo -n tee {path} >/dev/null; }} && "
        f"{{ chmod 644 {path} 2>/dev/null || true; }}"
    )


def register_lager_box_key(
    dest: str,
    *,
    key_path: str = _LAGER_BOX_KEY,
    timeout: int = 30,
) -> Tuple[bool, str]:
    """Publish this machine's lager_box public key into the box's key dir.

    Returns ``(ok, detail)``; `detail` is a short reason when ok is False.
    Call only once the key authenticates — it runs over that key, so it costs
    no extra password prompt.

    Idempotent: writes the same file with the same content on every run.
    """
    pub_path = f"{key_path}.pub"
    try:
        with open(pub_path, "r", encoding="utf-8") as fh:
            pub_key = fh.read().strip()
    except OSError as exc:
        return False, f"could not read {pub_path}: {exc}"
    if not pub_key:
        return False, f"{pub_path} is empty"

    cmd = register_key_command(pub_key, registered_key_name())
    try:
        proc = subprocess.run(
            ["ssh", "-i", key_path, "-o", "BatchMode=yes", dest, cmd],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, strip_ssh_banner(proc.stderr) or f"ssh exited {proc.returncode}"
    return True, ""


def ensure_lager_box_keypair(key_path: str = _LAGER_BOX_KEY) -> bool:
    """Generate the ed25519 lager_box keypair if it doesn't exist.

    Returns True if a new key was generated, False if one already existed.
    Raises :class:`LagerError` if ssh-keygen fails. This is the single
    definition of how the lager_box key is created — ``lager ssh-setup``
    and ``lager update`` both call it, so the key type and comment can
    never drift apart between the two provisioning paths.
    """
    if os.path.exists(key_path):
        return False
    os.makedirs(os.path.dirname(key_path), mode=0o700, exist_ok=True)
    keygen_exe = shutil.which("ssh-keygen")
    if keygen_exe is None:
        raise LagerError(
            "ssh-keygen not found — install the OpenSSH client.",
            fixes=["Windows: Settings → System → Optional Features → 'OpenSSH Client'"],
        )
    try:
        proc = subprocess.run(
            [keygen_exe, "-t", "ed25519", "-f", key_path, "-N", "", "-C", "lager-box-access"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise LagerError("ssh-keygen failed to launch.", cause=str(exc), raw=exc) from exc
    if proc.returncode != 0:
        raise LagerError(
            "Could not generate the SSH key.",
            cause=(proc.stderr or "").strip() or None,
            fixes=[f'Generate it manually: ssh-keygen -t ed25519 -f {key_path} -N ""'],
        )
    return True


def lager_box_pubkey_blob(key_path: str = _LAGER_BOX_KEY) -> Optional[str]:
    """The base64 blob of this machine's lager_box public key, or None.

    The blob rather than the whole line, because comments drift: the same key
    appears as `lager-box-access` here and under whatever name a key manager
    rendered it with there. The blob is the key. It is `[A-Za-z0-9+/=]` only,
    so it is safe to single-quote into a remote shell command.
    """
    try:
        with open(f"{key_path}.pub", "r", encoding="utf-8") as fh:
            fields = fh.read().strip().split()
    except OSError:
        return None
    return fields[1] if len(fields) >= 2 else None


def key_installed_on_box(
    dest: str,
    *,
    key_path: str = _LAGER_BOX_KEY,
    timeout: int = 30,
) -> Optional[bool]:
    """Is this machine's lager_box key in ``dest``'s authorized_keys?

    Returns True/False when the box could be asked, and None when it could
    not (unreachable, no local public key) — three outcomes, because
    "couldn't tell" must not be silently read as "not installed" and trigger
    a needless reinstall, nor as "installed" and skip a needed one.

    This asks the box directly instead of inferring installation from a
    successful authentication, because that inference does not hold. An auth
    probe is satisfied by ANY identity ssh offers, and an operator whose
    ssh_config has a `Host *` IdentityFile — a common fleet-management
    layout — supplies one to every host. Both `lager ssh-setup` and
    `lager update` gated their reinstall on such a probe, so on exactly the
    fleets where a key manager might delete lager_box, nothing could ever
    detect that it had been deleted. Grepping authorized_keys has no such
    blind spot: the key is there or it is not.

    Any working identity is fine for the query itself — this is a question
    about the box's state, not about which credential asked.
    """
    blob = lager_box_pubkey_blob(key_path)
    if blob is None or shutil.which("ssh") is None:
        return None
    try:
        proc = subprocess.run(
            [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=15",
                dest,
                f"grep -qF '{blob}' ~/.ssh/authorized_keys",
            ],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return True
    # grep exits 1 for "no match" and 2 for a missing file; both mean the key
    # is not installed. Anything else (255) is ssh failing to get there at
    # all, which is not an answer about the key.
    if proc.returncode in (1, 2):
        return False
    return None


def resolve_box_user(box_ip: str) -> str:
    """SSH user for a resolved box IP, defaulting to "lagerdata".

    `box_ip` is already a resolved IP (see _resolve_box). get_box_user
    is keyed by name, so reverse-look the name first; otherwise every
    box with a custom user silently falls back to "lagerdata".
    """
    from ...box_storage import get_box_name_by_ip, get_box_user
    name = get_box_name_by_ip(box_ip)
    return (get_box_user(name) if name else None) or "lagerdata"


def default_ssh_runner(
    box_ip: str,
    cmd: str,
    *,
    stdin: Optional[str] = None,
    timeout: int = 60,
) -> Tuple[int, str, str]:
    """Run `cmd` on the box over SSH and return (rc, stdout, stderr).

    BatchMode=yes refuses to prompt, so a missing key fails fast instead
    of hanging. `stdin` is piped to the remote command's stdin when
    supplied — used by sysctl_apply to ship the conf body to `sudo tee`
    without expanding metacharacters through the shell.

    The lager_box key is tried first when present; if the server rejects
    auth (ssh rc 255 + auth-failure stderr), retry once without it so the
    user's default identities (e.g. installed via ssh-copy-id) get a
    chance. An auth failure means the remote command never ran, so the
    retry can't double-execute anything. Timeouts/no-route are NOT
    retried — the second attempt would just hang the same way.
    """
    user = resolve_box_user(box_ip)
    dest = f"{user}@{box_ip}"

    def _run(use_key: bool) -> Tuple[int, str, str]:
        ssh_cmd = ["ssh", "-o", "BatchMode=yes"]
        ssh_cmd.extend(ssh_identity_args(_LAGER_BOX_KEY if use_key else None))
        ssh_cmd.extend([dest, cmd])
        try:
            proc = subprocess.run(
                ssh_cmd,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            # A hung connection (half-dead box, dropping firewall) must not
            # escape as a traceback. 255 is ssh's own-failure code, so every
            # caller's transport-failure handling applies. The stderr lacks
            # the auth-failure markers, so no useless keyless retry happens.
            return 255, "", f"ssh timed out after {timeout}s to {dest}"
        return proc.returncode, proc.stdout, proc.stderr

    use_key = os.path.exists(_LAGER_BOX_KEY) and dest not in _KEY_FALLBACK_DESTS
    rc, stdout, stderr = _run(use_key)
    if use_key and rc == 255 and is_auth_failure(stderr):
        _KEY_FALLBACK_DESTS.add(dest)
        rc, stdout, stderr = _run(False)
    return rc, stdout, stderr


_SSH_BANNER_PREFIXES = ("Warning: Permanently added",)


def strip_ssh_banner(stderr: str) -> str:
    """Drop ssh's informational host-key banner from captured stderr.

    ssh writes "Warning: Permanently added '<host>' (<type>) to the list
    of known hosts." to stderr the first time it learns a box's host key.
    It is not an error, but folding it verbatim into a "Failed to ..."
    message (as the bench.json write path used to) buries the real cause.
    Returns "" for empty/None input.
    """
    if not stderr:
        return ""
    kept = [
        ln for ln in stderr.splitlines()
        if not ln.strip().startswith(_SSH_BANNER_PREFIXES)
    ]
    return "\n".join(kept).strip()


def sudo_error_message(
    stderr: str,
    *,
    base_text: str,
    bootstrap_text: str,
) -> str:
    """Format a sudo-failure message for the caller's HostOpResult / PrepResult.

    Returns raw stderr for non-sudo errors (so e.g. apt's "Unable to
    locate package" isn't buried under bootstrap noise). For sudo
    errors, returns `base_text` plus the caller's `bootstrap_text` —
    the sudoers rule the caller needs is feature-specific (mount prep
    needs only mkdir+chown; apply needs apt/sysctl/tee/rm too), so the
    bootstrap is passed in rather than hard-coded here.

    The ssh host-key banner is stripped first so it never leaks into a
    surfaced message via the raw-stderr path.
    """
    err = strip_ssh_banner(stderr)
    if err and "a password is required" not in err.lower() and "sudo:" not in err.lower():
        return err
    out = base_text
    if err:
        out += f" Stderr: {err}"
    return out + "\n\n" + bootstrap_text
