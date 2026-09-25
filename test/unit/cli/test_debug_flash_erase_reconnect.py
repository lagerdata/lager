#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the erase step of `lager debug <net> flash`.

Pins the fix for the defect where `flash` erased the part and then never
programmed it. The command used to disconnect and `connect(force=True)`
between `/debug/erase` and `/debug/flash`, inside the same `try:` whose
handler prints "Flash erase failed" and calls `ctx.exit(1)`. Against a
just-erased nRF5340 that connect answers 500, so the run aborted with the chip
blank -- and because `flash` erases by DEFAULT, a plain
`lager debug NET flash --hex fw.hex` was the way to hit it.

That reconnect was never load-bearing for either backend:

  * J-Link  -- `/debug/flash` runs its own JLinkExe session. `flash_device`
    (box/lager/debug/api.py) opens with `stop_jlink()` +
    `stop_jlink_gdbserver()`, so anything the CLI started here was torn down
    ~0.5s later, and a gdbserver is re-established after programming anyway.
  * OpenOCD -- `/debug/erase` leaves the daemon running and `/debug/flash`
    programs over that same daemon, answering 400 when it is gone. The
    disconnect actively removed the session the flash needed.

So the contract pinned here is: between erase and flash the CLI issues NO
`/debug/connect` and NO `/debug/disconnect`; a failing connect cannot stop the
flash; a failing erase is still fatal; and the one reconnect that IS meant to
exist (`--force-reconnect`) stays non-fatal.

The box is mocked at the `DebugServiceClient` boundary -- a recording fake
handed back from `_get_service_client` -- so no hardware or network is touched.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from unittest.mock import patch

import pytest
import requests
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

debug_mod = importlib.import_module("cli.commands.development.debug.commands")

BOX_IP = "1.2.3.4"

# `_debug_net_jlink_device` reads `channel`, falling back to `pin`, and the
# result is what the DA1469x hint at the tail of `flash` keys off.
JLINK_NET = {
    "name": "debug1",
    "role": "debug",
    "channel": "NRF5340_XXAA_APP",
    "address": "USB0::0x1366::0x0101::000051014439::INSTR",
}

DA1469X_NET = {
    "name": "debug2",
    "role": "debug",
    "channel": "DA14695",
    "address": "USB0::0x1366::0x0101::000051014440::INSTR",
}


class _Obj:
    """Settable stand-in for the LagerContext (the group stashes `net_name`)."""


# "The box sent no `erase_range` key at all" -- what a box older than the
# key answers -- as distinct from `erase_range: None`, a full-chip erase.
_NO_RANGE = object()

# What a box that reads erase_start/erase_size lists under /health.
HEALTH_WITH_RANGE = {"status": "healthy", "version": "1.0.0", "features": ["erase_range"]}


class FakeClient:
    """A DebugServiceClient that records every box call in order.

    `connect_error` makes `/debug/connect` behave the way the box does against
    a just-erased part: HTTP 500. `erase_error` does the same for
    `/debug/erase`. `erase_range` is echoed in the erase response; left unset,
    the response has no such key, like an older box. `health` is what
    `/health` answers (a box that reads the erase range, by default) and
    `health_error` makes that call raise. Signatures mirror
    `service_client.DebugServiceClient` so a change there surfaces here as a
    TypeError rather than a false pass.
    """

    def __init__(self, connect_error=None, erase_error=None, flash_output="",
                 erase_output="Erase completed", erase_range=_NO_RANGE,
                 health=None, health_error=None, flash_verdict=None):
        self.calls: list[str] = []
        self.connect_error = connect_error
        self.erase_error = erase_error
        self.flash_output = flash_output
        # `programmed` / `error`, as a newer box adds them; None is an older box.
        self.flash_verdict = flash_verdict
        self.erase_output = erase_output
        self.erase_range = erase_range
        self.health = HEALTH_WITH_RANGE if health is None else health
        self.health_error = health_error
        self.erase_kwargs: list[dict] = []
        self.closed = False

    def erase(self, net, speed='4000', transport='SWD', *, erase_start=None,
              erase_size=None):
        self.calls.append("erase")
        self.erase_kwargs.append({"erase_start": erase_start, "erase_size": erase_size})
        if self.erase_error:
            raise self.erase_error
        result = {"status": "erase_complete", "output": self.erase_output}
        if self.erase_range is not _NO_RANGE:
            result["erase_range"] = self.erase_range
        return result

    def get_service_health(self, detailed=False):
        self.calls.append("health")
        if self.health_error:
            raise self.health_error
        return self.health

    def connect(self, net, speed=None, force=False, halt=False, gdb=False,
                gdb_port=None, jlink_script=None, openocd_config=None):
        self.calls.append("connect")
        if self.connect_error:
            raise self.connect_error
        return {"status": "connected"}

    def disconnect(self, net, keep_jlink_running=False):
        self.calls.append("disconnect")
        return {"status": "disconnected"}

    def flash(self, firmware_file, file_type='hex', address=None, verbose=False,
              net=None, jlink_script=None, openocd_config=None):
        self.calls.append("flash")
        result = {"status": "flash_complete", "output": self.flash_output}
        if self.flash_verdict is not None:
            result.update(self.flash_verdict)
        return result

    def reset(self, net, halt=False):
        self.calls.append("reset")
        return {"status": "reset_complete"}

    def close(self):
        self.closed = True


def http_500(message="Failed to power up DAP"):
    """An HTTPError shaped like the one the debug service client raises on 500.

    `service_client._request` ends in `response.raise_for_status()`, so this is
    what every CLI `except Exception` around a box call actually sees.
    """
    response = requests.Response()
    response.status_code = 500
    response.reason = "Internal Server Error"
    response.url = f"http://{BOX_IP}:8765/debug/connect"
    return requests.exceptions.HTTPError(
        f"500 Server Error: {message}", response=response)


