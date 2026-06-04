# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Shim protocol verbs as constants.

The host CLI invokes the in-container shim via `run_python_internal` with a
verb string as the first positional argument; the shim's `_dispatch` routes
on that string. Host-side typos used to surface as a generic
`unknown command: ...` from the shim — easy to ship, harder to spot in a
busy `apply` log.

Constants here are *host-only*. The box-side dispatcher keeps string
literals because mirroring would split the truth across two files that ship
separately (cli/ vs box/), reintroducing the drift problem this is meant to
fix. The dispatcher's own switch statement IS the box-side source of truth.
"""
from __future__ import annotations


# Read verbs (no mutation, idempotent — safe to retry / no audit entry).
SHOW = "show"
VALIDATE = "validate"
HASH = "hash"
APPLIED_HASH = "applied-hash"
APPLIED_SHOW = "applied-show"
AUDIT_TAIL = "audit-tail"

# Lifecycle verbs (mutate /etc/lager/box_config.json or applied state).
INIT = "init"
RESET = "reset"
SET_APPLIED_HASH = "set-applied-hash"
RESTORE_APPLIED = "restore-applied"
SET_RAW = "set-raw"

# Mount + volume CRUD.
MOUNT_ADD = "mount-add"
MOUNT_REMOVE = "mount-remove"
VOLUME_ADD = "volume-add"
VOLUME_REMOVE = "volume-remove"

# Per-ecosystem package CRUD.
PIP_ADD = "pip-add"
PIP_REMOVE = "pip-remove"
PIP_IMPORT_LEGACY = "pip-import-legacy"
APT_ADD = "apt-add"
APT_REMOVE = "apt-remove"
SYSCTL_SET = "sysctl-set"
SYSCTL_UNSET = "sysctl-unset"
ENV_SET = "env-set"
ENV_UNSET = "env-unset"
CARGO_ADD = "cargo-add"
CARGO_REMOVE = "cargo-remove"
NPM_ADD = "npm-add"
NPM_REMOVE = "npm-remove"
UDEV_ADD = "udev-add"
UDEV_REMOVE = "udev-remove"
