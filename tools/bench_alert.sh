#!/usr/bin/env bash
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
#
# Upsert the single bench-alert issue. Shared by the notify jobs in
# nightly-bench.yml and by bench-watchdog.yml so every bench problem lands in
# ONE open issue instead of a new issue per red night -- the 2026-08 nightly
# outage ran eight consecutive nights precisely because nothing filed anything.
#
#   bench_alert.sh alert   "<title>" <body-file>   comment on the open
#                                                  bench-alert issue, or
#                                                  create (and pin) it
#   bench_alert.sh recover <body-file>             comment on and close the
#                                                  open issue; no-op when
#                                                  none is open
#
# The body is a FILE, not a string, so callers can build multi-line markdown
# without fighting shell quoting (a heredoc inside `$()` with an apostrophe
# in it does not even parse under bash 3.2, which is what macOS ships).
#
# Requires: gh on PATH, GH_TOKEN with issues:write, GH_REPO set. The
# `bench-alert` label must already exist in the repo -- `gh issue create`
# fails on an unknown label, and that failure is deliberate: a missing label
# means the alerting setup is broken, which should be loud, not papered over.
#
# Pinning is best-effort: it needs more than issues:write, so it is allowed
# to fail without failing the alert itself. Issues are unpinned before being
# closed -- GitHub keeps a closed issue pinned, and pins cap at 3 per repo,
# so leaked pins would eventually break pinning entirely.
#
# GitHub's issue LIST endpoints -- search-backed `gh issue list` AND the
# REST issues endpoint -- are eventually consistent: measured live, an issue
# created 1-2s earlier is invisible to both, and each mechanism filed a
# duplicate during validation. No read-side query beats that, so the guards
# are temporal and structural instead:
#   - alert re-checks once after a grace sleep before creating, which covers
#     the observed seconds-scale lag window;
#   - recover closes EVERY open bench-alert issue it can see, in two passes
#     with the same grace sleep between them, so any duplicate that still
#     slips through heals on the next green night instead of accumulating.
# In production the writers (nightly notify, 6-hourly watchdog) are minutes
# to hours apart, far outside the lag window; the sleeps only ever cost the
# create path a few seconds.

set -euo pipefail

LABEL="${BENCH_ALERT_LABEL:-bench-alert}"
# Grace window for list-after-write consistency. Measured lag is 1-5s; 10s
# gives margin without meaningfully slowing an alert.
RECHECK_SECONDS="${BENCH_ALERT_RECHECK_SECONDS:-10}"

usage() {
    echo "usage: $0 alert <title> <body-file> | $0 recover <body-file>" >&2
    exit 2
}

[ $# -ge 1 ] || usage
mode=$1
shift

open_issues() {
    # REST, not `gh issue list` -- see header. The issues endpoint returns
    # pull requests too (they are issues underneath); filter them out.
    gh api "repos/${GH_REPO}/issues?labels=${LABEL}&state=open&per_page=20" \
        --jq '.[] | select(.pull_request | not) | .number'
}

pin_mutation() {
    # $1 = pinIssue | unpinIssue, $2 = issue number. Best-effort by contract:
    # pinning needs more than issues:write on some token shapes.
    node_id=$(gh api "repos/${GH_REPO}/issues/$2" --jq .node_id)
    gh api graphql \
        -f query="mutation(\$id: ID!) { $1(input: {issueId: \$id}) { issue { number } } }" \
        -f id="$node_id" >/dev/null 2>&1
}

case "$mode" in
    alert)
        [ $# -eq 2 ] || usage
        title=$1
        body_file=$2
        [ -f "$body_file" ] || { echo "no such body file: $body_file" >&2; exit 2; }
        existing=$(open_issues | head -1)
        if [ -z "$existing" ]; then
            # An empty read may be lag, not absence: a writer that created
            # the issue seconds ago is invisible to the list endpoints for a
            # few seconds (see header). Re-check once before creating.
            sleep "$RECHECK_SECONDS"
            existing=$(open_issues | head -1)
        fi
        if [ -n "$existing" ]; then
            gh issue comment "$existing" --repo "$GH_REPO" --body-file "$body_file"
            echo "commented on open ${LABEL} issue #${existing}"
        else
            url=$(gh issue create --repo "$GH_REPO" --title "$title" \
                --body-file "$body_file" --label "$LABEL")
            echo "created ${url}"
            number=${url##*/}
            pin_mutation pinIssue "$number" \
                && echo "pinned #${number}" \
                || echo "could not pin #${number} (non-fatal)"
        fi
        ;;
    recover)
        [ $# -eq 1 ] || usage
        body_file=$1
        [ -f "$body_file" ] || { echo "no such body file: $body_file" >&2; exit 2; }
        # Two passes with a grace sleep between them: a just-created
        # duplicate can be invisible to the first read (see header).
        closed_any=""
        for pass in 1 2; do
            numbers=$(open_issues)
            for number in $numbers; do
                gh issue comment "$number" --repo "$GH_REPO" --body-file "$body_file"
                pin_mutation unpinIssue "$number" || true
                gh issue close "$number" --repo "$GH_REPO"
                echo "closed ${LABEL} issue #${number}"
                closed_any=1
            done
            if [ "$pass" -eq 1 ]; then
                sleep "$RECHECK_SECONDS"
            fi
        done
        if [ -z "$closed_any" ]; then
            echo "no open ${LABEL} issue; nothing to close"
        fi
        ;;
    *)
        usage
        ;;
esac