@pytest.fixture
def hexfile(tmp_path):
    """`--hex` is a click.Path(exists=True), so it needs a real file."""
    path = tmp_path / "firmware.hex"
    path.write_text(":020000040000FA\n:00000001FF\n")
    return str(path)


def run_flash(client, args, net=JLINK_NET):
    """Invoke `lager debug <net> flash` with everything below the CLI mocked.

    Patched seams, all module-level names in commands.py:
      * `_resolve_box_with_username` / `_get_debug_net` -- no :9000 net fetch
      * `_resolve_debug_scripts`  -- no local `.lager` script lookup
      * `_get_service_client`     -- hand back the recording fake
      * `_auto_connect_if_needed` -- report "already connected", so every
        `connect` the fake records is one the erase/flash body itself issued
      * `time.sleep`              -- the surviving `--force-reconnect` branch
        does a function-local `import time`, which resolves the attribute off
        the real module at call time; keeps the suite instant
    """
    obj = _Obj()
    obj.net_name = net["name"]
    with patch.object(debug_mod, "_resolve_box_with_username",
                      lambda ctx, box: (BOX_IP, "lagerdata")), \
         patch.object(debug_mod, "_get_debug_net",
                      lambda ctx, box, net_name=None: net), \
         patch.object(debug_mod, "_resolve_debug_scripts",
                      lambda ctx, name, debug_net: (None, None)), \
         patch.object(debug_mod, "_get_service_client", lambda box: client), \
         patch.object(debug_mod, "_auto_connect_if_needed", lambda *a, **k: True), \
         patch("time.sleep", lambda *a, **k: None):
        return CliRunner().invoke(debug_mod.flash, args, obj=obj,
                                  catch_exceptions=False)


def run_erase(client, args, net=JLINK_NET, input=None):
    """Invoke `lager debug <net> erase` with everything below the CLI mocked.

    Same seams as `run_flash`. `--yes` skips the destructive-operation prompt;
    `input` answers it instead.
    """
    obj = _Obj()
    obj.net_name = net["name"]
    with patch.object(debug_mod, "_resolve_box_with_username",
                      lambda ctx, box: (BOX_IP, "lagerdata")), \
         patch.object(debug_mod, "_get_debug_net",
                      lambda ctx, box, net_name=None: net), \
         patch.object(debug_mod, "_resolve_debug_scripts",
                      lambda ctx, name, debug_net: (None, None)), \
         patch.object(debug_mod, "_get_service_client", lambda box: client), \
         patch.object(debug_mod, "_auto_connect_if_needed", lambda *a, **k: True), \
         patch("time.sleep", lambda *a, **k: None):
        return CliRunner().invoke(debug_mod.erase, args, obj=obj,
                                  catch_exceptions=False, input=input)


# --------------------------------------------------------------------------- #
# The regression guard                                                        #
# --------------------------------------------------------------------------- #

class TestNoReconnectBetweenEraseAndFlash:
    """Erase then flash, with nothing in between, on every device family."""

    def test_erase_then_flash_with_nothing_in_between(self, hexfile):
        client = FakeClient()
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 0, result.output
        assert client.calls == ["erase", "flash"]

    def test_failing_connect_cannot_stop_the_flash(self, hexfile):
        # THE defect. `/debug/connect` answers 500 on a just-erased part and
        # the CLI aborted the run there, leaving the chip blank. Wire the fake
        # so ANY connect raises: the flash must still be issued and the command
        # must still succeed.
        client = FakeClient(connect_error=http_500())
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 0, result.output
        assert "flash" in client.calls
        assert "Flash erase failed" not in result.output
        assert "Flashed!" in result.output

    def test_no_disconnect_between_erase_and_flash(self, hexfile):
        # OpenOCD-specific: /debug/erase leaves the daemon running and
        # /debug/flash programs over it, answering 400 when it is gone
        # (service.py handle_flash). The CLI must not take it down here.
        client = FakeClient()
        run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert "disconnect" not in client.calls

    def test_da1469x_takes_the_same_path(self, hexfile):
        # The DA1469x branch already skipped the reconnect. There is now one
        # path, so DA1469x must be indistinguishable from the J-Link case.
        client = FakeClient(connect_error=http_500())
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"],
                           net=DA1469X_NET)
        assert result.exit_code == 0, result.output
        assert client.calls == ["erase", "flash"]

    def test_halt_still_resets_after_a_successful_flash(self, hexfile):
        # Guards against the deletion swallowing the tail of the command.
        client = FakeClient()
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox", "--halt"])
        assert result.exit_code == 0, result.output
        assert client.calls == ["erase", "flash", "reset"]


# --------------------------------------------------------------------------- #
# What must NOT change                                                        #
# --------------------------------------------------------------------------- #

class TestEraseFailureIsStillFatal:
    """Removing the reconnect must not soften the erase itself. If the chip
    was not erased, programming it is not what the user asked for."""

    def test_erase_http_error_aborts_before_flash(self, hexfile):
        client = FakeClient(erase_error=http_500("chip erase failed"))
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 1
        assert "Flash erase failed" in result.output
        assert "flash" not in client.calls
        assert client.closed


class TestNoErase:
    def test_no_erase_skips_the_erase_entirely(self, hexfile):
        client = FakeClient()
        result = run_flash(
            client, ["--hex", hexfile, "--box", "mybox", "--no-erase"])
        assert result.exit_code == 0, result.output
        assert client.calls == ["flash"]


class TestForceReconnectStaysNonFatal:
    """`--force-reconnect` is the user asking for a clean session, so it is
    the one place a reconnect belongs -- and its failure has always been a
    warning. Pin that deliberately: the erase-path connect is gone, this one
    stays, and it still cannot abort the flash."""

    def test_force_reconnect_failure_warns_and_flashes_anyway(self, hexfile):
        client = FakeClient(connect_error=http_500())
        result = run_flash(
            client, ["--hex", hexfile, "--box", "mybox", "--force-reconnect"])
        assert result.exit_code == 0, result.output
        assert "Warning: Force reconnect failed" in result.output
        # The ONLY connect/disconnect pair left in the flash path.
        assert client.calls == ["erase", "disconnect", "connect", "flash"]


