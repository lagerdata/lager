#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Test suite for lager hsm commands against a YubiHSM 2 net.
# Generates, uses and deletes one key at a scratch object ID. Never resets.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e

init_harness

if [ $# -lt 2 ]; then
  echo "Usage: $0 <IP/BOX> <HSM_NET> [SCRATCH_ID]"
  echo ""
  echo "Examples:"
  echo "  LAGER_HSM_PASSWORD=... $0 my-box hsm1"
  echo "  LAGER_HSM_PASSWORD=... $0 <BOX_IP> hsm1 0x0F00"
  echo ""
  echo "Arguments:"
  echo "  IP/BOX      - Box ID or Tailscale IP address"
  echo "  HSM_NET     - Name of the hsm net to test"
  echo "  SCRATCH_ID  - Object ID for the test key (default 0x0F00); must be free"
  echo ""
  echo "LAGER_HSM_PASSWORD must hold the password of auth key 1."
  exit 1
fi

if [ -z "$LAGER_HSM_PASSWORD" ]; then
  echo "LAGER_HSM_PASSWORD is not set."
  exit 1
fi

BOX="$1"
HSM_NET="$2"
SCRATCH_ID="${3:-0x0F00}"
WORK_DIR="$(mktemp -d)"

harness_add_cleanup "rm -rf '$WORK_DIR'"

# Never touch a key this script did not create.
if ! lager hsm "$HSM_NET" pubkey --id "$SCRATCH_ID" --box "$BOX" 2>&1 | grep -q "No asymmetric-key"; then
  echo "Object $SCRATCH_ID is in use on $HSM_NET (or the HSM is unreachable). Pick a free SCRATCH_ID."
  exit 1
fi

echo "========================================================================"
echo "LAGER HSM TEST SUITE"
echo "========================================================================"
echo ""
echo "Box: $BOX"
echo "HSM Net: $HSM_NET"
echo "Scratch object ID: $SCRATCH_ID"
echo ""

# ============================================================================
# SECTION 1: DEVICE
# ============================================================================
start_section "Device"

run_and_track "hsm net is listed" "lager hsm --box '$BOX' | grep -q '$HSM_NET'"
run_and_track "info answers without a login" \
  "env -u LAGER_HSM_PASSWORD lager hsm '$HSM_NET' info --box '$BOX' </dev/null"
run_and_track "info --json has a firmware version" \
  "lager hsm '$HSM_NET' info --json --box '$BOX' | python3 -c 'import json,sys; assert json.load(sys.stdin)[\"firmware\"]'"
run_and_track "objects lists with the password" "lager hsm '$HSM_NET' objects --box '$BOX'"
run_expect_fail "a wrong password is refused" \
  "echo wrong-password | lager hsm '$HSM_NET' objects --password-stdin --box '$BOX'" \
  "Authentication failed"
echo ""

# ============================================================================
# SECTION 2: KEY LIFECYCLE
# ============================================================================
start_section "Key lifecycle"

if run_and_track "generate an ec-p256 key" \
  "lager hsm '$HSM_NET' generate --algorithm ec-p256 --id '$SCRATCH_ID' --label lager-test --box '$BOX' > '$WORK_DIR/gen.pem'"; then
  harness_add_cleanup "lager hsm '$HSM_NET' delete --id '$SCRATCH_ID' --yes --box '$BOX' >/dev/null 2>&1"
fi
run_and_track "pubkey matches the generated key" \
  "lager hsm '$HSM_NET' pubkey --id '$SCRATCH_ID' --box '$BOX' > '$WORK_DIR/pub.pem' && cmp -s '$WORK_DIR/gen.pem' '$WORK_DIR/pub.pem'"
run_expect_fail "generate at a used ID is refused" \
  "lager hsm '$HSM_NET' generate --algorithm ec-p256 --id '$SCRATCH_ID' --box '$BOX'" \
  "already exists"

printf 'lager hsm integration test\n' > "$WORK_DIR/msg.bin"
run_and_track "sign with ecdsa-sha256" \
  "lager hsm '$HSM_NET' sign --id '$SCRATCH_ID' --mechanism ecdsa-sha256 --data-file '$WORK_DIR/msg.bin' --out '$WORK_DIR/sig.der' --box '$BOX'"
if command -v openssl >/dev/null 2>&1; then
  run_and_track "openssl verifies the signature" \
    "openssl dgst -sha256 -verify '$WORK_DIR/pub.pem' -signature '$WORK_DIR/sig.der' '$WORK_DIR/msg.bin'"
else
  skip_test "openssl verifies the signature" "openssl not installed"
fi
run_expect_fail "eddsa on an ec-p256 key is refused" \
  "lager hsm '$HSM_NET' sign --id '$SCRATCH_ID' --mechanism eddsa --data-hex 00 --box '$BOX'" \
  "capability\|refused"

run_and_track "delete the key" \
  "lager hsm '$HSM_NET' delete --id '$SCRATCH_ID' --yes --box '$BOX'"
run_expect_fail "pubkey of the deleted key is refused" \
  "lager hsm '$HSM_NET' pubkey --id '$SCRATCH_ID' --box '$BOX'" \
  "No asymmetric-key"
echo ""

print_summary
exit_with_status
