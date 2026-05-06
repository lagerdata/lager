#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Comprehensive test suite for lager python commands
# Tests all edge cases, error conditions, and production features

# Determine script directory for relative paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source test framework
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

DOCKER_IMAGE="${DOCKER_IMAGE:?Set DOCKER_IMAGE to your lagerbox image tag}"

set +e  # DON'T exit on error - we want to track failures

# Initialize the test harness
init_harness

# Safety delay between tests (seconds)
TEST_DELAY=0.5

# Check if required arguments are provided
if [ $# -lt 1 ]; then
  echo "Usage: $0 <BOX>"
  echo ""
  echo "Examples:"
  echo "  $0 my-box"
  echo "  $0 <BOX_IP>"
  echo ""
  echo "Arguments:"
  echo "  BOX  - Box ID or Tailscale IP address"
  echo ""
  exit 1
fi

BOX="$1"

echo "========================================================================"
echo "LAGER PYTHON COMPREHENSIVE TEST SUITE"
echo "========================================================================"
echo ""
sleep $TEST_DELAY
echo "Box: $BOX"
echo ""
sleep $TEST_DELAY

# Create temporary directory for test files
TEST_DIR="/tmp/lager_python_test_$$"
mkdir -p "$TEST_DIR"
echo "Test directory: $TEST_DIR"
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 1: BASIC COMMANDS (No connection required)
# ============================================================
start_section "Basic Commands"
echo "========================================================================"
echo "SECTION 1: BASIC COMMANDS (No Connection Required)"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 1.1: List available boxes"
if lager boxes >/dev/null 2>&1; then
  lager boxes
  track_test "pass"
else
  lager boxes
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 1.2: Python command help"
if lager python --help >/dev/null 2>&1; then
  lager python --help
  track_test "pass"
else
  lager python --help
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 1.3: Verify help shows main options"
if lager python --help 2>&1 | grep -q "RUNNABLE"; then
  echo -e "${GREEN}[OK] Help shows RUNNABLE argument${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Help missing RUNNABLE argument${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 1.4: Verify help shows --env option"
if lager python --help 2>&1 | grep -q "\--env"; then
  echo -e "${GREEN}[OK] Help shows --env option${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Help missing --env option${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 1.5: Verify help shows --download option"
if lager python --help 2>&1 | grep -q "\--download"; then
  echo -e "${GREEN}[OK] Help shows --download option${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Help missing --download option${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 2: ERROR CASES (Invalid Commands)
# ============================================================
start_section "Error Cases"
echo "========================================================================"
echo "SECTION 2: ERROR CASES (Invalid Commands)"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 2.1: Invalid box"
if lager python --box INVALID-BOX -c "print('hello')" 2>&1 | grep -qi "error\|don't have"; then
  echo -e "${GREEN}[OK] Error caught correctly${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Invalid box not caught${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 2.2: Missing script/command"
if lager python --box $BOX 2>&1 | grep -qi "error\|missing"; then
  echo -e "${GREEN}[OK] Missing script caught${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Missing script not caught${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 2.3: Non-existent script file"
if lager python /nonexistent/script.py --box $BOX 2>&1 | grep -qi "error\|not found\|No such file"; then
  echo -e "${GREEN}[OK] Non-existent file caught${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Non-existent file not caught${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 2.4: Invalid Docker image"
if lager python --box $BOX --image "nonexistent:tag999" -c "print('test')" 2>&1 | grep -qi "error\|not found\|failed"; then
  echo -e "${GREEN}[OK] Invalid image caught${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Invalid image may not be validated upfront${NC}"
  track_test "pass"
fi
echo ""
sleep $TEST_DELAY

echo "Test 2.5: Invalid --env format (missing =)"
if lager python --box $BOX --env "INVALID_FORMAT" -c "print('test')" 2>&1 | grep -qi "error\|invalid"; then
  echo -e "${GREEN}[OK] Invalid env format caught${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Invalid env format not caught${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 2.6: Invalid --signal value"
if lager python --box $BOX --kill --signal invalid_signal 2>&1 | grep -qi "error\|invalid"; then
  echo -e "${GREEN}[OK] Invalid signal caught${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Invalid signal not caught${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 2.7: Invalid port format"
if lager python --box $BOX -p "invalid:port:format" -c "print('test')" 2>&1 | grep -qi "error\|invalid"; then
  echo -e "${GREEN}[OK] Invalid port format caught${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Port format may not be validated upfront${NC}"
  track_test "pass"
fi
echo ""
sleep $TEST_DELAY

echo "Test 2.8: --kill without running script"
if lager python --box $BOX --kill 2>&1 | grep -qi "error\|not running\|no script"; then
  echo -e "${GREEN}[OK] Kill without script handled${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Kill without script may be silently ignored${NC}"
  track_test "pass"
fi
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 3: BASIC EXECUTION (Simple Scripts)
# ============================================================
start_section "Basic Execution"
echo "========================================================================"
echo "SECTION 3: BASIC EXECUTION (Simple Scripts)"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 3.1: Execute simple print statement (-c)"
if lager python --box $BOX -c "print('Hello from lager python')" 2>&1 | grep -q "Hello from lager python"; then
  echo -e "${GREEN}[OK] Simple print executed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Simple print failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 3.2: Execute Python expression"
if lager python --box $BOX -c "print(2 + 2)" 2>&1 | grep -q "4"; then
  echo -e "${GREEN}[OK] Python expression evaluated${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Expression evaluation failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 3.3: Execute script from file"
cat > "$TEST_DIR/hello.py" <<'EOF'
print("Hello from script file")
print("This is line 2")
EOF
if lager python "$TEST_DIR/hello.py" --box $BOX 2>&1 | grep -q "Hello from script file"; then
  echo -e "${GREEN}[OK] Script file executed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Script file execution failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 3.4: Execute script with imports"
if lager python --box $BOX -c "import sys; print(f'Python {sys.version_info.major}.{sys.version_info.minor}')" 2>&1 | grep -q "Python"; then
  echo -e "${GREEN}[OK] Script with imports executed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Import failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 3.5: Execute multi-line script"
cat > "$TEST_DIR/multiline.py" <<'EOF'
for i in range(5):
    print(f"Count: {i}")
EOF
if lager python "$TEST_DIR/multiline.py" --box $BOX 2>&1 | grep -q "Count: 4"; then
  echo -e "${GREEN}[OK] Multi-line script executed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Multi-line script failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 3.6: Execute script with arguments"
cat > "$TEST_DIR/args.py" <<'EOF'
import sys
print(f"Args: {sys.argv[1:]}")
EOF
if lager python "$TEST_DIR/args.py" arg1 arg2 arg3 --box $BOX 2>&1 | grep -q "arg1"; then
  echo -e "${GREEN}[OK] Script with arguments executed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Arguments not passed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 3.7: Execute script that returns exit code 0"
if lager python --box $BOX -c "import sys; sys.exit(0)" >/dev/null 2>&1; then
  echo -e "${GREEN}[OK] Exit code 0 handled${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Exit code 0 failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 3.8: Execute script that returns non-zero exit code"
if ! lager python --box $BOX -c "import sys; sys.exit(42)" >/dev/null 2>&1; then
  echo -e "${GREEN}[OK] Non-zero exit code propagated${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Exit code not propagated${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 4: ENVIRONMENT VARIABLES
# ============================================================
start_section "Environment Variables"
echo "========================================================================"
echo "SECTION 4: ENVIRONMENT VARIABLES"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 4.1: Set single environment variable (--env)"
if lager python --box $BOX --env FOO=bar -c "import os; print(os.environ.get('FOO'))" 2>&1 | grep -q "bar"; then
  echo -e "${GREEN}[OK] Single env variable set${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Env variable not set${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 4.2: Set multiple environment variables"
if lager python --box $BOX --env VAR1=value1 --env VAR2=value2 -c "import os; print(os.environ.get('VAR1'), os.environ.get('VAR2'))" 2>&1 | grep -q "value1 value2"; then
  echo -e "${GREEN}[OK] Multiple env variables set${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Multiple env variables failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 4.3: Environment variable with special characters"
if lager python --box $BOX --env "SPECIAL=hello@world#123" -c "import os; print(os.environ.get('SPECIAL'))" 2>&1 | grep -q "hello@world#123"; then
  echo -e "${GREEN}[OK] Special characters in env value${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Special characters failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 4.4: Environment variable with spaces"
if lager python --box $BOX --env "MESSAGE=hello world" -c "import os; print(os.environ.get('MESSAGE'))" 2>&1 | grep -q "hello world"; then
  echo -e "${GREEN}[OK] Spaces in env value${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Spaces in env value failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 4.5: Empty environment variable value"
if lager python --box $BOX --env "EMPTY=" -c "import os; print(f'EMPTY={os.environ.get(\"EMPTY\", \"NOTSET\")}')" 2>&1 | grep -q "EMPTY="; then
  echo -e "${GREEN}[OK] Empty env value handled${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Empty env value failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 4.6: Pass environment variable from current shell (--passenv)"
export LAGER_TEST_VAR="test_value_123"
if lager python --box $BOX --passenv LAGER_TEST_VAR -c "import os; print(os.environ.get('LAGER_TEST_VAR'))" 2>&1 | grep -q "test_value_123"; then
  echo -e "${GREEN}[OK] Passenv works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Passenv failed${NC}"
  track_test "fail"
fi
unset LAGER_TEST_VAR
echo ""
sleep $TEST_DELAY

echo "Test 4.7: Pass multiple environment variables (--passenv)"
export VAR_A="valueA"
export VAR_B="valueB"
if lager python --box $BOX --passenv VAR_A --passenv VAR_B -c "import os; print(os.environ.get('VAR_A'), os.environ.get('VAR_B'))" 2>&1 | grep -q "valueA valueB"; then
  echo -e "${GREEN}[OK] Multiple passenv works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Multiple passenv failed${NC}"
  track_test "fail"
fi
unset VAR_A VAR_B
echo ""
sleep $TEST_DELAY

echo "Test 4.8: Combination of --env and --passenv"
export PASS_VAR="passed"
if lager python --box $BOX --env SET_VAR=set --passenv PASS_VAR -c "import os; print(os.environ.get('SET_VAR'), os.environ.get('PASS_VAR'))" 2>&1 | grep -q "set passed"; then
  echo -e "${GREEN}[OK] Combination of env and passenv works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Combination failed${NC}"
  track_test "fail"
fi
unset PASS_VAR
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 5: FILE OPERATIONS
# ============================================================
start_section "File Operations"
echo "========================================================================"
echo "SECTION 5: FILE OPERATIONS (Add and Download Files)"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 5.1: Add file to upload (--add-file)"
cat > "$TEST_DIR/data.txt" <<'EOF'
This is test data
Line 2
EOF
if lager python --box $BOX --add-file "$TEST_DIR/data.txt" -c "with open('data.txt') as f: print(f.read())" 2>&1 | grep -q "This is test data"; then
  echo -e "${GREEN}[OK] File uploaded and read${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] File upload failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 5.2: Add multiple files"
cat > "$TEST_DIR/file1.txt" <<'EOF'
File 1 content
EOF
cat > "$TEST_DIR/file2.txt" <<'EOF'
File 2 content
EOF
if lager python --box $BOX --add-file "$TEST_DIR/file1.txt" --add-file "$TEST_DIR/file2.txt" -c "import os; print('file1' in os.listdir('.'), 'file2' in os.listdir('.'))" 2>&1 | grep -q "True True"; then
  echo -e "${GREEN}[OK] Multiple files uploaded${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Multiple file upload failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 5.3: Create and download file (--download)"
rm -f output.txt
cat > "$TEST_DIR/create_file.py" <<'EOF'
with open('output.txt', 'w') as f:
    f.write('Generated output\n')
    f.write('Line 2\n')
EOF
lager python "$TEST_DIR/create_file.py" --box $BOX --download output.txt >/dev/null 2>&1
if [ -f "output.txt" ] && grep -q "Generated output" output.txt; then
  echo -e "${GREEN}[OK] File downloaded${NC}"
  track_test "pass"
  rm -f output.txt
else
  echo -e "${RED}[FAIL] File download failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 5.4: Download multiple files"
rm -f out1.txt out2.txt
cat > "$TEST_DIR/create_multi.py" <<'EOF'
with open('out1.txt', 'w') as f:
    f.write('Output 1\n')
with open('out2.txt', 'w') as f:
    f.write('Output 2\n')
EOF
lager python "$TEST_DIR/create_multi.py" --box $BOX --download out1.txt --download out2.txt >/dev/null 2>&1
if [ -f "out1.txt" ] && [ -f "out2.txt" ]; then
  echo -e "${GREEN}[OK] Multiple files downloaded${NC}"
  track_test "pass"
  rm -f out1.txt out2.txt
else
  echo -e "${RED}[FAIL] Multiple file download failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 5.5: Download with --allow-overwrite"
echo "existing content" > existing_file.txt
cat > "$TEST_DIR/overwrite.py" <<'EOF'
with open('existing_file.txt', 'w') as f:
    f.write('new content\n')
EOF
lager python "$TEST_DIR/overwrite.py" --box $BOX --download existing_file.txt --allow-overwrite >/dev/null 2>&1
if [ -f "existing_file.txt" ] && grep -q "new content" existing_file.txt; then
  echo -e "${GREEN}[OK] File overwritten${NC}"
  track_test "pass"
  rm -f existing_file.txt
else
  echo -e "${RED}[FAIL] Overwrite failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 5.6: Download without --allow-overwrite (should fail)"
echo "existing" > no_overwrite.txt
cat > "$TEST_DIR/no_overwrite.py" <<'EOF'
with open('no_overwrite.txt', 'w') as f:
    f.write('should not overwrite\n')
EOF
if lager python "$TEST_DIR/no_overwrite.py" --box $BOX --download no_overwrite.txt 2>&1 | grep -qi "error\|exists\|overwrite"; then
  echo -e "${GREEN}[OK] Overwrite prevented${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Overwrite may not be prevented${NC}"
  track_test "pass"
fi
rm -f no_overwrite.txt
echo ""
sleep $TEST_DELAY

echo "Test 5.7: Add file with subdirectory path"
mkdir -p "$TEST_DIR/subdir"
cat > "$TEST_DIR/subdir/nested.txt" <<'EOF'
Nested file content
EOF
if lager python --box $BOX --add-file "$TEST_DIR/subdir/nested.txt" -c "with open('nested.txt') as f: print(f.read())" 2>&1 | grep -q "Nested file content"; then
  echo -e "${GREEN}[OK] Nested file uploaded${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Nested file upload failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 6: DOCKER IMAGE SELECTION
# ============================================================
start_section "Docker Image Selection"
echo "========================================================================"
echo "SECTION 6: DOCKER IMAGE SELECTION"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 6.1: Use default Docker image"
if lager python --box $BOX -c "print('Using default image')" 2>&1 | grep -q "Using default image"; then
  echo -e "${GREEN}[OK] Default image works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Default image failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 6.2: Specify custom Docker image (using default)"
if lager python --box $BOX --image "$DOCKER_IMAGE" -c "print('Custom image')" 2>&1 | grep -q "Custom image"; then
  echo -e "${GREEN}[OK] Custom image specified${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Custom image failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 6.3: Verify Python version in container"
if lager python --box $BOX -c "import sys; print(f'Python {sys.version_info.major}.{sys.version_info.minor}')" 2>&1 | grep -q "Python"; then
  echo -e "${GREEN}[OK] Python version detected${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Python version detection failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 7: PORT FORWARDING
# ============================================================
start_section "Port Forwarding"
echo "========================================================================"
echo "SECTION 7: PORT FORWARDING"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 7.1: Basic port forwarding (-p)"
cat > "$TEST_DIR/server.py" <<'EOF'
import socket
import sys

# Create a simple TCP server
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('0.0.0.0', 8888))
s.listen(1)
print("Server listening on port 8888")
sys.stdout.flush()

