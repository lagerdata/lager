#!/bin/bash
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
#
# Check that `lager hello` reports the version a lifecycle phase just put on
# the box.
#
# The lifecycle job's "Verify box healthy" steps ran a bare `lager hello`,
# which exits 0 for any box that answers. A downgrade or install that left
# the box on the wrong code still passed them: only the version line says
# what the box runs.
#
# Usage: assert_hello_version.sh <box> <version> [<ref-text>]
#
#   box       the box name, as `lager boxes` lists it
#   version   the version the box must report, without a leading v (0.59.0)
#   ref-text  text the version line must also contain, such as a commit SHA
#             or "(release)". `hello` prints the box's ref there: "(release)"
#             on the release tag of that version, otherwise the ref itself.
#
# Prints the version line, then `::error` and exit 1 on a mismatch.
#
# LAGER_BIN overrides the lager command, for the unit tests only.

set -u

box="${1:?usage: assert_hello_version.sh <box> <version> [<ref-text>]}"
want="${2:?usage: assert_hello_version.sh <box> <version> [<ref-text>]}"
ref_text="${3:-}"
lager_bin="${LAGER_BIN:-lager}"

if ! out=$("$lager_bin" hello --box "$box" 2>&1); then
    echo "::error title=lager hello failed::Box ${box} did not answer lager hello: $(printf '%s' "$out" | tail -1)"
    exit 1
fi

# `hello` colours the ref of a non-release build.
line=$(printf '%s\n' "$out" | sed $'s/\033\\[[0-9;]*m//g' | grep -m1 '^Version:')
echo "${line:-no Version line}"
got=$(printf '%s\n' "$line" | awk '{print $2}')

if [ "$got" != "$want" ]; then
    echo "::error title=Wrong box version::Box ${box} reports version '${got:-none}', expected '${want}'."
    exit 1
fi
if [ -n "$ref_text" ] && ! printf '%s\n' "$line" | grep -qF -- "$ref_text"; then
    echo "::error title=Wrong box ref::Box ${box} reports '${line}', which does not name '${ref_text}'."
    exit 1
fi
exit 0
