# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``cli/commands/development/arm.py`` -- the `lager arm` command
layer.

What lives only here:

  * ``read-and-save-position`` sends M889, which overwrites the arm's stored
    joint calibration. It must not run without a confirmation.
  * ``move`` and ``move-by`` refuse a ``--timeout`` past the box's cap before
    anything reaches the box.
  * listing arm nets takes no box lock.

The box resolvers, net validation and ``post_net_command`` are patched on the
arm module, so nothing resolves a real box, takes a lock, or opens a socket.
"""

from importlib import import_module
from types import SimpleNamespace
from unittest import mock

from click.testing import CliRunner

arm_mod = import_module('cli.commands.development.arm')

ARM_NET = {"name": "arm1", "role": "arm", "instrument": "Rotrix_Dexarm",
           "pin": "/dev/ttyACM0",
           "address": "USB0::0x0483::0x5740::ARM0001234::INSTR"}


def _invoke(args, input=None):
    result = CliRunner().invoke(arm_mod.arm, args, obj=SimpleNamespace(),
                                input=input, catch_exceptions=False)
    return result, result.output


class _ArmCommandTest:
    def setup_method(self):
        self.locked = mock.patch.object(
            arm_mod, 'resolve_box_locked', return_value='10.0.0.5').start()
        self.resolve = mock.patch.object(
            arm_mod, 'resolve_box', return_value='10.0.0.5').start()
        mock.patch.object(arm_mod, 'validate_net_exists',
                          return_value=ARM_NET).start()
        mock.patch.object(arm_mod, 'list_nets_by_role',
                          return_value=[ARM_NET]).start()
        mock.patch.object(arm_mod, 'get_default_net', return_value=None).start()
        self.post = mock.patch.object(
            arm_mod, 'post_net_command', return_value={'message': 'ok'}).start()

    def teardown_method(self):
        mock.patch.stopall()


class TestReadAndSavePosition(_ArmCommandTest):
    def test_declining_the_prompt_sends_nothing(self):
        result, output = _invoke(
            ['arm1', 'read-and-save-position', '--box', 'DEMO'], input='n\n')
        assert result.exit_code == 0
        assert 'M889' in output
        assert 'Aborting' in output
        self.post.assert_not_called()

    def test_yes_skips_the_prompt(self):
        result, output = _invoke(
            ['arm1', 'read-and-save-position', '--yes', '--box', 'DEMO'])
        assert result.exit_code == 0
        assert 'Recalibrate now?' not in output
        assert self.post.call_args.args[3] == 'read_and_save_position'


class TestMoveTimeoutCap:
    """A wait past the cap outlives hardware_service's 30 s call deadline."""

    def setup_method(self):
        _ArmCommandTest.setup_method(self)

    def teardown_method(self):
        mock.patch.stopall()

    def test_move_timeout_over_the_cap_is_a_usage_error(self):
        result, output = _invoke(
            ['arm1', 'move', '--x', '0', '--y', '300', '--z', '0',
             '--timeout', '26', '--yes', '--box', 'DEMO'])
        assert result.exit_code == 2
        assert '--timeout' in output
        self.post.assert_not_called()

    def test_move_by_timeout_over_the_cap_is_a_usage_error(self):
        result, _ = _invoke(
            ['arm1', 'move-by', '--dz', '5', '--timeout', '30', '--yes',
             '--box', 'DEMO'])
        assert result.exit_code == 2
        self.post.assert_not_called()

    def test_timeout_at_the_cap_is_sent_with_a_wider_http_budget(self):
        result, _ = _invoke(
            ['arm1', 'move', '--x', '0', '--y', '300', '--z', '0',
             '--timeout', '25', '--yes', '--box', 'DEMO'])
        assert result.exit_code == 0
        kwargs = self.post.call_args.kwargs
        assert kwargs['timeout'] == 25.0
        assert kwargs['http_timeout'] == 55.0


