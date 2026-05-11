# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
JSON-only CLI for box config operations executed inside the container.

Stdout is always a single JSON value; diagnostics go to stderr. Mirrors
the contract of lager.nets.net_cli so the host-side CLI can shell in via
run_python_internal.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import sys
import traceback
from contextlib import contextmanager
from typing import Any

from . import config as cfg


_BOX_CONFIG_LOCK_PATH = "/etc/lager/box_config.lock"
_BOX_CONFIG_AUDIT_PATH = "/etc/lager/box_config.audit.log"


def _audit(verb: str, args) -> None:
    """Append one JSON-Lines record describing a successful mutation.

    Best-effort: failures to write the log do not propagate. The mutation
    already succeeded by the time we get here, so failing the response
    over a missing log line would lie about the actual outcome. The log
    is append-only by design — never rewritten — so operators can correlate
    box state at any point in time with the verb that produced it.
    """
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    if ts.endswith("+00:00"):
        ts = ts[:-6] + "Z"
    entry = {"ts": ts, "verb": verb, "args": args}
    try:
        d = os.path.dirname(_BOX_CONFIG_AUDIT_PATH)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(_BOX_CONFIG_AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


@contextmanager
def _box_config_lock():
    """Serialize shim invocations against /etc/lager/box_config.json.

    Without this, two near-simultaneous `lager box config X` calls do
    read-modify-write and silently lose one update. Held for the whole
    dispatch including reads — reads are sub-millisecond so the extra
    serialization is invisible. Advisory flock; mutual exclusion only
    holds because every shim invocation goes through the same wrapper.
    """
    os.makedirs(os.path.dirname(_BOX_CONFIG_LOCK_PATH), exist_ok=True)
    with open(_BOX_CONFIG_LOCK_PATH, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _stdout_json(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _load_raw():
    try:
        with open(cfg.BOX_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _cmd_show() -> None:
    raw = _load_raw()
    _stdout_json(raw)


def _cmd_validate() -> None:
    raw = _load_raw()
    if raw is None:
        _stdout_json({"ok": True, "errors": [], "exists": False})
        return
    errors = cfg.validate(raw)
    _stdout_json({"ok": not errors, "errors": errors, "exists": True})


def _cmd_init(force: bool) -> None:
    if os.path.exists(cfg.BOX_CONFIG_PATH) and not force:
        _stdout_json({"ok": True, "created": False, "imported": []})
        return
    new_cfg = cfg.init_default()
    imported, skipped = _import_legacy_into(new_cfg)
    cfg.save(new_cfg)
    _audit("init", {"force": force, "imported_count": len(imported), "skipped_count": len(skipped)})
    _stdout_json({"ok": True, "created": True, "imported": imported, "skipped": skipped})


def _cmd_hash() -> None:
    raw = _load_raw()
    if raw is None:
        _stdout_json({"hash": None})
        return
    try:
        c = cfg.BoxConfig.from_dict(raw)
    except cfg.ValidationError as e:
        _stdout_json({"hash": None, "error": str(e)})
        return
    _stdout_json({"hash": c.compute_hash()})


def _cmd_applied_hash() -> None:
    _stdout_json({"hash": cfg.read_applied_hash()})


def _cmd_set_applied_hash(value: str) -> None:
    # Snapshot the current config alongside its hash so a future apply can
    # roll back to it if a new config breaks the container at bounce time.
    raw = _load_raw()
    if raw is not None:
        try:
            cfg.write_applied_snapshot(cfg.BoxConfig.from_dict(raw))
        except cfg.ValidationError:
            # The on-disk config doesn't validate — odd, since we only call
            # set-applied-hash after a successful bounce. Skip the snapshot
            # rather than fail the whole call.
            pass
    cfg.write_applied_hash(value)
    _audit("set-applied-hash", {"hash": value})
    _stdout_json({"ok": True})


def _cmd_restore_applied() -> None:
    """Replace box_config.json with the last applied snapshot, so the host-
    side CLI can roll back when a new config's bounce fails."""
    snap = cfg.read_applied_snapshot()
    if snap is None:
        _stdout_json({"ok": False, "error": "no applied snapshot available"})
        return
    cfg.save(snap)
    _audit("restore-applied", {})
    _stdout_json({"ok": True})


def _load_or_init() -> "cfg.BoxConfig":
    raw = _load_raw()
    if raw is None:
        return cfg.init_default()
    return cfg.BoxConfig.from_dict(raw)


def _cmd_mount_add(payload: str) -> None:
    data = json.loads(payload)
    current = _load_or_init()
    new_mount = cfg.Mount(
        host=data["host"],
        container=data["container"],
        readonly=bool(data.get("readonly", False)),
    )
    current.mounts = [m for m in current.mounts if m.container != new_mount.container] + [new_mount]
    raw = current.to_dict()
    errors = cfg.validate(raw)
    if errors:
        _stdout_json({"ok": False, "errors": errors})
        return
    cfg.save(cfg.BoxConfig.from_dict(raw))
    _audit("mount-add", data)
    _stdout_json({"ok": True})


def _cmd_mount_remove(host: str, container: str) -> None:
    current = _load_or_init()
    before = len(current.mounts)
    current.mounts = [m for m in current.mounts if not (m.host == host and m.container == container)]
    removed = len(current.mounts) != before
    cfg.save(current)
    if removed:
        _audit("mount-remove", {"host": host, "container": container})
    _stdout_json({"ok": True, "removed": removed})


def _cmd_volume_add(payload: str) -> None:
    data = json.loads(payload)
    current = _load_or_init()
    new_vol = cfg.Volume(name=data["name"], container=data["container"])
    current.volumes = [v for v in current.volumes if v.name != new_vol.name] + [new_vol]
    raw = current.to_dict()
    errors = cfg.validate(raw)
    if errors:
        _stdout_json({"ok": False, "errors": errors})
        return
    cfg.save(cfg.BoxConfig.from_dict(raw))
    _audit("volume-add", data)
    _stdout_json({"ok": True})


def _cmd_volume_remove(name: str) -> None:
    current = _load_or_init()
    before = len(current.volumes)
    current.volumes = [v for v in current.volumes if v.name != name]
    removed = len(current.volumes) != before
    cfg.save(current)
    if removed:
        _audit("volume-remove", {"name": name})
    _stdout_json({"ok": True, "removed": removed})


_LEGACY_PIP_PATH = "/etc/lager/user_requirements.txt"


def _parse_legacy_requirements(text: str) -> list:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _import_legacy_into(current: "cfg.BoxConfig") -> tuple:
    """Merge /etc/lager/user_requirements.txt into current.pip_packages.
    Returns (imported, skipped) lists. Mutates current in-place."""
    import os
    if not os.path.exists(_LEGACY_PIP_PATH):
        return [], []
    try:
        with open(_LEGACY_PIP_PATH, "r", encoding="utf-8") as f:
            packages = _parse_legacy_requirements(f.read())
    except OSError as e:
        return [], [{"package": None, "reason": f"could not read {_LEGACY_PIP_PATH}: {e}"}]

    seen = {cfg.normalize_pip_name(p): p for p in current.pip_packages}
    imported = []
    skipped = []
    for p in packages:
        ok, reason = cfg.validate_pip_format(p)
        if not ok:
            skipped.append({"package": p, "reason": reason})
            continue
        canon = cfg.normalize_pip_name(p)
        if canon in seen:
            skipped.append({"package": p, "reason": f"already in pip_packages as {seen[canon]!r}"})
            continue
        current.pip_packages.append(p)
        seen[canon] = p
        imported.append(p)
    return imported, skipped


def _cmd_pip_add(payload: str) -> None:
    data = json.loads(payload)
    new_pkgs = data.get("packages", [])
    if not isinstance(new_pkgs, list):
        _stdout_json({"ok": False, "errors": ["payload.packages must be an array"]})
        return
    current = _load_or_init()
    seen = {cfg.normalize_pip_name(p): i for i, p in enumerate(current.pip_packages)}
    added = []
    for p in new_pkgs:
        if not isinstance(p, str):
            _stdout_json({"ok": False, "errors": [f"non-string package: {p!r}"]})
            return
        ok, reason = cfg.validate_pip_format(p)
        if not ok:
            _stdout_json({"ok": False, "errors": [f"{p!r}: {reason}"]})
            return
        canon = cfg.normalize_pip_name(p)
        if canon in seen:
            current.pip_packages[seen[canon]] = p
        else:
            current.pip_packages.append(p)
            seen[canon] = len(current.pip_packages) - 1
        added.append(p)
    raw = current.to_dict()
    errors = cfg.validate(raw)
    if errors:
        _stdout_json({"ok": False, "errors": errors})
        return
    cfg.save(cfg.BoxConfig.from_dict(raw))
    _audit("pip-add", {"added": added})
    _stdout_json({"ok": True, "added": added})


def _cmd_pip_remove(names: list) -> None:
    current = _load_or_init()
    targets = {cfg.normalize_pip_name(n) for n in names}
    before = len(current.pip_packages)
    removed = [p for p in current.pip_packages if cfg.normalize_pip_name(p) in targets]
    current.pip_packages = [p for p in current.pip_packages if cfg.normalize_pip_name(p) not in targets]
    cfg.save(current)
    if removed:
        _audit("pip-remove", {"removed": removed})
    _stdout_json({"ok": True, "removed": removed, "removed_count": before - len(current.pip_packages)})


def _cmd_pip_import_legacy() -> None:
    current = _load_or_init()
    imported, skipped = _import_legacy_into(current)
    if imported:
        raw = current.to_dict()
        errors = cfg.validate(raw)
        if errors:
            _stdout_json({"ok": False, "errors": errors, "imported": [], "skipped": skipped})
            return
        cfg.save(cfg.BoxConfig.from_dict(raw))
        _audit("pip-import-legacy", {"imported": imported, "skipped_count": len(skipped)})
    _stdout_json({"ok": True, "imported": imported, "skipped": skipped})


def _cmd_apt_add(payload: str) -> None:
    data = json.loads(payload)
    new_pkgs = data.get("packages", [])
    if not isinstance(new_pkgs, list):
        _stdout_json({"ok": False, "errors": ["payload.packages must be an array"]})
        return
    current = _load_or_init()
    seen = {p.lower(): i for i, p in enumerate(current.apt_packages)}
    added = []
    for p in new_pkgs:
        if not isinstance(p, str):
            _stdout_json({"ok": False, "errors": [f"non-string package: {p!r}"]})
            return
        ok, reason = cfg.validate_apt_format(p)
        if not ok:
            _stdout_json({"ok": False, "errors": [f"{p!r}: {reason}"]})
            return
        canon = p.lower()
        if canon in seen:
            current.apt_packages[seen[canon]] = p
        else:
            current.apt_packages.append(p)
            seen[canon] = len(current.apt_packages) - 1
        added.append(p)
    raw = current.to_dict()
    errors = cfg.validate(raw)
    if errors:
        _stdout_json({"ok": False, "errors": errors})
        return
    cfg.save(cfg.BoxConfig.from_dict(raw))
    _audit("apt-add", {"added": added})
    _stdout_json({"ok": True, "added": added})


def _cmd_apt_remove(names: list) -> None:
    current = _load_or_init()
    targets = {n.lower() for n in names}
    before = len(current.apt_packages)
    removed = [p for p in current.apt_packages if p.lower() in targets]
    current.apt_packages = [p for p in current.apt_packages if p.lower() not in targets]
    cfg.save(current)
    if removed:
        _audit("apt-remove", {"removed": removed})
    _stdout_json({"ok": True, "removed": removed, "removed_count": before - len(current.apt_packages)})


def _cmd_sysctl_set(payload: str) -> None:
    data = json.loads(payload)
    entries = data.get("entries", {})
    if not isinstance(entries, dict):
        _stdout_json({"ok": False, "errors": ["payload.entries must be an object"]})
        return
    current = _load_or_init()
    set_keys = []
    for k, v in entries.items():
        ok, reason = cfg.validate_sysctl_key(k)
        if not ok:
            _stdout_json({"ok": False, "errors": [f"{k!r}: {reason}"]})
            return
        if not isinstance(v, str):
            _stdout_json({"ok": False, "errors": [f"sysctl[{k!r}] value must be a string"]})
            return
        current.sysctl[k] = v
        set_keys.append(k)
    raw = current.to_dict()
    errors = cfg.validate(raw)
    if errors:
        _stdout_json({"ok": False, "errors": errors})
        return
    cfg.save(cfg.BoxConfig.from_dict(raw))
    _audit("sysctl-set", {"entries": dict(entries)})
    _stdout_json({"ok": True, "set": set_keys})


def _cmd_sysctl_unset(keys: list) -> None:
    current = _load_or_init()
    removed = [k for k in keys if k in current.sysctl]
    for k in removed:
        del current.sysctl[k]
    cfg.save(current)
    if removed:
        _audit("sysctl-unset", {"removed": removed})
    _stdout_json({"ok": True, "removed": removed})


def _cmd_cargo_add(payload: str) -> None:
    data = json.loads(payload)
    new_pkgs = data.get("packages", [])
    if not isinstance(new_pkgs, list):
        _stdout_json({"ok": False, "errors": ["payload.packages must be an array"]})
        return
    current = _load_or_init()
    seen = {cfg.normalize_cargo_name(p): i for i, p in enumerate(current.cargo_packages)}
    added = []
    for p in new_pkgs:
        if not isinstance(p, str):
            _stdout_json({"ok": False, "errors": [f"non-string package: {p!r}"]})
            return
        ok, reason = cfg.validate_cargo_format(p)
        if not ok:
            _stdout_json({"ok": False, "errors": [f"{p!r}: {reason}"]})
            return
        canon = cfg.normalize_cargo_name(p)
        if canon in seen:
            current.cargo_packages[seen[canon]] = p
        else:
            current.cargo_packages.append(p)
            seen[canon] = len(current.cargo_packages) - 1
        added.append(p)
    raw = current.to_dict()
    errors = cfg.validate(raw)
    if errors:
        _stdout_json({"ok": False, "errors": errors})
        return
    cfg.save(cfg.BoxConfig.from_dict(raw))
    _audit("cargo-add", {"added": added})
    _stdout_json({"ok": True, "added": added})


def _cmd_cargo_remove(names: list) -> None:
    current = _load_or_init()
    targets = {cfg.normalize_cargo_name(n) for n in names}
    before = len(current.cargo_packages)
    removed = [p for p in current.cargo_packages if cfg.normalize_cargo_name(p) in targets]
    current.cargo_packages = [
        p for p in current.cargo_packages if cfg.normalize_cargo_name(p) not in targets
    ]
    cfg.save(current)
    if removed:
        _audit("cargo-remove", {"removed": removed})
    _stdout_json({"ok": True, "removed": removed, "removed_count": before - len(current.cargo_packages)})


def _cmd_npm_add(payload: str) -> None:
    data = json.loads(payload)
    new_pkgs = data.get("packages", [])
    if not isinstance(new_pkgs, list):
        _stdout_json({"ok": False, "errors": ["payload.packages must be an array"]})
        return
    current = _load_or_init()
    seen = {cfg.normalize_npm_name(p): i for i, p in enumerate(current.npm_packages)}
    added = []
    for p in new_pkgs:
        if not isinstance(p, str):
            _stdout_json({"ok": False, "errors": [f"non-string package: {p!r}"]})
            return
        ok, reason = cfg.validate_npm_format(p)
        if not ok:
            _stdout_json({"ok": False, "errors": [f"{p!r}: {reason}"]})
            return
        canon = cfg.normalize_npm_name(p)
        if canon in seen:
            current.npm_packages[seen[canon]] = p
        else:
            current.npm_packages.append(p)
            seen[canon] = len(current.npm_packages) - 1
        added.append(p)
    raw = current.to_dict()
    errors = cfg.validate(raw)
    if errors:
        _stdout_json({"ok": False, "errors": errors})
        return
    cfg.save(cfg.BoxConfig.from_dict(raw))
    _audit("npm-add", {"added": added})
    _stdout_json({"ok": True, "added": added})


def _cmd_set_raw(payload: str) -> None:
    """Replace /etc/lager/box_config.json wholesale with a JSON payload.

    Validates the payload before writing — on failure the on-disk config
    is unchanged. Used by `lager box config edit` (round-trip via $EDITOR),
    `import` (load a file from local disk), and `copy --from --to`
    (clone between boxes). Returns the post-save hash so the host CLI can
    fold it into subsequent `set-applied-hash` calls without an extra
    round-trip.
    """
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as e:
        _stdout_json({"ok": False, "errors": [f"invalid JSON: {e}"]})
        return
    errors = cfg.validate(raw)
    if errors:
        _stdout_json({"ok": False, "errors": errors})
        return
    try:
        c = cfg.BoxConfig.from_dict(raw)
    except cfg.ValidationError as e:
        _stdout_json({"ok": False, "errors": str(e).split("\n")})
        return
    cfg.save(c)
    new_hash = c.compute_hash()
    _audit("set-raw", {"hash": new_hash})
    _stdout_json({"ok": True, "hash": new_hash})


def _cmd_npm_remove(names: list) -> None:
    current = _load_or_init()
    targets = {cfg.normalize_npm_name(n) for n in names}
    before = len(current.npm_packages)
    removed = [p for p in current.npm_packages if cfg.normalize_npm_name(p) in targets]
    current.npm_packages = [
        p for p in current.npm_packages if cfg.normalize_npm_name(p) not in targets
    ]
    cfg.save(current)
    if removed:
        _audit("npm-remove", {"removed": removed})
    _stdout_json({"ok": True, "removed": removed, "removed_count": before - len(current.npm_packages)})


def _cmd_applied_show() -> None:
    """Return the last-applied snapshot (or None) so the host CLI can do
    per-field diffing — `apt_packages` unchanged since last apply means
    skip the apt-get install round-trip."""
    snap = cfg.read_applied_snapshot()
    if snap is None:
        _stdout_json(None)
        return
    _stdout_json(snap.to_dict())


def _cmd_audit_tail(n_arg: str) -> None:
    """Return the last N JSON-Lines audit entries.

    Reads the whole file and slices the tail — O(file) but the audit log
    rotates by hand, not on size, and box deployments don't accumulate
    enough entries for this to matter. If we ever do rotate, just glob the
    .audit.log.* siblings and slice the concatenation.
    """
    try:
        n = max(0, int(n_arg))
    except (ValueError, TypeError):
        n = 20
    try:
        with open(_BOX_CONFIG_AUDIT_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        _stdout_json({"entries": []})
        return
    tail = lines[-n:] if n > 0 else lines
    entries: list = []
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            # Skip corrupted lines rather than fail the whole call — an
            # append could be torn by a power loss mid-write.
            continue
    _stdout_json({"entries": entries})


def _require(args: list, n: int):
    """Return args[n] (the nth positional after the verb) or raise."""
    if len(args) <= n:
        raise ValueError(f"{args[0]} requires positional arg #{n}")
    return args[n]


def _require_rest(args: list, min_count: int = 1) -> list:
    """Return args[1:] as a list, or raise if fewer than min_count items."""
    if len(args) - 1 < min_count:
        raise ValueError(f"{args[0]} requires at least {min_count} positional arg(s)")
    return list(args[1:])


# Verb -> handler dispatch table. Lambdas all take the full `args` list
# (including the verb at index 0) and call the matching `_cmd_*`. Single
# source of truth for the wire protocol; an unrecognized verb is a clean
# dict miss instead of a fall-through `unknown command` case at the bottom
# of a 60-line if/elif.
_DISPATCH = {
    "show":              lambda args: _cmd_show(),
    "validate":          lambda args: _cmd_validate(),
    "init":              lambda args: _cmd_init("--force" in args[1:]),
    "hash":              lambda args: _cmd_hash(),
    "applied-hash":      lambda args: _cmd_applied_hash(),
    "applied-show":      lambda args: _cmd_applied_show(),
    "restore-applied":   lambda args: _cmd_restore_applied(),
    "set-applied-hash":  lambda args: _cmd_set_applied_hash(_require(args, 1)),
    "mount-add":         lambda args: _cmd_mount_add(_require(args, 1)),
    "mount-remove":      lambda args: _cmd_mount_remove(_require(args, 1), _require(args, 2)),
    "volume-add":        lambda args: _cmd_volume_add(_require(args, 1)),
    "volume-remove":     lambda args: _cmd_volume_remove(_require(args, 1)),
    "pip-add":           lambda args: _cmd_pip_add(_require(args, 1)),
    "pip-remove":        lambda args: _cmd_pip_remove(_require_rest(args)),
    "pip-import-legacy": lambda args: _cmd_pip_import_legacy(),
    "apt-add":           lambda args: _cmd_apt_add(_require(args, 1)),
    "apt-remove":        lambda args: _cmd_apt_remove(_require_rest(args)),
    "sysctl-set":        lambda args: _cmd_sysctl_set(_require(args, 1)),
    "sysctl-unset":      lambda args: _cmd_sysctl_unset(_require_rest(args)),
    "cargo-add":         lambda args: _cmd_cargo_add(_require(args, 1)),
    "cargo-remove":      lambda args: _cmd_cargo_remove(_require_rest(args)),
    "npm-add":           lambda args: _cmd_npm_add(_require(args, 1)),
    "npm-remove":        lambda args: _cmd_npm_remove(_require_rest(args)),
    "set-raw":           lambda args: _cmd_set_raw(_require(args, 1)),
    "audit-tail":        lambda args: _cmd_audit_tail(args[1] if len(args) >= 2 else "20"),
}


def _dispatch(args: list) -> None:
    cmd = args[0] if args else "show"
    handler = _DISPATCH.get(cmd)
    if handler is None:
        _stdout_json({"ok": False, "error": f"unknown command: {cmd}"})
        return
    handler(args)


def _cli() -> None:
    args = sys.argv[1:]
    try:
        with _box_config_lock():
            _dispatch(args)
    except cfg.ValidationError as e:
        _stdout_json({"ok": False, "errors": str(e).split("\n")})
    except SystemExit:
        _stdout_json({"ok": False, "error": "exit"})
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        _stdout_json({"ok": False, "error": str(e)})


if __name__ == "__main__":
    _cli()
