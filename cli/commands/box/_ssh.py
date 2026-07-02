# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Shared SSH transport for `lager box config` host-side operations.

A single default runner (with optional stdin), and a parameterized
sudo-error helper. Callers (mount prep, apply-time host ops, container
bounce) inject custom runners for unit tests; production code uses the
default. Keeps the BatchMode + user-resolution policy in one place so
fixes don't have to be made three times.
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable, Optional, Tuple

from ...errors import LagerError

SshRunner = Callable[..., Tuple[int, str, str]]

# Dedicated key used by `lager install` / `lager update`. When present,
# it's tried first for all `lager box config` SSH calls — on boxes
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


def ensure_lager_box_keypair(key_path: str = _LAGER_BOX_KEY) -> bool:
    """Generate the ed25519 lager_box keypair if it doesn't exist.

    Returns True if a new key was generated, False if one already existed.
    Raises :class:`LagerError` if ssh-keygen fails. This is the single
    definition of how the lager_box key is created — ``lager authorize``
    and ``lager update`` both call it, so the key type and comment can
    never drift apart between the two provisioning paths.
    """
    if os.path.exists(key_path):
        return False
    os.makedirs(os.path.dirname(key_path), mode=0o700, exist_ok=True)
    proc = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", key_path, "-N", "", "-C", "lager-box-access"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise LagerError(
            "Could not generate the SSH key.",
            cause=(proc.stderr or "").strip() or None,
            fixes=[f'Generate it manually: ssh-keygen -t ed25519 -f {key_path} -N ""'],
        )
    return True


def key_auth_works(
    dest: str,
    *,
    key_path: str = _LAGER_BOX_KEY,
    connect_timeout: int = 15,
) -> bool:
    """Return True if ``dest`` (user@host) accepts the lager_box key unattended.

    BatchMode refuses any password/passphrase prompt, so this never hangs:
    a box that hasn't authorized the key fails fast instead of blocking on
    a prompt. accept-new auto-trusts a first-seen host key so a brand-new
    box doesn't wedge on the interactive host-key question either.

    The connect timeout is generous (15s) because this is the first, coldest
    connection to the box and a slow first hop — Tailscale/VPN establishing the
    path — can take several seconds. A too-short timeout here yields a false
    "key not authorized", which makes `lager update` spuriously re-prompt
    "SSH key not configured" on every run for a box that is in fact set up.
    """
    proc = subprocess.run(
        [
            "ssh", "-i", key_path,
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={connect_timeout}",
            dest, "true",
        ],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


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
        if use_key:
            ssh_cmd.extend(["-i", _LAGER_BOX_KEY])
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
    if use_key and rc == 255 and any(m in stderr.lower() for m in _AUTH_FAILURE_MARKERS):
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
