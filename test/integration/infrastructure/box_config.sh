#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# Smoke test for `lager box config` against a real box.
# ============================================================================
#
# Verifies:
#   1. No box_config.json -> container starts with the same mounts as before.
#   2. With box_config.json adding a mount -> auto-prep creates the host
#      directory and chowns it to uid 33; the mount is visible in container.
#   3. apply is idempotent (second apply is a no-op).
#   4. Auto-prep refuses to recursively chown a populated wrong-owner dir
#      unless --recursive-chown is passed.
#   5. Cleanup restores prior state.
#   6. Invalid config (mount with host="/") is rejected.
#
# Requires SSH access to the box as the 'lagerdata' user.
#
# USAGE:
#   ./box_config.sh <BOX>
#
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e

init_harness

if [ $# -lt 1 ]; then
  echo "Usage: $0 <BOX>"
  exit 1
fi

BOX="$1"

# Resolve box name -> IP for raw SSH commands. `lager` knows the mapping;
# `ssh` alone does not unless Tailscale MagicDNS or ~/.ssh/config is set up.
if echo "$BOX" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
    BOX_IP="$BOX"
else
    BOX_IP=$(lager boxes 2>/dev/null \
        | awk -v name="$BOX" '$1 == name { print $2; exit }')
    if [ -z "$BOX_IP" ]; then
        echo "Could not resolve box name '$BOX' to an IP via 'lager boxes'."
        echo "Pass the box's IP directly, or check 'lager boxes' output."
        exit 1
    fi
fi
SSH_HOST="lagerdata@${BOX_IP}"
echo "SSH target: $SSH_HOST"

echo "========================================================================"
echo "LAGER BOX CONFIG SMOKE TEST"
echo "========================================================================"
echo "Box: $BOX"
echo ""

# ------------------------------------------------------------
start_section "Baseline (no config)"
# ------------------------------------------------------------

echo "Test 1.1: Remove any existing box_config.json"
ssh "$SSH_HOST" 'rm -f /etc/lager/box_config.json /etc/lager/box_config.applied_hash /etc/lager/box_config.applied.json' 2>&1 \
  && track_test "pass" || track_test "fail"

echo "Test 1.2: Restart container without config"
ssh "$SSH_HOST" 'cd ~/box && ./start_box.sh' >/dev/null 2>&1 \
  && track_test "pass" || track_test "fail"

echo "Test 1.3: Box responds after restart"
sleep 5
lager hello --box "$BOX" 2>&1 | grep -qi "online" \
  && track_test "pass" || track_test "fail"

echo "Test 1.4: lager box config show reports no config"
lager box config show --box "$BOX" 2>&1 | grep -qi "no box_config" \
  && track_test "pass" || track_test "fail"

# Test mount paths. Use fresh paths under /tmp so auto-prep can mkdir/chown
# safely without touching /tmp itself or any pre-existing host content.
MOUNT_HOST_PATH="/tmp/lager_test_mount"
MOUNT_POPULATED_PATH="/tmp/lager_test_populated"

# Pre-clean any leftover state from prior runs (auto-prep rejects populated
# wrong-owner dirs, so a stale path would mask real failures).
ssh "$SSH_HOST" "sudo rm -rf '$MOUNT_HOST_PATH' '$MOUNT_POPULATED_PATH'" >/dev/null 2>&1

# ------------------------------------------------------------
start_section "Apply mount with auto-prep"
# ------------------------------------------------------------

echo "Test 2.1: Init default config"
lager box config init --box "$BOX" 2>&1 | grep -qi "created" \
  && track_test "pass" || track_test "fail"

echo "Test 2.2: Add ${MOUNT_HOST_PATH}:/host_tmp mount (path doesn't exist; auto-prep should create + chown)"
lager box config mount add "$MOUNT_HOST_PATH" /host_tmp --box "$BOX" 2>&1 | grep -qi "added mount" \
  && track_test "pass" || track_test "fail"

echo "Test 2.2a: Auto-prep created the host directory"
ssh "$SSH_HOST" "test -d '$MOUNT_HOST_PATH'" \
  && track_test "pass" || track_test "fail"

echo "Test 2.2b: Auto-prep chowned the host directory to uid 33:33"
OWNER=$(ssh "$SSH_HOST" "stat -c %u:%g '$MOUNT_HOST_PATH'" 2>/dev/null)
if [ "$OWNER" = "33:33" ]; then
  track_test "pass"
else
  echo "  Expected 33:33, got: $OWNER"
  track_test "fail"
fi

echo "Test 2.3: Validate"
lager box config validate --box "$BOX" 2>&1 | grep -qi "valid" \
  && track_test "pass" || track_test "fail"

echo "Test 2.4: Apply (restart container)"
lager box config apply --box "$BOX" --yes 2>&1 | grep -qi "applied" \
  && track_test "pass" || track_test "fail"

echo "Test 2.5: Container reports ${MOUNT_HOST_PATH}:/host_tmp mount"
sleep 5
ssh "$SSH_HOST" 'docker inspect lager --format "{{json .Mounts}}"' 2>/dev/null \
  | python3 -c "import json,sys; m=json.load(sys.stdin); sys.exit(0 if any(x.get('Source')=='$MOUNT_HOST_PATH' and x.get('Destination')=='/host_tmp' for x in m) else 1)" \
  && track_test "pass" || track_test "fail"

echo "Test 2.6: Container HOME aligns with /home/www-data mount convention"
HOME_VAL=$(ssh "$SSH_HOST" 'docker exec lager bash -c "echo \$HOME"' 2>/dev/null | tr -d '\r')
if [ "$HOME_VAL" = "/home/www-data" ]; then
  track_test "pass"
else
  echo "  Expected /home/www-data, got: $HOME_VAL"
  track_test "fail"
fi

echo "Test 2.7: Tilde expansion lands under the mount convention (e.g. ~/.cargo)"
TILDE_CARGO=$(ssh "$SSH_HOST" 'docker exec lager bash -c "echo ~/.cargo"' 2>/dev/null | tr -d '\r')
if [ "$TILDE_CARGO" = "/home/www-data/.cargo" ]; then
  track_test "pass"
else
  echo "  Expected /home/www-data/.cargo, got: $TILDE_CARGO"
  track_test "fail"
fi

# ------------------------------------------------------------
start_section "Idempotency"
# ------------------------------------------------------------

echo "Test 3.1: Second apply skips restart"
lager box config apply --box "$BOX" --yes 2>&1 | grep -qi "unchanged" \
  && track_test "pass" || track_test "fail"

# ------------------------------------------------------------
start_section "Auto-prep refuses populated wrong-owner dir"
# ------------------------------------------------------------

echo "Test 3.2: Pre-create ${MOUNT_POPULATED_PATH} owned by lagerdata with content"
ssh "$SSH_HOST" "mkdir -p '$MOUNT_POPULATED_PATH' && touch '$MOUNT_POPULATED_PATH/sentinel'" \
  && track_test "pass" || track_test "fail"

echo "Test 3.3: mount add (no --recursive-chown) refuses with hint"
OUT=$(lager box config mount add "$MOUNT_POPULATED_PATH" /host_populated --box "$BOX" 2>&1)
if echo "$OUT" | grep -qi -- "--recursive-chown"; then
  track_test "pass"
else
  echo "  Output: $OUT"
  track_test "fail"
fi

echo "Test 3.4: ${MOUNT_POPULATED_PATH} ownership unchanged (still NOT 33:33)"
OWNER=$(ssh "$SSH_HOST" "stat -c %u:%g '$MOUNT_POPULATED_PATH'" 2>/dev/null)
if [ "$OWNER" != "33:33" ]; then
  track_test "pass"
else
  echo "  Auto-prep should have refused; got owner $OWNER"
  track_test "fail"
fi

echo "Test 3.5: mount add --recursive-chown succeeds and flips ownership"
lager box config mount add "$MOUNT_POPULATED_PATH" /host_populated --recursive-chown --box "$BOX" 2>&1 \
  | grep -qi "added mount" \
  && track_test "pass" || track_test "fail"

OWNER=$(ssh "$SSH_HOST" "stat -c %u:%g '$MOUNT_POPULATED_PATH'" 2>/dev/null)
SENTINEL_OWNER=$(ssh "$SSH_HOST" "stat -c %u:%g '$MOUNT_POPULATED_PATH/sentinel'" 2>/dev/null)
if [ "$OWNER" = "33:33" ] && [ "$SENTINEL_OWNER" = "33:33" ]; then
  track_test "pass"
else
  echo "  Expected 33:33 dir + sentinel; got dir=$OWNER sentinel=$SENTINEL_OWNER"
  track_test "fail"
fi

# ------------------------------------------------------------
start_section "applied_hash persists after a real apply"
# ------------------------------------------------------------

# Issue 2: confirm that after `apply` succeeds, applied_hash actually equals
# the current config hash. Pre-PR, the set-applied-hash call raced the
# container restart and silently lost — every subsequent apply re-bounced.

echo "Test 3.6: Run a fresh apply to update applied_hash"
lager box config apply --box "$BOX" --yes --force >/dev/null 2>&1 \
  && track_test "pass" || track_test "fail"

echo "Test 3.7: Second apply prints 'unchanged' (applied_hash actually persisted)"
lager box config apply --box "$BOX" --yes 2>&1 | grep -qi "unchanged" \
  && track_test "pass" || track_test "fail"

# ------------------------------------------------------------
start_section "Reserved container path is rejected"
# ------------------------------------------------------------

# Issue 4: a user-defined mount that collides with a hard-coded mount in
# start_box.sh used to make the box headless mid-bounce. Now it's rejected
# at validation with a suggestion.

echo "Test 3.8: mount add /tmp -> /home/www-data/.ssh is rejected with suggestion"
OUT=$(lager box config mount add /tmp/lager_ssh_collide /home/www-data/.ssh --readonly --box "$BOX" 2>&1)
if echo "$OUT" | grep -qi "reserved by start_box.sh" \
   && echo "$OUT" | grep -qi "/home/www-data/.ssh-git"; then
  track_test "pass"
else
  echo "  Output: $OUT"
  track_test "fail"
fi

# ------------------------------------------------------------
start_section "mount add prep failure leaves JSON unchanged"
# ------------------------------------------------------------

# Issue 1: prep used to run AFTER the JSON was written, so a refused-populated
# prep left a mount entry orphaned in box_config.json that the duplicate-
# container validator then blocked on retry.

PREP_FAIL_PATH="/tmp/lager_test_prep_fail"
ssh "$SSH_HOST" "rm -rf '$PREP_FAIL_PATH' && mkdir -p '$PREP_FAIL_PATH' && touch '$PREP_FAIL_PATH/sentinel'" >/dev/null 2>&1

echo "Test 3.9: Snapshot mount count before prep-failure attempt"
BEFORE_COUNT=$(lager box config mount list --json --box "$BOX" 2>/dev/null | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
[ -n "$BEFORE_COUNT" ] && track_test "pass" || track_test "fail"

echo "Test 3.10: mount add (no --recursive-chown) on populated dir refuses"
OUT=$(lager box config mount add "$PREP_FAIL_PATH" /host_prep_fail --box "$BOX" 2>&1)
if echo "$OUT" | grep -qi "Mount NOT added"; then
  track_test "pass"
else
  echo "  Output: $OUT"
  track_test "fail"
fi

echo "Test 3.11: Mount count unchanged (JSON not written)"
AFTER_COUNT=$(lager box config mount list --json --box "$BOX" 2>/dev/null | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
if [ "$BEFORE_COUNT" = "$AFTER_COUNT" ]; then
  track_test "pass"
else
  echo "  Mount count went from $BEFORE_COUNT to $AFTER_COUNT — JSON was mutated despite prep failure"
  track_test "fail"
fi

ssh "$SSH_HOST" "sudo rm -rf '$PREP_FAIL_PATH'" >/dev/null 2>&1

# ------------------------------------------------------------
start_section "Cleanup"
# ------------------------------------------------------------

echo "Test 4.1: Remove both mounts"
lager box config mount remove "$MOUNT_HOST_PATH" /host_tmp --box "$BOX" --yes 2>&1 | grep -qi "removed" \
  && track_test "pass" || track_test "fail"
lager box config mount remove "$MOUNT_POPULATED_PATH" /host_populated --box "$BOX" --yes >/dev/null 2>&1

echo "Test 4.2: Apply (restart container without test mounts)"
lager box config apply --box "$BOX" --yes >/dev/null 2>&1 \
  && track_test "pass" || track_test "fail"

echo "Test 4.3: /host_tmp mount is gone"
sleep 5
ssh "$SSH_HOST" 'docker inspect lager --format "{{json .Mounts}}"' 2>/dev/null \
  | python3 -c 'import json,sys; m=json.load(sys.stdin); sys.exit(1 if any(x.get("Destination")=="/host_tmp" for x in m) else 0)' \
  && track_test "pass" || track_test "fail"

echo "Test 4.4: Remove config + test dirs and restart to baseline"
ssh "$SSH_HOST" 'rm -f /etc/lager/box_config.json /etc/lager/box_config.applied_hash /etc/lager/box_config.applied.json' >/dev/null 2>&1
ssh "$SSH_HOST" "sudo rm -rf '$MOUNT_HOST_PATH' '$MOUNT_POPULATED_PATH'" >/dev/null 2>&1
ssh "$SSH_HOST" 'cd ~/box && ./start_box.sh' >/dev/null 2>&1 \
  && track_test "pass" || track_test "fail"

# ------------------------------------------------------------
start_section "Validation rejects /"
# ------------------------------------------------------------

echo "Test 5.1: Write a config with mount host=\"/\""
ssh "$SSH_HOST" 'cat > /etc/lager/box_config.json' <<'EOF'
{"version": 1, "mounts": [{"host": "/", "container": "/host"}]}
EOF
track_test "pass"

echo "Test 5.2: validate exits non-zero"
lager box config validate --box "$BOX" 2>&1 | grep -qi "cannot be '/'" \
  && track_test "pass" || track_test "fail"

echo "Test 5.3: apply refuses"
OUT=$(lager box config apply --box "$BOX" --yes 2>&1)
if echo "$OUT" | grep -qi "refusing to apply"; then
  track_test "pass"
else
  track_test "fail"
fi

echo "Test 5.4: Final cleanup"
ssh "$SSH_HOST" 'rm -f /etc/lager/box_config.json /etc/lager/box_config.applied_hash /etc/lager/box_config.applied.json' >/dev/null 2>&1
ssh "$SSH_HOST" 'cd ~/box && ./start_box.sh' >/dev/null 2>&1 \
  && track_test "pass" || track_test "fail"

print_summary
exit_with_status
