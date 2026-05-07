# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
JSON-only CLI for box config operations executed inside the container.

Stdout is always a single JSON value; diagnostics go to stderr. Mirrors
the contract of lager.nets.net_cli so the host-side CLI can shell in via
run_python_internal.
"""
from __future__ import annotations

import json
import sys
import traceback
from typing import Any

from . import config as cfg


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
    import os
    if os.path.exists(cfg.BOX_CONFIG_PATH) and not force:
        _stdout_json({"ok": True, "created": False, "imported": []})
        return
    new_cfg = cfg.init_default()
    imported, skipped = _import_legacy_into(new_cfg)
    cfg.save(new_cfg)
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
    _stdout_json({"ok": True})


def _cmd_restore_applied() -> None:
    """Replace box_config.json with the last applied snapshot, so the host-
    side CLI can roll back when a new config's bounce fails."""
    snap = cfg.read_applied_snapshot()
    if snap is None:
        _stdout_json({"ok": False, "error": "no applied snapshot available"})
        return
    cfg.save(snap)
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
    _stdout_json({"ok": True})


def _cmd_mount_remove(host: str, container: str) -> None:
    current = _load_or_init()
    before = len(current.mounts)
    current.mounts = [m for m in current.mounts if not (m.host == host and m.container == container)]
    removed = len(current.mounts) != before
    cfg.save(current)
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
    _stdout_json({"ok": True})


def _cmd_volume_remove(name: str) -> None:
    current = _load_or_init()
    before = len(current.volumes)
    current.volumes = [v for v in current.volumes if v.name != name]
    removed = len(current.volumes) != before
    cfg.save(current)
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
    _stdout_json({"ok": True, "added": added})


def _cmd_pip_remove(names: list) -> None:
    current = _load_or_init()
    targets = {cfg.normalize_pip_name(n) for n in names}
    before = len(current.pip_packages)
    removed = [p for p in current.pip_packages if cfg.normalize_pip_name(p) in targets]
    current.pip_packages = [p for p in current.pip_packages if cfg.normalize_pip_name(p) not in targets]
    cfg.save(current)
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
    _stdout_json({"ok": True, "added": added})


def _cmd_apt_remove(names: list) -> None:
    current = _load_or_init()
    targets = {n.lower() for n in names}
    before = len(current.apt_packages)
    removed = [p for p in current.apt_packages if p.lower() in targets]
    current.apt_packages = [p for p in current.apt_packages if p.lower() not in targets]
    cfg.save(current)
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
    _stdout_json({"ok": True, "set": set_keys})


def _cmd_sysctl_unset(keys: list) -> None:
    current = _load_or_init()
    removed = [k for k in keys if k in current.sysctl]
    for k in removed:
        del current.sysctl[k]
    cfg.save(current)
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
    _stdout_json({"ok": True, "removed": removed, "removed_count": before - len(current.cargo_packages)})


def _cmd_applied_show() -> None:
    """Return the last-applied snapshot (or None) so the host CLI can do
    per-field diffing — `apt_packages` unchanged since last apply means
    skip the apt-get install round-trip."""
    snap = cfg.read_applied_snapshot()
    if snap is None:
        _stdout_json(None)
        return
    _stdout_json(snap.to_dict())


def _cli() -> None:
    try:
        args = sys.argv[1:]
        cmd = args[0] if args else "show"

        if cmd == "show":
            _cmd_show()
        elif cmd == "validate":
            _cmd_validate()
        elif cmd == "init":
            force = "--force" in args[1:]
            _cmd_init(force)
        elif cmd == "hash":
            _cmd_hash()
        elif cmd == "applied-hash":
            _cmd_applied_hash()
        elif cmd == "set-applied-hash":
            if len(args) < 2:
                raise ValueError("set-applied-hash requires a hash value")
            _cmd_set_applied_hash(args[1])
        elif cmd == "restore-applied":
            _cmd_restore_applied()
        elif cmd == "mount-add":
            if len(args) < 2:
                raise ValueError("mount-add requires JSON payload")
            _cmd_mount_add(args[1])
        elif cmd == "mount-remove":
            if len(args) < 3:
                raise ValueError("mount-remove requires HOST CONTAINER")
            _cmd_mount_remove(args[1], args[2])
        elif cmd == "volume-add":
            if len(args) < 2:
                raise ValueError("volume-add requires JSON payload")
            _cmd_volume_add(args[1])
        elif cmd == "volume-remove":
            if len(args) < 2:
                raise ValueError("volume-remove requires NAME")
            _cmd_volume_remove(args[1])
        elif cmd == "pip-add":
            if len(args) < 2:
                raise ValueError("pip-add requires JSON payload")
            _cmd_pip_add(args[1])
        elif cmd == "pip-remove":
            if len(args) < 2:
                raise ValueError("pip-remove requires at least one package name")
            _cmd_pip_remove(list(args[1:]))
        elif cmd == "pip-import-legacy":
            _cmd_pip_import_legacy()
        elif cmd == "apt-add":
            if len(args) < 2:
                raise ValueError("apt-add requires JSON payload")
            _cmd_apt_add(args[1])
        elif cmd == "apt-remove":
            if len(args) < 2:
                raise ValueError("apt-remove requires at least one package name")
            _cmd_apt_remove(list(args[1:]))
        elif cmd == "sysctl-set":
            if len(args) < 2:
                raise ValueError("sysctl-set requires JSON payload")
            _cmd_sysctl_set(args[1])
        elif cmd == "sysctl-unset":
            if len(args) < 2:
                raise ValueError("sysctl-unset requires at least one key")
            _cmd_sysctl_unset(list(args[1:]))
        elif cmd == "cargo-add":
            if len(args) < 2:
                raise ValueError("cargo-add requires JSON payload")
            _cmd_cargo_add(args[1])
        elif cmd == "cargo-remove":
            if len(args) < 2:
                raise ValueError("cargo-remove requires at least one package name")
            _cmd_cargo_remove(list(args[1:]))
        elif cmd == "applied-show":
            _cmd_applied_show()
        else:
            _stdout_json({"ok": False, "error": f"unknown command: {cmd}"})

    except cfg.ValidationError as e:
        _stdout_json({"ok": False, "errors": str(e).split("\n")})

    except SystemExit:
        _stdout_json({"ok": False, "error": "exit"})

    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        _stdout_json({"ok": False, "error": str(e)})


if __name__ == "__main__":
    _cli()
