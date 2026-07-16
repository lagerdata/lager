# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for box/lager/box_origin.py: the Host and Origin checks that keep the
box HTTP services from being usable by a web page.

The threat these exist for: a browser attaches an Origin header to every request
whose method is not GET or HEAD, and page JavaScript can neither forge nor strip
it, while the CLI and control planes never send one at all. So the presence of a
foreign Origin is what distinguishes "a website is driving this box through
someone's browser" from "a tool is talking to it". Host validation covers the
other way in, DNS rebinding, which needs a name that resolves to the box.

box_origin.py imports nothing but the standard library, so it loads directly via
importlib without pulling in the box-side lager package.
"""

import importlib.util
import os
import unittest
from unittest import mock


HERE = os.path.dirname(__file__)
MODULE_PATH = os.path.normpath(
    os.path.join(HERE, '..', '..', '..', 'box', 'lager', 'box_origin.py')
)


def _load_module():
    spec = importlib.util.spec_from_file_location('box_origin_mod', MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


box_origin = _load_module()


def allowed(host, origin=None, path=None):
    """True if a request with these headers would be served."""
    return box_origin.check_request(host, origin, path=path) is None


class HostValidationTests(unittest.TestCase):
    """Host is validated structurally: any IP literal, or a name we answer to."""

    def test_ip_literal_allowed(self):
        # The normal case: the CLI addresses boxes by IP.
        self.assertTrue(allowed('100.64.0.1:5000'))
        self.assertTrue(allowed('10.0.0.5:5000'))
        self.assertTrue(allowed('192.168.1.50'))

    def test_second_published_port_allowed(self):
        # start_box.sh publishes port 5000 twice, on 5000 and 8301. A check
        # derived from "the port I bound" would reject this one.
        self.assertTrue(allowed('100.64.0.1:8301'))

    def test_ipv6_literal_allowed(self):
        self.assertTrue(allowed('[::1]:5000'))
        self.assertTrue(allowed('[fd00::1]:9000'))
        self.assertTrue(allowed('::1'))

    def test_localhost_allowed(self):
        self.assertTrue(allowed('localhost:5000'))
        self.assertTrue(allowed('localhost'))

    def test_docker_network_alias_allowed(self):
        # The Stout daemon reaches Lager as http://lager:5000 over lagernet.
        self.assertTrue(allowed('lager:5000'))

    def test_own_hostname_allowed(self):
        with mock.patch.object(box_origin.socket, 'gethostname', return_value='PRD-1'):
            self.assertTrue(allowed('PRD-1:5000'))
            # Host is case-insensitive.
            self.assertTrue(allowed('prd-1:5000'))

    def test_attacker_domain_rejected(self):
        # DNS rebinding: the attacker points their own name at this box's IP so
        # the browser treats the request as same-origin. It needs a *name*,
        # which is what this rejects.
        self.assertFalse(allowed('evil.example'))
        self.assertFalse(allowed('evil.example:5000'))
        self.assertFalse(allowed('box.attacker.test:8301'))

    def test_rebinding_with_matching_origin_still_rejected(self):
        # Rebinding makes Origin match Host, defeating the Origin check. Host
        # validation is what actually stops it.
        self.assertFalse(allowed('evil.example:5000', 'http://evil.example:5000'))

    def test_missing_host_rejected(self):
        self.assertFalse(allowed(None))
        self.assertFalse(allowed(''))


class OriginValidationTests(unittest.TestCase):
    """A foreign Origin means a browser on someone else's site."""

    def test_no_origin_allowed(self):
        # The CLI, the Stout daemon, and the control plane never send one.
        self.assertTrue(allowed('100.64.0.1:5000', None))

    def test_cross_origin_rejected(self):
        # The reported attack: a page the user has open POSTs to the box.
        self.assertFalse(allowed('100.64.0.1:5000', 'http://evil.example'))
        self.assertFalse(allowed('100.64.0.1:5000', 'https://evil.example'))
        self.assertFalse(allowed('100.64.0.1:5000', 'http://evil.example:8080'))

    def test_self_origin_allowed(self):
        # /web_oscilloscope.html is served by the box itself, so a page it
        # served may call back to the origin it came from.
        self.assertTrue(allowed('100.64.0.1:8080', 'http://100.64.0.1:8080'))
        self.assertTrue(allowed('100.64.0.1:8080', 'https://100.64.0.1:8080'))

    def test_different_port_is_a_different_origin(self):
        # Same host, different port is cross-origin per the same-origin policy,
        # so a page served from :8080 may not drive :5000.
        self.assertFalse(allowed('100.64.0.1:5000', 'http://100.64.0.1:8080'))

    def test_null_origin_rejected(self):
        # Sandboxed iframes and some file:// contexts send Origin: null.
        self.assertFalse(allowed('100.64.0.1:5000', 'null'))


class RealCallerTests(unittest.TestCase):
    """The callers that must keep working, spelled out."""

    def test_cli_request(self):
        self.assertTrue(allowed('100.64.0.1:5000', None, path='/python'))

    def test_stout_daemon_over_lagernet(self):
        self.assertTrue(allowed('lager:5000', None, path='/python'))

    def test_control_plane_by_ip(self):
        # Node's http.request generates Host from hostname and sends no Origin.
        self.assertTrue(allowed('100.64.0.1:5000', None, path='/python'))

    def test_drive_by_on_every_dangerous_endpoint(self):
        for path in ('/python', '/pip', '/binaries/add', '/debug/flash', '/invoke'):
            with self.subTest(path=path):
                self.assertFalse(
                    allowed('100.64.0.1:5000', 'http://evil.example', path=path)
                )


class RejectionShapeTests(unittest.TestCase):
    def test_rejection_is_403_with_a_reason(self):
        status, message = box_origin.check_request('100.64.0.1:5000', 'http://evil.example')
        self.assertEqual(status, 403)
        self.assertIn('origin', message.lower())

    def test_bad_host_says_so(self):
        status, message = box_origin.check_request('evil.example', None)
        self.assertEqual(status, 403)
        self.assertIn('host', message.lower())


if __name__ == '__main__':
    unittest.main()
