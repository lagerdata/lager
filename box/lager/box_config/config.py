# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Box config – declarative per-box provisioning.

Reads /etc/lager/box_config.json (when present) and turns it into mount,
volume, and env declarations consumed by start_box.sh on every container
restart. Missing file = no behavior change vs. pre-feature boxes.

PR #1 surface: mounts, volumes, env. apt_packages / rustup / hooks are
round-tripped lossless via `extras` for future PRs.
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
SCHEMA_VERSION = 1

_VOLUME_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]+$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PIP_SPEC_RE = re.compile(
    r'^[a-zA-Z][a-zA-Z0-9\-_\.]*'
    r'(\[[a-zA-Z0-9\-_,\s]+\])?'
    r'(([<>=!~]=?|@)[a-zA-Z0-9\.\-_,\s\*<>=!~@]+)?$'
)
_PIP_NAME_RE = re.compile(r'^([a-zA-Z0-9\-_\.]+)')


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


@dataclass
class BoxConfig:
    version: int = SCHEMA_VERSION
    mounts: List[Mount] = field(default_factory=list)
    volumes: List[Volume] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    pip_packages: List[str] = field(default_factory=list)
    extras: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "BoxConfig":
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
        extras = {
            k: v for k, v in raw.items()
            if k not in {"version", "mounts", "volumes", "env", "pip_packages"}
        }
        return cls(
            version=int(raw["version"]),
            mounts=mounts,
            volumes=volumes,
            env=env,
            pip_packages=pip_packages,
            extras=extras,
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"version": self.version}
        out["mounts"] = [m.to_dict() for m in self.mounts]
        out["volumes"] = [v.to_dict() for v in self.volumes]
        out["env"] = dict(self.env)
        out["pip_packages"] = list(self.pip_packages)
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
    errors.extend(_validate_env(raw))
    errors.extend(_validate_pip_packages(raw))

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


def _validate_env(raw: Dict[str, Any]) -> List[str]:
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
