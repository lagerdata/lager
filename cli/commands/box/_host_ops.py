# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Host-side SSH helpers for `lager box config apply`:
  - apt_install: `sudo apt-get install -y` over SSH
  - sysctl_apply: write /etc/sysctl.d/99-lager-box-config.conf + sysctl --system

Both use the same passwordless-sudo + BatchMode SSH pattern as
`_mount_prep.ensure_host_path_owned`. When sudo isn't configured, callers
get back a structured failure with a copy-pasteable bootstrap fix.

Pure logic; the SSH transport is injected so unit tests can drive every
branch without a real box.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

SYSCTL_CONF_PATH = "/etc/sysctl.d/99-lager-box-config.conf"
_SYSCTL_HEADER = (
    "# Managed by `lager box config sysctl`; manual edits are overwritten on apply.\n"
)

SshRunner = Callable[[str, str], Tuple[int, str, str]]


@dataclass
class HostOpResult:
    ok: bool
    action: str
    message: str = ""
    manual_fix: Optional[str] = None


def _default_ssh_runner(box_ip: str, cmd: str) -> Tuple[int, str, str]:
    from ...box_storage import get_box_user

    user = get_box_user(box_ip) or "lagerdata"
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", f"{user}@{box_ip}", cmd],
        capture_output=True,
        text=True,
        timeout=600,
    )
    return proc.returncode, proc.stdout, proc.stderr


def apt_install(
    box_ip: str,
    packages: List[str],
    *,
    ssh_runner: Optional[SshRunner] = None,
) -> HostOpResult:
    """Install the given apt packages on the box host. Idempotent — apt
    skips packages that are already at the requested version."""
    if not packages:
        return HostOpResult(ok=True, action="noop", message="No apt packages configured.")
    runner = ssh_runner or _default_ssh_runner
    quoted_pkgs = " ".join(shlex.quote(p) for p in packages)
    cmd = (
        "sudo -n apt-get update -qq && "
        f"sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {quoted_pkgs}"
    )
    rc, _stdout, stderr = runner(box_ip, cmd)
    if rc != 0:
        return HostOpResult(
            ok=False,
            action="failed",
            message=_sudo_or_apt_error(stderr, packages),
            manual_fix=f"sudo apt-get install -y {quoted_pkgs}",
        )
    return HostOpResult(
        ok=True,
        action="installed",
        message=f"Installed/verified {len(packages)} apt package(s): " + ", ".join(packages),
    )


def sysctl_apply(
    box_ip: str,
    sysctl: Dict[str, str],
    *,
    conf_path: str = SYSCTL_CONF_PATH,
    ssh_runner: Optional[SshRunner] = None,
) -> HostOpResult:
    """Write the sysctl conf and run `sysctl --system`. When `sysctl` is
    empty, removes the conf file so the previously-set keys revert to
    defaults on next reboot (and immediately, where the kernel allows it).
    """
    runner = ssh_runner or _default_ssh_runner
    quoted_path = shlex.quote(conf_path)

    if not sysctl:
        cmd = f"sudo -n rm -f {quoted_path} && sudo -n sysctl --system >/dev/null"
        rc, _stdout, stderr = runner(box_ip, cmd)
        if rc != 0:
            return HostOpResult(
                ok=False,
                action="failed",
                message=_sudo_error(stderr),
                manual_fix=f"sudo rm -f {quoted_path} && sudo sysctl --system",
            )
        return HostOpResult(ok=True, action="cleared", message=f"Removed {conf_path}.")

    body = _SYSCTL_HEADER + "".join(f"{k} = {v}\n" for k, v in sorted(sysctl.items()))
    # Pipe content via stdin to `sudo -n tee` so we never expand sysctl values
    # inline in a shell command (a stray $ or backtick in a value would
    # otherwise be expanded by the remote shell).
    cmd = f"sudo -n tee {quoted_path} >/dev/null && sudo -n sysctl --system >/dev/null"
    rc, _stdout, stderr = _run_with_stdin(runner, box_ip, cmd, body)
    if rc != 0:
        return HostOpResult(
            ok=False,
            action="failed",
            message=_sudo_error(stderr),
            manual_fix=(
                f"printf '%s' {shlex.quote(body)} | sudo tee {quoted_path} "
                "&& sudo sysctl --system"
            ),
        )
    return HostOpResult(
        ok=True,
        action="applied",
        message=f"Wrote {len(sysctl)} sysctl key(s) to {conf_path} and reloaded.",
    )


def _run_with_stdin(
    runner: SshRunner,
    box_ip: str,
    cmd: str,
    stdin_data: str,
) -> Tuple[int, str, str]:
    """SSH with stdin support. The default runner can't accept stdin via the
    SshRunner protocol, so when stdin is needed we shell out directly here.
    Test runners that accept the SshRunner signature can still inject by
    encoding the stdin payload into the command they receive — but in
    practice unit tests for sysctl just inspect the command and return a
    canned (rc, out, err)."""
    if runner is _default_ssh_runner:
        from ...box_storage import get_box_user
        user = get_box_user(box_ip) or "lagerdata"
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", f"{user}@{box_ip}", cmd],
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return proc.returncode, proc.stdout, proc.stderr
    # Injected runner: encode stdin as a here-doc so the test sees the full
    # body of what gets piped in.
    encoded = f"__STDIN__<<EOF\n{stdin_data}\nEOF\n{cmd}"
    return runner(box_ip, encoded)


SUDOERS_BOOTSTRAP = (
    "Box-config apply needs passwordless sudo for a small set of commands. "
    "Run this ONCE on the box (you'll be prompted for the sudo password "
    "the one time):\n"
    "\n"
    "  echo 'lagerdata ALL=(root) NOPASSWD: /bin/mkdir, /bin/chown, "
    "/usr/bin/apt-get, /usr/sbin/sysctl, /sbin/sysctl, /usr/bin/tee, /bin/rm' \\\n"
    "    | sudo tee /etc/sudoers.d/lager-box-config\n"
    "  sudo chmod 440 /etc/sudoers.d/lager-box-config\n"
    "\n"
    "Then re-run `lager box config apply`. Each command is narrow-scoped."
)


def _sudo_error(stderr: str) -> str:
    err = (stderr or "").strip()
    base = "passwordless sudo is not configured on the box for the apply commands."
    if err and "a password is required" not in err.lower() and "sudo:" not in err.lower():
        # Real error from the underlying tool — surface it without the
        # bootstrap noise (it won't help).
        return err
    if err:
        base += f" Stderr: {err}"
    return base + "\n\n" + SUDOERS_BOOTSTRAP


def _sudo_or_apt_error(stderr: str, packages: List[str]) -> str:
    err = (stderr or "").strip()
    low = err.lower()
    if "a password is required" in low or low.startswith("sudo:"):
        return _sudo_error(stderr)
    if "unable to locate package" in low or "has no installation candidate" in low:
        # apt failed for a real reason — package name typo, missing repo, etc.
        # The bootstrap message would only confuse the user.
        return f"apt-get failed: {err}"
    return _sudo_error(stderr) if err else _sudo_error("")
