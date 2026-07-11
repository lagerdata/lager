#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the custom-device assignment helpers in
``cli/commands/box/net_tui.py``.

The TUI's assign flow (AssignDeviceScreen / DevicePickDialog /
ConfirmUnassignDialog) is driven by three module-level helpers that carry
all the decision logic, so they're tested here without a running Textual
app — same approach as test_net_tui_uart_guard.py:

  * ``_assign_payload`` — picks the durable cable identity (serial when the
    cable has one, else port path) and forwards the optional baud override.
  * ``_cable_ident`` — the human label used by both the tree rows and the
    confirmation dialogs.
  * ``_run_custom_devices`` — wraps the box's /custom-devices/* endpoints;
    None (transport failure / non-JSON / 404 from an old box image) is the
    failure signal, while box-reported errors come back as {"error": ...}.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import threading
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

tui = importlib.import_module('cli.commands.box.net_tui')


# --------------------------------------------------------------------------- #
# _assign_payload                                                             #
# --------------------------------------------------------------------------- #

class TestAssignPayload:
    def test_prefers_serial_identity(self):
        cable = {"serial": "00000006", "port_path": "1-1.2"}
        payload = tui._assign_payload(cable, "Rigol_DP711", None)
        assert payload == {"instrument": "Rigol_DP711", "serial": "00000006"}

    def test_falls_back_to_port_path(self):
        cable = {"serial": None, "port_path": "1-1.2"}
        payload = tui._assign_payload(cable, "Rigol_DP711", None)
        assert payload == {"instrument": "Rigol_DP711", "port_path": "1-1.2"}

    def test_baud_included_only_when_set(self):
        cable = {"serial": "S"}
        assert "baud" not in tui._assign_payload(cable, "Rigol_DP711", None)
        assert tui._assign_payload(cable, "Rigol_DP711", 19200)["baud"] == 19200


# --------------------------------------------------------------------------- #
# _cable_ident                                                                #
# --------------------------------------------------------------------------- #

class TestCableIdent:
    def test_serial_form(self):
        assert tui._cable_ident({"serial": "00000006"}) == "serial 00000006"

    def test_port_form_when_no_serial(self):
        assert tui._cable_ident({"serial": None, "port_path": "1-1.2"}) == "port 1-1.2"


# --------------------------------------------------------------------------- #
# _run_custom_devices                                                         #
# --------------------------------------------------------------------------- #

class _Resp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class TestRunCustomDevices:
    def _with_response(self, resp):
        with patch("requests.get", return_value=resp), \
             patch("requests.post", return_value=resp):
            return tui._run_custom_devices("box", "list")

    def test_parses_dict_response(self):
        data = {"catalog": [], "assignments": [], "cables": []}
        assert self._with_response(_Resp(200, data)) == data

    def test_missing_endpoint_is_failure(self):
        # Old box image: /custom-devices/* doesn't exist yet → 404.
        assert self._with_response(_Resp(404, {"error": "not found"})) is None

    def test_non_json_response_is_failure(self):
        assert self._with_response(_Resp(200, None)) is None

    def test_transport_failure_is_none(self):
        import requests
        with patch("requests.get", side_effect=requests.ConnectionError("down")):
            assert tui._run_custom_devices("box", "list") is None

    def test_box_error_dict_is_passed_through(self):
        # 400 + {"error": ...} (AssignmentError on the box) comes back as-is
        # so callers can surface the reason.
        err = {"error": "No USB-serial cable with serial number X"}
        with patch("requests.post", return_value=_Resp(400, err)):
            assert tui._run_custom_devices("box", "assign", {"x": 1}) == err

    def test_list_gets_and_actions_post_payload(self):
        with patch("requests.get", return_value=_Resp(200, {})) as get:
            tui._run_custom_devices("mybox", "list")
        url = get.call_args[0][0]
        assert url.endswith("/custom-devices/list") and "mybox" in url

        with patch("requests.post", return_value=_Resp(200, {})) as post:
            tui._run_custom_devices("mybox", "assign", {"x": 1})
        url = post.call_args[0][0]
        assert url.endswith("/custom-devices/assign")
        assert post.call_args[1]["json"] == {"x": 1}


# --------------------------------------------------------------------------- #
# --as-net twin (CreateNetDialog helpers)                                     #
# --------------------------------------------------------------------------- #