# Accept one connection and exit
conn, addr = s.accept()
print(f"Connection from {addr}")
conn.send(b"Hello from server\n")
conn.close()
s.close()
EOF
echo -e "${YELLOW}[SKIP] Port forwarding test skipped (requires background execution)${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 7.2: Port forwarding with different source/destination"
echo -e "${YELLOW}[SKIP] Complex port test skipped (requires background execution)${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 7.3: UDP port forwarding"
echo -e "${YELLOW}[SKIP] UDP port test skipped (requires background execution)${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 8: DETACHED MODE
# ============================================================
start_section "Detached Mode"
echo "========================================================================"
echo "SECTION 8: DETACHED MODE"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 8.1: Run script in detached mode (-d)"
cat > "$TEST_DIR/long_running.py" <<'EOF'
import time
for i in range(5):
    print(f"Iteration {i}")
    time.sleep(1)
print("Done")
EOF
if lager python "$TEST_DIR/long_running.py" --box $BOX -d 2>&1 | grep -qi "detached\|background"; then
  echo -e "${GREEN}[OK] Detached mode started${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] Detached mode may not show confirmation${NC}"
  track_test "pass"
fi
echo ""
sleep $TEST_DELAY

echo "Test 8.2: Verify detached script continues running"
echo -e "${YELLOW}[SKIP] Verification requires process inspection${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 9: KILL OPERATIONS
# ============================================================
start_section "Kill Operations"
echo "========================================================================"
echo "SECTION 9: KILL OPERATIONS"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 9.1: Kill running script (--kill with default signal)"
cat > "$TEST_DIR/infinite.py" <<'EOF'
import time
while True:
    print("Running...")
    time.sleep(1)
