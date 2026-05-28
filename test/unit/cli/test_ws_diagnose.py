#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for `cli/core/ws_diagnose.make_ws_failure_message` — the helper
that turns a WebSocket failure into an actionable one-liner for the
battery/supply TUIs. We pin the message bodies because they're the
user-facing payoff for this 0.20.0 work.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from cli.core.ws_diagnose import make_ws_failure_message


class MakeWsFailureMessageTests(unittest.TestCase):

    def test_healthy_box_means_ws_namespace_missing(self):
        """Box answers /health with 200 → box is up, WS namespace didn't
        register → suggest box update."""
        with patch('cli.core.ws_diagnose.requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            msg = make_ws_failure_message('10.0.0.5', original_error='handshake timeout')

        self.assertIn('10.0.0.5:9000', msg)
        self.assertIn('pre-0.20 image', msg)
        self.assertIn('lager box update', msg)
        self.assertIn('handshake timeout', msg)  # original error preserved

    def test_non_200_response_says_services_partially_up(self):
        with patch('cli.core.ws_diagnose.requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=503)
            msg = make_ws_failure_message('10.0.0.5')

        self.assertIn('HTTP 503', msg)
        self.assertIn('partially up', msg)
        self.assertIn('docker restart lager', msg)

    def test_connect_timeout_calls_out_network(self):
        with patch('cli.core.ws_diagnose.requests.get') as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectTimeout('timed out')
            msg = make_ws_failure_message('10.0.0.5')

        self.assertIn('timed out reaching 10.0.0.5:9000', msg)
        self.assertIn('network/Tailscale', msg)
        self.assertIn('lager box hello', msg)

    def test_connection_refused_calls_out_container(self):
        with patch('cli.core.ws_diagnose.requests.get') as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectionError('refused')
            msg = make_ws_failure_message('10.0.0.5')

        self.assertIn('cannot reach 10.0.0.5:9000', msg)
        self.assertIn('lager container', msg)
        self.assertIn('sudo docker start lager', msg)

    def test_never_raises_on_arbitrary_probe_error(self):
        with patch('cli.core.ws_diagnose.requests.get') as mock_get:
            mock_get.side_effect = ValueError('something weird')
            msg = make_ws_failure_message('10.0.0.5')

        self.assertIn('errored', msg)
        self.assertIn('something weird', msg)
        self.assertIsInstance(msg, str)

    def test_original_error_blank_omits_parenthetical(self):
        with patch('cli.core.ws_diagnose.requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            msg = make_ws_failure_message('10.0.0.5')

        self.assertNotIn('()', msg)  # no empty parens

    def test_includes_action_label(self):
        """Every code path includes an 'Action:' label so users know
        what to do, not just what's broken."""
        with patch('cli.core.ws_diagnose.requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            self.assertIn('Action:', make_ws_failure_message('host'))

            mock_get.return_value = MagicMock(status_code=500)
            self.assertIn('Action:', make_ws_failure_message('host'))

            mock_get.side_effect = requests.exceptions.ConnectTimeout()
            self.assertIn('Action:', make_ws_failure_message('host'))

            mock_get.side_effect = requests.exceptions.ConnectionError()
            self.assertIn('Action:', make_ws_failure_message('host'))


if __name__ == '__main__':
    unittest.main()
