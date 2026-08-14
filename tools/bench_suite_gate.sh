#!/bin/bash
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
#
# Two-sided baseline ratchet for one bench suite.
#
# The bench integration suites are wired into CI carrying known failures.
# A suite that simply "must pass" would either sit permanently red (a check
# people learn to ignore) or force the failures to be softened away. Instead
# each suite declares how many checks are expected to fail today, and this
# gate gets stricter automatically:
#
#   failures  > baseline  -> ERROR, exit 1. New breakage beyond the known gap.
#   failures == baseline  -> warning. Known gap, run stays green.
#   failures  < baseline  -> notice telling the reader to lower the baseline
#                            and lock the improvement in.
#
# Every non-zero baseline must name an owning issue where it is set. A number
# with no issue behind it is softening, not baselining.
#
# Usage: bench_suite_gate.sh <format> <label> <baseline> <logfile>
#
#   format    harness    suites sourcing test/framework/harness.sh
#             deployment deployment.sh, which counts for itself
#   label     name shown in the annotation, e.g. "nets.sh"
#   baseline  expected failure count (integer)
#   logfile   the suite's combined output, already captured
#
# The suite's own exit status is deliberately NOT consulted: it is the same
# information as the failure count, and reading it through a `tee` pipeline is
# how a gate ends up reporting tee's status instead of the suite's.

set -uo pipefail

if [ "$#" -ne 4 ]; then
    echo "usage: $0 <harness|deployment> <label> <baseline> <logfile>" >&2
    exit 2
fi

FORMAT="$1"
LABEL="$2"
BASELINE="$3"
LOGFILE="$4"

if ! [ -f "$LOGFILE" ]; then
    echo "::error title=$LABEL::no log at $LOGFILE - the suite step did not run"
    exit 1
fi

case "$BASELINE" in
    ''|*[!0-9]*)
        echo "::error title=$LABEL::baseline '$BASELINE' is not a non-negative integer"
        exit 1
        ;;
esac

# Strip ANSI first: every suite colours its summary, and a colour reset sits
# between the label and the number.
STRIPPED="$(sed 's/\x1b\[[0-9;]*m//g' "$LOGFILE")"

total=""
failed=""

case "$FORMAT" in
    harness)
        # harness.sh print_summary emits
        #     TOTAL  <total> <passed> <failed>
        # with an EMPTY description cell, and appends a FIFTH <excluded>
        # column whenever GLOBAL_EXCLUDED > 0 -- which any track_test "skip"
        # triggers. Address the columns by position; `$NF` reads the skip
        # count on exactly those runs and lets the ratchet pass with an
        # arbitrary number of real failures.
        read -r total failed <<<"$(printf '%s\n' "$STRIPPED" \
            | awk '$1 == "TOTAL" && NF >= 4 {print $2, $4}' | tail -1)"
        ;;
    deployment)
        # deployment.sh keeps its own counters and prints
        #     Total Tests:   <n>
        #     Failed:        <n>
        read -r total failed <<<"$(printf '%s\n' "$STRIPPED" \
            | awk '$1 == "Total" && $2 == "Tests:" {t = $3}
                   $1 == "Failed:" {f = $2}
                   END {if (t != "" && f != "") print t, f}')"
        ;;
    *)
        echo "::error title=$LABEL::unknown summary format '$FORMAT'"
        exit 1
        ;;
esac

# A missing summary is the signature of a suite that died partway -- exactly
# what a `set -e` abort or a step timeout looks like. That must be loud, not
# silently treated as zero failures.
if [ -z "$total" ] || [ -z "$failed" ]; then
    echo "::error title=$LABEL::no test summary found - the suite did not run to completion"
    exit 1
fi

case "$failed" in
    ''|*[!0-9]*)
        echo "::error title=$LABEL::parsed failure count '$failed' is not a number"
        exit 1
        ;;
esac

if [ "$failed" -eq 0 ]; then
    echo "::notice title=$LABEL::all $total checks pass"
else
    echo "::warning title=$LABEL::$failed of $total checks fail (baseline $BASELINE)"
fi

if [ "$failed" -gt "$BASELINE" ]; then
    echo "::error title=$LABEL::failures grew from $BASELINE to $failed - new breakage beyond the known gap"
    exit 1
fi

if [ "$failed" -lt "$BASELINE" ]; then
    echo "::notice title=$LABEL::only $failed failures (baseline $BASELINE) - lower the baseline to lock in the improvement"
fi

exit 0