# --------------------------------------------------------------------------- #
# The flash must not claim success when nothing was programmed                #
# --------------------------------------------------------------------------- #

# Excerpts below are trimmed from real runs against nRF5340 benches, not
# invented -- the exact byte patterns are what the verdict has to survive.

# A run where the probe never attached. /debug/flash still answered 200: the
# box's flash_device() is a generator that only yields the programmer's stdout
# and has no success channel at all, so this text was the sole evidence.
JLINK_CONNECT_FAILED = """\
Flashing device nRF5340_xxAA_APP via JLinkExe...
Connecting to J-Link...
J-Link is connected.
Target voltage: 1.81 V
Connecting to target...
AP[0]: Skipped. Could not read CPUID register
Attach to CPU failed. Executing connect under reset.
Failed to power up DAP
ERROR: Could not connect to target.
Error occurred: Could not connect to the target device.
02-00000000-00-00000027-002F: T356A06C0 000:061.794 - 10.056ms returns "O.K."
Please check power, connection and settings.
"""

# The same shape as above, programmed successfully.
JLINK_PROGRAMMED = """\
Flashing device nRF5340_xxAA_APP via JLinkExe...
Cortex-M33 identified.
'loadfile': Performing implicit reset & halt of MCU.
Downloading file [/tmp/tmpbh7c0j19.hex]...
J-Link: Flash download: Bank 2 @ 0x00000000: 1 range affected (4096 bytes)
J-Link: Flash download: Program speed: 222 KB/s
O.K.
"""

# Programmed, then the post-flash gdbserver failed to come back. flash_device()
# re-establishes a gdbserver AFTER programming, so this connect error belongs to
# the reconnect, not to the flash -- the part is programmed and the command must
# say so.
JLINK_PROGRAMMED_THEN_RECONNECT_FAILED = JLINK_PROGRAMMED + """\
Reconnecting GDB server...
Connecting to target...
ERROR: Could not connect to target.
Target connection failed. GDBServer will be closed...
"""

OPENOCD_PROGRAMMED = """\
** Programming Started **
wrote 32768 bytes from file /tmp/fw.hex in 1.203366s (26.593 KiB/s)
** Programming Finished **
"""

# J-Link could not download the RAMCode it programs flash with, so nothing was
# programmed -- from a DA1469x bench, box output verbatim. `Downloading file`
# comes FIRST: J-Link prints it before the RAMCode download, so it is no
# evidence of programming. The reset lines at the end are the box's post-flash
# step, which ran regardless on boxes before the fix.
JLINK_RAMCODE_VERIFY_FAILED = """\
Flashing device DA14695 via JLinkExe...
Downloading file [/tmp/tmp72n8yao0.bin]...
****** Error: Verification of RAMCode failed @ address 0x0080073C.
Write: 0x23009306 00039309
Read: 0x91804986 00039309
Failed to prepare for programming.
Failed to download RAMCode!
Error while determining flash info (Bank @ 0x16000000)
Unspecified error -1

DA1469x: resetting target via J-Link Commander...
Target reset — bootrom will reinitialise and boot application
"""

# The same failure reading back all zeros.
JLINK_RAMCODE_READ_ZEROS = JLINK_RAMCODE_VERIFY_FAILED.replace(
    "Write: 0x23009306 00039309\nRead: 0x91804986 00039309",
    "Write: 0x401D6541 D0092E00\nRead: 0x00000000 00000000",
)

JLINK_PROGRAMMING_FAILED = """\
Flashing device nRF5340_xxAA_APP via JLinkExe...
'loadfile': Performing implicit reset & halt of MCU.
Downloading file [/tmp/tmpbh7c0j19.hex]...
Error while programming flash: Programming failed.
"""

# An erase whose probe never attached, captured from a real run: J-Link Plus
# on an nRF5340, board unpowered, probe still enumerated. /debug/erase answered
# HTTP 200 with status "erase_complete" for this, which is the whole defect --
# the box's chip_erase() is a generator that only yields JLinkExe's stdout and
# has no success channel.
#
# chip_erase() runs `connect` then `erase`, and BOTH failed, which is why the
# text repeats. Note the real failure mode is a voltage complaint, not the
# "Failed to power up DAP" a detached-SWD run produces -- the verdict has to
# catch either.
JLINK_ERASE_CONNECT_FAILED = """\
Device "NRF5340_XXAA_APP" selected.


Connecting to target via SWD
Target voltage too low. Please check https://kb.segger.com/J-Link_cannot_connect_to_the_CPU#Target_connection.
Error occurred: Could not connect to the target device.
For troubleshooting steps visit: https://kb.segger.com/J-Link_Troubleshooting

Target connection not established yet but required for command.
Device "NRF5340_XXAA_APP" selected.


Connecting to target via SWD
Target voltage too low. Please check https://kb.segger.com/J-Link_cannot_connect_to_the_CPU#Target_connection.
Error occurred: Could not connect to the target device.
For troubleshooting steps visit: https://kb.segger.com/J-Link_Troubleshooting
"""

# A real successful erase on the same bench, trimmed of the CoreSight ROM-table
# dump. Kept verbatim otherwise, because of one line: `CPUID register:` is a
# HEALTHY line that sits one careless substring match away from the failure
# signature `Could not read CPUID register`. This fixture is what proves the
# verdict does not fire on it.
JLINK_ERASED = """\
Device "NRF5340_XXAA_APP" selected.


Connecting to target via SWD
Found SW-DP with ID 0x6BA02477
AP[0]: Core found
CPUID register: 0x410FD214. Implementer code: 0x41 (ARM)
Found Cortex-M33 r0p4, Little endian.
Cortex-M33 identified.

No address range specified, 'Erase Chip' will be executed
'erase': Performing implicit reset & halt of MCU.
Erasing device...
J-Link: Flash download: Only internal flash banks will be erased.
J-Link: Flash download: Total time needed: 0.317s (Prepare: 0.080s, Erase: 0.172s)
Erasing done.
"""