ASSIGNMENT = {
    "instrument": "Rigol_DP711",
    "address": "serial://067b:23a3/serial/00000006",
    "roles": ["power-supply"],
    "channels": {"power-supply": ["1"]},
}


class TestNetFromAssignment:
    def test_builds_power_supply_net(self):
        net = tui._net_from_assignment(ASSIGNMENT, "main_supply")
        assert net.net == "main_supply"
        assert net.instrument == "Rigol_DP711"
        # REGRESSION: the saved role must be the scanner-vocabulary
        # "power-supply" — the supply CLI and the box dispatcher match it
        # exactly; the legacy "supply" token saves an undriveable net.
        assert net.type == "power-supply"
        assert net.chan == "1"
        assert net.addr == ASSIGNMENT["address"]
        assert net.saved is False

    def test_falls_back_to_power_supply_defaults(self):
        # Old backend response without roles/channels: sane defaults.
        net = tui._net_from_assignment(
            {"instrument": "Rigol_DP711", "address": "serial://x"}, "n")
        assert net.type == "power-supply"
        assert net.chan == "1"


class TestDefaultNetName:
    def test_derives_from_instrument(self):
        assert tui._default_net_name(ASSIGNMENT) == "rigol_dp711"

    def test_tolerates_missing_instrument(self):
        assert tui._default_net_name({}) == "net"


class TestNetNameTaken:
    def test_saved_name_is_taken(self):
        nets = [tui.Net("I", "1", "power-supply", "supply1", "a", saved=True)]
        assert tui._net_name_taken(nets, "supply1") is True

    def test_unsaved_placeholder_does_not_block(self):
        # Placeholders get auto-names; only *saved* nets reserve a name.
        nets = [tui.Net("I", "1", "power-supply", "supply1", "a", saved=False)]
        assert tui._net_name_taken(nets, "supply1") is False
        assert tui._net_name_taken(nets, "other") is False


class TestAddressHasSavedNet:
    """REGRESSION: a baud-only re-assign must not re-offer the Create Net
    dialog — a second net at the same serial:// address would bypass the
    single-channel constraint nets-add enforces."""

    ADDR = "serial://067b:23a3/serial/00000006"

    def test_saved_net_at_address_suppresses_offer(self):
        nets = [tui.Net("Rigol_DP711", "1", "power-supply", "supply1",
                        self.ADDR, saved=True)]
        assert tui._address_has_saved_net(nets, self.ADDR) is True

    def test_unsaved_placeholder_does_not_suppress(self):
        nets = [tui.Net("Rigol_DP711", "1", "power-supply", "supply1",
                        self.ADDR, saved=False)]
        assert tui._address_has_saved_net(nets, self.ADDR) is False

    def test_other_address_does_not_suppress(self):
        nets = [tui.Net("Rigol_DP711", "1", "power-supply", "supply1",
                        "serial://067b:23a3/serial/OTHER", saved=True)]
        assert tui._address_has_saved_net(nets, self.ADDR) is False
        assert tui._address_has_saved_net(nets, "") is False


# --------------------------------------------------------------------------- #
# row labels — markup safety                                                  #
# --------------------------------------------------------------------------- #

class TestRowLabels:
    """REGRESSION: device fields like ``[067b:23a3]`` crashed the TUI with
    ``MarkupError: Expected markup style value`` when interpolated into
    markup-parsed widget content. Tree rows therefore use ``rich.Text``
    objects (markup-inert) and the dialogs pass ``markup=False``.
    """

    CABLE = {"serial": "00000006", "vid": "067b", "pid": "23a3",
             "port_path": "1-1.2", "tty": "/dev/ttyUSB0"}

    def test_cable_label_is_markup_inert_text(self):
        from rich.text import Text
        label = tui._cable_row_label(self.CABLE)
        assert isinstance(label, Text)
        # The vid:pid brackets survive literally instead of being parsed.
        assert "[067b:23a3]" in label.plain
        assert "/dev/ttyUSB0" in label.plain

    def test_assignment_label_is_markup_inert_text(self):
        from rich.text import Text
        label = tui._assignment_row_label({
            "instrument": "Rigol_DP711", "serial": "00000006",
            "tty": "/dev/ttyUSB0", "baud": 19200,
        })
        assert isinstance(label, Text)
        assert "Rigol_DP711" in label.plain
        assert "baud 19200" in label.plain
        assert "→ /dev/ttyUSB0" in label.plain

    def test_assignment_label_unplugged_cable(self):
        label = tui._assignment_row_label({"instrument": "Rigol_DP711",
                                           "serial": "S"})
        assert "(cable not connected)" in label.plain

    def test_bracketed_device_data_renders_literally_with_markup_false(self):
        # Pin the contract this guards: device strings contain markup-like
        # brackets ("[067b:23a3]"), so DevicePickDialog / ConfirmUnassignDialog
        # Statics must be built with markup=False, which renders the brackets
        # literally. (The failure mechanism varies by textual version: < 8
        # crashed outright on markup=True; >= 8 silently eats the brackets
        # as style tags. markup=False is correct on both.)
        from textual.widgets import Static
        content = tui._cable_row_label(self.CABLE).plain

        rendered = Static(content, markup=False).render()
        plain = getattr(rendered, 'plain', None)
        text = plain if isinstance(plain, str) else str(rendered)
        assert "[067b:23a3]" in text


