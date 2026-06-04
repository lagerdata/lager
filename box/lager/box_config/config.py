# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Box config – declarative per-box provisioning.

Reads /etc/lager/box_config.json (when present) and turns it into mount,
volume, and env declarations consumed by start_box.sh on every container
restart. Missing file = no behavior change vs. pre-feature boxes.

First-class fields: mounts, volumes, env, pip_packages, apt_packages,
sysctl, cargo_packages. Unknown keys (rustup / hooks / future fields) are
round-tripped lossless via `extras`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


BOX_CONFIG_PATH = "/etc/lager/box_config.json"
APPLIED_HASH_PATH = "/etc/lager/box_config.applied_hash"
APPLIED_CONFIG_PATH = "/etc/lager/box_config.applied.json"
SCHEMA_VERSION = 1

# Container paths hard-coded by box/start_box.sh. A user-defined mount whose
# container path collides with one of these would fail at `docker run` with
# "Duplicate mount point" — at the worst possible moment, mid-bounce. Reject
# at validation time instead. Keep this in sync with the `-v` list in
# start_box.sh.
RESERVED_CONTAINER_PATHS: Dict[str, str] = {
    "/tmp": "shared host /tmp",
    "/dev": "host device tree",
    "/sys/bus/usb": "USB device topology",
    "/sys/devices": "device topology",
    "/var/run/dbus": "host dbus socket",
    "/etc/lager": "lager config (where box_config.json itself lives)",
    "/home/www-data/.ssh": "lagerdata's authorized_keys (incoming SSH auth)",
    "/host/etc/hostname": "host hostname",
    "/opt/SEGGER": "SEGGER J-Link tools",
    "/opt/picoscope/lib": "PicoScope libraries",
    "/opt/rust/cargo": "named volume persisting user cargo crates",
    "/home/www-data/.npm-global": "named volume persisting user npm packages",
}

# Suggested non-colliding alternatives for common reserved paths. The big one
# is .ssh — users want to mount git creds for `cargo install --git`, etc.
_RESERVED_PATH_ALTERNATIVES: Dict[str, str] = {
    "/home/www-data/.ssh": "/home/www-data/.ssh-git",
}


def suggest_alternative(container_path: str) -> Optional[str]:
    return _RESERVED_PATH_ALTERNATIVES.get(container_path)

_VOLUME_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]+$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PIP_SPEC_RE = re.compile(
    r'^[a-zA-Z][a-zA-Z0-9\-_\.]*'
    r'(\[[a-zA-Z0-9\-_,\s]+\])?'
    r'(([<>=!~]=?|@)[a-zA-Z0-9\.\-_,\s\*<>=!~@]+)?$'
)
_PIP_NAME_RE = re.compile(r'^([a-zA-Z0-9\-_\.]+)')

# Debian package name format (lowercased; no version pinning in v1).
_APT_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9+\-.]*$')

# sysctl namespaced keys: net.ipv4.ip_forward, kernel.shmmax, etc.
_SYSCTL_KEY_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_.]*$')

# cargo crate name + optional @version. No git+ URLs in v1.
_CARGO_SPEC_RE = re.compile(r'^[a-z0-9][a-z0-9_\-]*(?:@[a-zA-Z0-9.+\-]+)?$')
_CARGO_NAME_RE = re.compile(r'^([a-z0-9][a-z0-9_\-]*)')

# npm package spec: optional @scope/, name, optional @version. Versions can
# include semver ranges (^1.0.0, ~1.0.0, >=1.0.0) — keep that character set
# permissive but bounded to what npm CLI accepts. No tarball URLs or git refs
# in v1.
_NPM_SPEC_RE = re.compile(
    r'^(?:@[a-z0-9][a-z0-9._\-]*\/)?'           # optional @scope/
    r'[a-z0-9][a-z0-9._\-]*'                     # name
    r'(?:@[a-zA-Z0-9.+\-~^*<>=|\s]+)?$'          # optional @version-or-range
)
_NPM_NAME_RE = re.compile(
    r'^((?:@[a-z0-9][a-z0-9._\-]*\/)?[a-z0-9][a-z0-9._\-]*)'
)

# USB vendor/product IDs: exactly 4 lowercase hex digits, matching the
# ATTRS{idVendor}/ATTRS{idProduct} form udev expects (e.g. "1209", "0001").
_UDEV_HEXID_RE = re.compile(r'^[0-9a-f]{4}$')
# Octal file mode for the device node, e.g. "0666". Three octal digits with a
# leading zero is the only form we emit into MODE="...".
_UDEV_MODE_RE = re.compile(r'^0[0-7]{3}$')


