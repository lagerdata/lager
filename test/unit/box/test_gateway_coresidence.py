# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
start_box.sh next to a gateway that owns lager's host ports.

A box whose --no-publish marker had been deleted (a full uninstall removed
/etc/lager) was reinstalled with no flag. start_box.sh then published lager's
ports, Docker refused the new container because a gateway on lagernet already
held them, and the old container was already gone: the box was left with no
lager container and an install that never finished.

The blocks run here verbatim, extracted by their BEGIN/END sentinels, against
a fake `docker` on PATH that reads its answers from files. What is pinned:
  * a container on lagernet holding lager's API ports switches the start to
    --no-publish and writes the marker;
  * any other holder of a port lager would publish fails the start with exit
    5, naming port and container, before the running container is stopped;
  * a `docker run` refused over a port says which port and which holder, and
    removes the half-created container.
"""

import os
import stat
import subprocess
import tempfile
import textwrap
import unittest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
_START_BOX = os.path.join(_REPO, 'box', 'start_box.sh')
_DEPLOY = os.path.join(_REPO, 'cli', 'deployment', 'scripts', 'setup_and_deploy_box.sh')

PORT_CONFLICT = 5


def _extract(topic):
    """Return the shell between the BEGIN/END sentinels naming `topic`."""
    begin, end = f"# --- BEGIN {topic}", f"# --- END {topic}"
    body, inside, seen = [], False, False
    with open(_START_BOX, encoding='utf-8') as f:
        for line in f.read().splitlines():
            if line.startswith(begin):
                inside, seen = True, True
                continue
            if line.startswith(end):
                inside = False
                continue
            if inside:
                body.append(line)
    assert seen, f"sentinel {begin!r} not found in {_START_BOX}"
    assert body, f"no shell extracted for {topic!r}"
    return "\n".join(body)


# Answers `docker ps --format`, `docker inspect -f ... <name>` and records
# `docker rm`. The state lives in $FAKE_DOCKER_DIR: ps.txt holds the
# "<name>|<ports>" lines a real `docker ps --format '{{.Names}}|{{.Ports}}'`
# prints, and nets/<name> the networks of each container.
_FAKE_DOCKER = textwrap.dedent("""\
    #!/bin/bash
    d="$FAKE_DOCKER_DIR"
    echo "$*" >> "$d/calls.log"
    case "$1" in
        ps) cat "$d/ps.txt" 2>/dev/null ;;
        inspect) name="${@: -1}"; cat "$d/nets/$name" 2>/dev/null || exit 1 ;;
        rm) exit 0 ;;
        *) exit 0 ;;
    esac
