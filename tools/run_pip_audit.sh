#!/usr/bin/env bash
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
#
# Run pip-audit over the CURRENT environment, ignoring exactly the IDs
# listed (with reasons) in tools/pip_audit_ignore.txt. Wrapped in a script
# so the ignore list lives in one reviewable file instead of workflow args.
#
# --skip-editable: the editable lager-cli install itself has no published
# advisories to check; its DEPENDENCIES are installed concretely in the env
# and are audited.

set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
ignore_file="$here/pip_audit_ignore.txt"

args=()
while IFS= read -r raw; do
    line=${raw%%#*}
    line=$(echo "$line" | tr -d '[:space:]')
    [ -n "$line" ] && args+=(--ignore-vuln "$line")
done < "$ignore_file"

echo "pip-audit with ${#args[@]} ignore args"
pip-audit --skip-editable "${args[@]+"${args[@]}"}"
