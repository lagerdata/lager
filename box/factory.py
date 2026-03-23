# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
factory - Factory Test Step Framework

Provides the Step base class and run() orchestrator for interactive,
multi-step hardware test sequences. Test scripts import from this module:

    from factory import run, get_secret, Step

All structured events are emitted on stdout with the protocol format:
    \\x02FACTORY:<json>\\n

Stdin responses from the operator are single-line JSON:
    {"value": <response>}\\n
"""

import sys
import json
import os

FACTORY_PREFIX = '\x02FACTORY:'


def _emit(event):
    """Write a factory protocol event to stdout."""
    sys.stdout.write(FACTORY_PREFIX + json.dumps(event) + '\n')
    sys.stdout.flush()


def _read_response():
    """Block reading a JSON response from stdin. Returns the 'value' field."""
    line = sys.stdin.readline()
    if not line:
        return None
    data = json.loads(line)
    return data.get('value')


def get_secret(name):
    """
    Retrieve a secret by name.

    Looks for LAGER_SECRET_<name> first (injected by the executor),
    then falls back to the bare name.
    """
    value = os.environ.get(f'LAGER_SECRET_{name}')
    if value is not None:
        return value
    return os.environ.get(name, '')


class Step:
    """
    Base class for factory test steps.

    Subclasses override run() to implement test logic. The run() method
    should return True for pass, False for fail.
    """

    DisplayName = None
    Description = None
    Image = None
    Link = None
    StopOnFail = True

    def __init__(self, state):
        self.state = state

    def run(self):
        """Override in subclasses. Return True for pass, False for fail."""
        raise NotImplementedError

    def log(self, message, file=None):
        """Emit a log event. file is 'stdout' or 'stderr' (default 'stdout')."""
        stream = file if file in ('stdout', 'stderr') else 'stdout'
        _emit({
            'type': 'lager-log',
            'class': type(self).__name__,
            'file': stream,
            'content': str(message),
        })

    def present_buttons(self, buttons, timeout=None):
        """
        Present a list of buttons to the operator and wait for a response.

        Args:
            buttons: list of [label, value] pairs, e.g. [["Pass", True], ["Fail", False]]

        Returns:
            The value of the button the operator clicked.
        """
        _emit({
            'type': 'present_buttons',
            'class': type(self).__name__,
            'data': buttons,
        })
        return _read_response()

    def present_pass_fail_buttons(self, timeout=None):
        """Shorthand for presenting Pass/Fail buttons. Returns True or False."""
        return self.present_buttons([['Pass', True], ['Fail', False]], timeout=timeout)

    def present_text_input(self, prompt, size=25):
        """Present a text input field. Returns the string the operator entered."""
        _emit({
            'type': 'present_text_input',
            'class': type(self).__name__,
            'data': {'prompt': prompt, 'size': size},
        })
        return _read_response()

    def present_radios(self, label, choices):
        """
        Present radio buttons.

        Args:
            label: Label for the radio group.
            choices: list of [label, value] pairs.

        Returns:
            The selected value.
        """
        _emit({
            'type': 'present_radios',
            'class': type(self).__name__,
            'data': {'label': label, 'choices': choices},
        })
        return _read_response()

    def present_checkboxes(self, label, choices):
        """
        Present checkboxes.

        Args:
            label: Label for the checkbox group.
            choices: list of [label, value] pairs.

        Returns:
            List of selected values.
        """
        _emit({
            'type': 'present_checkboxes',
            'class': type(self).__name__,
            'data': {'label': label, 'choices': choices},
        })
        return _read_response()

    def present_select(self, label, choices, allow_multiple=False):
        """
        Present a dropdown select.

        Args:
            label: Label for the select.
            choices: list of [label, value] pairs.
            allow_multiple: Allow multiple selections.

        Returns:
            Selected value (or list if allow_multiple).
        """
        _emit({
            'type': 'present_select',
            'class': type(self).__name__,
            'data': {'label': label, 'choices': choices, 'allow_multiple': allow_multiple},
        })
        return _read_response()

    def update_heading(self, text):
        """Update the heading text in the UI. Non-blocking."""
        _emit({
            'type': 'update_heading',
            'class': type(self).__name__,
            'data': text,
        })

    def present_link(self, url, text=''):
        """Display a clickable link. Non-blocking."""
        _emit({
            'type': 'present_link',
            'class': type(self).__name__,
            'data': {'url': url, 'text': text},
        })

    def present_image(self, filename):
        """Display an image. Non-blocking."""
        _emit({
            'type': 'present_image',
            'class': type(self).__name__,
            'data': {'filename': filename},
        })


def run(steps, finalizer_cls=None):
    """
    Execute a list of Step classes in order.

    Args:
        steps: list of Step subclasses (not instances)
        finalizer_cls: optional Step subclass that runs at the end regardless of success/failure
    """
    state = {}
    success_count = 0
    failure_count = 0
    failed_step = ''
    all_passed = True

    for step_cls in steps:
        step = step_cls(state)
        class_name = type(step).__name__
        display_name = step.DisplayName or class_name

        _emit({'type': 'start', 'class': class_name, 'name': display_name})

        try:
            result = step.run()
            passed = bool(result) if result is not None else True
        except Exception as e:
            _emit({'type': 'error', 'class': class_name, 'message': str(e)})
            passed = False

        _emit({
            'type': 'done',
            'class': class_name,
            'data': passed,
            'stop_on_fail': step.StopOnFail,
        })

        if passed:
            success_count += 1
        else:
            failure_count += 1
            all_passed = False
            if not failed_step:
                failed_step = display_name
            if step.StopOnFail:
                break

    if finalizer_cls:
        try:
            finalizer = finalizer_cls(state)
            finalizer.run()
        except Exception:
            pass

    _emit({
        'type': 'lager-factory-complete',
        'result': all_passed,
        'success': success_count,
        'failure': failure_count,
        'failed_step': failed_step,
    })
