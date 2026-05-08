# `box config` cleanup tasks

Independent refactors for the `feature/box-config` branch. Each task is
self-contained, mergeable on its own, and verifiable with the existing
test suite. Pick any order.

**Touched files (read first):**

- `cli/commands/box/config.py` (1220 lines — the main offender)
- `cli/commands/box/_host_ops.py`
- `cli/commands/box/_mount_prep.py`
- `cli/commands/box/_pip_validation.py`
- `box/lager/box_config/box_config_cli.py` (in-container shim, 482 lines)
- `box/lager/box_config/config.py` (canonical schema + validators)

**Test entry points:**

```
pytest test/unit/box/test_box_config.py
pytest test/unit/box/test_box_config_cli.py
pytest test/unit/box/test_host_ops.py
pytest test/unit/box/test_mount_prep.py
```

Tests load box-side modules via `importlib.util` rather than imports —
package layout matters. Don't try to `from lager.box_config import ...`
from the cli/ side; the two are separate distributions.

---

## Task A — Consolidate the SSH runner

**Why.** `_default_ssh_runner` is reimplemented in `_host_ops.py` and
`_mount_prep.py` with the same `BatchMode=yes` + `get_box_user(...) or
"lagerdata"` pattern, and `_bounce_container` in `config.py` shells out
to ssh inline a third time. One bug fix has to land in three places.

**Do.**

1. Add `cli/commands/box/_ssh.py` exporting:
   - `SshRunner` type alias
   - `default_ssh_runner(box_ip, cmd, *, timeout=60, stdin=None)`
   - The `SUDOERS_BOOTSTRAP` text and `_sudo_error_message` helper
     currently duplicated between `_host_ops.py` and `_mount_prep.py`.
2. Replace the duplicate `_default_ssh_runner` definitions in
   `_host_ops.py` and `_mount_prep.py` with imports from `_ssh`.
3. Replace the inline `subprocess.run(["ssh", ...])` in `_bounce_container`
   in `config.py` with a call to the shared helper.
4. Drop the `_run_with_stdin` workaround in `_host_ops.py` once
   `default_ssh_runner` accepts `stdin=`.

**Don't change.** Public signatures of `apt_install`, `sysctl_apply`,
`ensure_host_path_owned`. Tests inject `ssh_runner=` and that contract
must keep working.

**Verify.**

```
pytest test/unit/box/test_host_ops.py test/unit/box/test_mount_prep.py
```

---

## Task B — Delete redundant host-side validators

**Why.** `cli/commands/box/_pip_validation.validate_format` literally has
the comment "Mirror of box-side validate_pip_format. Kept in sync
intentionally." The shim already validates everything inside
`_cmd_*_add` — the host-side check just saves one SSH round trip on
typos. The maintenance cost (silent drift between the two regexes) is
not worth that.

**Do.**

1. Drop `validate_format` from `cli/commands/box/_pip_validation.py`.
   Keep `normalize_for_pypi`, `is_direct_ref`, and `validate_on_pypi`
   (PyPI lookup is genuinely host-only).
2. Drop the inline regex/validator pairs from `cli/commands/box/config.py`:
   - `_APT_NAME_RE` + `_validate_apt_name_host`
   - `_SYSCTL_KEY_RE_HOST` + `_validate_sysctl_key_host`
   - `_CARGO_SPEC_RE_HOST` + `_validate_cargo_spec_host`
3. Remove their callers in `apt_add_cmd`, `sysctl_set_cmd`,
   `cargo_add_cmd`, `pip_add_cmd`. The shim returns the same error
   shape, just one round-trip later.
4. Remove the `import re as _re` workaround at the top of the apt
   section now that there's no host-side regex.

**Don't change.** `validate_on_pypi` and the format check on `key=value`
sysctl input parsing (`if "=" not in entry`). Both are host-side
ergonomics, not duplicates.

**Verify.**

```
pytest test/unit/box/test_box_config_cli.py
```

If a test pins to the exact error string from the deleted host-side
validator, update it to match the shim's error string.

---

## Task C — Shim protocol constants

