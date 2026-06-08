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

# User udev rules live in their own file so they never collide with the
# shipped 99-instrument.rules. The filename matches the `99-*.rules` glob the
# box's `/etc/sudoers.d/lagerdata-udev` NOPASSWD entries already allow, so no
# new sudo grant is needed (provisioned by setup_and_deploy_box.sh).
UDEV_RULES_FILENAME = "99-lager-user.rules"
UDEV_RULES_DIR = "/etc/udev/rules.d/"
UDEV_RULES_PATH = UDEV_RULES_DIR + UDEV_RULES_FILENAME
_UDEV_TMP_PATH = "/tmp/" + UDEV_RULES_FILENAME
_UDEV_HEADER = (
    "# Managed by `lager box config udev`; manual edits are overwritten on apply.\n"
)

UDEV_SUDOERS_BOOTSTRAP = (
    "Applying user udev rules needs the passwordless-sudo udev grant that the "
    "box setup script installs. If it's missing (older box), re-run the box "
    "setup/deploy, or add it ONCE on the box:\n"
    "\n"
    "  sudo tee /etc/sudoers.d/lagerdata-udev >/dev/null <<'SUDOERS'\n"
    "  lagerdata ALL=(ALL) NOPASSWD: /bin/cp /tmp/*.rules /etc/udev/rules.d/\n"
    "  lagerdata ALL=(ALL) NOPASSWD: /bin/chmod 644 /etc/udev/rules.d/*.rules\n"
    "  lagerdata ALL=(ALL) NOPASSWD: /usr/bin/udevadm control --reload-rules\n"
    "  lagerdata ALL=(ALL) NOPASSWD: /usr/bin/udevadm trigger\n"
    "  SUDOERS\n"
    "  sudo chmod 440 /etc/sudoers.d/lagerdata-udev\n"
    "\n"
    "Then re-run `lager box config apply`."
)

SUDOERS_BOOTSTRAP = (
    "Box-config apply needs passwordless sudo for a small set of commands. "
    "Run this ONCE on the box (you'll be prompted for the sudo password "
    "the one time):\n"
    "\n"
    "  printf '%s\\n' \\\n"
    "    'lagerdata ALL=(root) NOPASSWD: SETENV: /usr/bin/apt-get' \\\n"
    "    'lagerdata ALL=(root) NOPASSWD: /bin/mkdir, /bin/chown, "
    "/usr/sbin/sysctl --system, /sbin/sysctl --system, "
    "/usr/bin/tee /etc/sysctl.d/99-lager-box-config.conf, "
    "/bin/rm -f /etc/sysctl.d/99-lager-box-config.conf, "
    "/bin/cp /etc/lager/box_config.applied.json /etc/lager/box_config.json' \\\n"
    "    | sudo tee /etc/sudoers.d/lager-box-config >/dev/null\n"
    "  sudo chmod 440 /etc/sudoers.d/lager-box-config\n"
    "  sudo touch /etc/lager/.boxcfg-sudoers-v2 && sudo chmod 644 /etc/lager/.boxcfg-sudoers-v2\n"
    "\n"
    "Then re-run `lager box config apply`. tee/rm/sysctl are path-scoped so a "
    "compromised lagerdata account cannot escalate to root via them; apt-get "
    "and mkdir/chown are unscoped because the package list and host paths are "
    "user-defined. SETENV on apt-get is required so "
    "`DEBIAN_FRONTEND=noninteractive` propagates and package postinst scripts "
    "(iptables-persistent, etc.) don't prompt."
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


def render_udev_rules_file(rules: List[Dict[str, object]]) -> str:
    """Render the .rules file body from a list of rule dicts (as stored in
    box_config.json's `udev_rules`). Kept in sync with
    box/lager/box_config/config.py::UdevRule.to_rule_lines — the box module
    isn't importable host-side, so the rendering is intentionally duplicated
    here and pinned by a unit test."""
    body = _UDEV_HEADER
    for r in rules:
        vid = str(r.get("vid", ""))
        pid = str(r.get("pid", ""))
        mode = str(r.get("mode", "0666"))
        body += f"# vid:pid {vid}:{pid} (added via `lager box config udev`)\n"
        body += (
            f'SUBSYSTEM=="usb", ATTRS{{idVendor}}=="{vid}", '
            f'ATTRS{{idProduct}}=="{pid}", MODE="{mode}"\n'
        )
        if bool(r.get("usbtmc", False)):
            body += (
                f'ACTION=="bind", SUBSYSTEM=="usb", DRIVER=="usbtmc", '
                f'ATTRS{{idVendor}}=="{vid}", ATTRS{{idProduct}}=="{pid}", '
                f"RUN+=\"/bin/sh -c 'echo %k > /sys/bus/usb/drivers/usbtmc/unbind "
                f"2>/dev/null || true'\"\n"
            )
    return body


def udev_apply(
    box_ip: str,
    rules: List[Dict[str, object]],
    *,
    ssh_runner: Optional[SshRunner] = None,
) -> HostOpResult:
    """Install the user udev rules file on the box host and reload udev.

    The rendered file is written to /tmp (no sudo — /tmp is user-writable),
    then copied into /etc/udev/rules.d via the pre-provisioned passwordless
    sudo grant, and udev is reloaded + retriggered so existing device nodes
    pick up the new MODE. When `rules` is empty we still write a header-only
    file and retrigger, which reverts previously-granted device permissions
    (no `rm` permission is needed this way).
    """
    runner = ssh_runner or default_ssh_runner
    body = render_udev_rules_file(rules)
    quoted_tmp = shlex.quote(_UDEV_TMP_PATH)
    quoted_path = shlex.quote(UDEV_RULES_PATH)
    # tee writes our stdin to /tmp; the rest run under the udev sudoers grant.
    # Absolute binary paths so they match the NOPASSWD command specs exactly.
    cmd = (
        f"tee {quoted_tmp} >/dev/null && "
        f"sudo -n /bin/cp {quoted_tmp} {shlex.quote(UDEV_RULES_DIR)} && "
        f"sudo -n /bin/chmod 644 {quoted_path} && "
        f"sudo -n /usr/bin/udevadm control --reload-rules && "
        f"sudo -n /usr/bin/udevadm trigger"
    )
    rc, _stdout, stderr = runner(box_ip, cmd, stdin=body)
    if rc != 0:
        return HostOpResult(
            ok=False,
            action="failed",
            message=sudo_error_message(
                stderr, base_text=_SUDO_BASE_TEXT, bootstrap_text=UDEV_SUDOERS_BOOTSTRAP
            ),
            manual_fix=(
                f"printf '%s' {shlex.quote(body)} | sudo tee {quoted_path} >/dev/null "
                f"&& sudo chmod 644 {quoted_path} "
                "&& sudo udevadm control --reload-rules && sudo udevadm trigger"
            ),
        )
    if not rules:
        return HostOpResult(ok=True, action="cleared", message=f"Cleared {UDEV_RULES_PATH}.")
    return HostOpResult(
        ok=True,
        action="applied",
        message=(
            f"Installed {len(rules)} udev rule(s) to {UDEV_RULES_PATH} and reloaded udev."
        ),
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