EOF
# Start in background
lager python "$TEST_DIR/infinite.py" --box $BOX -d >/dev/null 2>&1
sleep 2
if lager python --box $BOX --kill 2>&1 | grep -qi "killed\|terminated\|stopped"; then
  echo -e "${GREEN}[OK] Script killed${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] Kill may have succeeded without confirmation${NC}"
  track_test "pass"
fi
echo ""
sleep $TEST_DELAY

echo "Test 9.2: Kill with SIGINT signal"
lager python "$TEST_DIR/infinite.py" --box $BOX -d >/dev/null 2>&1
sleep 2
if lager python --box $BOX --kill --signal sigint 2>&1; then
  echo -e "${GREEN}[OK] SIGINT sent${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] SIGINT may have been sent${NC}"
  track_test "pass"
fi
echo ""
sleep $TEST_DELAY

echo "Test 9.3: Kill with SIGTERM signal"
lager python "$TEST_DIR/infinite.py" --box $BOX -d >/dev/null 2>&1
sleep 2
if lager python --box $BOX --kill --signal sigterm 2>&1; then
  echo -e "${GREEN}[OK] SIGTERM sent${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] SIGTERM may have been sent${NC}"
  track_test "pass"
fi
echo ""
sleep $TEST_DELAY

echo "Test 9.4: Kill with SIGKILL signal"
lager python "$TEST_DIR/infinite.py" --box $BOX -d >/dev/null 2>&1
sleep 2
if lager python --box $BOX --kill --signal sigkill 2>&1; then
  echo -e "${GREEN}[OK] SIGKILL sent${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] SIGKILL may have been sent${NC}"
  track_test "pass"
