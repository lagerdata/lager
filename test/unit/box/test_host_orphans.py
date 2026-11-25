# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The host agent's children do not outlive it.

A supervisor killed outright cannot stop its children, and one left behind
keeps the agent's port so the next worker can never bind. Covers both
guards: a child exits when its parent changes, and a starting supervisor
ends agent children that have no supervisor -- and only those.
"""
import signal
from unittest import mock

from lager.host import __main__ as host_main
from lager.host import orphans

LISTING = """\
  100     1   501 /venv/bin/python -m lager.host run
  101   100   501 /venv/bin/python -m lager.host worker --config-dir /etc/lager
  102   100   501 /venv/bin/python -m lager.host debug
  200     1   501 /venv/bin/python -m lager.host worker --config-dir /etc/lager
  201     1   501 /opt/Python.app/Contents/MacOS/Python -m lager.host debug
  202     1   501 /app/host/bin/oscilloscope-daemon
  207     1   501 /Users/me/Library/Application Support/Lager/host/venv/bin/python -m lager.host worker --config-dir /etc/lager
  203     1   501 /app/host/bin/oscilloscope-daemon-helper
  204     1   502 /venv/bin/python -m lager.host worker --config-dir /etc/lager
  205     1   501 /usr/bin/python3 -m http.server
  206     1   501 vim notes about -m lager.host workers.txt
garbage line
"""


def test_only_this_users_supervisorless_agent_children_are_stale():
    stale = orphans.find_stale(["/app/host/bin/oscilloscope-daemon", None],
                               listing=LISTING, uid=501, own_pid=999)
    assert stale == [200, 201, 202, 207]


def test_a_supervised_child_and_the_supervisor_itself_are_never_stale():
    stale = orphans.find_stale(listing=LISTING, uid=501, own_pid=200)
    assert 100 not in stale and 101 not in stale and 102 not in stale
    assert 200 not in stale  # never itself


def test_the_listing_asks_ps_for_untruncated_commands():
    run = mock.Mock(return_value=mock.Mock(stdout=LISTING))
    assert orphans._listing(run) == LISTING
    argv = run.call_args.args[0]
    assert argv[0] == "ps" and "ww" in argv[1] and argv[2] == "pid=,ppid=,uid=,command="


def test_a_leftover_is_asked_to_stop_and_killed_only_if_it_does_not():
    living = {200, 201}
    sent = []

    def kill(pid, sig):
        sent.append((pid, sig))
        if sig == signal.SIGTERM and pid == 200:
            living.discard(pid)  # 200 obeys, 201 ignores

    reaped = orphans.reap_stale(find=lambda extra: [200, 201], kill=kill,
                                alive=lambda pid: pid in living,
                                sleep=lambda s: None, wait_s=0.4)
    assert reaped == [200, 201]
    assert sent == [(200, signal.SIGTERM), (201, signal.SIGTERM), (201, signal.SIGKILL)]


def test_nothing_stale_and_a_failed_listing_are_both_quiet():
    kill = mock.Mock()
    assert orphans.reap_stale(find=lambda extra: [], kill=kill, sleep=lambda s: None) == []

    def broken(extra):
        raise OSError("no ps")
    assert orphans.reap_stale(find=broken, kill=kill, sleep=lambda s: None) == []
    kill.assert_not_called()


def test_a_child_exits_when_its_parent_changes():
    parents = iter([100, 100, 100, 1])
    events = []
    thread = orphans.watch_parent(
        getppid=lambda: next(parents), sleep=lambda s: events.append(("sleep", s)),
        terminate=lambda: events.append("terminate"),
        force=lambda code: events.append(("force", code)), poll_s=1.0, grace_s=5.0)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert events == [("sleep", 1.0), ("sleep", 1.0), "terminate", ("sleep", 5.0), ("force", 0)]


def test_the_children_watch_their_parent_and_the_supervisor_reaps_first():
    args = mock.Mock(config_dir="/etc/lager", scope_daemon="/bin/scope")
    with mock.patch.object(orphans, "watch_parent") as watch, \
            mock.patch("lager.host.worker.serve") as serve:
        assert host_main.cmd_worker(args) == 0
    watch.assert_called_once_with()
    serve.assert_called_once_with("/etc/lager")

    order = []
    with mock.patch.object(host_main, "cmd_init", side_effect=lambda a: order.append("init")), \
            mock.patch.object(orphans, "reap_stale",
                              side_effect=lambda extra: order.append(("reap", extra))), \
            mock.patch.object(host_main, "_children",
                              side_effect=lambda a: order.append("children") or []):
        host_main.cmd_run(args)
    assert order[:3] == ["init", ("reap", ["/bin/scope"]), "children"]