**Why.** `"mount-add"`, `"pip-add"`, `"sysctl-set"` etc. are string
literals on both sides of the shim boundary. A typo on the host side
fails as a generic `unknown command: ...` from the shim — easy to ship,
hard to spot.

**Do.**

1. Add a module of constants. Two reasonable homes:
   - `cli/commands/box/_shim_verbs.py` (host-only; the box side keeps
     literals). Lower friction.
   - `box/lager/box_config/protocol.py` imported by the shim, and a
     parallel constants file on the cli side that mirrors it. Higher
     fidelity, more code.
   Pick (a) unless you're also doing Task D/E in the same session.
2. Replace string literals in `cli/commands/box/config.py`:
   `"show"`, `"validate"`, `"init"`, `"hash"`, `"applied-hash"`,
   `"set-applied-hash"`, `"restore-applied"`, `"applied-show"`,
   `"mount-add"`, `"mount-remove"`, `"volume-add"`, `"volume-remove"`,
   `"pip-add"`, `"pip-remove"`, `"pip-import-legacy"`, `"apt-add"`,
   `"apt-remove"`, `"sysctl-set"`, `"sysctl-unset"`, `"cargo-add"`,
   `"cargo-remove"`.

**Verify.** Tests should still pass unchanged.

```
pytest test/unit/box/test_box_config_cli.py
```

---

## Task D — Collapse list commands

**Why.** Six `*_list_cmd` functions in `cli/commands/box/config.py`
(`mount_list_cmd`, `volume_list_cmd`, `pip_list_cmd`, `apt_list_cmd`,
`sysctl_list_cmd`, `cargo_list_cmd`) all do:

1. Resolve box.
2. Run shim `show`.
3. Pluck a single key from the response.
4. If `--json`, dump JSON; else iterate and print.

Plus an "empty" message that's the same shape every time.

**Do.**

1. In `cli/commands/box/config.py`, add a helper near the top:

   ```python
   def _list_field(
       ctx: click.Context,
       box: Optional[str],
       *,
       key: str,
       empty_msg: str,
       formatter,            # callable taking one entry, returning str
       as_json: bool,
   ) -> None:
   ```

   It does the resolve + show + pluck + print loop.
2. Rewrite each `*_list_cmd` body to one line calling `_list_field`
   with the right `key`, `empty_msg`, and `formatter`.
3. Mounts and volumes are dicts (`{"host": ..., "container": ..., ...}`),
   so their formatters return `f"{host} -> {container}{ro}"`. Pip/apt/
   cargo are bare strings — formatter is just `str`. Sysctl is a dict
   keyed by key — special-case it or have the helper accept either
   sequence or mapping.

**Don't change.** Output format. The CLI is shipped — humans grep its
output. Diff the help text and the `--json` path before/after.

**Verify.**

```
pytest test/unit/box/test_box_config_cli.py -k list
```

---

## Task E — `_render_human` driver table

**Why.** `_render_human` in `cli/commands/box/config.py` (~lines 145-210)
has seven near-identical sections per first-class field. Adding the
next field (e.g. `cargo_packages` was the most recent) requires
appending another copy-pasted block.

**Do.**

1. Define a registry near the top of the module:

   ```python
   _FIRST_CLASS_FIELDS = [
       ("mounts",         "Mounts",              _format_mount),
       ("volumes",        "Volumes",             _format_volume),
       ("env",            "Env",                 _format_env_pair),
       ("pip_packages",   "Pip packages",        str),
       ("apt_packages",   "Apt packages (host)", str),
       ("sysctl",         "Sysctl",              _format_sysctl_pair),
       ("cargo_packages", "Cargo packages",      str),
   ]
   ```

   where `_format_mount`, `_format_volume`, `_format_env_pair`,
   `_format_sysctl_pair` are tiny helpers extracted from the existing
   bodies.
2. Rewrite `_render_human` to iterate the registry and render each
   block with one shared print loop.
3. Derive `_FIRST_CLASS_KEYS` from `_FIRST_CLASS_FIELDS` so the
   "Extras" section can never drift.

