#!/usr/bin/env bash
#
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
#
# Hardware smoke test for the auto-lock feature.
#
# Exercises the 11 scenarios laid out in the test plan against a real
# Lager box: server endpoints, CLI auto-acquire/release, collision in
# dev vs CI, heartbeat, TTL reap, Ctrl+C, SIGKILL fallback to TTL,
# --detach retention, and pre-existing user lock pass-through.
#
# Usage:
#     test/manual/hardware_lock_smoke.sh <BOX_NAME_OR_IP> [--user USER] [--password]
#
# Defaults:
#   - BOX_SSH_USER  = third column of `lager boxes` for this box, else
#                     lagerdata
#   - SSH key       = ~/.ssh/lager_box if it exists (the key the lager
#                     CLI provisions), else your default agent/key
#   - Pass --password (or LAGER_BOX_SSH_PASS=1) to drop BatchMode and
#     use interactive password auth.
#
# Env vars (override transport without flags):
#   LAGER_BOX_SSH_USER   default SSH user
#   LAGER_BOX_SSH_KEY    private key path (default: ~/.ssh/lager_box)
#   LAGER_BOX_SSH_PASS   "1" -> interactive password auth
#
# Safety:
#   1. Refuses to run if the box is currently locked by a non-test
#      holder. (You can override with `SMOKE_FORCE=1`.)
#   2. Snapshots /etc/lager to a tarball on the box BEFORE the test and
#      restores it after, even if the test fails.
#   3. Final teardown also: docker restart lager, /cache/clear, and
#      kill any leftover python processes.
#
# Each test prints PASS/FAIL on a single line and the script exits
# non-zero on any failure.

set -u
set -o pipefail

# ----------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------

if [ $# -lt 1 ]; then
    cat <<EOF >&2
Usage: $0 <BOX_NAME_OR_IP> [--user USER] [--password]

  BOX_NAME_OR_IP   The box name (resolved via 'lager boxes') or raw IP.
  --user USER      Override SSH user (default: stored / lagerdata).
  --password       Use interactive password auth (no BatchMode).

Env vars:
  SMOKE_FORCE=1         Skip "box is locked by another user" pre-flight.
  SMOKE_KEEP=1          Skip the restore step (useful for post-mortem).
  SMOKE_VERBOSE=1       Echo every command before running.
  LAGER_BOX_SSH_USER    Default SSH user.
  LAGER_BOX_SSH_KEY     Private key path (default: ~/.ssh/lager_box).
  LAGER_BOX_SSH_PASS=1  Same as --password.
EOF
    exit 1
fi

BOX="$1"
shift

BOX_SSH_USER="${LAGER_BOX_SSH_USER:-}"
USE_PASSWORD="${LAGER_BOX_SSH_PASS:-0}"
SMOKE_FORCE="${SMOKE_FORCE:-0}"
SMOKE_KEEP="${SMOKE_KEEP:-0}"
SMOKE_VERBOSE="${SMOKE_VERBOSE:-0}"

while [ $# -gt 0 ]; do
    case "$1" in
        --user)    BOX_SSH_USER="$2"; shift 2 ;;
        --user=*)  BOX_SSH_USER="${1#*=}"; shift ;;
        --password|-p) USE_PASSWORD=1; shift ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

if [ "$SMOKE_VERBOSE" = "1" ]; then
    set -x
fi

# Colors — force the variables to contain *real* ESC bytes (via $'...')
# rather than the literal backslash-escape strings in framework/colors.sh,
# because this script prints them via `printf '%s'` (which does NOT
# interpret backslash escapes). Without this, you'd see `\033[0m` in
# the output instead of color.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GREEN=$'\033[0;32m'
RED=$'\033[0;31m'
YELLOW=$'\033[1;33m'
NC=$'\033[0m'

# ----------------------------------------------------------------------
# Globals
# ----------------------------------------------------------------------

