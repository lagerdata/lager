# Copyright 2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Host-OS lager CLI install, shared between `lager install` and `lager update`.

Both flows install the CLI onto the box's host OS (outside docker) so a
self-hosted CI runner on the box can invoke `lager` locally, and keep it
version-matched to the deployed box code. The install source is the box's own
checkout (`~/box/cli`, materialized via cone-mode `git sparse-checkout add
cli`), not PyPI: branch and RC deploys have no PyPI release, and installing
from the checkout guarantees the host CLI matches the deployed ref exactly.

The install target is a dedicated venv at `~/.lager/venv` with the `lager`
entry point symlinked into `~/.local/bin`. A venv sidesteps PEP 668
(`externally-managed-environment` on Ubuntu 23.04+/Debian 12+, where a plain
`pip3 install --user` fails outright), works on hosts with no pip3 at all,
and keeps the CLI's dependencies out of the system Python. Creating a venv
needs a working `ensurepip`, which on Debian/Ubuntu means the `python3-venv`
package — callers probe for it and install it through their existing
privileged-operation paths.

Pure logic: no SSH, no click. `lager update` uses these helpers directly;
`setup_and_deploy_box.sh` mirrors the install sequence in shell (it must stay
standalone-runnable). A drift-guard test in cli/tests/test_host_cli.py pins
the load-bearing literals in both scripts to this module.
"""

# Keep in sync with `python_requires` in cli/setup.py.
HOST_MIN_PYTHON = (3, 10)

_VENV = '"$HOME/.lager/venv"'
_VENV_PY = '"$HOME/.lager/venv/bin/python"'
_VENV_PIP = '"$HOME/.lager/venv/bin/pip"'
_LOCAL_BIN = '"$HOME/.local/bin"'

# Spliced into `lager update`'s one-shot box-state probe. Same contract as the
# rest of the probe script: every fact is guarded so it yields an empty value
# instead of aborting, and the snippet never changes the script's exit status.
#
# HOST_CLI_VERSION doubles as the venv health check: importing `cli` from the
# venv interpreter proves the interpreter runs (not a symlink left dangling by
# a host python upgrade) and the package is importable, and `cli/__init__.py`
# is a tiny pure-constant module so this costs milliseconds — unlike `lager
# --version`, which imports the whole command tree. Empty means "missing or
# broken, reinstall".
HOST_CLI_PROBE_SNIPPET = f'''\
echo "LAGER_PROBE_HOST_CLI_VERSION=$({_VENV_PY} -c 'import cli; print(cli.__version__)' 2>/dev/null)"
if [ -d {_VENV} ]; then echo "LAGER_PROBE_HOST_VENV_DIR=1"; else echo "LAGER_PROBE_HOST_VENV_DIR=0"; fi
echo "LAGER_PROBE_HOST_PY_VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
if python3 -Im ensurepip --version >/dev/null 2>&1; then echo "LAGER_PROBE_HOST_ENSUREPIP=1"; else echo "LAGER_PROBE_HOST_ENSUREPIP=0"; fi
if [ -d "$HOME/box/cli" ]; then echo "LAGER_PROBE_HOST_BOX_CLI_DIR=1"; else echo "LAGER_PROBE_HOST_BOX_CLI_DIR=0"; fi
'''

# apt command for the venv prerequisite. The SETENV DEBIAN_FRONTEND form
# matches the box-config sudoers grant (`_host_ops.boxcfg_sudoers_rules`), so
# provisioned boxes run it promptless.
# NEEDRESTART_SUSPEND=1 alongside DEBIAN_FRONTEND: needrestart is an apt
# post-invoke hook, and DEBIAN_FRONTEND does not reach it. Left on, it can open
# a full-screen "Pending kernel upgrade" dialog that blocks on a keypress over
# a non-tty ssh channel, and in mode=a it restarts services on its own --
# including docker, which spends a start against docker.service's
# StartLimitBurst.
HOST_VENV_APT_CMD = (
    'sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get update -qq && '
    'sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get install -y '
    '--no-install-recommends python3-venv'
)

# Distinct exit codes so callers can report what actually failed instead of
# grepping pip's stdout (the failure mode of the removed pyOCD install step,
# which lost the exit code in a `grep -q "Successfully installed"` pipeline).
HOST_CLI_EXIT_MESSAGES = {
    41: 'could not materialize ~/box/cli (git sparse-checkout add failed — git too old, or fetch blocked?)',
    42: 'venv creation failed (is the python3-venv package installed?)',
    43: 'pip install of ~/box/cli failed',
    44: 'could not symlink lager into ~/.local/bin',
}


def host_cli_install_cmd():
    """The canonical install/upgrade sequence, as one shell command for a
    single SSH call. Exit 0 on success with the installed version as the last
    stdout line; exits 41-44 map to HOST_CLI_EXIT_MESSAGES.

    Line by line: materialize `~/box/cli` if the sparse checkout predates
    this feature (`add cli` is cone-mode-legal — it's a directory — and the
    blobs lazy-fetch on blob:none clones); self-heal the venv by recreating
    it whenever its interpreter can't import pip (covers a missing venv and
    one broken by a host python upgrade); install non-editable so the host
    CLI only advances when a deploy actually runs; symlink the entry points
    (`lager-mcp` only where the deployed ref ships it, so no dangling link on
    older refs); print the installed version, which is also the verification
    that the venv interpreter and package both work.
    """
    return '\n'.join([
        'test -d "$HOME/box/cli" || git -C "$HOME/box" sparse-checkout add cli 2>/dev/null || exit 41',
        f'{_VENV_PY} -c "import pip" >/dev/null 2>&1 || {{ rm -rf {_VENV} && python3 -m venv {_VENV}; }} || exit 42',
        f'{_VENV_PIP} install --quiet "$HOME/box/cli" || exit 43',
        f'mkdir -p {_LOCAL_BIN} && ln -sfn "$HOME/.lager/venv/bin/lager" "$HOME/.local/bin/lager" || exit 44',
        f'{{ test -x "$HOME/.lager/venv/bin/lager-mcp" && ln -sfn "$HOME/.lager/venv/bin/lager-mcp" "$HOME/.local/bin/lager-mcp"; }} || true',
        f'{_VENV_PY} -c "import cli; print(cli.__version__)"',
    ])


def host_cli_failure_message(returncode):
    """Honest one-line description of a failed install command."""
    return HOST_CLI_EXIT_MESSAGES.get(
        returncode, f'install command failed (exit {returncode})'
    )


def host_python_supported(facts):
    """Whether the box host's python3 can run the CLI.

    Returns True/False from the probe's HOST_PY_VERSION fact, or None when
    the fact is absent/unparseable (no python3, or an old box probe without
    the key) — callers treat None as "skip with a warning", same as False,
    but can word the message differently.
    """
    raw = facts.get('HOST_PY_VERSION', '')
    try:
        major, minor = raw.strip().split('.', 1)
        return (int(major), int(minor)) >= HOST_MIN_PYTHON
    except (ValueError, AttributeError):
        return None


def host_cli_reconcile_action(host_version, box_version, *, code_changed):
    """Decide whether a run needs to (re)install the host CLI.

    Returns 'install', 'skip', or 'skip-unknown'.

    On the rebuild path (`code_changed=True`) the answer is always 'install':
    the deployed ref changed this run, and branch deploys can change code
    without bumping `__version__`, so a version compare proves nothing.

    On the fast path (no pull happened) matching versions genuinely mean
    matching code, so compare. An empty host version means missing-or-broken
    (the probe couldn't import `cli` from the venv) — install. An unreadable
    box version with a present host CLI fails open ('skip-unknown') rather
    than manufacturing churn, the same philosophy as _deployed_version_stale.
    """
    if code_changed:
        return 'install'
    if not host_version:
        return 'install'
    if not box_version:
        return 'skip-unknown'
    return 'skip' if host_version == box_version else 'install'


def host_cli_check_status(facts, tree_version, code_in_sync):
    """`lager update --check` line for the host CLI: (label, will_change).

    will_change must stay False for states an update cannot fix (unsupported
    host python, unreadable box version) — reporting "would change" forever
    on those would wedge --check's exit-code contract for CI.
    """
    supported = host_python_supported(facts)
    if supported is False:
        raw = facts.get('HOST_PY_VERSION', '').strip()
        return (
            f'skipped (host python3 {raw} < '
            f'{HOST_MIN_PYTHON[0]}.{HOST_MIN_PYTHON[1]})',
            False,
        )
    if supported is None:
        return ('skipped (host python3 version unknown)', False)

    host_version = facts.get('HOST_CLI_VERSION', '').strip()
    if not code_in_sync:
        # The update will pull new code and always reinstalls afterwards.
        return ('will install/upgrade (update will run)', True)
    if not host_version:
        if facts.get('HOST_VENV_DIR') == '1':
            return ('will reinstall (venv broken)', True)
        return ('will install (not present)', True)
    if not tree_version:
        return ('unknown (box version unreadable)', False)
    if host_version == tree_version:
        return (f'in sync ({host_version})', False)
    return (f'will upgrade ({host_version} -> {tree_version})', True)
