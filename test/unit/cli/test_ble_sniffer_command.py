# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for `lager ble-sniffer` (cli/commands/communication/ble_sniffer.py).

Each test runs the real root group against the in-process ``fake_box`` from
conftest.py. The fake box starts a capture job, reports it done on the first
poll unless a test says otherwise, and ``requests.get`` is wrapped so GET
/download-file returns the capture bytes. That checks the whole start, poll,
download and delete sequence and the exact payload of each request. Polling
does not sleep: the command module's ``time`` is replaced.
"""

from __future__ import annotations

import importlib
import json
import struct
import time
from types import SimpleNamespace

import pytest

BOX_FILE = "/tmp/lager-output/ble-sniffer/sniffer1-20261108T000000Z.pcap"
PCAP = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 272)
CAPTURE_VALUE = {"file": BOX_FILE, "bytes": len(PCAP), "packets": 0, "duration": 2.0,
                 "port": "/dev/ttyACM0", "firmware": "5.0.0", "followed": None,
                 "follow_seen": None, "stopped": False}


def _job(state: str, result: dict | None = None, error: str | None = None,
         file_saved: bool = False) -> dict:
    return {"success": True, "message": f"Capture j1 {state}",
            "value": {"job": "j1", "net": "sniffer1", "state": state, "file": BOX_FILE,
                      "duration": 2.0, "elapsed": 2.0, "packets": 0, "result": result,
                      "error": error, "error_kind": "backend" if error else None,
                      "file_saved": file_saved}}


@pytest.fixture
def sniffer_box(fake_box, monkeypatch):
    """``fake_box`` with a sniffer net and a /download-file that serves PCAP."""
    import requests

    fake_box.net("sniffer1", "ble-sniffer")
    downloads = []
    base_get = fake_box.get

    def get(url, params=None, **kwargs):
        if url.endswith("/download-file"):
            downloads.append(params["filename"])
            resp = requests.Response()
            resp.status_code = 200 if params["filename"] == BOX_FILE else 404
            resp._content = PCAP if resp.status_code == 200 else b'{"error": "not found"}'
            return resp
        return base_get(url, params=params, **kwargs)

    monkeypatch.setattr("requests.get", get)

    mod = importlib.import_module("cli.commands.communication.ble_sniffer")
    monkeypatch.setattr(mod, "time", SimpleNamespace(sleep=lambda s: None,
                                                     monotonic=time.monotonic))

    def answer(url, body):
        if body["action"] == "start":
            return _job("running")
        if body["action"] == "job":
            return _job("done", CAPTURE_VALUE)
        if body["action"] == "delete":
            return {"success": True, "message": "Deleted",
                    "value": {"file": BOX_FILE, "deleted": True}}
        return {"success": True, "message": "Sniffer on /dev/ttyACM0, idle",
                "value": {"serial": "S", "port": "/dev/ttyACM0", "present": True,
                          "busy": False}}

    fake_box.answer = answer
    fake_box.downloads = downloads
    return fake_box


def _actions(box) -> list[str]:
    return [body["action"] for _, body in box.posts]


def _replace(box, action: str, replies):
    """Answer ``action`` with each of ``replies`` in turn (an exception is raised)."""
    answer = box.answer
    queue = list(replies)

    def replaced(url, body):
        if body["action"] == action and queue:
            reply = queue.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply
        return answer(url, body)

    box.answer = replaced


def test_capture_starts_polls_downloads_and_deletes(lager, sniffer_box, tmp_path):
    result = lager("ble-sniffer", "sniffer1", "capture", "-d", "2", "-o", "cap.pcap")
    assert result.exit_code == 0, result.output
    start, poll, delete = (body for _, body in sniffer_box.posts)
    assert start == {"netname": "sniffer1", "action": "start", "role": "ble-sniffer",
                     "params": {"duration": 2.0, "follow": None, "address_type": None,
                                "follow_by_name": None, "only_advertising": False,
                                "coded": False, "rssi_cut_off": None}}
    assert poll == {"netname": "sniffer1", "action": "job", "role": "ble-sniffer",
                    "params": {"job": "j1"}}
    assert delete == {"netname": "sniffer1", "action": "delete", "role": "ble-sniffer",
                      "params": {"file": BOX_FILE}}
    assert sniffer_box.downloads == [BOX_FILE]
    assert (tmp_path / "cap.pcap").read_bytes() == PCAP
    assert "Saved cap.pcap" in result.output


def test_capture_polls_until_the_job_ends(lager, sniffer_box):
    _replace(sniffer_box, "job", [_job("running"), _job("running")])
    result = lager("ble-sniffer", "sniffer1", "capture", "-d", "2")
    assert result.exit_code == 0, result.output
    assert _actions(sniffer_box) == ["start", "job", "job", "job", "delete"]


def test_capture_options_are_forwarded(lager, sniffer_box):
    result = lager("ble-sniffer", "sniffer1", "capture", "--duration", "3600",
                   "--follow", "AA:BB:CC:DD:EE:FF", "--public", "--only-advertising",
                   "--coded", "--rssi-cut-off", "-70", "--keep")
    assert result.exit_code == 0, result.output
    params = sniffer_box.posts[0][1]["params"]
    assert params == {"duration": 3600.0, "follow": "AA:BB:CC:DD:EE:FF",
                      "address_type": "public", "follow_by_name": None,
                      "only_advertising": True, "coded": True, "rssi_cut_off": -70}
    # --keep: the box copy stays and its path is printed.
    assert _actions(sniffer_box) == ["start", "job"]
    assert f"Box copy: {BOX_FILE}" in result.output


def test_capture_json(lager, sniffer_box):
    result = lager("ble-sniffer", "sniffer1", "capture", "-d", "2", "-o", "c.pcap", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data == {"netname": "sniffer1", "job": "j1", "file": "c.pcap", "box_file": None,
                    "bytes": len(PCAP), "packets": 0, "duration": 2.0,
                    "port": "/dev/ttyACM0", "firmware": "5.0.0", "followed": None,
                    "follow_seen": None, "stopped": False, "error": None}


@pytest.mark.parametrize("argv, followed, seen, expected, stream", [
    (["--follow-by-name", "DUT"], "11:22:33:44:55:66", True,
     "Followed 11:22:33:44:55:66", "stdout"),
    (["--follow-by-name", "DUT"], None, False,
     "nothing from 'DUT' was captured; no advertisement with that name", "stderr"),
    (["--follow", "11:22:33:44:55:66"], None, False,
     "address type was not known", "stderr"),
    (["--follow", "11:22:33:44:55:66", "--public"], "11:22:33:44:55:66", False,
     "check the address and --public/--random", "stderr"),
])
def test_follow_result_is_reported(lager, sniffer_box, argv, followed, seen, expected, stream):
    _replace(sniffer_box, "job", [
        _job("done", {**CAPTURE_VALUE, "followed": followed, "follow_seen": seen})])
    result = lager("ble-sniffer", "sniffer1", "capture", "-d", "2", *argv)
    assert result.exit_code == 0, result.output
    assert expected in getattr(result, stream)


def test_ctrl_c_stops_the_box_capture_and_saves_it(lager, sniffer_box, tmp_path):
    _replace(sniffer_box, "job", [KeyboardInterrupt(),
                                  _job("done", {**CAPTURE_VALUE, "stopped": True})])
    _replace(sniffer_box, "stop", [{"success": True, "message": "Stopping",
                                    "value": {"stopped": True, "file": BOX_FILE, "job": "j1"}}])
    result = lager("ble-sniffer", "sniffer1", "capture", "-d", "60", "-o", "cut.pcap")
    assert result.exit_code == 130, result.output
    assert _actions(sniffer_box) == ["start", "job", "stop", "job", "delete"]
    assert (tmp_path / "cut.pcap").read_bytes() == PCAP
    assert "stopped early" in result.output


def test_second_ctrl_c_gives_up(lager, sniffer_box):
    _replace(sniffer_box, "job", [KeyboardInterrupt(), KeyboardInterrupt()])
    result = lager("ble-sniffer", "sniffer1", "capture", "-d", "60")
    assert result.exit_code == 1
    assert "Aborted" in result.output
    assert _actions(sniffer_box) == ["start", "job", "stop", "job"]


def test_failed_job_without_a_file(lager, sniffer_box):
    _replace(sniffer_box, "job", [_job("failed", error="Sniffer S stopped answering")])
    result = lager("ble-sniffer", "sniffer1", "capture", "-d", "2")
    assert result.exit_code == 1
    assert "Sniffer S stopped answering" in result.output
    assert sniffer_box.downloads == []


def test_failed_job_with_packets_saves_them_and_fails(lager, sniffer_box, tmp_path):
    _replace(sniffer_box, "job", [_job("failed", error="lost; 5 packets in the file",
                                       file_saved=True)])
    result = lager("ble-sniffer", "sniffer1", "capture", "-d", "2", "-o", "part.pcap")
    assert result.exit_code == 1
    assert (tmp_path / "part.pcap").read_bytes() == PCAP
    assert "lost; 5 packets in the file" in result.output
    assert _actions(sniffer_box) == ["start", "job", "delete"]


@pytest.mark.parametrize("argv, message", [
    (["-d", "0.5"], "between 1 and 86400"),
    (["-d", "86401"], "between 1 and 86400"),
    (["--follow", "AA:BB:CC"], "not an address"),
    (["--follow", "AA:BB:CC:DD:EE:FF", "--follow-by-name", "dut"], "cannot be combined"),
    (["--rssi-cut-off", "-5"], "between -255 and -10"),
    (["--public"], "apply only to --follow"),
    (["--only-advertising"], "needs --follow or --follow-by-name"),
    (["--coded", "--follow-by-name", "dut"], "LE Coded PHY"),
])
def test_bad_options_send_nothing(lager, sniffer_box, argv, message):
    result = lager("ble-sniffer", "sniffer1", "capture", *argv)
    assert result.exit_code == 2
    assert message in result.output
    assert sniffer_box.posts == []


def test_unknown_net_sends_nothing(lager, sniffer_box):
    result = lager("ble-sniffer", "nosuch", "capture", "-d", "2")
    assert result.exit_code != 0
    assert sniffer_box.posts == []


def test_failed_download_keeps_the_box_copy(lager, sniffer_box):
    _replace(sniffer_box, "job", [
        _job("done", {**CAPTURE_VALUE, "file": "/tmp/lager-output/ble-sniffer/gone.pcap"})])
    result = lager("ble-sniffer", "sniffer1", "capture", "-d", "2")
    assert result.exit_code == 1
    assert "Download of /tmp/lager-output/ble-sniffer/gone.pcap failed: HTTP 404" in result.output
    assert _actions(sniffer_box) == ["start", "job"]


def test_status(lager, sniffer_box):
    result = lager("ble-sniffer", "sniffer1", "status")
    assert result.exit_code == 0, result.output
    assert sniffer_box.last == {"netname": "sniffer1", "action": "status",
                                "role": "ble-sniffer", "params": {}}
    assert result.output.strip() == "Sniffer on /dev/ttyACM0, idle"


def test_status_shows_the_last_capture(lager, sniffer_box):
    job = _job("running")["value"]
    _replace(sniffer_box, "status", [{"success": True, "message": "Sniffer on /dev/ttyACM0, "
                                      "busy with a capture", "value": {"busy": True,
                                                                       "job": job}}])
    result = lager("ble-sniffer", "sniffer1", "status")
    assert result.exit_code == 0, result.output
    assert f"Last capture: running, 0 packets in 2 of 2 s, {BOX_FILE}" in result.output


def test_stop_command(lager, sniffer_box):
    _replace(sniffer_box, "stop", [{"success": True,
                                    "message": "Stopping the capture on sniffer S",
                                    "value": {"stopped": True, "file": BOX_FILE, "job": "j1"}}])
    result = lager("ble-sniffer", "sniffer1", "stop", "--json")
    assert result.exit_code == 0, result.output
    assert sniffer_box.last == {"netname": "sniffer1", "action": "stop",
                                "role": "ble-sniffer", "params": {}}
    assert json.loads(result.output) == {"netname": "sniffer1", "stopped": True,
                                         "file": BOX_FILE, "job": "j1"}