# The box hands these back with CRLF. `splitlines()` plus the `.strip()` in
# `_line_matches` absorb it, and this pins that -- a verdict that only worked
# on LF would pass every test here and fail on every real box.
JLINK_ERASE_CONNECT_FAILED_CRLF = JLINK_ERASE_CONNECT_FAILED.replace("\n", "\r\n")

# An erase that completed, whose scan skipped an access port it could not read.
# `Could not read CPUID register` is deliberately absent from
# `_CONNECT_FAILURE_SIGNATURES`, so this must NOT be read as a failure: the
# line is emitted per AP during a scan and does not on its own establish that
# the session never attached.
JLINK_ERASED_AFTER_AP_SKIP = """\
Erasing device NRF5340_XXAA_APP via JLinkExe...
AP[0]: Skipped. Could not read CPUID register
AP[1]: AHB-AP (IDR: 0x84770001)
Cortex-M33 identified.
Erasing device...
Erasing done.
O.K.
"""


class TestFlashVerdictFollowsTheProgrammer:
    """`lager debug flash` used to print "Flashed!" unconditionally.

    `flash()` did `result = client.flash(...)`, echoed `result['output']`, then
    ran `click.secho("Flashed!")` without ever inspecting either. Because
    /debug/flash answers 200 even when the probe never attached, the command
    could not fail short of an HTTP error -- observed on a bench printing
    "Flashed!" over a log reading "Could not connect to target", with the part
    left blank by the erase that preceded it.

    That matters most on THIS command: `flash` erases by default, so a silent
    failure does not leave the old image in place, it leaves nothing.
    """

    def test_connect_failure_is_reported_and_exits_nonzero(self, hexfile):
        client = FakeClient(flash_output=JLINK_CONNECT_FAILED)
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 1, result.output
        assert "Flash failed" in result.output
        assert "Flashed!" not in result.output

    def test_failure_message_warns_the_part_is_now_erased(self, hexfile):
        client = FakeClient(flash_output=JLINK_CONNECT_FAILED)
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert "NOT programmed" in result.output

    def test_api_trace_ok_in_a_failed_session_is_not_success(self, hexfile):
        """The failing log contains `returns "O.K."` -- J-Link's API trace, not
        the loadfile verdict. A substring test for success text passes on
        exactly the run this check exists to catch, so matching is per-line."""
        assert '"O.K."' in JLINK_CONNECT_FAILED
        client = FakeClient(flash_output=JLINK_CONNECT_FAILED)
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 1, result.output

    def test_successful_jlink_flash_still_reports_flashed(self, hexfile):
        client = FakeClient(flash_output=JLINK_PROGRAMMED)
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 0, result.output
        assert "Flashed!" in result.output

    def test_programmed_then_failed_gdbserver_reconnect_is_still_success(self, hexfile):
        """The false positive a bare denylist would ship: a connect error that
        arrives after programming is the reconnect, and calling that a failed
        flash is worse than the bug being fixed."""
        client = FakeClient(flash_output=JLINK_PROGRAMMED_THEN_RECONNECT_FAILED)
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 0, result.output
        assert "Flashed!" in result.output

    def test_openocd_programmed_output_is_success(self, hexfile):
        client = FakeClient(flash_output=OPENOCD_PROGRAMMED)
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 0, result.output
        assert "Flashed!" in result.output

    def test_unrecognised_output_keeps_its_existing_meaning(self, hexfile):
        """Older boxes return an empty body, and backends we have not
        characterised print something else entirely. Neither may start failing
        on an upgrade, so anything unmatched stays a success."""
        for output in ("", "Flashing device DA14695 via JLinkExe...\n"):
            client = FakeClient(flash_output=output)
            result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
            assert result.exit_code == 0, result.output
            assert "Flashed!" in result.output

    def test_verdict_applies_to_no_erase_runs_too(self, hexfile):
        client = FakeClient(flash_output=JLINK_CONNECT_FAILED)
        result = run_flash(
            client, ["--hex", hexfile, "--box", "mybox", "--no-erase"])
        assert result.exit_code == 1, result.output
        assert "Flash failed" in result.output


