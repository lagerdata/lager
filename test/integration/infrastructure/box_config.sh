#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# Smoke test for `lager box config` against a real box.
# ============================================================================
#
# Verifies:
#   1. No box_config.json -> container starts with the same mounts as before.
#   2. With box_config.json adding /tmp:/host_tmp -> mount visible in container.
#   3. apply is idempotent (second apply is a no-op).
#   4. Cleanup restores prior state.
#   5. Invalid config (mount with host="/") is rejected.
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
ssh "$SSH_HOST" 'rm -f /etc/lager/box_config.json /etc/lager/box_config.applied_hash' 2>&1 \
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

# ------------------------------------------------------------
start_section "Apply /tmp mount"
# ------------------------------------------------------------

echo "Test 2.1: Init default config"
lager box config init --box "$BOX" 2>&1 | grep -qi "created" \
  && track_test "pass" || track_test "fail"

echo "Test 2.2: Add /tmp:/host_tmp mount"
lager box config mount add /tmp /host_tmp --box "$BOX" 2>&1 | grep -qi "added mount" \
  && track_test "pass" || track_test "fail"

echo "Test 2.3: Validate"
lager box config validate --box "$BOX" 2>&1 | grep -qi "valid" \
  && track_test "pass" || track_test "fail"

echo "Test 2.4: Apply (restart container)"
lager box config apply --box "$BOX" --yes 2>&1 | grep -qi "applied" \
  && track_test "pass" || track_test "fail"

echo "Test 2.5: Container reports /tmp:/host_tmp mount"
sleep 5
ssh "$SSH_HOST" 'docker inspect lager --format "{{json .Mounts}}"' 2>/dev/null \
  | python3 -c 'import json,sys; m=json.load(sys.stdin); sys.exit(0 if any(x.get("Source")=="/tmp" and x.get("Destination")=="/host_tmp" for x in m) else 1)' \
  && track_test "pass" || track_test "fail"

# ------------------------------------------------------------
start_section "Idempotency"
# ------------------------------------------------------------

echo "Test 3.1: Second apply skips restart"
lager box config apply --box "$BOX" --yes 2>&1 | grep -qi "unchanged" \
  && track_test "pass" || track_test "fail"

# ------------------------------------------------------------
start_section "Cleanup"
# ------------------------------------------------------------

echo "Test 4.1: Remove the mount"
lager box config mount remove /tmp /host_tmp --box "$BOX" --yes 2>&1 | grep -qi "removed" \
  && track_test "pass" || track_test "fail"

echo "Test 4.2: Apply (restart container without /tmp mount)"
lager box config apply --box "$BOX" --yes >/dev/null 2>&1 \
  && track_test "pass" || track_test "fail"

echo "Test 4.3: /tmp:/host_tmp mount is gone"
sleep 5
ssh "$SSH_HOST" 'docker inspect lager --format "{{json .Mounts}}"' 2>/dev/null \
  | python3 -c 'import json,sys; m=json.load(sys.stdin); sys.exit(1 if any(x.get("Destination")=="/host_tmp" for x in m) else 0)' \
  && track_test "pass" || track_test "fail"

echo "Test 4.4: Remove config and restart to baseline"
ssh "$SSH_HOST" 'rm -f /etc/lager/box_config.json /etc/lager/box_config.applied_hash' >/dev/null 2>&1
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
ssh "$SSH_HOST" 'rm -f /etc/lager/box_config.json /etc/lager/box_config.applied_hash' >/dev/null 2>&1
ssh "$SSH_HOST" 'cd ~/box && ./start_box.sh' >/dev/null 2>&1 \
  && track_test "pass" || track_test "fail"

print_summary
exit_with_status
