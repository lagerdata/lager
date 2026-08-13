# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Host-side SSH helpers for `lager box-config apply`:
  - apt_install: `sudo apt-get install -y` over SSH
  - sysctl_apply: write /etc/sysctl.d/99-lager-box-config.conf + sysctl --system

Both use the same passwordless-sudo + BatchMode SSH pattern as
`_mount_prep.ensure_host_path_owned`. When sudo isn't configured, callers
get back a structured failure with a copy-pasteable bootstrap fix.

Pure logic; the SSH transport is injected so unit tests can drive every
branch without a real box.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Dict, List, Optional

from ._ssh import SshRunner, default_ssh_runner, sudo_error_message

SYSCTL_CONF_PATH = "/etc/sysctl.d/99-lager-box-config.conf"
_SYSCTL_HEADER = (
    "# Managed by `lager box-config sysctl`; manual edits are overwritten on apply.\n"
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
    "# Managed by `lager box-config udev`; manual edits are overwritten on apply.\n"
)

# --- Sudoers ownership contract ---------------------------------------------
#
# Lager writes exactly three files under /etc/sudoers.d/ and never reads,
# edits, or removes any other file in that directory:
#
#   lagerdata-udev    cli/deployment/scripts/setup_and_deploy_box.sh
#                     (`lager install` only)
#   lager-box-config  boxcfg_sudoers_bootstrap_cmd() below
#                     (`lager install` and `lager update`)
#   lager-bench-json  the operator-pasted snippet in cli/commands/box/dut.py
#                     (fallback for boxes predating the bench.json grants in
#                     lagerdata-udev)
#
# Every write is a `tee` to one of those fixed paths, guarded by `visudo -c`.
# There are no globs, no directory recreation, no renames; the only removal is
# `lager uninstall`'s `rm -f` naming those same three paths. Operator- and
# platform-owned files in /etc/sudoers.d/ are safe, by construction.
#
# What is NOT safe is a grant added *inside* one of the three: the files are
# regenerated wholesale on every run, which is deliberate (a box always ends
# up with the current grant shape) but silently discards anything an operator
# put there. Nothing on the box said so, which is what sudoers_banner_lines()
# below fixes — every writer emits the banner, so the warning is where the
# operator is. test/unit/box/test_sudoers_contract.py pins both the banner and
# the three-path allowlist.
#
# The banner text must survive two hostile quoting contexts, so it contains no
# single quotes and no backticks: boxcfg_sudoers_bootstrap_cmd() wraps each
# line in shell single quotes, and setup_and_deploy_box.sh emits its copy from
# an *unquoted* heredoc where backticks would run as command substitutions.
# Command names therefore appear bare (`lager install`, not quoted).

# The example filename sorts after every file Lager owns (lager-bench-json,
# lager-box-config, lagerdata-udev). sudo reads /etc/sudoers.d/ in lexical
# order and the LAST matching rule wins, so a grant there is never narrowed by
# one of Lager's — which is the point of sending operators to it. A dot in the
# name would make sudo skip the file entirely, so the example has none.
_SUDOERS_CONTRACT_LINES = (
    "# Lager writes only the files it owns under /etc/sudoers.d/ and never touches",
    "# any other file in this directory, so keep operator or platform grants in a",
    "# SEPARATE file (for example /etc/sudoers.d/zz-local); those survive every",
    "# Lager run.",
)


def sudoers_banner_lines(managed_by: str) -> List[str]:
    """Comment header for a Lager-managed /etc/sudoers.d/ file. `managed_by`
    is the file-specific lifecycle sentence (which command rewrites or removes
    it); the shared contract paragraph follows it unchanged.

    Returns bare lines with no trailing newlines — callers join them the way
    their transport needs (printf args, heredoc body, pasted snippet)."""
    if "'" in managed_by or "`" in managed_by:
        # Would break the shell quoting at one of the two emit sites; see the
        # contract comment above.
        raise ValueError(f"sudoers banner text must not be quoted: {managed_by!r}")
    return [f"# {managed_by}", *_SUDOERS_CONTRACT_LINES]