def normalize_pip_name(pkg: str) -> str:
    """Canonical key for dedupe / removal: lowercase, underscores→dashes,
    strip version specifiers / extras."""
    m = _PIP_NAME_RE.match(pkg)
    base = m.group(1) if m else pkg
    return base.lower().replace('_', '-')


def validate_pip_format(pkg: str) -> tuple[bool, Optional[str]]:
    """Format check for a single requirement string. Mirrors the validator
    that previously lived in cli/commands/utility/pip.py so we don't change
    behavior when packages move from user_requirements.txt to pip_packages."""
    if not isinstance(pkg, str) or not pkg.strip():
        return False, "package name cannot be empty"
    if pkg[0] in '0123456789.-_':
        return False, "package name must start with a letter"
    if not _PIP_SPEC_RE.match(pkg):
        return False, "invalid package specification format"
    return True, None


def validate_apt_format(pkg: str) -> tuple[bool, Optional[str]]:
    """Format check for a single Debian package name. v1 is names only —
    no version pinning, no architecture qualifier."""
    if not isinstance(pkg, str) or not pkg.strip():
        return False, "package name cannot be empty"
    if not _APT_NAME_RE.match(pkg):
        return False, "invalid Debian package name (must match [a-z0-9][a-z0-9+-.]*)"
    return True, None


def validate_sysctl_key(key: str) -> tuple[bool, Optional[str]]:
    if not isinstance(key, str) or not key.strip():
        return False, "sysctl key cannot be empty"
    if not _SYSCTL_KEY_RE.match(key):
        return False, "invalid sysctl key (must match [a-zA-Z][a-zA-Z0-9_.]*)"
    return True, None


def validate_env_key(key: str) -> tuple[bool, Optional[str]]:
    """Format check for a single environment variable name. Rejects PATH
    explicitly — PATH inside the container is managed via PATH_PREPEND."""
    if not isinstance(key, str) or not key.strip():
        return False, "env key cannot be empty"
    if not _ENV_KEY_RE.match(key):
        return False, "invalid env variable name (must match [A-Za-z_][A-Za-z0-9_]*)"
    if key == "PATH":
        return False, "env key 'PATH' is not allowed; use 'PATH_PREPEND' to extend PATH inside the container"
    return True, None


def validate_cargo_format(pkg: str) -> tuple[bool, Optional[str]]:
    """Format check for a cargo crate spec. Accepts `name` or `name@version`."""
    if not isinstance(pkg, str) or not pkg.strip():
        return False, "package name cannot be empty"
    if not _CARGO_SPEC_RE.match(pkg):
        return False, "invalid cargo crate spec (must match [a-z0-9][a-z0-9_-]*(@version)?)"
    return True, None


def normalize_cargo_name(pkg: str) -> str:
    """Bare crate name, used as the dedupe key. Cargo crate names are
    case-sensitive but in practice crates.io is lowercase; we mirror pip's
    underscore->dash normalization for forgiveness."""
    m = _CARGO_NAME_RE.match(pkg)
    base = m.group(1) if m else pkg
    return base.lower().replace('_', '-')


def validate_npm_format(pkg: str) -> tuple[bool, Optional[str]]:
    """Format check for an npm package spec. Accepts `name`, `@scope/name`,
    `name@version`, `@scope/name@version`. Versions may be semver ranges."""
    if not isinstance(pkg, str) or not pkg.strip():
        return False, "package name cannot be empty"
    if len(pkg) > 214:
        # npm registry hard limit on package name length.
        return False, "npm package name exceeds 214 chars"
    if not _NPM_SPEC_RE.match(pkg):
        return False, "invalid npm package spec (must be lowercase name, optional @scope/, optional @version)"
    return True, None


def normalize_npm_name(pkg: str) -> str:
    """Bare package name (including @scope/ when present), used as the
    dedupe key. npm registry is case-insensitive in practice."""
    m = _NPM_NAME_RE.match(pkg)
    base = m.group(1) if m else pkg
    return base.lower()


def normalize_udev_id(value: str) -> str:
    """Canonical form of a USB vid/pid: lowercase, with an optional 0x prefix
    stripped. `lsusb` prints lowercase already, but accept "0x1209" / "1209"
    interchangeably so users can paste either."""
    if isinstance(value, str) and value.lower().startswith("0x"):
        value = value[2:]
    return value.lower() if isinstance(value, str) else value