# --------------------------------------------------------------------------- #
# UI responsiveness (textual test pilot)                                      #
# --------------------------------------------------------------------------- #
# REGRESSION (reported against 0.25.0): box round-trips ran synchronously
# inside button handlers and on_mount, freezing the whole event loop — the
# TUI ignored clicks for seconds at launch and after pressing Assign Device.
# Box I/O must run through NetApp.run_box_job (worker thread), never on the
# UI thread.

from textual.widgets import Button  # noqa: E402

DEVICES_DATA = {"catalog": [], "assignments": [], "cables": []}


def _make_app():
    return tui.NetApp(ctx=None, dut="box", inst_list=[], nets=[])


class TestStartupDoesNotBlock:
    def test_on_mount_makes_no_box_roundtrip(self):
        # launch_tui already fetched instruments and saved nets; re-fetching
        # at mount blocked the freshly painted UI for another round-trip.
        async def main():
            with patch("requests.request") as req, \
                 patch("requests.get") as get, \
                 patch("requests.post") as post:
                app = _make_app()
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause()
            assert req.call_count == 0
            assert get.call_count == 0
            assert post.call_count == 0
        asyncio.run(main())


class TestAssignButtonOffloadsBoxCall:
    def test_ui_stays_live_while_listing_devices(self):
        gate = threading.Event()
        seen: dict = {}

        def fake_list(dut, action, payload=None):
            seen["thread"] = threading.current_thread()
            gate.wait(timeout=2)
            return DEVICES_DATA

        async def main():
            app = _make_app()
            with patch.object(tui, "_run_custom_devices", side_effect=fake_list):
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause()
                    app.on_button_pressed(Button.Pressed(app.assign_btn))
                    # The handler must return with the box call still in
                    # flight: button disabled as feedback, screen not yet
                    # pushed, event loop alive (this pause would hang under
                    # the old synchronous code).
                    await pilot.pause()
                    assert app.assign_btn.disabled is True
                    assert not isinstance(app.screen, tui.AssignDeviceScreen)
                    gate.set()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    assert isinstance(app.screen, tui.AssignDeviceScreen)
                    assert app.assign_btn.disabled is False
            assert seen["thread"] is not threading.main_thread()

        asyncio.run(main())

    def test_old_box_image_shows_error_and_reenables_button(self):
        async def main():
            app = _make_app()
            with patch.object(tui, "_run_custom_devices", return_value=None):
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause()
                    app.on_button_pressed(Button.Pressed(app.assign_btn))
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    assert not isinstance(app.screen, tui.AssignDeviceScreen)
                    assert app.assign_btn.disabled is False

        asyncio.run(main())