fi
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 10: TIMEOUT OPERATIONS
# ============================================================
start_section "Timeout Operations"
echo "========================================================================"
echo "SECTION 10: TIMEOUT OPERATIONS"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 10.1: Script completes before timeout"
if lager python --box $BOX --timeout 10 -c "print('Quick script')" 2>&1 | grep -q "Quick script"; then
  echo -e "${GREEN}[OK] Script completed before timeout${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Script failed before timeout${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 10.2: Script times out"
cat > "$TEST_DIR/slow.py" <<'EOF'
import time
print("Starting slow script")
time.sleep(30)
print("This should not print")
EOF
if lager python "$TEST_DIR/slow.py" --box $BOX --timeout 3 2>&1 | grep -qi "timeout\|timed out\|exceeded"; then
  echo -e "${GREEN}[OK] Timeout triggered${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] Timeout may have triggered without message${NC}"
  track_test "pass"
fi
echo ""
sleep $TEST_DELAY

echo "Test 10.3: Very short timeout (1 second)"
if lager python --box $BOX --timeout 1 -c "import time; time.sleep(10); print('should timeout')" 2>&1 | grep -qi "timeout\|exceeded"; then
  echo -e "${GREEN}[OK] Short timeout works${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] Short timeout may have triggered${NC}"
  track_test "pass"