def validate_udev_format(
    vid: str, pid: str, mode: str = "0666"
) -> tuple[bool, Optional[str]]:
    """Format check for a single udev rule. vid/pid must be 4 hex digits
    (after normalization); mode must be a 4-char octal like 0666."""
    if not isinstance(vid, str) or not _UDEV_HEXID_RE.match(normalize_udev_id(vid)):
        return False, "vendor id must be 4 hex digits (e.g. 1209)"
    if not isinstance(pid, str) or not _UDEV_HEXID_RE.match(normalize_udev_id(pid)):
        return False, "product id must be 4 hex digits (e.g. 0001)"
    if not isinstance(mode, str) or not _UDEV_MODE_RE.match(mode):
        return False, "mode must be an octal like 0666"
    return True, None


class ValidationError(Exception):
    """Raised by from_dict when the config document fails validation."""


@dataclass(frozen=True)
class Mount:
    host: str
    container: str
    readonly: bool = False

    def to_docker_arg(self) -> str:
        spec = f"{self.host}:{self.container}"
        if self.readonly:
            spec += ":ro"
        return f"-v {shlex.quote(spec)}"

    def to_dict(self) -> Dict[str, Any]:
        return {"host": self.host, "container": self.container, "readonly": self.readonly}


@dataclass(frozen=True)
class Volume:
    name: str
    container: str

    def to_docker_arg(self) -> str:
        spec = f"{self.name}:{self.container}"
        return f"-v {shlex.quote(spec)}"

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "container": self.container}


@dataclass(frozen=True)
class UdevRule:
    """A user-declared udev rule granting access to a USB device by vid:pid.

    udev runs on the box *host*; setting MODE on the device node is what lets
    tools inside the container (which sees /dev and /sys/bus/usb via bind
    mounts) open the device — fixing e.g. `dfu-util`'s "No DFU capable USB
    device available". Applied host-side during `lager box config apply`.
    """
    vid: str
    pid: str
    mode: str = "0666"
    usbtmc: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"vid": self.vid, "pid": self.pid, "mode": self.mode, "usbtmc": self.usbtmc}

    def to_rule_lines(self) -> List[str]:
        """Render the udev rule file lines for this device. The permission line
        is always emitted; USBTMC instruments also need the driver-unbind line
        so libusb/PyVISA can claim the interface (see box/udev_rules/README.md).
        """
        lines = [
            f"# vid:pid {self.vid}:{self.pid} (added via `lager box config udev`)",
            f'SUBSYSTEM=="usb", ATTRS{{idVendor}}=="{self.vid}", '
            f'ATTRS{{idProduct}}=="{self.pid}", MODE="{self.mode}"',
        ]
        if self.usbtmc:
            lines.append(
                f'ACTION=="bind", SUBSYSTEM=="usb", DRIVER=="usbtmc", '
                f'ATTRS{{idVendor}}=="{self.vid}", ATTRS{{idProduct}}=="{self.pid}", '
                f"RUN+=\"/bin/sh -c 'echo %k > /sys/bus/usb/drivers/usbtmc/unbind "
                f"2>/dev/null || true'\""
            )
        return lines


_FIRST_CLASS_KEYS = frozenset({
    "version", "mounts", "volumes", "env",
    "pip_packages", "apt_packages", "sysctl", "cargo_packages", "npm_packages",
    "udev_rules",
})


# Schema migrations. Populate when a future schema bump renames a field,
# splits a value, or computes defaults for a previously-absent key. Each
# _MIGRATIONS[N] is a callable taking a v=N raw dict and returning a v=N+1
# raw dict (with `version` bumped). `migrate_raw` walks the chain. Empty
# today because only v1 exists — leave this here so the upgrade pattern is
# obvious to the next person who bumps SCHEMA_VERSION.
_MIGRATIONS: Dict[int, Any] = {}