# --- Box-config sudoers rule: single source of truth ------------------------
#
# `lager install` and `lager update` write this rule to BOXCFG_SUDOERS_PATH
# so `lager box-config apply` can run apt-get/sysctl/mkdir/chown over
# BatchMode SSH. The rule must name the actual login user: it used to
# hardcode `lagerdata`, so on boxes with a different login user the grant
# never matched — install ended with "Sudoers file installed
# but `sudo -n apt-get` still fails" and every apply needed manual setup.
#
# The username lands inside a root-owned sudoers file, so callers must gate
# interpolation on is_valid_unix_username().
#
# The marker gates rewrites: `lager update`'s probe skips the bootstrap
# entirely when the marker is present and `sudo -n apt-get` works, so any
# change to what this file contains reaches an already-provisioned box only if
# the marker name changes too.
#
# **Deliberately still v2 after the ownership banner was added.** Bumping it
# would rewrite the file on every existing box, and that rewrite needs an
# interactive sudo password — `sudo tee` into /etc/sudoers.d/ is not covered
# by any NOPASSWD grant Lager installs. Charging the whole fleet a password
# prompt to deliver five comment lines is a bad trade, and on a box updated by
# a script with no tty the bootstrap cannot succeed at all, so it would fail
# and warn on every subsequent run. Existing boxes therefore keep the
# banner-less file until they are reinstalled; both generations grant exactly
# the same commands, so nothing depends on which one a box has.
#
# Bump it for a change to the RULE LINES, where a stale generation is a real
# defect. Say so in the changelog when you do — see the note in update.py's
# box-config sudoers step for what a bump costs.
#
# Import the constant rather than writing the path out. install/update used to
# hardcode it in their probes, which meant a bump would have moved the file
# Lager wrote without moving the file it checked for — the rewrite would have
# been skipped everywhere, forever. test_sudoers_contract.py fails on any new
# hardcoded copy.

BOXCFG_SUDOERS_PATH = "/etc/sudoers.d/lager-box-config"
BOXCFG_SUDOERS_MARKER = "/etc/lager/.boxcfg-sudoers-v2"
BOXCFG_SUDOERS_BANNER = sudoers_banner_lines(
    "Managed by lager install and lager update; manual edits are overwritten."
)

# The udev grants have no marker and no update-path writer: only
# setup_and_deploy_box.sh (`lager install`) writes /etc/sudoers.d/lagerdata-udev,
# so this banner reaches an existing box only when someone re-installs it. The
# lifecycle line says `lager install` alone rather than claiming an update path
# that does not exist — `lager update` only chowns the file to root:root.
# The heredoc in setup_and_deploy_box.sh carries a byte-identical copy (it runs
# from shell, with no access to this module); test_sudoers_contract.py pins the
# two together.
UDEV_SUDOERS_BANNER = sudoers_banner_lines(
    "Managed by lager install; manual edits are overwritten."
)

# useradd's default charset plus uppercase and dots (both appear in real
# deployments and are harmless in sudoers). Every allowed character is inert
# inside the single-quoted rule strings and sudoers syntax; anything else
# (spaces, quotes, $(), newlines, ...) is refused.
_UNIX_USERNAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\$?")


def is_valid_unix_username(user: Optional[str]) -> bool:
    """True for plain unix usernames — the gate for interpolating a name
    into sudoers content."""
    return bool(user) and _UNIX_USERNAME_RE.fullmatch(user) is not None