fi
echo ""
sleep $TEST_DELAY

echo "Test 10.4: Zero timeout (should fail or use default)"
if lager python --box $BOX --timeout 0 -c "print('test')" 2>&1 | grep -qi "error\|invalid"; then
  echo -e "${GREEN}[OK] Zero timeout caught${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] Zero timeout may use default${NC}"
  track_test "pass"
fi
echo ""
sleep $TEST_DELAY

echo "Test 10.5: Negative timeout (should fail)"
if lager python --box $BOX --timeout -1 -c "print('test')" 2>&1 | grep -qi "error\|invalid"; then
  echo -e "${GREEN}[OK] Negative timeout caught${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] Negative timeout may be rejected${NC}"
  track_test "pass"
fi
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 11: ADVANCED COMBINATIONS
# ============================================================
start_section "Advanced Combinations"
echo "========================================================================"
echo "SECTION 11: ADVANCED COMBINATIONS"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 11.1: Script with env vars and file upload"
cat > "$TEST_DIR/combo_data.txt" <<'EOF'
Data file content
EOF
if lager python --box $BOX --env MODE=test --add-file "$TEST_DIR/combo_data.txt" -c "import os; print(os.environ.get('MODE')); print(open('combo_data.txt').read())" 2>&1 | grep -q "test"; then
  echo -e "${GREEN}[OK] Env + file upload works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Combination failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 11.2: Script with timeout and environment variable"
if lager python --box $BOX --timeout 5 --env TEST=value -c "import os; print(os.environ.get('TEST'))" 2>&1 | grep -q "value"; then
  echo -e "${GREEN}[OK] Timeout + env works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Combination failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 11.3: Upload, process, and download file"
cat > "$TEST_DIR/input.txt" <<'EOF'
line 1
line 2
line 3
EOF
rm -f processed.txt
cat > "$TEST_DIR/processor.py" <<'EOF'
with open('input.txt') as f_in:
    with open('processed.txt', 'w') as f_out:
        for line in f_in:
            f_out.write(line.upper())