class TestAssignActionsOffloadBoxCalls:
    CABLE = {"serial": "00000006", "vid": "067b", "pid": "23a3",
             "port_path": "1-1.2", "tty": "/dev/ttyUSB0"}

    def test_do_assign_runs_in_worker_and_offers_create_net(self):
        seen: dict = {}

        def fake_devices(dut, action, payload=None):
            seen.setdefault("threads", []).append(threading.current_thread())
            if action == "assign":
                return {"instrument": "Rigol_DP711", "address": "serial://x"}
            return DEVICES_DATA

        async def main():
            app = _make_app()
            app._fetch_instruments = lambda: []
            with patch.object(tui, "_run_custom_devices", side_effect=fake_devices):
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause()
                    screen = tui.AssignDeviceScreen({"catalog": [],
                                                     "assignments": [],
                                                     "cables": [self.CABLE]})
                    app.push_screen(screen)
                    await pilot.pause()
                    screen._do_assign(self.CABLE, "Rigol_DP711", None)
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    # Success path: busy state cleared, data refreshed, and
                    # the --as-net twin dialog offered on top.
                    assert screen.busy_note.display is False
                    assert screen.assign_tree.disabled is False
                    assert screen.data == DEVICES_DATA
                    assert isinstance(app.screen, tui.CreateNetDialog)
            assert all(t is not threading.main_thread() for t in seen["threads"])

        asyncio.run(main())

    def test_do_unassign_runs_in_worker_and_rebuilds(self):
        assignment = {"instrument": "Rigol_DP711", "serial": "00000006"}

        def fake_devices(dut, action, payload=None):
            if action == "remove":
                return {"removed": True}
            return DEVICES_DATA

        async def main():
            app = _make_app()
            app._fetch_instruments = lambda: []
            app._fetch_saved_records = lambda: []
            with patch.object(tui, "_run_custom_devices", side_effect=fake_devices):
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause()
                    screen = tui.AssignDeviceScreen(
                        {"catalog": [], "assignments": [assignment], "cables": []})
                    app.push_screen(screen)
                    await pilot.pause()
                    screen._do_unassign(assignment)
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    assert screen.busy_note.display is False
                    assert screen.assign_tree.disabled is False
                    assert screen.data == DEVICES_DATA

        asyncio.run(main())


class TestRunPythonOffMainThread:
    """REGRESSION: run_python_internal installed a SIGINT handler on every
    call, but signal.signal() raises ValueError off the main thread — so the
    TUI's run_box_job workers crashed every box action with 'signal only
    works in main thread of the main interpreter'. The handler is now only
    installed for main-thread (interactive CLI) runs."""

    def test_worker_thread_run_does_not_touch_signal(self, tmp_path):
        from types import SimpleNamespace
        pymod = importlib.import_module('cli.commands.development.python')

        script = tmp_path / "script.py"
        script.write_text("print('hi')\n")
        fake_resp = SimpleNamespace(status_code=200)
        fake_session = SimpleNamespace(
            run_python=lambda box, files: fake_resp,
            kill_python=lambda *a, **k: None,
        )
        fake_ctx = SimpleNamespace(obj=SimpleNamespace(
            get_session_for_box=lambda ip, box_name=None: fake_session))

        errors: list[Exception] = []

        def run():
            try:
                with patch.object(pymod, 'stream_python_output',
                                  return_value=iter([])):
                    pymod.run_python_internal(
                        fake_ctx, str(script), 'mybox', env={}, passenv=(),
                        kill=False, download=(), allow_overwrite=False,
                        signum='SIGTERM', timeout=30, detach=False, port=(),
                        org=None, args=(), dut_name='mybox',
                        watch_stdin_resume=False)
            except Exception as exc:
                errors.append(exc)

        t = threading.Thread(target=run)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive()
        assert errors == []


