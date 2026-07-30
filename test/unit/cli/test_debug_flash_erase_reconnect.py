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


class FakeClient:
    """A DebugServiceClient that records every box call in order.

    `connect_error` makes `/debug/connect` behave the way the box does against
    a just-erased part: HTTP 500. `erase_error` does the same for
    `/debug/erase`. Signatures mirror `service_client.DebugServiceClient` so a
    change there surfaces here as a TypeError rather than a false pass.
    """

    def __init__(self, connect_error=None, erase_error=None, flash_output=""):
        self.calls: list[str] = []
        self.connect_error = connect_error
        self.erase_error = erase_error
        self.flash_output = flash_output
        self.closed = False

    def erase(self, net, speed='4000', transport='SWD'):
        self.calls.append("erase")
        if self.erase_error:
            raise self.erase_error
        return {"status": "erase_complete", "output": "Erase completed"}

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
        return {"status": "flash_complete", "output": self.flash_output}

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