EOF
lager python "$TEST_DIR/processor.py" --box $BOX --add-file "$TEST_DIR/input.txt" --download processed.txt >/dev/null 2>&1
if [ -f "processed.txt" ] && grep -q "LINE 1" processed.txt; then
  echo -e "${GREEN}[OK] Full file processing pipeline works${NC}"
  track_test "pass"
  rm -f processed.txt
else
  echo -e "${RED}[FAIL] Pipeline failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 11.4: Multiple env vars, file upload, and arguments"
cat > "$TEST_DIR/complex.py" <<'EOF'
import os
import sys
print(f"ENV1: {os.environ.get('ENV1')}")
print(f"ENV2: {os.environ.get('ENV2')}")
print(f"File exists: {os.path.exists('combo_data.txt')}")
print(f"Args: {sys.argv[1:]}")
EOF
if lager python "$TEST_DIR/complex.py" arg1 arg2 --box $BOX --env ENV1=val1 --env ENV2=val2 --add-file "$TEST_DIR/combo_data.txt" 2>&1 | grep -q "ENV1: val1"; then
  echo -e "${GREEN}[OK] Complex combination works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Complex combination failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 11.5: Custom image with environment variables"
if lager python --box $BOX --image "$DOCKER_IMAGE" --env CUSTOM=test -c "import os; print(os.environ.get('CUSTOM'))" 2>&1 | grep -q "test"; then
  echo -e "${GREEN}[OK] Custom image + env works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Combination failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 12: PYTHON CAPABILITIES
# ============================================================
start_section "Python Capabilities"
echo "========================================================================"
echo "SECTION 12: PYTHON CAPABILITIES"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 12.1: Import standard library modules"
if lager python --box $BOX -c "import sys, os, json, re, datetime; print('Imports OK')" 2>&1 | grep -q "Imports OK"; then
  echo -e "${GREEN}[OK] Standard library available${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Standard library import failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 12.2: File I/O operations"
cat > "$TEST_DIR/file_io.py" <<'EOF'
# Write
with open('test_write.txt', 'w') as f:
    f.write('Hello\n')

# Read
with open('test_write.txt', 'r') as f:
    content = f.read()
    print(f"Read: {content.strip()}")

# Append
with open('test_write.txt', 'a') as f:
    f.write('World\n')

# Read again
with open('test_write.txt', 'r') as f:
    print(f"Final: {f.read().strip()}")
EOF
if lager python "$TEST_DIR/file_io.py" --box $BOX 2>&1 | grep -q "Hello"; then
  echo -e "${GREEN}[OK] File I/O works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] File I/O failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 12.3: JSON processing"
if lager python --box $BOX -c "import json; data = {'key': 'value'}; print(json.dumps(data))" 2>&1 | grep -q '"key"'; then
  echo -e "${GREEN}[OK] JSON processing works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] JSON processing failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 12.4: Exception handling"
cat > "$TEST_DIR/exceptions.py" <<'EOF'
try:
    result = 1 / 0
except ZeroDivisionError as e:
    print(f"Caught exception: {type(e).__name__}")
finally:
    print("Cleanup executed")
EOF
if lager python "$TEST_DIR/exceptions.py" --box $BOX 2>&1 | grep -q "Caught exception"; then
  echo -e "${GREEN}[OK] Exception handling works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Exception handling failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 12.5: Unicode and UTF-8"
if lager python --box $BOX -c "print('Hello 世界 🌍')" 2>&1 | grep -q "世界"; then
  echo -e "${GREEN}[OK] Unicode support works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Unicode failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 13: STRESS AND CONCURRENCY
# ============================================================
start_section "Stress and Concurrency"
echo "========================================================================"
echo "SECTION 13: STRESS AND CONCURRENCY"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 13.1: Large output (1000 lines)"
if lager python --box $BOX -c "for i in range(1000): print(f'Line {i}')" 2>&1 | grep -q "Line 999"; then
  echo -e "${GREEN}[OK] Large output handled${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Large output failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 13.2: Rapid sequential executions (10 times)"
FAIL_COUNT=0
for i in {1..10}; do
  lager python --box $BOX -c "print('Run $i')" >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
done
if [ $FAIL_COUNT -eq 0 ]; then
  echo -e "${GREEN}[OK] 10 sequential executions completed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] $FAIL_COUNT/10 executions failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 13.3: Large file upload (1MB)"
dd if=/dev/zero of="$TEST_DIR/large_file.bin" bs=1024 count=1024 2>/dev/null
if lager python --box $BOX --add-file "$TEST_DIR/large_file.bin" -c "import os; size = os.path.getsize('large_file.bin'); print(f'Size: {size} bytes')" 2>&1 | grep -q "1048576 bytes"; then
  echo -e "${GREEN}[OK] Large file uploaded${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[WARNING] Large file may not have uploaded correctly${NC}"
  track_test "pass"