def boxcfg_sudoers_rules(user: str = "lagerdata") -> List[str]:
    """The NOPASSWD rule lines for `lager box-config apply`.

    THE BOX LOGIN USER IS ROOT-EQUIVALENT BY DESIGN. These grants are a
    convenience — they keep provisioning from prompting for a password over
    BatchMode SSH — not a privilege boundary, and they cannot be made into
    one by narrowing individual entries. Three independent paths to root
    exist in what Lager must grant to provision a box at all:

      - `apt-get` runs arbitrary commands as root via its own config
        (`-o APT::Update::Pre-Invoke::=...`), and SETENV widens that further.
        SETENV is required so DEBIAN_FRONTEND=noninteractive propagates and
        package postinst scripts (iptables-persistent, etc.) don't prompt.
      - the lagerdata-udev grants pair `cp /tmp/*.rules /etc/udev/rules.d/`
        with `udevadm control --reload-rules`; udev rules run commands as
        root via `RUN+=`, so deploying them IS root by construction.
      - the same file lets the login user `install` a script from
        world-writable /tmp to a root-owned path and then execute it.

    Path-scoping tee/rm/sysctl (and any future scoping of mkdir/chown) is
    blast-radius hygiene: it keeps a mistake from spreading, and makes an
    audit of this file shorter. It is NOT containment, and describing it that
    way is worse than saying nothing — a reader who sees a tidy list of
    specific commands concludes the account is confined when it is not. This
    docstring used to make exactly that claim.

    Treat the login account as equivalent to root when deciding who may hold
    its SSH key. Confining it would mean replacing the three paths above with
    fixed root-owned wrapper scripts the login user cannot edit; that is a
    real project, not a rule edit.

    Raises ValueError on a non-plain username: callers validate first, so
    this is a backstop that makes a future caller that forgets fail loudly
    instead of interpolating into root-owned sudoers content."""
    if not is_valid_unix_username(user):
        raise ValueError(f"invalid unix username for sudoers rule: {user!r}")
    return [
        f"{user} ALL=(root) NOPASSWD: SETENV: /usr/bin/apt-get",
        f"{user} ALL=(root) NOPASSWD: /bin/mkdir, /bin/chown, "
        "/usr/sbin/sysctl --system, /sbin/sysctl --system, "
        f"/usr/bin/tee {SYSCTL_CONF_PATH}, "
        f"/bin/rm -f {SYSCTL_CONF_PATH}, "
        "/bin/cp /etc/lager/box_config.applied.json /etc/lager/box_config.json",
    ]


def boxcfg_sudoers_bootstrap_cmd(user: str = "lagerdata") -> str:
    """One shell command that installs the box-config sudoers rule plus the
    versioned marker file. Used verbatim by `lager install` and
    `lager update`; the manual snippet in sudoers_bootstrap() mirrors it.

    The banner lines are `#` comments, so they are inert to sudoers and to
    `visudo -c` — they exist to tell whoever opens the file on the box that
    Lager rewrites it (see the ownership-contract comment above)."""
    lines = BOXCFG_SUDOERS_BANNER + boxcfg_sudoers_rules(user)
    quoted_rules = " ".join(f"'{r}'" for r in lines)
    return (
        f"printf '%s\\n' {quoted_rules} "
        f"| sudo tee {BOXCFG_SUDOERS_PATH} >/dev/null "
        f"&& sudo chmod 440 {BOXCFG_SUDOERS_PATH} "
        f"&& sudo touch {BOXCFG_SUDOERS_MARKER} "
        f"&& sudo chmod 644 {BOXCFG_SUDOERS_MARKER}"
    )


def udev_sudoers_bootstrap(user: str = "lagerdata") -> str:
    """Manual-fix text for a box missing the udev sudoers grant."""
    # Error-path text renderer: must never raise (it runs while composing a
    # failure message). The user comes from local box storage unvalidated
    # (resolve_box_user), so a non-plain name falls back to the historical
    # default rather than being interpolated into a paste-into-root-shell
    # snippet; the operator substitutes their real user.
    if not is_valid_unix_username(user):
        user = "lagerdata"
    banner = "".join(f"  {line}\n" for line in UDEV_SUDOERS_BANNER)
    return (
        "Applying user udev rules needs the passwordless-sudo udev grant that the "
        "box setup script installs. If it's missing (older box), re-run the box "
        "setup/deploy, or add it ONCE on the box:\n"
        "\n"
        "  sudo tee /etc/sudoers.d/lagerdata-udev >/dev/null <<'SUDOERS'\n"
        + banner
        + f"  {user} ALL=(ALL) NOPASSWD: /bin/cp /tmp/*.rules /etc/udev/rules.d/\n"
        f"  {user} ALL=(ALL) NOPASSWD: /bin/chmod 644 /etc/udev/rules.d/*.rules\n"
        f"  {user} ALL=(ALL) NOPASSWD: /usr/bin/udevadm control --reload-rules\n"
        f"  {user} ALL=(ALL) NOPASSWD: /usr/bin/udevadm trigger\n"
        "  SUDOERS\n"
        "  sudo chmod 440 /etc/sudoers.d/lagerdata-udev\n"
        "\n"
        "Then re-run `lager box-config apply`."
    )