def migrate_raw(raw: Any) -> Any:
    """Upgrade a raw config dict from its declared version up to SCHEMA_VERSION.

    No-op when version is already current (the common case). Returns `raw`
    unchanged when the version is older than current but no migrator exists
    — `validate` will then report the version mismatch with a useful error.
    Raises ValidationError when the version is *newer* than this CLI knows;
    silently downgrading a newer config would lose data.
    """
    if not isinstance(raw, dict):
        return raw
    v = raw.get("version")
    if not isinstance(v, int):
        return raw  # let validate() report the missing/bad version
    while v < SCHEMA_VERSION:
        migrator = _MIGRATIONS.get(v)
        if migrator is None:
            return raw
        raw = migrator(raw)
        v = raw.get("version", v) if isinstance(raw, dict) else v
    if v > SCHEMA_VERSION:
        raise ValidationError(
            f"Config version {v} is newer than this CLI supports "
            f"({SCHEMA_VERSION}). Upgrade the lager CLI on the box."
        )
    return raw


@dataclass
class BoxConfig:
    version: int = SCHEMA_VERSION
    mounts: List[Mount] = field(default_factory=list)
    volumes: List[Volume] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    pip_packages: List[str] = field(default_factory=list)
    apt_packages: List[str] = field(default_factory=list)
    sysctl: Dict[str, str] = field(default_factory=dict)
    cargo_packages: List[str] = field(default_factory=list)
    npm_packages: List[str] = field(default_factory=list)
    udev_rules: List[UdevRule] = field(default_factory=list)
    extras: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "BoxConfig":
        raw = migrate_raw(raw)
        errors = validate(raw)
        if errors:
            raise ValidationError("\n".join(errors))

        mounts = [
            Mount(
                host=m["host"],
                container=m["container"],
                readonly=bool(m.get("readonly", False)),
            )
            for m in raw.get("mounts", [])
        ]
        volumes = [
            Volume(name=v["name"], container=v["container"])
            for v in raw.get("volumes", [])
        ]
        env = dict(raw.get("env", {}))
        pip_packages = list(raw.get("pip_packages", []))
        apt_packages = list(raw.get("apt_packages", []))
        sysctl = dict(raw.get("sysctl", {}))
        cargo_packages = list(raw.get("cargo_packages", []))
        npm_packages = list(raw.get("npm_packages", []))
        udev_rules = [
            UdevRule(
                vid=normalize_udev_id(u.get("vid", "")),
                pid=normalize_udev_id(u.get("pid", "")),
                mode=u.get("mode", "0666"),
                usbtmc=bool(u.get("usbtmc", False)),
            )
            for u in raw.get("udev_rules", [])
            if isinstance(u, dict)
        ]
        extras = {k: v for k, v in raw.items() if k not in _FIRST_CLASS_KEYS}
        return cls(
            version=int(raw["version"]),
            mounts=mounts,
            volumes=volumes,
            env=env,
            pip_packages=pip_packages,
            apt_packages=apt_packages,
            sysctl=sysctl,
            cargo_packages=cargo_packages,
            npm_packages=npm_packages,
            udev_rules=udev_rules,
            extras=extras,
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"version": self.version}
        out["mounts"] = [m.to_dict() for m in self.mounts]
        out["volumes"] = [v.to_dict() for v in self.volumes]
        out["env"] = dict(self.env)
        out["pip_packages"] = list(self.pip_packages)
        out["apt_packages"] = list(self.apt_packages)
        out["sysctl"] = dict(self.sysctl)
        out["cargo_packages"] = list(self.cargo_packages)
        out["npm_packages"] = list(self.npm_packages)
        out["udev_rules"] = [u.to_dict() for u in self.udev_rules]
        for k, v in self.extras.items():
            out[k] = v
        return out

    def compute_hash(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def docker_mount_args(self) -> List[str]:
        return [m.to_docker_arg() for m in self.mounts] + [v.to_docker_arg() for v in self.volumes]

    def docker_env_args(self) -> List[str]:
        return [f"--env {shlex.quote(f'{k}={v}')}" for k, v in self.env.items()]


def init_default() -> BoxConfig:
    return BoxConfig(
        version=SCHEMA_VERSION,
        mounts=[],
        volumes=[Volume(name="box-tools", container="/opt/box-tools")],
        env={},
        extras={},
    )


def load(path: str = BOX_CONFIG_PATH) -> Optional[BoxConfig]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None
    return BoxConfig.from_dict(raw)


def save(cfg: BoxConfig, path: str = BOX_CONFIG_PATH) -> None:
    _atomic_write_json(path, cfg.to_dict())


def read_applied_hash(path: str = APPLIED_HASH_PATH) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def write_applied_hash(value: str, path: str = APPLIED_HASH_PATH) -> None:
    _ensure_dir(path)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(value)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_applied_snapshot(path: str = APPLIED_CONFIG_PATH) -> Optional["BoxConfig"]:
    """Return the last successfully-applied config (the rollback target).

    None when no apply has succeeded yet on this box, or when the snapshot
    file is missing/corrupt — callers must treat None as "rollback unavailable".
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    try:
        return BoxConfig.from_dict(raw)
    except ValidationError:
        return None


def write_applied_snapshot(cfg: "BoxConfig", path: str = APPLIED_CONFIG_PATH) -> None:
    """Save a JSON snapshot of the config alongside applied_hash. The pair
    is the rollback target if a future apply's bounce blows up."""
    _atomic_write_json(path, cfg.to_dict())


def validate(raw: Any) -> List[str]:
    errors: List[str] = []

    if not isinstance(raw, dict):
        return [f"Top-level must be a JSON object, got {_typename(raw)}."]

    version = raw.get("version")
    if version is None:
        errors.append("Missing required key 'version'.")
    elif version != SCHEMA_VERSION:
        errors.append(
            f"Unsupported config version: {version!r}. This CLI only supports version {SCHEMA_VERSION}."
        )

    errors.extend(_validate_mounts(raw))
    errors.extend(_validate_volumes(raw))
    errors.extend(_validate_cross_path_collisions(raw))
    errors.extend(_validate_reserved_paths(raw))
    errors.extend(_validate_env(raw))
    errors.extend(_validate_pip_packages(raw))
    errors.extend(_validate_apt_packages(raw))
    errors.extend(_validate_sysctl(raw))
    errors.extend(_validate_cargo_packages(raw))
    errors.extend(_validate_npm_packages(raw))
    errors.extend(_validate_udev_rules(raw))

    return errors


def _validate_mounts(raw: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    mounts = raw.get("mounts")
    if mounts is None:
        return errors
    if not isinstance(mounts, list):
        return [f"'mounts' must be an array, got {_typename(mounts)}."]

    seen_containers: Dict[str, int] = {}
    for i, m in enumerate(mounts):
        if not isinstance(m, dict):
            errors.append(f"mounts[{i}] must be an object, got {_typename(m)}.")
            continue
        host = m.get("host")
        container = m.get("container")
        if host is None:
            errors.append(f"mounts[{i}]: missing required key 'host'.")
        elif not isinstance(host, str):
            errors.append(f"mounts[{i}].host must be a string, got {_typename(host)}.")
        if container is None:
            errors.append(f"mounts[{i}]: missing required key 'container'.")
        elif not isinstance(container, str):
            errors.append(f"mounts[{i}].container must be a string, got {_typename(container)}.")
        if "readonly" in m and not isinstance(m["readonly"], bool):
            errors.append(f"mounts[{i}].readonly must be a boolean, got {_typename(m['readonly'])}.")

        if isinstance(host, str):
            if not host.startswith("/"):
                errors.append(f"mounts[{i}].host must be an absolute path (got {host!r}).")
            if host == "/":
                errors.append(
                    f"mounts[{i}].host cannot be '/' — refusing to bind-mount the entire host filesystem."
                )
        if isinstance(container, str):
            if not container.startswith("/"):
                errors.append(f"mounts[{i}].container must be an absolute path (got {container!r}).")
            if container == "/":
                errors.append(
                    f"mounts[{i}].container cannot be '/' — refusing to overlay the entire container filesystem."
                )
            if container in seen_containers:
                errors.append(
                    f"mounts[{i}].container {container!r} duplicates mounts[{seen_containers[container]}]."
                )
            else:
                seen_containers[container] = i
    return errors


def _validate_volumes(raw: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    volumes = raw.get("volumes")
    if volumes is None:
        return errors
    if not isinstance(volumes, list):
        return [f"'volumes' must be an array, got {_typename(volumes)}."]

    seen_names: Dict[str, int] = {}
    for i, v in enumerate(volumes):
        if not isinstance(v, dict):
            errors.append(f"volumes[{i}] must be an object, got {_typename(v)}.")
            continue
        name = v.get("name")
        container = v.get("container")
        if name is None:
            errors.append(f"volumes[{i}]: missing required key 'name'.")
        elif not isinstance(name, str):
            errors.append(f"volumes[{i}].name must be a string, got {_typename(name)}.")
        elif not _VOLUME_NAME_RE.match(name):
            errors.append(
                f"volumes[{i}].name {name!r} is not a valid Docker volume name "
                f"(must match [a-zA-Z0-9][a-zA-Z0-9_.-]+)."
            )
        elif name in seen_names:
            errors.append(
                f"volumes[{i}].name {name!r} duplicates volumes[{seen_names[name]}]."
            )
        else:
            seen_names[name] = i

        if container is None:
            errors.append(f"volumes[{i}]: missing required key 'container'.")
        elif not isinstance(container, str):
            errors.append(f"volumes[{i}].container must be a string, got {_typename(container)}.")
        else:
            if not container.startswith("/"):
                errors.append(f"volumes[{i}].container must be an absolute path (got {container!r}).")
            if container == "/":
                errors.append(f"volumes[{i}].container cannot be '/'.")
    return errors


def _validate_cross_path_collisions(raw: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    mounts = raw.get("mounts")
    volumes = raw.get("volumes")
    if not isinstance(mounts, list) or not isinstance(volumes, list):
        return errors
    mount_containers: Dict[str, int] = {}
    for j, m in enumerate(mounts):
        if isinstance(m, dict) and isinstance(m.get("container"), str):
            mount_containers.setdefault(m["container"], j)
    for i, v in enumerate(volumes):
        if not isinstance(v, dict):
            continue
        c = v.get("container")
        if isinstance(c, str) and c in mount_containers:
            errors.append(
                f"volumes[{i}].container {c!r} collides with mounts[{mount_containers[c]}].container."
            )
    return errors


def _validate_reserved_paths(raw: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    mounts = raw.get("mounts")
    if not isinstance(mounts, list):
        return errors
    for i, m in enumerate(mounts):
        if not isinstance(m, dict):
            continue
        c = m.get("container")
        if not isinstance(c, str) or c not in RESERVED_CONTAINER_PATHS:
            continue
        purpose = RESERVED_CONTAINER_PATHS[c]
        msg = (
            f"mounts[{i}].container {c!r} is reserved by start_box.sh ({purpose}); "
            "pick a different container path."
        )
        alt = suggest_alternative(c)
        if alt:
            msg += f" Suggestion: use {alt} instead."
        errors.append(msg)
    return errors


def _validate_env(raw: Dict[str, Any]) -> List[str]:
    # Recognized lager-side env keys (handled inside the container, not just
    # passed through):
    #   PATH_PREPEND               extends PATH inside the container
    #   LAGER_DISABLE_UART_SERVICE truthy ("1"/"true"/"yes") skips the
    #                              port-9000 box_http_server so customers can
    #                              run their own service on that port
    errors: List[str] = []
    env = raw.get("env")
    if env is None:
        return errors
    if not isinstance(env, dict):
        return [f"'env' must be an object, got {_typename(env)}."]
    for k, v in env.items():
        if not isinstance(k, str) or not _ENV_KEY_RE.match(k):
            errors.append(f"env key {k!r} is not a valid environment variable name.")
            continue
        if k == "PATH":
            errors.append(
                "env key 'PATH' is not allowed; use 'PATH_PREPEND' to extend PATH inside the container."
            )
        if not isinstance(v, str):
            errors.append(f"env[{k!r}] must be a string, got {_typename(v)}.")
    return errors


def _validate_pip_packages(raw: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    pkgs = raw.get("pip_packages")
    if pkgs is None:
        return errors
    if not isinstance(pkgs, list):
        return [f"'pip_packages' must be an array, got {_typename(pkgs)}."]

    seen: Dict[str, int] = {}
    for i, p in enumerate(pkgs):
        if not isinstance(p, str):
            errors.append(f"pip_packages[{i}] must be a string, got {_typename(p)}.")
            continue
        ok, reason = validate_pip_format(p)
        if not ok:
            errors.append(f"pip_packages[{i}] {p!r}: {reason}.")
            continue
        canonical = normalize_pip_name(p)
        if canonical in seen:
            errors.append(
                f"pip_packages[{i}] {p!r} duplicates pip_packages[{seen[canonical]}] "
                f"(both normalize to {canonical!r})."
            )
        else:
            seen[canonical] = i
    return errors


def _validate_apt_packages(raw: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    pkgs = raw.get("apt_packages")
    if pkgs is None:
        return errors
    if not isinstance(pkgs, list):
        return [f"'apt_packages' must be an array, got {_typename(pkgs)}."]

    seen: Dict[str, int] = {}
    for i, p in enumerate(pkgs):
        if not isinstance(p, str):
            errors.append(f"apt_packages[{i}] must be a string, got {_typename(p)}.")
            continue
        ok, reason = validate_apt_format(p)
        if not ok:
            errors.append(f"apt_packages[{i}] {p!r}: {reason}.")
            continue
        canonical = p.lower()
        if canonical in seen:
            errors.append(
                f"apt_packages[{i}] {p!r} duplicates apt_packages[{seen[canonical]}]."
            )
        else:
            seen[canonical] = i
    return errors


def _validate_sysctl(raw: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    sysctl = raw.get("sysctl")
    if sysctl is None:
        return errors
    if not isinstance(sysctl, dict):
        return [f"'sysctl' must be an object, got {_typename(sysctl)}."]
    for k, v in sysctl.items():
        ok, reason = validate_sysctl_key(k)
        if not ok:
            errors.append(f"sysctl key {k!r}: {reason}.")
            continue
        if not isinstance(v, str):
            errors.append(f"sysctl[{k!r}] must be a string, got {_typename(v)}.")
            continue
        if "\n" in v:
            errors.append(f"sysctl[{k!r}] must not contain newlines.")
    return errors


def _validate_cargo_packages(raw: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    pkgs = raw.get("cargo_packages")
    if pkgs is None:
        return errors
    if not isinstance(pkgs, list):
        return [f"'cargo_packages' must be an array, got {_typename(pkgs)}."]

    seen: Dict[str, int] = {}
    for i, p in enumerate(pkgs):
        if not isinstance(p, str):
            errors.append(f"cargo_packages[{i}] must be a string, got {_typename(p)}.")
            continue
        ok, reason = validate_cargo_format(p)
        if not ok:
            errors.append(f"cargo_packages[{i}] {p!r}: {reason}.")
            continue
        canonical = normalize_cargo_name(p)
        if canonical in seen:
            errors.append(
                f"cargo_packages[{i}] {p!r} duplicates cargo_packages[{seen[canonical]}] "
                f"(both normalize to {canonical!r})."
            )
        else:
            seen[canonical] = i
    return errors


def _validate_npm_packages(raw: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    pkgs = raw.get("npm_packages")
    if pkgs is None:
        return errors
    if not isinstance(pkgs, list):
        return [f"'npm_packages' must be an array, got {_typename(pkgs)}."]

    seen: Dict[str, int] = {}
    for i, p in enumerate(pkgs):
        if not isinstance(p, str):
            errors.append(f"npm_packages[{i}] must be a string, got {_typename(p)}.")
            continue
        ok, reason = validate_npm_format(p)
        if not ok:
            errors.append(f"npm_packages[{i}] {p!r}: {reason}.")
            continue
        canonical = normalize_npm_name(p)
        if canonical in seen:
            errors.append(
                f"npm_packages[{i}] {p!r} duplicates npm_packages[{seen[canonical]}] "
                f"(both normalize to {canonical!r})."
            )
        else:
            seen[canonical] = i
    return errors


def _validate_udev_rules(raw: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    rules = raw.get("udev_rules")
    if rules is None:
        return errors
    if not isinstance(rules, list):
        return [f"'udev_rules' must be an array, got {_typename(rules)}."]

    seen: Dict[tuple, int] = {}
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            errors.append(f"udev_rules[{i}] must be an object, got {_typename(r)}.")
            continue
        vid = r.get("vid")
        pid = r.get("pid")
        mode = r.get("mode", "0666")
        if vid is None:
            errors.append(f"udev_rules[{i}]: missing required key 'vid'.")
        if pid is None:
            errors.append(f"udev_rules[{i}]: missing required key 'pid'.")
        if "usbtmc" in r and not isinstance(r["usbtmc"], bool):
            errors.append(f"udev_rules[{i}].usbtmc must be a boolean, got {_typename(r['usbtmc'])}.")
        if vid is None or pid is None:
            continue
        ok, reason = validate_udev_format(vid, pid, mode)
        if not ok:
            errors.append(f"udev_rules[{i}] {vid}:{pid}: {reason}.")
            continue
        key = (normalize_udev_id(vid), normalize_udev_id(pid))
        if key in seen:
            errors.append(
                f"udev_rules[{i}] {vid}:{pid} duplicates udev_rules[{seen[key]}]."
            )
        else:
            seen[key] = i
    return errors


def _typename(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)


def _atomic_write_json(path: str, payload: Any) -> None:
    _ensure_dir(path)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