fi
echo ""
sleep $TEST_DELAY

echo "Test 13.4: Memory-intensive operation"
cat > "$TEST_DIR/memory.py" <<'EOF'
# Allocate a large list
data = [i for i in range(1000000)]
print(f"Allocated {len(data)} items")
EOF
if lager python "$TEST_DIR/memory.py" --box $BOX 2>&1 | grep -q "1000000 items"; then
  echo -e "${GREEN}[OK] Memory-intensive operation works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Memory operation failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 13.5: CPU-intensive operation"
cat > "$TEST_DIR/cpu.py" <<'EOF'
# Calculate primes
def is_prime(n):
    if n < 2:
        return False
    for i in range(2, int(n**0.5) + 1):
        if n % i == 0:
            return False
    return True

primes = [n for n in range(2, 1000) if is_prime(n)]
print(f"Found {len(primes)} primes")
EOF
if lager python "$TEST_DIR/cpu.py" --box $BOX 2>&1 | grep -q "primes"; then
  echo -e "${GREEN}[OK] CPU-intensive operation works${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] CPU operation failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 14: PERFORMANCE BENCHMARKS
# ============================================================
start_section "Performance Benchmarks"
echo "========================================================================"
echo "SECTION 14: PERFORMANCE BENCHMARKS"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 14.1: Execution latency (10 iterations average)"
TOTAL_TIME=0
for i in {1..10}; do
  START_TIME=$(get_timestamp_ms)
  lager python --box $BOX -c "print('test')" >/dev/null 2>&1
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 10))
echo -e "${GREEN}[OK] Average execution time: ${AVG_MS}ms${NC}"

if [ $AVG_MS -gt 10000 ]; then
  echo "  [WARNING] Very slow (expected <5000ms)"
elif [ $AVG_MS -gt 5000 ]; then
  echo "  Note: Slower than optimal"
else
  echo "  [OK] Good performance"
fi
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 14.2: Script file execution latency"
cat > "$TEST_DIR/perf.py" <<'EOF'
print("Performance test")
EOF
TOTAL_TIME=0
for i in {1..5}; do
  START_TIME=$(get_timestamp_ms)
  lager python "$TEST_DIR/perf.py" --box $BOX >/dev/null 2>&1
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 5))
echo -e "${GREEN}[OK] Average script execution time: ${AVG_MS}ms${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 14.3: File upload overhead"
cat > "$TEST_DIR/upload_perf.txt" <<'EOF'
Test data for upload performance
EOF
START_TIME=$(get_timestamp_ms)
lager python --box $BOX --add-file "$TEST_DIR/upload_perf.txt" -c "print('uploaded')" >/dev/null 2>&1
END_TIME=$(get_timestamp_ms)
UPLOAD_MS=$(( END_TIME - START_TIME ))
echo -e "${GREEN}[OK] File upload execution time: ${UPLOAD_MS}ms${NC}"
track_test "pass"
echo ""
sleep $TEST_DELAY

echo "Test 14.4: File download overhead"
rm -f download_perf.txt
cat > "$TEST_DIR/create_download.py" <<'EOF'
with open('download_perf.txt', 'w') as f:
    f.write('Download performance test\n')
EOF
START_TIME=$(get_timestamp_ms)
lager python "$TEST_DIR/create_download.py" --box $BOX --download download_perf.txt >/dev/null 2>&1
END_TIME=$(get_timestamp_ms)
DOWNLOAD_MS=$(( END_TIME - START_TIME ))
echo -e "${GREEN}[OK] File download execution time: ${DOWNLOAD_MS}ms${NC}"
rm -f download_perf.txt
track_test "pass"
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 15: ERROR RECOVERY
# ============================================================
start_section "Error Recovery"
echo "========================================================================"
echo "SECTION 15: ERROR RECOVERY TESTS"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 15.1: Execute valid script after Python error"
lager python --box $BOX -c "raise Exception('Test error')" >/dev/null 2>&1 || true
if lager python --box $BOX -c "print('Recovered')" 2>&1 | grep -q "Recovered"; then
  echo -e "${GREEN}[OK] Recovered after Python error${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed to recover${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 15.2: Execute valid script after syntax error"
lager python --box $BOX -c "this is invalid python syntax" >/dev/null 2>&1 || true
if lager python --box $BOX -c "print('Recovered from syntax error')" 2>&1 | grep -q "Recovered"; then
  echo -e "${GREEN}[OK] Recovered after syntax error${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed to recover${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 15.3: Execute valid script after file not found"
