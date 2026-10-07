# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""How the box container's service supervisor stops.

``docker stop`` and ``docker restart`` send TERM to tini, which passes it to
``start-services.sh``. When that script exits, tini does too, and the kernel
SIGKILLs whatever is still running in the container. The script used to sit in
a foreground ``tail -f /dev/null`` with no trap, so it died on the spot and
every service was SIGKILLed. For the oscilloscope daemon that left the
PicoScope mid-transfer, and the next open of it blocked inside the driver for
about three minutes -- a scope page that would not load after every deploy.

These run the script's own supervisor functions, extracted from the shipped
file, against a fake service that takes a moment to shut down.
"""

import pathlib
import re
import signal
import subprocess
import textwrap
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "box" / "lager" / "docker" / "start-services.sh"

SUPERVISOR = ("rotate_log", "restart_service", "start_service", "stop_services")

# Writes "start", then on TERM writes "term", takes half a second to clean up,
# and writes "clean". A supervisor that does not wait exits before "clean".
# The trap goes in before "start": the tests send TERM as soon as they see it.
FAKE_SERVICE = textwrap.dedent("""\
    #!/bin/bash
    events="$1"
    runs="$2"
    trap 'echo term >> "$events"; sleep 0.5; echo clean >> "$events"; exit 0' TERM
    echo start >> "$events"
    count=$(( $(cat "$runs" 2>/dev/null || echo 0) + 1 ))
    echo "$count" > "$runs"
    if [ "$3" = crash-first ] && [ "$count" = 1 ]; then
        exit 1
    fi
    while true; do sleep 0.05; done
    """)


def _function(text, name):
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, flags=re.M | re.S)
    assert match, f"{name}() is missing from start-services.sh"
    return match.group(0)


def _harness(tmp_path, mode=""):
    text = SCRIPT.read_text()
    functions = "\n".join(_function(text, name) for name in SUPERVISOR)
    service = tmp_path / "service.sh"
    service.write_text(FAKE_SERVICE)
    service.chmod(0o755)
    events = tmp_path / "events"
    runs = tmp_path / "runs"
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "LOG_MAX_BYTES=33554432\n"
        "SERVICE_PIDS=()\n"
        f"{functions}\n"
        "trap stop_services TERM INT\n"
        f'start_service "fake" "{service} {events} {runs} {mode}" "{tmp_path / "service.log"}"\n'
        "wait\n"
    )
    proc = subprocess.Popen(["bash", str(harness)], stdout=subprocess.DEVNULL)
    return proc, events


def _events(path):
    return path.read_text().split() if path.exists() else []


def _wait_for(predicate, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class TestTermReachesTheService:
    def test_the_service_gets_term_and_finishes_before_the_script_exits(self, tmp_path):
        proc, events = _harness(tmp_path)
        try:
            assert _wait_for(lambda: "start" in _events(events)), "service never started"
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=10) == 0
            # Read at the moment the supervisor exited. Without the wait,
            # the half-second cleanup would still be running. A single
            # "start" means the stopped service was not restarted.
            assert _events(events) == ["start", "term", "clean"]
        finally:
            if proc.poll() is None:
                proc.kill()


class TestRestartStillWorks:
    def test_a_crashed_service_is_started_again(self, tmp_path):
        proc, events = _harness(tmp_path, mode="crash-first")
        try:
            assert _wait_for(lambda: _events(events).count("start") == 2), (
                "the service crashed and was not restarted")
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=10) == 0
            assert _events(events)[-2:] == ["term", "clean"]
        finally:
            if proc.poll() is None:
                proc.kill()


class TestEveryServiceIsSupervised:
    def test_services_start_through_start_service(self):
        text = SCRIPT.read_text()
        text = text.replace(_function(text, "start_service"), "")
        # A bare `restart_service ... &` would be missing from SERVICE_PIDS,
        # so stop_services would not wait for it.
        bare = re.findall(r"^\s*restart_service .*&\s*$", text, flags=re.M)
        assert bare == [], bare
        assert len(re.findall(r"^\s*start_service ", text, flags=re.M)) >= 6

    def test_the_script_waits_rather_than_running_a_foreground_command(self):
        # bash runs a trap only after the foreground command returns, and
        # `tail -f /dev/null` never does.
        lines = [line for line in SCRIPT.read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
        assert lines[-1].strip() == "wait"