class TestListing(_ArmCommandTest):
    def test_listing_takes_no_box_lock(self):
        result, output = _invoke(['--box', 'DEMO'])
        assert result.exit_code == 0
        assert 'arm1' in output
        self.resolve.assert_called_once()
        self.locked.assert_not_called()

    def test_a_subcommand_still_takes_the_lock(self):
        result, _ = _invoke(['arm1', 'position', '--box', 'DEMO'])
        assert result.exit_code == 0
        self.locked.assert_called()


class TestDisableMotor(_ArmCommandTest):
    """M18 drops the arm onto whatever is below it."""

    def test_declining_the_prompt_sends_nothing(self):
        result, output = _invoke(['arm1', 'disable-motor', '--box', 'DEMO'],
                                 input='n\n')
        assert result.exit_code == 0
        assert 'goes limp' in output
        assert 'Aborting' in output
        self.post.assert_not_called()

    def test_yes_skips_the_prompt(self):
        result, output = _invoke(['arm1', 'disable-motor', '--yes', '--box', 'DEMO'])
        assert result.exit_code == 0
        assert 'goes limp' not in output
        assert self.post.call_args.args[3] == 'disable_motor'


class TestAcceleration(_ArmCommandTest):
    def test_get_acceleration(self):
        result, _ = _invoke(['arm1', 'get-acceleration', '--box', 'DEMO'])
        assert result.exit_code == 0
        assert self.post.call_args.args[3] == 'get_acceleration'

    def test_set_without_retract_leaves_it_to_the_arm(self):
        # --retract used to default to 60, replacing V2.4.x's default of 40.
        result, _ = _invoke(['arm1', 'set-acceleration', '--acceleration', '61',
                             '--travel', '62', '--box', 'DEMO'])
        assert result.exit_code == 0
        kwargs = self.post.call_args.kwargs
        assert kwargs['acceleration'] == 61
        assert kwargs['travel_acceleration'] == 62
        assert 'retract_acceleration' not in kwargs

    def test_set_with_retract_sends_it(self):
        result, _ = _invoke(['arm1', 'set-acceleration', '--acceleration', '61',
                             '--travel', '62', '--retract', '43', '--box', 'DEMO'])
        assert result.exit_code == 0
        assert self.post.call_args.kwargs['retract_acceleration'] == 43

    def test_a_readback_mismatch_exits_non_zero(self):
        # post_net_command prints the box's error and exits 1 on a 502.
        self.post.side_effect = SystemExit(1)
        result = CliRunner().invoke(
            arm_mod.arm, ['arm1', 'set-acceleration', '--acceleration', '61',
                          '--travel', '62', '--box', 'DEMO'],
            obj=SimpleNamespace())
        assert result.exit_code == 1


class TestNetNameOmitted(_ArmCommandTest):
    """`lager arm position --box X` used to fail with "No such command '--box'"."""

    def test_subcommand_first_uses_the_default_net(self):
        with mock.patch.object(arm_mod, 'get_default_net', return_value='arm1'):
            result, _ = _invoke(['position', '--box', 'DEMO'])
        assert result.exit_code == 0
        assert self.post.call_args.args[2] == 'arm1'
        assert self.post.call_args.args[3] == 'position'

    def test_group_box_then_subcommand_runs_the_subcommand(self):
        # This form used to take "position" as the net name and list nets.
        with mock.patch.object(arm_mod, 'get_default_net', return_value='arm1'):
            result, _ = _invoke(['--box', 'DEMO', 'position'])
        assert result.exit_code == 0
        assert self.post.call_args.args[3] == 'position'

    def test_no_default_net_is_a_clear_error(self):
        result = CliRunner().invoke(arm_mod.arm, ['position', '--box', 'DEMO'],
                                    obj=SimpleNamespace())
        assert result.exit_code != 0
        assert 'NETNAME is required' in result.output
        self.post.assert_not_called()