class TestAFailedProgrammingStepIsNotSuccess:
    """J-Link prints `Downloading file [...]` and only THEN downloads the
    RAMCode it programs flash with. `Downloading file` counted as evidence of
    programming, so a failed RAMCode download -- nothing programmed, the part
    left erased -- printed "Flashed!" and exited 0."""

    def test_a_failed_ramcode_download_fails_the_flash(self, hexfile):
        client = FakeClient(flash_output=JLINK_RAMCODE_VERIFY_FAILED)
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 1, result.output
        assert ("Flash failed: ****** Error: Verification of RAMCode failed "
                "@ address 0x0080073C.") in result.output
        assert "NOT programmed" in result.output
        assert "now erased" in result.output
        assert "Flashed!" not in result.output

    def test_the_all_zeros_read_fails_the_flash(self, hexfile):
        assert "Read: 0x00000000 00000000" in JLINK_RAMCODE_READ_ZEROS
        client = FakeClient(flash_output=JLINK_RAMCODE_READ_ZEROS)
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 1, result.output
        assert "Flashed!" not in result.output

    def test_crlf_output_from_a_real_box_is_handled(self, hexfile):
        client = FakeClient(
            flash_output=JLINK_RAMCODE_VERIFY_FAILED.replace("\n", "\r\n"))
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 1, result.output

    def test_no_erase_runs_fail_too(self, hexfile):
        client = FakeClient(flash_output=JLINK_RAMCODE_VERIFY_FAILED)
        result = run_flash(
            client, ["--hex", hexfile, "--box", "mybox", "--no-erase"])
        assert result.exit_code == 1, result.output
        assert "Flash failed" in result.output

    def test_a_programming_failure_after_downloading_fails_the_flash(self, hexfile):
        client = FakeClient(flash_output=JLINK_PROGRAMMING_FAILED)
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 1, result.output
        assert ("Flash failed: Error while programming flash: Programming failed."
                in result.output)

    @pytest.mark.parametrize("line", [
        "Failed to download RAMCode!",
        "Failed to download RAMCode.",
        "Failed to prepare for programming.",
        "Error while determining flash info (Bank @ 0x16000000)",
        "ERROR: Verification of RAMCode failed @ address 0x0080073C.",
    ])
    def test_each_failure_line_wins_over_programmed_evidence(self, line):
        assert debug_mod._flash_failure_line(
            JLINK_PROGRAMMED + line + "\n") == line

    @pytest.mark.parametrize("line", [
        # RAMCode J-Link downloads for other jobs, not for programming flash.
        "Failed to download RAMCode for indirect memory access!",
        "Failed to download RAMCode used to read FPU registers.",
        # A signature inside a line, not a line of its own.
        'note: "Failed to download RAMCode!" appears in this log',
        # Out of scope on purpose: a DA1469x cached-XIP compare reports a false
        # one on a correctly programmed part.
        "Verification failed @ address 0x16000000.",
    ])
    def test_lines_that_are_not_a_programming_failure(self, line):
        assert debug_mod._flash_failure_line(JLINK_PROGRAMMED + line + "\n") is None

    def test_a_verify_failure_after_programming_is_still_success(self, hexfile):
        """As a DA1469x bench printed it for an encrypted image body that was
        programmed correctly."""
        client = FakeClient(flash_output=(
            "Downloading file [/tmp/img.bin]...\n"
            "J-Link: Flash download: Bank 0 @ 0x16000000: 1 range affected (131072 bytes)\n"
            "Verification failed @ address 0x16020000.\n"))
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 0, result.output
        assert "Flashed!" in result.output


# A second J-Link client was driving the probe (a raw JLinkExe halting and
# resuming the target), so this Commander session could not use it at all.
# The session still exits normally; on a DA1469x bench the CLI printed
# "Erase complete" (in 0.24 s) and "Flashed!", exit 0, with the image header
# untouched. Note `Downloading file` and no `Flash download` line.
JLINK_PROBE_UNUSABLE = """\
Flashing device DA14695 via JLinkExe...
Selected interface (SWD) is not supported by the connected probe.
Downloading file [/tmp/tmpq1w2e3r4.bin]...
Target connection not established yet but required for command.
"""

JLINK_ERASE_PROBE_UNUSABLE = """\
Device "DA14695" selected.
Selected interface (SWD) is not supported by the connected probe.
Target connection not established yet but required for command.
"""


class TestAProbeThatCannotBeUsedIsNotSuccess:
    """Commander refusing every command because the probe is busy with another
    client is not a connect failure J-Link names as such, and it does not
    stop the session -- so erase and flash both read as complete."""

    def test_the_flash_fails_on_the_first_unusable_line(self, hexfile):
        client = FakeClient(flash_output=JLINK_PROBE_UNUSABLE)
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 1, result.output
        assert ("Flash failed: Selected interface (SWD) is not supported by the "
                "connected probe.") in result.output
        assert "Flashed!" not in result.output

    @pytest.mark.parametrize("line", [
        "Selected interface (SWD) is not supported by the connected probe.",
        "Target connection not established yet but required for command.",
        "J-Link connection not established yet but required for command.",
        "Connecting to J-Link via USB...FAILED",
    ])
    def test_each_unusable_line_wins_over_programmed_evidence(self, line):
        assert debug_mod._flash_failure_line(JLINK_PROGRAMMED + line + "\n") == line

    def test_the_erase_fails_too(self):
        client = FakeClient(erase_output=JLINK_ERASE_PROBE_UNUSABLE)
        result = run_erase(client, ["--box", "mybox", "--yes"])
        assert result.exit_code == 1, result.output
        assert "Erase complete!" not in result.output
        assert "Selected interface (SWD) is not supported" in result.output

    def test_the_flash_pre_erase_fails_too(self, hexfile):
        client = FakeClient(erase_output=JLINK_ERASE_PROBE_UNUSABLE,
                            flash_output=JLINK_PROGRAMMED)
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 1, result.output
        assert "Flash erase failed" in result.output
        assert "flash" not in client.calls


class TestADownloadThatNeverReachedFlashIsNotSuccess:
    """J-Link prints `J-Link: Flash download: Bank ...` for every bank it
    touches, a skipped one included. `Downloading file` with none after it
    means `loadfile` never reached flash, whatever else was printed."""

    def test_no_flash_download_line_fails_the_flash(self, hexfile):
        client = FakeClient(flash_output=(
            "Downloading file [/tmp/img.bin]...\nO.K.\n"))
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 1, result.output
        assert ("Flash failed: J-Link printed `Downloading file` but no "
                "`Flash download` line after it") in result.output

    def test_a_second_file_without_a_flash_download_fails(self):
        output = JLINK_PROGRAMMED + "Downloading file [/tmp/second.bin]...\nO.K.\n"
        assert debug_mod._flash_failure_line(output) == debug_mod._NO_FLASH_DOWNLOAD

    def test_a_bank_skipped_because_it_already_matches_is_success(self, hexfile):
        client = FakeClient(flash_output=(
            "Downloading file [/tmp/img.bin]...\n"
            "J-Link: Flash download: Bank 0 @ 0x16000000: Skipped. Contents already match\n"
            "O.K.\n"))
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 0, result.output
        assert "Flashed!" in result.output

    def test_openocd_output_is_not_held_to_the_j_link_rule(self, hexfile):
        client = FakeClient(flash_output=OPENOCD_PROGRAMMED)
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 0, result.output