**Don't change.** The human output. Snapshot the current
`lager box config show` output (or read tests that pin it) and confirm
identical output after.

**Verify.**

```
pytest test/unit/box/test_box_config_cli.py -k show
```

---

## Task F — Shim dispatch table

**Why.** `_cli()` in `box/lager/box_config/box_config_cli.py` is a
60-line if/elif chain (`if cmd == "show": ... elif cmd == "validate":
... elif cmd == "init": ...`). New verbs require touching this and the
host-side caller; a missing branch silently fails as `unknown command`.

**Do.**

1. Replace the if/elif with a dispatch dict:

   ```python
   _DISPATCH = {
       "show":              lambda args: _cmd_show(),
       "validate":          lambda args: _cmd_validate(),
       "init":              lambda args: _cmd_init("--force" in args),
       "hash":              lambda args: _cmd_hash(),
       "applied-hash":      lambda args: _cmd_applied_hash(),
       "applied-show":      lambda args: _cmd_applied_show(),
       "restore-applied":   lambda args: _cmd_restore_applied(),
       "set-applied-hash":  lambda args: _cmd_set_applied_hash(_require(args, 1)),
       "mount-add":         lambda args: _cmd_mount_add(_require(args, 1)),
       # ... etc.
   }
   ```

2. Add a small `_require(args, n)` helper that raises `ValueError` with
   a clear message when the verb is missing positional args (replaces
   the per-branch `if len(args) < N: raise ValueError(...)`).
3. The "unknown command" branch becomes a single dict miss.

**Don't change.** Stdout JSON shape, exception handling around the
dispatcher (the `except cfg.ValidationError`, `SystemExit`, `Exception`
blocks stay).

**Verify.**

```
pytest test/unit/box/test_box_config_cli.py
```

---

## Task G — Lint pass (small wins, quick to ship)

**Why.** Stragglers worth fixing while you're already in the file.

**Do.**

1. Hoist the inline imports inside function bodies in
   `cli/commands/box/config.py`:
   - `from ...box_storage import get_box_ip, list_boxes` (in `_resolve_box`)
   - `from ..development.python import run_python_internal_get_output`
   - `from ._mount_prep import ensure_host_path_owned, manual_fix_command`
   - `from ._host_ops import apt_install, sysctl_apply`
   - `from ...box_storage import get_box_user` (in `_bounce_container`)
   - `from ._pip_validation import validate_format, validate_on_pypi, is_direct_ref`
   - `import requests` in `_box_api_responding`

   Move them to the top of the file unless one introduces a circular
   import (none should — the box-side modules under `_*.py` don't
   import back).
2. Remove `import re as _re` and the underscore-prefixed regex names
   (`_APT_NAME_RE`, `_SYSCTL_KEY_RE_HOST`, `_CARGO_SPEC_RE_HOST`) once
   Task B has deleted their callers. If Task B isn't done, just
   replace `import re as _re` at line ~943 with a single `import re`
   at the top of the module.
3. The `noqa: BLE001` on `except Exception as e` in
   `cli/commands/box/_pip_validation.py:89` is fine; leave it.

**Verify.**

```
pytest test/unit/box/
```

---

## Out of scope (don't do these on the road)

- Anything that requires SSHing to a real Lagerbox.
- Renaming `lager box config apt` / `sysctl` to top-level `lager box
  apt` / `lager box sysctl`. That's a public-CLI break and needs a
  CHANGELOG note + deprecation period.
- Touching `start_box.sh` or the Dockerfile. Those need an end-to-end
  bounce test on real hardware.
- The 1100+ lines of new tests in `test/unit/box/test_box_config*.py`.
  They're verbose but they're the safety net for everything above.

---

## Definition of done (per task)

1. The relevant `pytest` invocation passes.
2. `git diff` shows only the intended files changed.
3. The commit message follows the existing branch convention
   (`refactor(box-config): ...` or `chore(box-config): ...` rather
   than `feat`).
4. No public CLI surface change: `lager box config --help`, every
   subcommand's `--help`, and the `--json` output for `list` commands
   must be byte-identical before and after.
