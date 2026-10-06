#!/bin/bash
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
#
# Release a bench box lock left by a run of this repository, and say so when
# it cannot.
#
# The bench workflows used to release with
#
#     lager boxes unlock --box "$LAGER_BOX" --force 2>/dev/null || true
#
# When the box's lager service was down, that unlock failed, `2>/dev/null`
# discarded the error and `|| true` passed the step. The lock is stored on the
# box, so it came back with the service and blocked the run's own re-run, with
# nothing in the first run saying it was still held (#639).
#
# This helper asks the box who holds the lock, releases it only when a run of
# this repository holds it, and confirms the release. A box that does not
# answer is asked again until the attempts run out. A lock still held at the
# end is a `::warning` naming the holder and the command that clears it.
#
# Only a lock held by a run of THIS repository is released, the same rule as
# the "Release a box lock left by a dead CI run" step at the start of every
# bench job. A lock a person took to work on the bench is reported and left
# alone. Every bench workflow shares one non-cancelling concurrency group, so a
# run-held lock found here belongs to this run or to a dead one.
#
# The exit status is always 0. A failed release must be reported, never fail a
# cleanup step or end the recovery script it runs inside.
#
# Usage: bench_release_lock.sh <box> [<attempts>] [<interval-seconds>]
#
#   box       the box name, as `lager boxes` lists it
#   attempts  how many times to ask the box (default 6)
#   interval  seconds between attempts while the box does not answer (default 5)
#
# LAGER_BIN overrides the lager command, for the unit tests only.

set -u

box="${1:?usage: bench_release_lock.sh <box> [<attempts>] [<interval-seconds>]}"
attempts="${2:-6}"
interval="${3:-5}"
lager_bin="${LAGER_BIN:-lager}"
repo="${GITHUB_REPOSITORY:-}"

ours_prefix="is locked by github ${repo} run "
released=""
holder=""
answered=0

for ((i = 1; i <= attempts; i++)); do
    if out=$("$lager_bin" hello --box "$box" 2>&1); then
        answered=1
        holder=$(printf '%s\n' "$out" | grep -o "is locked by [^;]*" | head -1 | sed 's/^is locked by //')
        if [ -z "$holder" ]; then
            if [ -n "$released" ]; then
                echo "Released the box lock on ${box} held by ${released}."
            else
                echo "No box lock held on ${box}."
            fi
            exit 0
        fi
        if [ -z "$repo" ] || ! printf '%s\n' "$out" | grep -qF "$ours_prefix"; then
            echo "::notice title=Box lock held by someone else::Box ${box} is locked by ${holder}. It is not a run of this repository, so it was left in place."
            exit 0
        fi
        if "$lager_bin" boxes unlock --box "$box" --force >/dev/null 2>&1; then
            released="$holder"
            # Confirm on the next pass, without waiting.
            continue
        fi
    fi
    if [ "$i" -lt "$attempts" ]; then
        sleep "$interval"
    fi
done

# An unlock on the last attempt has not been confirmed yet.
if [ -n "$released" ] && out=$("$lager_bin" hello --box "$box" 2>&1) \
        && ! printf '%s\n' "$out" | grep -q "is locked by"; then
    echo "Released the box lock on ${box} held by ${released}."
    exit 0
fi

if [ "$answered" -eq 0 ]; then
    echo "::warning title=Box lock state unknown::Box ${box} did not answer after ${attempts} attempts, so a lock this run took can still be held. Once the box is back: lager boxes unlock --box ${box} --force"
else
    echo "::warning title=Box lock still held::Box ${box} is still locked by ${holder} after ${attempts} attempts. It blocks the next run, including a re-run of this one. To clear it: lager boxes unlock --box ${box} --force"
fi
exit 0
