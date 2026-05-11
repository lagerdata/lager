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
from dataclasses import dataclass
from typing import Dict, List, Optional

from ._ssh import SshRunner, default_ssh_runner, sudo_error_message

SYSCTL_CONF_PATH = "/etc/sysctl.d/99-lager-box-config.conf"
_SYSCTL_HEADER = (
    "# Managed by `lager box config sysctl`; manual edits are overwritten on apply.\n"
)

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

_SUDO_BASE_TEXT = "passwordless sudo is not configured on the box for the apply commands."


@dataclass
class HostOpResult:
    ok: bool
    action: str
    message: str = ""
    manual_fix: Optional[str] = None


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
    runner = ssh_runner or default_ssh_runner
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
            message=_sudo_or_apt_error(stderr),
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
    runner = ssh_runner or default_ssh_runner
    quoted_path = shlex.quote(conf_path)

    if not sysctl:
        cmd = f"sudo -n rm -f {quoted_path} && sudo -n sysctl --system >/dev/null"
        rc, _stdout, stderr = runner(box_ip, cmd)
        if rc != 0:
            return HostOpResult(
                ok=False,
                action="failed",
                message=sudo_error_message(stderr, base_text=_SUDO_BASE_TEXT, bootstrap_text=SUDOERS_BOOTSTRAP),
                manual_fix=f"sudo rm -f {quoted_path} && sudo sysctl --system",
            )
        return HostOpResult(ok=True, action="cleared", message=f"Removed {conf_path}.")

    body = _SYSCTL_HEADER + "".join(f"{k} = {v}\n" for k, v in sorted(sysctl.items()))
    # Pipe content via stdin to `sudo -n tee` so we never expand sysctl values
    # inline in a shell command (a stray $ or backtick in a value would
    # otherwise be expanded by the remote shell).
    cmd = f"sudo -n tee {quoted_path} >/dev/null && sudo -n sysctl --system >/dev/null"
    rc, _stdout, stderr = runner(box_ip, cmd, stdin=body)
    if rc != 0:
        return HostOpResult(
            ok=False,
            action="failed",
            message=sudo_error_message(stderr, base_text=_SUDO_BASE_TEXT, bootstrap_text=SUDOERS_BOOTSTRAP),
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


def _sudo_or_apt_error(stderr: str) -> str:
    err = (stderr or "").strip()
    low = err.lower()
    if "a password is required" in low or low.startswith("sudo:"):
        return sudo_error_message(stderr, base_text=_SUDO_BASE_TEXT, bootstrap_text=SUDOERS_BOOTSTRAP)
    if "unable to locate package" in low or "has no installation candidate" in low:
        # apt failed for a real reason — package name typo, missing repo, etc.
        # The bootstrap message would only confuse the user.
        return f"apt-get failed: {err}"
    return sudo_error_message(stderr, base_text=_SUDO_BASE_TEXT, bootstrap_text=SUDOERS_BOOTSTRAP)