SMOKE_RUN_ID="smoke-$(date +%s)-$RANDOM"
TEST_HOLDER="test-holder:$SMOKE_RUN_ID"
TEST_HOLDER_ALT="test-holder-alt:$SMOKE_RUN_ID"
LOCAL_TMP="$(mktemp -d -t lager_lock_smoke.XXXXXX)"
BOX_TMP_TAR="/tmp/lager_etc_snapshot_${SMOKE_RUN_ID}.tar.gz"
SLEEP_SCRIPT="${LOCAL_TMP}/sleep.py"

TESTS_TOTAL=0
TESTS_PASSED=0
TESTS_FAILED=0
FAILED_NAMES=()

# Resolve BOX -> (IP, user). Tolerates names and raw IPs. Adopts the
# third column of `lager boxes` as the SSH user when --user / env var
# weren't supplied (matches the deploy script's behavior).
if [[ "$BOX" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    BOX_IP="$BOX"
else
    if ! command -v lager >/dev/null 2>&1; then
        echo "ERROR: '$BOX' is not an IP and 'lager' CLI is not on PATH." >&2
        exit 1
    fi
    BOX_LINE="$(lager boxes 2>/dev/null | awk -v n="$BOX" '$1==n {print; exit}')"
    if [ -z "$BOX_LINE" ]; then
        echo "Could not resolve '$BOX' to an IP. Add it with:" >&2
        echo "  lager boxes add --name $BOX --ip <IP> --user <ssh-user>" >&2
        exit 1
    fi
    BOX_IP="$(echo "$BOX_LINE" | awk '{print $2}')"
    if [ -z "$BOX_SSH_USER" ]; then
        col3="$(echo "$BOX_LINE" | awk '{print $3}')"
        if [ -n "$col3" ] && [[ ! "$col3" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            BOX_SSH_USER="$col3"
        fi
    fi
fi
BOX_SSH_USER="${BOX_SSH_USER:-lagerdata}"
BOX_SSH="${BOX_SSH_USER}@${BOX_IP}"
LOCK_URL="http://${BOX_IP}:5000/lock"

# SSH options — same pattern as test/manual/deploy_branch_to_tester.sh.
# SSH_OPTS for non-sudo (BatchMode=yes, fail fast on auth errors).
# SSH_OPTS_SUDO drops BatchMode and forces a TTY so sudo can prompt.
LAGER_KEY="${LAGER_BOX_SSH_KEY:-$HOME/.ssh/lager_box}"
SSH_OPTS=( -o ConnectTimeout=10 )
SSH_OPTS_SUDO=( -tt -o ConnectTimeout=10 )
if [ "$USE_PASSWORD" = "1" ]; then
    for arr in SSH_OPTS SSH_OPTS_SUDO; do
        eval "$arr+=( -o NumberOfPasswordPrompts=3 -o PreferredAuthentications=password,keyboard-interactive )"
    done
else
    SSH_OPTS+=( -o BatchMode=yes )
    if [ -f "$LAGER_KEY" ]; then
        SSH_OPTS+=( -i "$LAGER_KEY" )
        SSH_OPTS_SUDO+=( -i "$LAGER_KEY" )
    fi
fi

_ssh_auth_summary() {
    if [ "$USE_PASSWORD" = "1" ]; then
        echo "password (interactive)"
    elif [ -f "$LAGER_KEY" ]; then
        echo "key ($LAGER_KEY)"
    else
        echo "default agent / keys"
    fi
}

cat <<EOF
========================================================================
LAGER AUTO-LOCK HARDWARE SMOKE TEST
========================================================================
Box:           $BOX ($BOX_IP)
SSH user:      $BOX_SSH_USER
SSH auth:      $(_ssh_auth_summary)
Test holder:   $TEST_HOLDER
Local tmpdir:  $LOCAL_TMP
Box snapshot:  $BOX_TMP_TAR
EOF

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

ssh_box() {
    ssh "${SSH_OPTS[@]}" "$BOX_SSH" "$@"
}

# For commands that need sudo on the box. Combines multiple operations
# into one ssh -tt session so sudo's credential cache gives at most one
# password prompt per call.
ssh_box_sudo() {
    ssh "${SSH_OPTS_SUDO[@]}" "$BOX_SSH" "$@"
}

curl_box() {
    # Always JSON, always silent, always tolerate failures so we can
    # decide ourselves what's pass/fail.
    curl -s --max-time 10 -H 'Content-Type: application/json' "$@"
}

post_lock() {
    curl_box -X POST "$LOCK_URL" -d "$1"
}

post_heartbeat() {
    curl_box -X POST "${LOCK_URL}/heartbeat" -d "$1"
}

post_unlock() {
    curl_box -X POST "http://${BOX_IP}:5000/unlock" -d "$1"
}

get_lock() {
    curl_box "$LOCK_URL"
}

# Returns 0 if jq is on PATH, 1 otherwise.
have_jq() { command -v jq >/dev/null 2>&1; }

# Pretty-print JSON if jq available, else raw.
pp_json() {
    if have_jq; then
        jq -c .
    else
        cat
    fi
}

# JSON key extractor: extract_field <key> from stdin. Uses jq if
# available, else a simple grep+sed (good enough for our flat objects).
extract_field() {
    local key="$1"
    if have_jq; then
        # `.locked // empty` is WRONG: jq's `//` operator treats both
        # null AND false as "missing", so `extract_field locked` on a
        # `{"locked": false}` body returned "" instead of "false". This
        # masked half of the smoke failures (Tests 1, 3, 4, 8, 9 in the
        # 2026-06-10 run). Use has() + tostring so:
        #   - field absent       -> ""
        #   - field present null -> ""  (preserves the legacy ttl=null
        #                                check; both expected and actual
        #                                are "" so they still compare)
        #   - false              -> "false"
        #   - true               -> "true"
        #   - 1800               -> "1800"
        #   - "ci"               -> "ci"
        jq -r --arg k "$key" \
            'if has($k) and .[$k] != null then (.[$k] | tostring) else "" end'
    else
        grep -oE "\"$key\"[[:space:]]*:[[:space:]]*(\"[^\"]*\"|true|false|null|[0-9]+)" \
            | head -1 \
            | sed -E "s/^.*:[[:space:]]*//" \
            | sed -E 's/^"//; s/"$//' \
            | sed -E 's/^null$//'
    fi
}

# Assert that two strings are equal; mark a test pass/fail.
assert_eq() {
    local name="$1" actual="$2" expected="$3"
    TESTS_TOTAL=$((TESTS_TOTAL + 1))
    if [ "$actual" = "$expected" ]; then
        printf '  %s[PASS]%s %s\n' "$GREEN" "$NC" "$name"
        TESTS_PASSED=$((TESTS_PASSED + 1))
    else
        printf '  %s[FAIL]%s %s\n' "$RED" "$NC" "$name"
        printf '         expected: %q\n         actual:   %q\n' "$expected" "$actual"
        TESTS_FAILED=$((TESTS_FAILED + 1))
        FAILED_NAMES+=("$name")
    fi
}

# Assert that the predicate succeeds (exits 0). predicate is a bash
# expression evaluated with `eval`.
assert_true() {
    local name="$1" expr="$2"
    TESTS_TOTAL=$((TESTS_TOTAL + 1))
    if eval "$expr" >/dev/null 2>&1; then
        printf '  %s[PASS]%s %s\n' "$GREEN" "$NC" "$name"
        TESTS_PASSED=$((TESTS_PASSED + 1))
    else
        printf '  %s[FAIL]%s %s\n' "$RED" "$NC" "$name"
        printf '         predicate: %s\n' "$expr"
        TESTS_FAILED=$((TESTS_FAILED + 1))
        FAILED_NAMES+=("$name")
    fi
}

section() {
    printf '\n%s========================================================================\n' "$NC"
    printf '%s %s\n' "$NC" "$1"
    printf '========================================================================%s\n' "$NC"
}

# ----------------------------------------------------------------------
# Pre-flight + backup
# ----------------------------------------------------------------------

preflight() {
    section "PRE-FLIGHT"

    # 1. Box reachable via SSH (needed for backup/restore)?
    if ! ssh_box 'echo ok' >/dev/null 2>&1; then
        printf '%s[FAIL]%s SSH to %s failed; cannot back up /etc/lager.\n' \
            "$RED" "$NC" "$BOX_SSH" >&2
        cat >&2 <<EOF
       Tried key: $LAGER_KEY (exists: $([ -f "$LAGER_KEY" ] && echo yes || echo no))
       Tried opts: ${SSH_OPTS[*]}

       Fix one of:
         1. Put the lager-provisioned key at $LAGER_KEY
         2. Authorize your default key: ssh-copy-id $BOX_SSH
         3. Use password auth: $0 $BOX --password
         4. Different SSH user: $0 $BOX --user <user>
       Or skip the snapshot entirely with SMOKE_KEEP=1.
EOF
        exit 1
    fi
    echo "  SSH to $BOX_SSH ok"

    # 2. HTTP server reachable?
    if ! get_lock >/dev/null; then
        printf '%s[FAIL]%s HTTP GET %s failed; is the box container up?\n' \
            "$RED" "$NC" "$LOCK_URL" >&2
        exit 1
    fi
    echo "  HTTP $LOCK_URL ok"

    # 3. Refuse to run if the box is currently held by something that
    #    isn't us (avoid stomping a real user's debug session).
    local current
    current="$(get_lock | extract_field user || true)"
    if [ -n "$current" ] && [[ "$current" != test-holder:* ]] \
        && [[ "$current" != "$TEST_HOLDER" ]] \
        && [[ "$current" != "$TEST_HOLDER_ALT" ]]; then
        if [ "$SMOKE_FORCE" = "1" ]; then
            printf '%s[WARN]%s Box is locked by %q (SMOKE_FORCE=1 — continuing)\n' \
                "$YELLOW" "$NC" "$current"
        else
            printf '%s[FAIL]%s Box is currently locked by %q.\n' \
                "$RED" "$NC" "$current" >&2
            echo "       Re-run with SMOKE_FORCE=1 to override (this will overwrite the lock)." >&2
            exit 2
        fi
    fi
    echo "  Box not currently locked (or only by a previous smoke run)"
}

backup_etc_lager() {
    section "BACKUP /etc/lager"
    echo "  (you may be prompted for the sudo password on the box)"
    # Snapshot config dir on the box (NOT pulling locally — faster and
    # the data never has to leave the box). tar respects /etc/lager
    # being root-owned via sudo. ssh_box_sudo allocates a TTY so sudo
    # can prompt; sudo's credential cache covers the restore at exit.
    if ssh_box_sudo "sudo tar czf '${BOX_TMP_TAR}' -C /etc lager 2>/dev/null"; then
        echo "  Snapshotted /etc/lager -> $BOX_TMP_TAR"
    else
        printf '%s[WARN]%s Could not snapshot /etc/lager (continuing without backup)\n' \
            "$YELLOW" "$NC" >&2
    fi
}

restore_etc_lager() {
    section "RESTORE /etc/lager"
    if [ "$SMOKE_KEEP" = "1" ]; then
        echo "  SMOKE_KEEP=1 — leaving box state as-is (snapshot remains at $BOX_TMP_TAR)"
        return
    fi

    # If we never set up SSH (e.g., pre-flight bailed before backup),
    # don't pester the user for a sudo password just to fail again.
    if ! ssh_box 'echo ok' >/dev/null 2>&1; then
        printf '%s[WARN]%s SSH not available; skipping restore\n' "$YELLOW" "$NC"
        return
    fi

    # 1. Check whether the snapshot exists before opening a sudo session.
    if ! ssh_box "test -f '${BOX_TMP_TAR}'" 2>/dev/null; then
        printf '%s[WARN]%s No snapshot at %s — skipping restore\n' \
            "$YELLOW" "$NC" "$BOX_TMP_TAR"
        return
    fi

    echo "  (you may be prompted for the sudo password on the box)"
    # 2. Stop -> wipe -> restore -> start, all in one sudo session.
    if ! ssh_box_sudo "
        sudo docker stop lager >/dev/null 2>&1 || true
        sudo rm -rf /etc/lager
        sudo tar xzf '${BOX_TMP_TAR}' -C /etc
        sudo docker start lager >/dev/null 2>&1 || true
        sudo rm -f '${BOX_TMP_TAR}'
    "; then
        printf '%s[WARN]%s Restore failed; /etc/lager may be inconsistent\n' \
            "$YELLOW" "$NC" >&2
    else
        echo "  Restored /etc/lager from $BOX_TMP_TAR"
    fi

    # 3. Wait for the HTTP server to be reachable again.
    local attempts=0
    while ! get_lock >/dev/null 2>&1; do
        attempts=$((attempts + 1))
        if [ "$attempts" -gt 30 ]; then
            printf '%s[WARN]%s Box container did not come back within 30s\n' \
                "$YELLOW" "$NC" >&2
            break
        fi
        sleep 1
    done
    echo "  Box container is back ($attempts s)"

    # 5. Best-effort hardware cache clear (drops any leftover VISA
    #    sessions a test may have opened).
    curl -s -m 5 -X POST "http://${BOX_IP}:8080/cache/clear" >/dev/null 2>&1 \
        && echo "  /cache/clear OK" \
        || echo "  /cache/clear skipped (hardware_service may not be up yet)"

    # 6. Kill any leftover python processes from the smoke test.
    lager python --kill-all --box "$BOX" >/dev/null 2>&1 || true
}

# Even on Ctrl+C / failure, restore. Always remove local tmpdir.
cleanup() {
    local rc=$?
    set +e
    restore_etc_lager
    rm -rf "$LOCAL_TMP"
    exit "$rc"
}
trap cleanup EXIT INT TERM

# ----------------------------------------------------------------------
# Test fixtures (test scripts uploaded to the box via `lager python`)
# ----------------------------------------------------------------------

cat > "$SLEEP_SCRIPT" <<'PY'
import os, sys, time
seconds = int(os.environ.get("LAGER_SMOKE_SLEEP", "30"))
print(f"smoke: sleeping {seconds}s", flush=True)
for i in range(seconds):
    time.sleep(1)
    if i % 5 == 0:
        print(f"smoke: tick {i}", flush=True)
print("smoke: done")
PY

# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------

test_01_server_roundtrip() {
    section "1. Server round-trip on the new endpoints"

    # Reset state
    post_unlock "{\"user\":\"$TEST_HOLDER\",\"force\":true}" >/dev/null

    local acquire_resp
    acquire_resp="$(post_lock "{\"user\":\"$TEST_HOLDER\",\"holder_type\":\"ci\",\"ttl_seconds\":10}")"
    assert_eq "POST /lock acquires" \
        "$(echo "$acquire_resp" | extract_field user)" "$TEST_HOLDER"
    assert_eq "Returns holder_type=ci" \
        "$(echo "$acquire_resp" | extract_field holder_type)" "ci"
    assert_eq "Returns ttl_seconds=10" \
        "$(echo "$acquire_resp" | extract_field ttl_seconds)" "10"

    local hb_resp
    hb_resp="$(post_heartbeat "{\"user\":\"$TEST_HOLDER\"}")"
    assert_eq "POST /lock/heartbeat refreshes (user echoes back)" \
        "$(echo "$hb_resp" | extract_field user)" "$TEST_HOLDER"

    echo "  Waiting 15s for TTL auto-reap..."
    sleep 15
    local after_ttl
    after_ttl="$(get_lock | extract_field locked)"
    assert_eq "Lock auto-reaped after TTL+grace" "$after_ttl" "false"
}

test_02_legacy_payload_compat() {
    section "2. Backward compat — legacy payload is eternal user lock"

    post_unlock "{\"user\":\"$TEST_HOLDER\",\"force\":true}" >/dev/null
    local resp
    # Only "user" — no holder_type, no ttl_seconds. This is what an
    # older `lager boxes lock` client emits.
    resp="$(post_lock "{\"user\":\"$TEST_HOLDER\"}")"
    assert_eq "Legacy payload sets holder_type=user" \
        "$(echo "$resp" | extract_field holder_type)" "user"
    assert_eq "Legacy payload sets ttl_seconds=null" \
        "$(echo "$resp" | extract_field ttl_seconds)" ""
    post_unlock "{\"user\":\"$TEST_HOLDER\"}" >/dev/null
}

test_03_boxes_lock_unlock_cli() {
    section "3. CLI: lager boxes lock/unlock still works"

    post_unlock "{\"user\":\"$TEST_HOLDER\",\"force\":true}" >/dev/null
    if lager boxes lock --box "$BOX" --user "$TEST_HOLDER" >/dev/null 2>&1; then
        assert_eq "lager boxes lock works" "0" "0"
    else
        assert_eq "lager boxes lock works" "1" "0"
    fi
    local locked_user
    locked_user="$(get_lock | extract_field user)"
    assert_eq "Lock holder matches" "$locked_user" "$TEST_HOLDER"
    lager boxes unlock --box "$BOX" --force >/dev/null 2>&1
    local final_locked
    final_locked="$(get_lock | extract_field locked)"
    assert_eq "Box unlocked after lager boxes unlock" "$final_locked" "false"
}

test_04_auto_lock_around_lager_python() {
    section "4. Auto-lock around lager python <script>"

    post_unlock "{\"user\":\"$TEST_HOLDER\",\"force\":true}" >/dev/null

    # Run a short script in the background so we can probe the box mid-run.
    LAGER_LOCK_HOLDER="$TEST_HOLDER" \
        LAGER_SMOKE_SLEEP=15 \
        lager python "$SLEEP_SCRIPT" --box "$BOX" >"${LOCAL_TMP}/run1.log" 2>&1 &
    local pid=$!
    sleep 5
    local locked_user
    locked_user="$(get_lock | extract_field user)"
    assert_eq "lager python auto-acquires lock" "$locked_user" "$TEST_HOLDER"

    wait $pid || true
    sleep 1
    local final_locked
    final_locked="$(get_lock | extract_field locked)"
    assert_eq "lager python releases lock on clean exit" "$final_locked" "false"
}

test_05_dev_fail_fast_on_collision() {
    section "5. Dev fail-fast on collision"

    # Put a lock owned by someone else.
    post_unlock "{\"user\":\"$TEST_HOLDER\",\"force\":true}" >/dev/null
    post_lock "{\"user\":\"$TEST_HOLDER_ALT\",\"holder_type\":\"user\",\"ttl_seconds\":null}" >/dev/null

    # `lager python` takes a file path positionally, NOT a -c flag.
    # Use the same sleep fixture but with LAGER_SMOKE_SLEEP=1 so the
    # script is quick if it ever runs (it shouldn't — we expect the
    # lock acquire to fail before script upload).
    local start=$SECONDS
    LAGER_LOCK_HOLDER="$TEST_HOLDER" \
        LAGER_LOCK_WAIT=0 \
        LAGER_SMOKE_SLEEP=1 \
        lager python "$SLEEP_SCRIPT" --box "$BOX" >"${LOCAL_TMP}/run5.log" 2>&1
    local rc=$?
    local elapsed=$((SECONDS - start))

    assert_eq "exit 1 on locked-by-other" "$rc" "1"
    assert_true "fails within 5s (no waiting)" "[ $elapsed -lt 5 ]"

    # Clean up
    post_unlock "{\"user\":\"$TEST_HOLDER_ALT\",\"force\":true}" >/dev/null
}

test_06_ci_wait_then_acquire() {
    section "6. CI: wait-then-acquire after holder releases"

    post_unlock "{\"user\":\"$TEST_HOLDER\",\"force\":true}" >/dev/null
    post_lock "{\"user\":\"$TEST_HOLDER_ALT\",\"holder_type\":\"user\",\"ttl_seconds\":null}" >/dev/null

    # Release the conflicting lock after 8s, in parallel.
    ( sleep 8 && post_unlock "{\"user\":\"$TEST_HOLDER_ALT\",\"force\":true}" >/dev/null ) &
    local releaser=$!

    local start=$SECONDS
    LAGER_LOCK_HOLDER="$TEST_HOLDER" \
        LAGER_LOCK_WAIT=60 \
        LAGER_SMOKE_SLEEP=1 \
        lager python "$SLEEP_SCRIPT" --box "$BOX" >"${LOCAL_TMP}/run6.log" 2>&1
    local rc=$?
    local elapsed=$((SECONDS - start))
    wait $releaser 2>/dev/null || true

    assert_eq "exit 0 after waiting and acquiring" "$rc" "0"
    assert_true "waited at least 7s (queue worked)" "[ $elapsed -ge 7 ]"
    # Upper bound is LAGER_LOCK_WAIT (60s). If we'd actually hit the
    # wait timeout, rc would be 1 (acquire_box_lock raises SystemExit(1)
    # after the deadline) and the exit-0 assertion above would already
    # have failed.
    #
    # The 2026-06-10 smoke had this bound at <20s, which was unreachable
    # because the test's elapsed = (queue wait) + (lager python end-to-
    # end overhead, ~25-30s for script upload, container exec, result
    # streaming, teardown). Observed breakdown via parallel-observer
    # instrumentation: queue wait 9s, script execution 30s, total 39s.
    assert_true "didn't time out (under LAGER_LOCK_WAIT=60s)" "[ $elapsed -lt 60 ]"
}

test_07_heartbeat_survives_one_ttl() {
    section "7. Heartbeat keeps lock alive past one TTL"

    post_unlock "{\"user\":\"$TEST_HOLDER\",\"force\":true}" >/dev/null

    LAGER_LOCK_HOLDER="$TEST_HOLDER" \
        LAGER_LOCK_TTL=10 \
        LAGER_LOCK_HEARTBEAT=3 \
        LAGER_SMOKE_SLEEP=25 \
        lager python "$SLEEP_SCRIPT" --box "$BOX" >"${LOCAL_TMP}/run7.log" 2>&1 &
    local pid=$!

    # After ~12s the original lock would have expired without heartbeats;
    # with heartbeats it must still be alive.
    sleep 12
    local locked_user
    locked_user="$(get_lock | extract_field user)"
    assert_eq "Lock still held past one TTL thanks to heartbeat" \
        "$locked_user" "$TEST_HOLDER"

    wait $pid || true
}

test_08_ctrl_c_releases_lock() {
    section "8. Ctrl+C releases the lock"

    post_unlock "{\"user\":\"$TEST_HOLDER\",\"force\":true}" >/dev/null

    LAGER_LOCK_HOLDER="$TEST_HOLDER" \
        LAGER_SMOKE_SLEEP=60 \
        lager python "$SLEEP_SCRIPT" --box "$BOX" >"${LOCAL_TMP}/run8.log" 2>&1 &
    local pid=$!
    sleep 4
    # SIGINT mimics Ctrl+C; the python module's sigint_handler should
    # release the lock before exit.
    kill -INT $pid
    wait $pid || true
    sleep 2

    local locked
    locked="$(get_lock | extract_field locked)"
    assert_eq "Lock released after SIGINT" "$locked" "false"
}

test_09_sigkill_falls_back_to_ttl() {
    section "9. SIGKILL falls back to TTL reap"

    post_unlock "{\"user\":\"$TEST_HOLDER\",\"force\":true}" >/dev/null

    LAGER_LOCK_HOLDER="$TEST_HOLDER" \
        LAGER_LOCK_TTL=12 \
        LAGER_LOCK_HEARTBEAT=4 \
        LAGER_SMOKE_SLEEP=60 \
        lager python "$SLEEP_SCRIPT" --box "$BOX" >"${LOCAL_TMP}/run9.log" 2>&1 &
    local pid=$!
    sleep 3
    kill -9 $pid 2>/dev/null || true
    wait $pid 2>/dev/null || true

    # SIGKILL bypassed every CLI cleanup; lock should still be held.
    local locked_now
    locked_now="$(get_lock | extract_field locked)"
    assert_eq "Lock survives SIGKILL (only TTL can reap)" "$locked_now" "true"

    # Wait out the TTL.
    echo "  Waiting 15s for TTL to reap stale lock..."
    sleep 15
    local locked_after_ttl
    locked_after_ttl="$(get_lock | extract_field locked)"
    assert_eq "Lock auto-reaped after TTL elapsed" "$locked_after_ttl" "false"
}

test_10_detach_keeps_lock() {
    section "10. --detach keeps the lock (no TTL)"

    post_unlock "{\"user\":\"$TEST_HOLDER\",\"force\":true}" >/dev/null

    LAGER_LOCK_HOLDER="$TEST_HOLDER" \
        LAGER_SMOKE_SLEEP=20 \
        lager python "$SLEEP_SCRIPT" --box "$BOX" --detach >"${LOCAL_TMP}/run10.log" 2>&1 || true

    sleep 2
    local locked_user ttl_after
    locked_user="$(get_lock | extract_field user)"
    ttl_after="$(get_lock | extract_field ttl_seconds)"
    assert_eq "Detached run holds lock as $TEST_HOLDER" "$locked_user" "$TEST_HOLDER"
    assert_eq "Detached lock has null TTL (eternal)" "$ttl_after" ""

    # Manual cleanup
    lager boxes unlock --box "$BOX" --force >/dev/null 2>&1
    lager python --kill-all --box "$BOX" >/dev/null 2>&1 || true
}

test_11_user_lock_passthrough() {
    section "11. Pre-existing user lock is not released by lager python"

    post_unlock "{\"user\":\"$TEST_HOLDER\",\"force\":true}" >/dev/null

    # User pre-locks the box explicitly.
    post_lock "{\"user\":\"$TEST_HOLDER\",\"holder_type\":\"user\",\"ttl_seconds\":null}" >/dev/null

    # A real runnable, and we assert the run actually succeeded. (The
    # previous version of this scenario invoked `lager python -c ...`,
    # which is not a valid invocation — Click exited 2 before any lock
    # traffic, so the assertions below passed against a lock that was
    # never exercised.)
    printf 'print("ok")\n' > "${LOCAL_TMP}/run11.py"
    LAGER_LOCK_HOLDER="$TEST_HOLDER" \
        lager python "${LOCAL_TMP}/run11.py" --box "$BOX" >"${LOCAL_TMP}/run11.log" 2>&1
    assert_eq "lager python run exits 0" "$?" "0"

    # After lager python exits, the user lock must still be held (we saw
    # "already_ours" and did NOT release it), still classified as a user
    # lock, and still eternal — a re-acquire must not stamp a TTL on it,
    # or the reservation silently expires ~30 minutes later.
    sleep 1
    local locked
    locked="$(get_lock | extract_field locked)"
    assert_eq "User lock survives lager python exit" "$locked" "true"
    local holder_type
    holder_type="$(get_lock | extract_field holder_type)"
    assert_eq "Lock still holder_type=user" "$holder_type" "user"
    local ttl
    ttl="$(get_lock | extract_field ttl_seconds)"
    # extract_field yields "" for json null under jq and "null" under the
    # grep fallback; normalize so both count as eternal.
    [ -z "$ttl" ] && ttl="null"
    assert_eq "Lock still eternal (ttl_seconds=null)" "$ttl" "null"

    # Clean up
    post_unlock "{\"user\":\"$TEST_HOLDER\"}" >/dev/null
}

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

preflight
backup_etc_lager

test_01_server_roundtrip
test_02_legacy_payload_compat
test_03_boxes_lock_unlock_cli
test_04_auto_lock_around_lager_python
test_05_dev_fail_fast_on_collision
test_06_ci_wait_then_acquire
test_07_heartbeat_survives_one_ttl
test_08_ctrl_c_releases_lock
test_09_sigkill_falls_back_to_ttl
test_10_detach_keeps_lock
test_11_user_lock_passthrough

# Summary (the trap still runs after this, so the restore output prints
# AFTER the summary — that's intentional so the user sees the totals
# without scrolling past restore noise).
section "SUMMARY"
printf '  Total:  %d\n' "$TESTS_TOTAL"
printf '  %sPassed: %d%s\n' "$GREEN" "$TESTS_PASSED" "$NC"
printf '  %sFailed: %d%s\n' "$RED" "$TESTS_FAILED" "$NC"
if [ "$TESTS_FAILED" -gt 0 ]; then
    echo
    echo "Failed tests:"
    for name in "${FAILED_NAMES[@]}"; do
        echo "  - $name"
    done
    exit 1
fi
exit 0