class TestSavedNetFlowsOffloadBoxCalls:
    """The pre-0.24 flows (save details, delete, rename, batch add,
    delete-all) used to run their box round-trips synchronously inside
    button handlers — same freeze as the assign regression. They now go
    through run_box_job too."""

    RECORD = {"name": "supply1", "role": "power-supply",
              "instrument": "Rigol_DP832", "pin": "1",
              "address": "USB0::0x1AB1::INSTR"}

    @staticmethod
    def _saved_net():
        return tui.Net("Rigol_DP832", "1", "power-supply", "supply1",
                       "USB0::0x1AB1::INSTR", saved=True)

    @classmethod
    def _fake_box(cls, records=None):
        """In-memory :9000 API; every request must land on a worker thread."""
        from test.unit.cli.nets_http_fake import FakeBoxHTTP

        class ThreadCheckedBox(FakeBoxHTTP):
            def request(self, method, url, **kwargs):
                assert threading.current_thread() is not threading.main_thread()
                return super().request(method, url, **kwargs)

        box = ThreadCheckedBox()
        box.saved_nets = [dict(r) for r in (records if records is not None
                                            else [cls.RECORD])]
        return box

    def test_delete_all_runs_in_worker(self):
        async def main():
            app = tui.NetApp(ctx=None, dut="box", inst_list=[],
                             nets=[self._saved_net()])
            with patch("requests.request", self._fake_box().request):
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause()
                    screen = tui.ConfirmDeleteAll()
                    app.push_screen(screen)
                    await pilot.pause()
                    screen.on_button_pressed(
                        Button.Pressed(screen.query_one("#confirm", Button)))
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    assert not isinstance(app.screen, tui.ConfirmDeleteAll)
                    assert not any(n.saved for n in app.nets)

        asyncio.run(main())

    def test_delete_single_net_runs_in_worker(self):
        net = self._saved_net()

        async def main():
            app = tui.NetApp(ctx=None, dut="box", inst_list=[], nets=[net])
            with patch("requests.request", self._fake_box().request):
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause()
                    screen = tui.ConfirmDelete(net)
                    app.push_screen(screen)
                    await pilot.pause()
                    screen.on_button_pressed(
                        Button.Pressed(screen.query_one("#confirm", Button)))
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    assert net not in app.nets
                    # The channel is re-offered as an unsaved placeholder.
                    assert any(not n.saved and n.chan == net.chan
                               for n in app.nets)

        asyncio.run(main())

    def test_rename_runs_in_worker(self):
        net = self._saved_net()

        async def main():
            app = tui.NetApp(ctx=None, dut="box", inst_list=[], nets=[net])
            box = self._fake_box()
            with patch("requests.request", box.request):
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause()
                    screen = tui.RenameDialog(net)
                    app.push_screen(screen)
                    await pilot.pause()
                    screen.input.value = "main_supply"
                    screen.on_button_pressed(
                        Button.Pressed(screen.query_one("#confirm", Button)))
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    assert net.net == "main_supply"
                    assert any(n.saved and n.net == "main_supply"
                               for n in app.nets)

        asyncio.run(main())

    def test_save_details_runs_in_worker(self):
        net = self._saved_net()

        async def main():
            app = tui.NetApp(ctx=None, dut="box", inst_list=[], nets=[net])
            with patch("requests.request", self._fake_box().request):
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause()
                    screen = tui.EditDetailsDialog(net)
                    app.push_screen(screen)
                    await pilot.pause()
                    screen.purpose_input.value = "Main DUT power rail"
                    screen.on_button_pressed(
                        Button.Pressed(screen.query_one("#save_details", Button)))
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    assert not isinstance(app.screen, tui.EditDetailsDialog)
                    assert net.purpose == "Main DUT power rail"

        asyncio.run(main())

    def test_batch_save_runs_in_worker_and_pops_add_screen(self):
        net = tui.Net("Rigol_DP832", "1", "power-supply", "supply1",
                      "USB0::0x1AB1::INSTR", saved=False)

        async def main():
            app = tui.NetApp(ctx=None, dut="box", inst_list=[], nets=[net])
            app._fetch_saved_records = lambda: []
            with patch.object(tui, "_save_nets_batch", return_value=True) as sb:
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause()
                    screen = tui.AddScreen([net], False)
                    app.push_screen(screen)
                    await pilot.pause()
                    screen._batch_save_and_close(app, [net])
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    assert sb.call_count == 1
                    assert not isinstance(app.screen, tui.AddScreen)

        asyncio.run(main())

    def test_batch_save_validation_error_keeps_add_screen_up(self):
        net = tui.Net("FTDI", "0", "uart", "uart1", "usb", saved=False)

        async def main():
            app = tui.NetApp(ctx=None, dut="box", inst_list=[], nets=[net])
            with patch.object(
                tui, "_save_nets_batch",
                side_effect=tui.UARTNetSaveValidationError("bad pin"),
            ):
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause()
                    screen = tui.AddScreen([net], False)
                    app.push_screen(screen)
                    await pilot.pause()
                    screen._batch_save_and_close(app, [net])
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    # Selection preserved so the user can fix and retry.
                    assert app.screen is screen

        asyncio.run(main())


# --------------------------------------------------------------------------- #
# table regression                                                            #
# --------------------------------------------------------------------------- #

class TestSingleChannelTable:
    def test_dp711_is_single_channel_with_power_supply_role(self):
        # Catalog says single_channel=True; the role tuple uses the
        # scanner-vocabulary "power-supply" (what saved nets carry).
        assert tui._SINGLE_CHANNEL_INST["Rigol_DP711"] == ("power-supply",)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