class TestTheBoxVerdictIsUsedWhenPresent:
    """A newer box judges the flash from the programming session alone and
    says so in `programmed` / `error`; an older box sends neither, and its
    text is read instead."""

    def test_a_box_reporting_not_programmed_fails_the_flash(self, hexfile):
        client = FakeClient(
            flash_output=JLINK_RAMCODE_VERIFY_FAILED,
            flash_verdict={"programmed": False, "error": "Failed to download RAMCode!"})
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 1, result.output
        assert "Flash failed: Failed to download RAMCode!" in result.output
        assert "now erased" in result.output

    def test_a_box_reporting_not_programmed_without_a_line_still_fails(self, hexfile):
        client = FakeClient(flash_output="",
                            flash_verdict={"programmed": False, "error": None})
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 1, result.output
        assert "Flash failed" in result.output

    def test_a_box_reporting_programmed_wins_over_reconnect_noise(self, hexfile):
        client = FakeClient(flash_output=JLINK_PROGRAMMED_THEN_RECONNECT_FAILED,
                            flash_verdict={"programmed": True, "error": None})
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 0, result.output
        assert "Flashed!" in result.output

    def test_an_older_box_is_judged_by_its_text(self, hexfile):
        client = FakeClient(flash_output=JLINK_RAMCODE_VERIFY_FAILED)
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 1, result.output


# --------------------------------------------------------------------------- #
# The erase must not claim success when nothing was erased                    #
# --------------------------------------------------------------------------- #