def sudoers_bootstrap(user: str = "lagerdata") -> str:
    """Manual-fix text for a box missing the box-config sudoers grant."""
    # Same never-raise rule as udev_sudoers_bootstrap: fall back before
    # boxcfg_sudoers_rules so its ValueError backstop can't fire mid-error.
    if not is_valid_unix_username(user):
        user = "lagerdata"
    # Same line list as boxcfg_sudoers_bootstrap_cmd(), banner included, so a
    # hand-pasted file is byte-identical to the one install/update write —
    # otherwise the operator ends up with the grants but not the warning.
    lines = BOXCFG_SUDOERS_BANNER + boxcfg_sudoers_rules(user)
    printf_args = "".join(f"    '{line}' \\\n" for line in lines)
    return (
        "Box-config apply needs passwordless sudo for a small set of commands. "
        "Run this ONCE on the box (you'll be prompted for the sudo password "
        "the one time):\n"
        "\n"
        "  printf '%s\\n' \\\n"
        + printf_args
        + f"    | sudo tee {BOXCFG_SUDOERS_PATH} >/dev/null\n"
        f"  sudo chmod 440 {BOXCFG_SUDOERS_PATH}\n"
        f"  sudo touch {BOXCFG_SUDOERS_MARKER} && sudo chmod 644 {BOXCFG_SUDOERS_MARKER}\n"
        "\n"
        "Then re-run `lager box-config apply`.\n"
        "\n"
        f"These grants make the {user} account root-equivalent, by design: "
        "`apt-get` can run arbitrary commands as root through its own config, "
        "and SETENV on it is required so `DEBIAN_FRONTEND=noninteractive` "
        "propagates and package postinst scripts (iptables-persistent, etc.) "
        "don't prompt. Path-scoping on tee/rm/sysctl limits blast radius; it "
        "is not a privilege boundary. Provisioning a box needs root, so treat "
        f"anyone holding the {user} SSH key as holding root on this box."
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
    user: str = "lagerdata",
) -> HostOpResult:
    """Install the given apt packages on the box host. Idempotent — apt
    skips packages that are already at the requested version. `user` only
    names the login user in the manual-fix bootstrap text on failure."""
    if not packages:
        return HostOpResult(ok=True, action="noop", message="No apt packages configured.")
    runner = ssh_runner or default_ssh_runner
    quoted_pkgs = " ".join(shlex.quote(p) for p in packages)
    cmd = (
        "sudo -n apt-get update -qq && "
        f"sudo -n DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get install -y --no-install-recommends {quoted_pkgs}"
    )
    rc, _stdout, stderr = runner(box_ip, cmd)
    if rc != 0:
        return HostOpResult(
            ok=False,
            action="failed",
            message=_sudo_or_apt_error(stderr, user),
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
    user: str = "lagerdata",
) -> HostOpResult:
    """Write the sysctl conf and run `sysctl --system`. When `sysctl` is
    empty, removes the conf file so the previously-set keys revert to
    defaults on next reboot (and immediately, where the kernel allows it).
    `user` only names the login user in the bootstrap text on failure.
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
                message=sudo_error_message(stderr, base_text=_SUDO_BASE_TEXT, bootstrap_text=sudoers_bootstrap(user)),
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
            message=sudo_error_message(stderr, base_text=_SUDO_BASE_TEXT, bootstrap_text=sudoers_bootstrap(user)),
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
        mode = str(r.get("mode", "0660"))
        body += f"# vid:pid {vid}:{pid} (added via `lager box-config udev`)\n"
        body += (
            f'SUBSYSTEM=="usb", ATTRS{{idVendor}}=="{vid}", '
            f'ATTRS{{idProduct}}=="{pid}", MODE="{mode}", GROUP="lager"\n'
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
    user: str = "lagerdata",
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
                stderr, base_text=_SUDO_BASE_TEXT, bootstrap_text=udev_sudoers_bootstrap(user)
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


def _sudo_or_apt_error(stderr: str, user: str = "lagerdata") -> str:
    err = (stderr or "").strip()
    low = err.lower()
    if "a password is required" in low or low.startswith("sudo:"):
        return sudo_error_message(stderr, base_text=_SUDO_BASE_TEXT, bootstrap_text=sudoers_bootstrap(user))
    if "unable to locate package" in low or "has no installation candidate" in low:
        # apt failed for a real reason — package name typo, missing repo, etc.
        # The bootstrap message would only confuse the user.
        return f"apt-get failed: {err}"
    return sudo_error_message(stderr, base_text=_SUDO_BASE_TEXT, bootstrap_text=sudoers_bootstrap(user))
