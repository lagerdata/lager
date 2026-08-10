#!/usr/bin/env python3

# Copyright 2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Remote-execution smoke test. Touches no instruments — exercises the box's
python executor end to end: stdout and stderr streaming, a run long enough
to observe box-lock behavior, and a clean exit code.

Run with: lager python test/api/utility/test_remote_smoke.py --box <YOUR-BOX>

Override the wait with: SMOKE_SLEEP_S=0 lager python ...
"""
import os
import sys
import time

SLEEP_S = float(os.environ.get("SMOKE_SLEEP_S", "15"))

print("remote smoke: starting")
print("remote smoke: this line goes to stderr", file=sys.stderr)

for i in range(int(SLEEP_S)):
    print(f"remote smoke: tick {i + 1}/{int(SLEEP_S)}")
    time.sleep(1)

print("remote smoke: done")
sys.exit(0)