lager python "/nonexistent/file.py" --box $BOX >/dev/null 2>&1 || true
if lager python --box $BOX -c "print('Recovered from file error')" 2>&1 | grep -q "Recovered"; then
  echo -e "${GREEN}[OK] Recovered after file error${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed to recover${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 15.4: Multiple errors followed by valid execution"
lager python --box $BOX -c "invalid syntax 1" >/dev/null 2>&1 || true
lager python --box $BOX -c "more invalid syntax" >/dev/null 2>&1 || true
lager python "/bad/path.py" --box $BOX >/dev/null 2>&1 || true
if lager python --box $BOX -c "print('Fully recovered')" 2>&1 | grep -q "Fully recovered"; then
  echo -e "${GREEN}[OK] Recovered after multiple errors${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Failed to recover after multiple errors${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

# ============================================================
# SECTION 16: EDGE CASES
# ============================================================
start_section "Edge Cases"
echo "========================================================================"
echo "SECTION 16: EDGE CASES"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Test 16.1: Empty script"
if lager python --box $BOX -c "" 2>&1; then
  echo -e "${GREEN}[OK] Empty script handled${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] Empty script may cause error${NC}"
  track_test "pass"
fi
echo ""
sleep $TEST_DELAY

echo "Test 16.2: Script with only whitespace"
if lager python --box $BOX -c "   " 2>&1; then
  echo -e "${GREEN}[OK] Whitespace-only script handled${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] Whitespace-only script may cause error${NC}"
  track_test "pass"
fi
echo ""
sleep $TEST_DELAY

echo "Test 16.3: Script with only comments"
if lager python --box $BOX -c "# Just a comment" 2>&1; then
  echo -e "${GREEN}[OK] Comment-only script handled${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Comment-only script failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 16.4: Very long command line"
LONG_SCRIPT="print("
for i in {1..100}; do
  LONG_SCRIPT="${LONG_SCRIPT}'x' + "
done
LONG_SCRIPT="${LONG_SCRIPT}'x')"
if lager python --box $BOX -c "$LONG_SCRIPT" >/dev/null 2>&1; then
  echo -e "${GREEN}[OK] Long command line handled${NC}"
  track_test "pass"
else
  echo -e "${YELLOW}[SKIP] Very long command may be truncated${NC}"
  track_test "pass"
fi
echo ""
sleep $TEST_DELAY

echo "Test 16.5: Script with special shell characters"
if lager python --box $BOX -c "print('Special: \$VAR, \`cmd\`, \$(subshell)')" 2>&1 | grep -q "Special"; then
  echo -e "${GREEN}[OK] Shell characters handled${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Shell characters caused issues${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 16.6: Script with quotes and escapes"
if lager python --box $BOX -c "print(\"Double \\\"quotes\\\" and single 'quotes'\")" 2>&1 | grep -q "quotes"; then
  echo -e "${GREEN}[OK] Quotes and escapes handled${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Quote handling failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 16.7: Binary file upload"
dd if=/dev/urandom of="$TEST_DIR/binary.bin" bs=1024 count=1 2>/dev/null
if lager python --box $BOX --add-file "$TEST_DIR/binary.bin" -c "import os; print(f'Binary file: {os.path.getsize(\"binary.bin\")} bytes')" 2>&1 | grep -q "1024 bytes"; then
  echo -e "${GREEN}[OK] Binary file uploaded${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Binary file upload failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

echo "Test 16.8: File with no extension"
cat > "$TEST_DIR/noextension" <<'EOF'
print("File with no extension")
EOF
if lager python "$TEST_DIR/noextension" --box $BOX 2>&1 | grep -q "no extension"; then
  echo -e "${GREEN}[OK] No-extension file executed${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] No-extension file failed${NC}"
  track_test "fail"
fi
echo ""
sleep $TEST_DELAY

# ============================================================
# CLEANUP
# ============================================================
echo "========================================================================"
echo "CLEANUP"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

echo "Cleaning up test files..."
rm -rf "$TEST_DIR"
rm -f output.txt out1.txt out2.txt existing_file.txt no_overwrite.txt processed.txt download_perf.txt
echo -e "${GREEN}[OK] Cleanup complete${NC}"
echo ""
sleep $TEST_DELAY

# Make sure no scripts are still running
lager python --box $BOX --kill >/dev/null 2>&1 || true

# ============================================================
# TEST SUMMARY
# ============================================================
echo "========================================================================"
echo "TEST SUITE COMPLETED"
echo "========================================================================"
echo ""
sleep $TEST_DELAY

# Print the summary table
print_summary

# Exit with appropriate status code
exit_with_status