class TestEraseVerdictFollowsTheProgrammer:
    """`lager debug <net> erase` used to print "Erase complete!" on any HTTP
    200. /debug/erase answers 200 on the J-Link path whether or not the probe
    ever attached, so with the probe enumerated and the target unplugged the
    command reported a successful erase, exited 0, and left the part untouched.

    Same verdict rule as flash: the programmer's own output decides, and output
    matching nothing keeps its existing meaning, so an older box or a backend
    we have not characterised is never newly reported as failing.
    """

    def test_connect_failure_is_reported_and_exits_nonzero(self):
        client = FakeClient(erase_output=JLINK_ERASE_CONNECT_FAILED)
        result = run_erase(client, ["--box", "mybox", "--yes"])
        assert result.exit_code == 1, result.output
        assert "Erase complete!" not in result.output
        assert "Erase failed" in result.output

    def test_failure_names_the_line_it_failed_on(self):
        # The captured line, verbatim. Note it is NOT the
        # "ERROR: Could not connect to target." shape the flash fixture has --
        # a real erase against an unpowered board says this instead, and the
        # signature list has to cover both.
        client = FakeClient(erase_output=JLINK_ERASE_CONNECT_FAILED)
        result = run_erase(client, ["--box", "mybox", "--yes"])
        assert "Error occurred: Could not connect to the target device." in result.output

    def test_failure_says_the_target_was_not_erased(self):
        # The operator's next move depends on knowing the part is untouched --
        # the opposite of flash, which warns that it IS now erased.
        client = FakeClient(erase_output=JLINK_ERASE_CONNECT_FAILED)
        result = run_erase(client, ["--box", "mybox", "--yes"])
        assert "was NOT erased" in result.output

    def test_it_bails_before_the_post_erase_reconnect(self):
        # A failed erase must not go on to disconnect/reconnect the debugger:
        # that is the work of a successful erase, and its own failure would
        # print a yellow warning on top of a red one.
        client = FakeClient(erase_output=JLINK_ERASE_CONNECT_FAILED)
        run_erase(client, ["--box", "mybox", "--yes"])
        assert client.calls == ["erase"]
        assert client.closed

    def test_api_trace_ok_in_a_failed_session_is_not_success(self):
        # J-Link's API trace prints `returns "O.K."` mid-failure. Substring
        # matching on success text would pass exactly the run this exists to
        # catch.
        failed = JLINK_ERASE_CONNECT_FAILED + (
            '02-00000000-00-00000027-002F: T356A06C0 000:061.794 - '
            '10.056ms returns "O.K."\n')
        client = FakeClient(erase_output=failed)
        result = run_erase(client, ["--box", "mybox", "--yes"])
        assert result.exit_code == 1, result.output

    def test_successful_erase_still_reports_erase_complete(self):
        client = FakeClient(erase_output=JLINK_ERASED)
        result = run_erase(client, ["--box", "mybox", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Erase complete!" in result.output

    def test_a_skipped_ap_is_not_a_failure(self):
        # Pins the deliberate exclusion. `Could not read CPUID register` is
        # matched by the box's RETRY predicate and by neither verdict
        # predicate, because it is emitted per access port during a scan and
        # does not on its own mean the session never attached.
        client = FakeClient(erase_output=JLINK_ERASED_AFTER_AP_SKIP)
        result = run_erase(client, ["--box", "mybox", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Erase complete!" in result.output

    def test_crlf_output_from_a_real_box_is_handled(self):
        # The box returns CRLF. A verdict that only split on LF would pass
        # every other test here and fail on every real box.
        client = FakeClient(erase_output=JLINK_ERASE_CONNECT_FAILED_CRLF)
        result = run_erase(client, ["--box", "mybox", "--yes"])
        assert result.exit_code == 1, result.output

    def test_unrecognised_output_keeps_its_existing_meaning(self):
        # What an older box sends when chip_erase() yielded nothing.
        client = FakeClient(erase_output="Erase completed")
        result = run_erase(client, ["--box", "mybox", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Erase complete!" in result.output

    def test_verbose_list_output_is_joined_before_the_verdict(self):
        # Verbose mode hands back a list of lines, not a string.
        client = FakeClient(
            erase_output=JLINK_ERASE_CONNECT_FAILED.splitlines())
        result = run_erase(client, ["--box", "mybox", "--yes"])
        assert result.exit_code == 1, result.output

    def test_json_mode_still_prints_the_payload_on_failure(self):
        client = FakeClient(erase_output=JLINK_ERASE_CONNECT_FAILED)
        result = run_erase(client, ["--box", "mybox", "--yes", "--json"])
        assert result.exit_code == 1, result.output
        assert '"status": "erase_complete"' in result.output

    def test_the_verdict_applies_to_the_flash_pre_erase_too(self, hexfile):
        # flash() erases by default and discarded the result entirely, so it
        # printed "Erase complete!" and then programmed a part that was never
        # reached.
        client = FakeClient(erase_output=JLINK_ERASE_CONNECT_FAILED)
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 1, result.output
        assert "Flash erase failed" in result.output
        assert "flash" not in client.calls
        assert client.closed


# --------------------------------------------------------------------------- #
# The box's message is what the user sees, once (#517)                        #
# --------------------------------------------------------------------------- #

def http_500_with_error(error, path="/debug/erase"):
    """An HTTPError whose response carries the box's JSON `error` body."""
    response = requests.Response()
    response.status_code = 500
    response.reason = "Internal Server Error"
    response.url = f"http://{BOX_IP}:8765{path}"
    response._content = json.dumps({"error": error}).encode()
    return requests.exceptions.HTTPError(
        "500 Server Error: Internal Server Error", response=response)


class TestTheBoxErrorIsPrintedOnce:
    """`lager debug erase` read the box's `error` field; the flash pre-erase
    printed the raw HTTPError instead. And the box's erase message already
    starts with "Erase failed:", so `erase` printed that prefix twice.
    """

    BOX_ERROR = "Erase failed: Could not connect to target."

    def test_a_failed_pre_erase_prints_the_box_error(self, hexfile):
        client = FakeClient(erase_error=http_500_with_error(self.BOX_ERROR))
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox"])
        assert result.exit_code == 1, result.output
        assert "Flash erase failed: Could not connect to target." in result.output
        assert "500 Server Error" not in result.output
        assert "flash" not in client.calls

    def test_a_failed_erase_says_erase_failed_once(self):
        client = FakeClient(erase_error=http_500_with_error(self.BOX_ERROR))
        result = run_erase(client, ["--box", "mybox", "--yes"])
        assert result.exit_code == 1, result.output
        assert result.output.count("Erase failed:") == 1, result.output
        assert "Erase failed: Could not connect to target." in result.output

    def test_a_box_error_without_the_prefix_is_kept_whole(self):
        client = FakeClient(erase_error=http_500_with_error("No debugger connection found"))
        result = run_erase(client, ["--box", "mybox", "--yes"])
        assert "Erase failed: No debugger connection found" in result.output

    def test_an_error_with_no_json_body_falls_back_to_the_exception(self):
        client = FakeClient(erase_error=http_500("Failed to power up DAP"))
        result = run_erase(client, ["--box", "mybox", "--yes"])
        assert result.exit_code == 1, result.output
        assert "Erase failed: 500 Server Error: Failed to power up DAP" in result.output


# --------------------------------------------------------------------------- #
# --erase-start / --erase-size                                                #
# --------------------------------------------------------------------------- #

RANGE_2M = ["--erase-start", "0x16000000", "--erase-size", "2M"]

# What the box reports for RANGE_2M.
ERASED_2M = {"start": 0x16000000, "end": 0x161FFFFF, "length": 0x200000,
             "source": "request", "text": "0x16000000-0x161FFFFF (2 MiB)"}


class TestEraseRangeFlags:
    """An explicit erase range: parsed on the CLI, checked before any box
    traffic, refused on a box that would ignore it, and reported back."""

    def test_both_flags_exist_on_both_commands(self):
        for command in (debug_mod.flash, debug_mod.erase):
            assert {"erase_start", "erase_size"} <= {p.name for p in command.params}

    def test_the_range_is_sent_as_integers(self):
        client = FakeClient(erase_range=ERASED_2M)
        result = run_erase(client, ["--box", "mybox", "--yes", *RANGE_2M], net=DA1469X_NET)
        assert result.exit_code == 0, result.output
        assert client.erase_kwargs == [{"erase_start": 0x16000000, "erase_size": 0x200000}]
        assert "Erasing flash memory (0x16000000-0x161FFFFF (2 MiB))..." in result.output
        assert "Erase complete: 0x16000000-0x161FFFFF (2 MiB)" in result.output

    def test_no_flags_send_no_range_and_ask_nothing_of_health(self):
        client = FakeClient()
        run_erase(client, ["--box", "mybox", "--yes"])
        assert client.erase_kwargs == [{"erase_start": None, "erase_size": None}]
        assert "health" not in client.calls

    def test_the_flash_pre_erase_carries_the_range(self, hexfile):
        client = FakeClient(erase_range=ERASED_2M)
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox", *RANGE_2M],
                           net=DA1469X_NET)
        assert result.exit_code == 0, result.output
        assert client.calls == ["health", "erase", "flash"]
        assert client.erase_kwargs == [{"erase_start": 0x16000000, "erase_size": 0x200000}]
        assert "Erasing flash memory (0x16000000-0x161FFFFF (2 MiB))..." in result.output
        assert "Erase complete: 0x16000000-0x161FFFFF (2 MiB)" in result.output

    @pytest.mark.parametrize("args, net, message", [
        (["--erase-start", "0x16000000"], DA1469X_NET, "must be given together"),
        (["--erase-size", "2M"], DA1469X_NET, "must be given together"),
        (["--erase-start", "0x15000000", "--erase-size", "1M"], DA1469X_NET,
         "outside the DA1469x QSPI XIP window"),
        (["--erase-start", "0x17F00000", "--erase-size", "2M"], DA1469X_NET,
         "outside the DA1469x QSPI XIP window"),
        (["--erase-start", "0xFFFFF000", "--erase-size", "8K"], JLINK_NET,
         "32-bit address space"),
        (["--erase-start=-1", "--erase-size", "1M"], JLINK_NET, "must not be negative"),
    ], ids=["start-only", "size-only", "below-window", "past-window", "past-4gib", "negative"])
    def test_a_bad_range_is_refused_before_any_box_traffic(self, args, net, message):
        client = FakeClient()
        result = run_erase(client, ["--box", "mybox", "--yes", *args], net=net)
        assert result.exit_code == 1, result.output
        assert message in result.output
        assert client.calls == []

    def test_a_bad_range_is_refused_by_flash_too(self, hexfile):
        client = FakeClient()
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox", "--erase-size", "2M"])
        assert result.exit_code == 1, result.output
        assert "must be given together" in result.output
        assert client.calls == []

    def test_no_erase_with_a_range_is_refused(self, hexfile):
        client = FakeClient()
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox", "--no-erase", *RANGE_2M],
                           net=DA1469X_NET)
        assert result.exit_code == 1, result.output
        assert "--no-erase cannot be combined with --erase-start/--erase-size" in result.output
        assert client.calls == []

    def test_a_non_da1469x_net_is_not_held_to_the_window(self):
        client = FakeClient(erase_range={"start": 0x08000000, "end": 0x08000FFF, "length": 4096,
                                         "source": "request",
                                         "text": "0x08000000-0x08000FFF (4 KiB)"})
        result = run_erase(client, ["--box", "mybox", "--yes",
                                    "--erase-start", "0x08000000", "--erase-size", "4096"])
        assert result.exit_code == 0, result.output
        assert client.erase_kwargs == [{"erase_start": 0x08000000, "erase_size": 4096}]
        assert "Erase complete: 0x08000000-0x08000FFF (4 KiB)" in result.output

    def test_an_old_box_is_refused_before_the_erase(self):
        # A box that predates the keys lists no `features`: it would accept
        # the request, ignore the range, and erase its default 1 MiB.
        client = FakeClient(health={"status": "healthy", "version": "1.0.0"})
        result = run_erase(client, ["--box", "mybox", "--yes", *RANGE_2M], net=DA1469X_NET)
        assert result.exit_code == 1, result.output
        assert "does not support --erase-start/--erase-size" in result.output
        assert "requires box version 0.50.0 or later" in result.output
        assert "lager update --box mybox" in result.output
        assert client.calls == ["health"]
        assert client.closed

    def test_an_unreachable_health_endpoint_reads_as_unsupported(self):
        client = FakeClient(health_error=http_500("connection refused"))
        result = run_erase(client, ["--box", "mybox", "--yes", *RANGE_2M], net=DA1469X_NET)
        assert result.exit_code == 1, result.output
        assert "does not support --erase-start/--erase-size" in result.output
        assert "erase" not in client.calls

    def test_the_flash_pre_erase_is_gated_the_same_way(self, hexfile):
        client = FakeClient(health={"status": "healthy"})
        result = run_flash(client, ["--hex", hexfile, "--box", "mybox", *RANGE_2M],
                           net=DA1469X_NET)
        assert result.exit_code == 1, result.output
        assert "does not support --erase-start/--erase-size" in result.output
        assert client.calls == ["health"]
        assert client.closed

    def test_a_full_chip_erase_says_so(self):
        client = FakeClient(erase_range=None)
        result = run_erase(client, ["--box", "mybox", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Erase complete: full chip" in result.output

    def test_a_reported_default_range_is_shown_without_flags(self):
        client = FakeClient(erase_range={"start": 0x16000000, "end": 0x160FFFFF,
                                         "length": 0x100000, "source": "default",
                                         "text": "0x16000000-0x160FFFFF (1 MiB)"})
        result = run_erase(client, ["--box", "mybox", "--yes"], net=DA1469X_NET)
        assert "Erase complete: 0x16000000-0x160FFFFF (1 MiB)" in result.output

    def test_an_older_box_keeps_the_old_line(self, hexfile):
        client = FakeClient()  # no erase_range key at all
        assert "Erase complete!" in run_erase(client, ["--box", "mybox", "--yes"]).output
        assert "Erase complete!" in run_flash(client, ["--hex", hexfile, "--box", "mybox"]).output

    def test_json_output_carries_the_range(self):
        client = FakeClient(erase_range=ERASED_2M)
        result = run_erase(client, ["--box", "mybox", "--json", *RANGE_2M], net=DA1469X_NET)
        assert result.exit_code == 0, result.output
        # The payload follows the progress line; it is the box dict, verbatim.
        start = result.output.index("{")
        payload = json.loads(result.output[start:result.output.index("\n}", start) + 2])
        assert payload["erase_range"] == ERASED_2M

    def test_the_prompt_names_the_range(self):
        client = FakeClient(erase_range=ERASED_2M)
        result = run_erase(client, ["--box", "mybox", *RANGE_2M], net=DA1469X_NET, input="n\n")
        assert "This will erase 0x16000000-0x161FFFFF (2 MiB) on DA14695" in result.output
        assert "Chip erase cancelled." in result.output
        assert client.calls == []


# --------------------------------------------------------------------------- #
# `health` shows the features the gate reads                                  #
# --------------------------------------------------------------------------- #

def run_health(client, args, net=JLINK_NET):
    """Invoke `lager debug <net> health` with the box mocked at the client."""
    obj = _Obj()
    obj.net_name = net["name"]
    with patch.object(debug_mod, "_resolve_box_with_username",
                      lambda ctx, box: (BOX_IP, "lagerdata")), \
         patch.object(debug_mod, "_get_service_client", lambda box: client):
        return CliRunner().invoke(debug_mod.health, args, obj=obj, catch_exceptions=False)


class TestHealthListsFeatures:
    """The only way to see the `erase_range` capability used to be the refusal
    message; `health` now prints the list the gate reads."""

    def test_features_are_listed(self):
        client = FakeClient(health={"status": "healthy", "version": "1.0.0",
                                    "features": ["erase_range"], "uptime": 42.0})
        result = run_health(client, ["--box", "mybox"])
        assert result.exit_code == 0, result.output
        assert "Features: erase_range" in result.output

    def test_an_older_box_prints_none_reported(self):
        client = FakeClient(health={"status": "healthy", "version": "1.0.0", "uptime": 42.0})
        result = run_health(client, ["--box", "mybox"])
        assert result.exit_code == 0, result.output
        assert "Features: none reported" in result.output