""")


class _Box(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.docker_dir = os.path.join(root, 'docker')
        os.makedirs(os.path.join(self.docker_dir, 'nets'))
        bin_dir = os.path.join(root, 'bin')
        os.makedirs(bin_dir)
        docker = os.path.join(bin_dir, 'docker')
        with open(docker, 'w') as f:
            f.write(_FAKE_DOCKER)
        os.chmod(docker, os.stat(docker).st_mode | stat.S_IEXEC)
        self.marker = os.path.join(root, 'no_publish')
        self.env = {
            **os.environ,
            'PATH': f"{bin_dir}:{os.environ['PATH']}",
            'FAKE_DOCKER_DIR': self.docker_dir,
        }

    def container(self, name, ports, nets=('bridge',)):
        with open(os.path.join(self.docker_dir, 'ps.txt'), 'a') as f:
            f.write(f"{name}|{ports}\n")
        with open(os.path.join(self.docker_dir, 'nets', name), 'w') as f:
            f.write(' '.join(nets) + ' ')

    def calls(self):
        path = os.path.join(self.docker_dir, 'calls.log')
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return f.read().splitlines()

    def run_coresidence(self, *, no_publish='', explicit_publish='', preflight=''):
        script = "\n".join([
            "set -e",
            f"NO_PUBLISH='{no_publish}'",
            f"EXPLICIT_PUBLISH='{explicit_publish}'",
            f"PREFLIGHT='{preflight}'",
            f"NO_PUBLISH_MARKER='{self.marker}'",
            _extract("gateway co-residence"),
            'echo "NO_PUBLISH=$NO_PUBLISH"',
        ])
        return subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, env=self.env)


# A gateway that publishes lager's API ports, the way `docker ps` shows it.
GATEWAY_PORTS = ("0.0.0.0:5000->5000/tcp, [::]:5000->5000/tcp, 0.0.0.0:8080->8080/tcp, "
                 "0.0.0.0:8765->8765/tcp, 0.0.0.0:9000-9001->9000-9001/tcp")


class GatewayOnLagernet(_Box):
    def test_switches_to_no_publish_and_writes_the_marker(self):
        self.container('gateway', GATEWAY_PORTS, nets=('lagernet',))
        out = self.run_coresidence()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("NO_PUBLISH=1", out.stdout)
        self.assertIn("'gateway', a container on lagernet", out.stdout)
        self.assertTrue(os.path.exists(self.marker), "the mode must survive a plain restart")

    def test_preflight_then_passes(self):
        self.container('gateway', GATEWAY_PORTS, nets=('lagernet',))
        out = self.run_coresidence(preflight='1')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("Preflight OK (publish mode: no-publish)", out.stdout)

    def test_explicit_publish_is_not_overridden(self):
        # The gateway-off path restarts lager with --publish after taking its
        # own ports back; the operator's flag wins over the guess.
        self.container('gateway', GATEWAY_PORTS, nets=('lagernet',))
        out = self.run_coresidence(explicit_publish='1')
        self.assertIn("NO_PUBLISH=\n", out.stdout + "\n")
        self.assertFalse(os.path.exists(self.marker))

    def test_explicit_publish_into_a_held_port_fails_preflight(self):
        self.container('gateway', GATEWAY_PORTS, nets=('lagernet',))
        out = self.run_coresidence(explicit_publish='1', preflight='1')
        self.assertEqual(out.returncode, PORT_CONFLICT)
        self.assertIn("port 5000: container 'gateway'", out.stdout)

    def test_the_old_lager_container_is_not_a_holder(self):
        # This run replaces it: its own published ports are not a conflict.
        self.container('lager', "0.0.0.0:5000->5000/tcp", nets=('lagernet',))
        out = self.run_coresidence(preflight='1')
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("publish mode: publish", out.stdout)
        self.assertFalse(os.path.exists(self.marker))


class ForeignHolder(_Box):
    def test_preflight_fails_naming_port_and_container(self):
        self.container('webapp', "0.0.0.0:8080->80/tcp", nets=('bridge',))
        out = self.run_coresidence(preflight='1')
        self.assertEqual(out.returncode, PORT_CONFLICT)
        self.assertIn("port 8080: container 'webapp'", out.stdout)
        self.assertIn("No container was stopped", out.stdout)
        self.assertFalse(os.path.exists(self.marker), "not a gateway: no mode change")

    def test_nothing_held_passes(self):
        out = self.run_coresidence(preflight='1')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("Preflight OK (publish mode: publish)", out.stdout)

    def test_no_publish_skips_the_port_check(self):
        self.container('webapp', "0.0.0.0:5000->5000/tcp")
        out = self.run_coresidence(no_publish='1', preflight='1')
        self.assertEqual(out.returncode, 0, out.stdout)

    def test_unpublished_ports_are_not_holders(self):
        # `5000/tcp` with no `->` is an exposed port, not a host binding.
        self.container('other', "5000/tcp, 8080/tcp")
        out = self.run_coresidence(preflight='1')
        self.assertEqual(out.returncode, 0, out.stdout)


class PortPreflightBeforeTeardown(_Box):
    def run_preflight(self, publish_args):
        script = "\n".join([
            "set -e",
            "NO_PUBLISH='1'",  # skip the co-residence switch; test the check alone
            "EXPLICIT_PUBLISH=''",
            "PREFLIGHT=''",
            f"NO_PUBLISH_MARKER='{self.marker}'",
            _extract("gateway co-residence"),
            f"PORT_PUBLISH_ARGS=({publish_args})",
            _extract("port preflight"),
            'echo "proceeding to teardown"',
        ])
        return subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, env=self.env)

    def test_a_port_inside_a_published_range_conflicts(self):
        self.container('debugger', "0.0.0.0:4445->4445/tcp")
        out = self.run_preflight("-p 5000:5000 -p 4444-4447:4444-4447")
        self.assertEqual(out.returncode, PORT_CONFLICT)
        self.assertIn("port 4445: container 'debugger'", out.stdout)
        self.assertNotIn("proceeding to teardown", out.stdout)

    def test_a_holder_range_covering_a_published_port_conflicts(self):
        self.container('cams', "0.0.0.0:8086-8090->8086-8090/tcp")
        out = self.run_preflight("-p 8081-8090:8081-8090")
        self.assertEqual(out.returncode, PORT_CONFLICT)
        self.assertIn("port 8086: container 'cams'", out.stdout)

    def test_no_publishing_means_no_check(self):
        self.container('debugger', "0.0.0.0:5000->5000/tcp")
        out = self.run_preflight("")
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("proceeding to teardown", out.stdout)


class DockerRunFailure(_Box):
    def report(self, stderr_text, rc=125):
        err = os.path.join(self.tmp.name, 'run.err')
        with open(err, 'w') as f:
            f.write(stderr_text)
        script = "\n".join([
            "NO_PUBLISH='1'", "EXPLICIT_PUBLISH=''", "PREFLIGHT=''",
            f"NO_PUBLISH_MARKER='{self.marker}'",
            _extract("gateway co-residence"),
            _extract("docker run failure"),
            f"_report_run_failure '{err}' {rc}",
            'echo "rc=$?"',
        ])
        return subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, env=self.env)

    def test_names_the_container_holding_the_port(self):
        self.container('gateway', GATEWAY_PORTS, nets=('lagernet',))
        out = self.report("docker: Error response from daemon: driver failed programming "
                          "external connectivity on endpoint lager (abc): Bind for "
                          "0.0.0.0:5000 failed: port is already allocated.")
        self.assertIn("host port 5000 is held by container 'gateway'", out.stdout)
        self.assertIn(f"rc={PORT_CONFLICT}", out.stdout)
        self.assertIn("rm -f lager", self.calls(), "the Created leftover is removed")

    def test_a_port_held_outside_docker(self):
        out = self.report("docker: Error response from daemon: ... listen tcp4 "
                          "0.0.0.0:9000: bind: address already in use.")
        self.assertIn("host port 9000 is in use by a process outside Docker", out.stdout)
        self.assertIn("sport = :9000", out.stdout)
        self.assertIn(f"rc={PORT_CONFLICT}", out.stdout)

    def test_another_failure_keeps_its_exit_status(self):
        out = self.report("docker: Error response from daemon: No such image: lager.", rc=125)
        self.assertIn("docker run failed (exit 125)", out.stdout)
        self.assertIn("rc=125", out.stdout)
        self.assertIn("rm -f lager", self.calls())


class Ordering(unittest.TestCase):
    """Textual: the order of top-level steps is the guarantee."""

    def setUp(self):
        with open(_START_BOX, encoding='utf-8') as f:
            self.text = f.read()

    def test_the_old_container_is_stopped_only_after_the_port_check(self):
        preflight = self.text.index("# --- END port preflight")
        teardown = self.text.index('echo "Stopping existing lager container..."')
        run = self.text.index("docker run -d")
        self.assertLess(preflight, teardown)
        self.assertLess(teardown, run)

    def test_the_mode_is_decided_before_ports_are_chosen(self):
        self.assertLess(self.text.index("# --- END gateway co-residence"),
                        self.text.index("# --- BEGIN port publishing"))

    def test_docker_run_failure_is_handled(self):
        self.assertRegex(self.text, r'lager 2>"\$_RUN_ERR" \|\| _run_rc=\$\?')

    def test_deploy_runs_the_preflight_before_its_teardown(self):
        with open(_DEPLOY, encoding='utf-8') as f:
            deploy = f.read()
        preflight = deploy.index("./start_box.sh --preflight")
        teardown = deploy.index('print_info "Stopping and removing lager containers..."')
        self.assertLess(preflight, teardown)
        # An older start_box.sh would run a full start on an unknown flag.
        self.assertRegex(deploy, r"grep -q -- '--preflight' start_box\.sh")


if __name__ == '__main__':
    unittest.main()
