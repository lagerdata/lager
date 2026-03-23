# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

import io
import json
import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
BOX_DIR = os.path.join(REPO_ROOT, 'box')
sys.path.insert(0, BOX_DIR)

import factory  # noqa: E402
from factory import Step, run, get_secret, _emit, _read_response, FACTORY_PREFIX  # noqa: E402


def parse_events(output):
    """Parse factory protocol events from captured stdout."""
    events = []
    for line in output.strip().split('\n'):
        if line.startswith(FACTORY_PREFIX):
            events.append(json.loads(line[len(FACTORY_PREFIX):]))
    return events


class PassingStep(Step):
    DisplayName = 'Passing'

    def run(self):
        return True


class FailingStep(Step):
    DisplayName = 'Failing'

    def run(self):
        return False


class FailingStopStep(Step):
    DisplayName = 'FailStop'
    StopOnFail = True

    def run(self):
        return False


class FailingContinueStep(Step):
    DisplayName = 'FailContinue'
    StopOnFail = False

    def run(self):
        return False


class ErrorStep(Step):
    DisplayName = 'Error'

    def run(self):
        raise RuntimeError('something broke')


class LoggingStep(Step):
    DisplayName = 'Logger'

    def run(self):
        self.log('hello')
        self.log('oops', file='stderr')
        return True


class StateWriterStep(Step):
    DisplayName = 'Writer'

    def run(self):
        self.state['key'] = 'value'
        return True


class StateReaderStep(Step):
    DisplayName = 'Reader'

    def run(self):
        return self.state.get('key') == 'value'


class FinalizerStep(Step):
    DisplayName = 'Finalizer'

    def run(self):
        self.state['finalized'] = True
        return True


class FailingFinalizerStep(Step):
    def run(self):
        raise RuntimeError('finalizer error')


class NoneReturnStep(Step):
    DisplayName = 'NoneReturn'

    def run(self):
        return None


class TestEmit:
    def test_emit_writes_protocol_format(self, capsys):
        _emit({'type': 'test', 'data': 123})
        captured = capsys.readouterr()
        assert captured.out.startswith(FACTORY_PREFIX)
        payload = json.loads(captured.out[len(FACTORY_PREFIX):])
        assert payload == {'type': 'test', 'data': 123}

    def test_emit_ends_with_newline(self, capsys):
        _emit({'type': 'test'})
        captured = capsys.readouterr()
        assert captured.out.endswith('\n')


class TestReadResponse:
    def test_read_response_parses_json(self, monkeypatch):
        monkeypatch.setattr('sys.stdin', io.StringIO('{"value": "hello"}\n'))
        assert _read_response() == 'hello'

    def test_read_response_returns_none_on_eof(self, monkeypatch):
        monkeypatch.setattr('sys.stdin', io.StringIO(''))
        assert _read_response() is None

    def test_read_response_returns_bool(self, monkeypatch):
        monkeypatch.setattr('sys.stdin', io.StringIO('{"value": true}\n'))
        assert _read_response() is True

    def test_read_response_missing_value_key(self, monkeypatch):
        monkeypatch.setattr('sys.stdin', io.StringIO('{"other": 1}\n'))
        assert _read_response() is None


class TestGetSecret:
    def test_get_secret_from_lager_env(self, monkeypatch):
        monkeypatch.setenv('LAGER_SECRET_MY_KEY', 'secret123')
        assert get_secret('MY_KEY') == 'secret123'

    def test_get_secret_fallback_to_bare_name(self, monkeypatch):
        monkeypatch.delenv('LAGER_SECRET_API_TOKEN', raising=False)
        monkeypatch.setenv('API_TOKEN', 'bare_value')
        assert get_secret('API_TOKEN') == 'bare_value'

    def test_get_secret_prefers_prefixed(self, monkeypatch):
        monkeypatch.setenv('LAGER_SECRET_KEY', 'prefixed')
        monkeypatch.setenv('KEY', 'bare')
        assert get_secret('KEY') == 'prefixed'

    def test_get_secret_returns_empty_when_missing(self, monkeypatch):
        monkeypatch.delenv('LAGER_SECRET_NOPE', raising=False)
        monkeypatch.delenv('NOPE', raising=False)
        assert get_secret('NOPE') == ''


class TestStepLog:
    def test_log_default_stdout(self, capsys):
        step = LoggingStep({})
        step.log('test message')
        events = parse_events(capsys.readouterr().out)
        assert events[0]['type'] == 'lager-log'
        assert events[0]['file'] == 'stdout'
        assert events[0]['content'] == 'test message'

    def test_log_stderr(self, capsys):
        step = LoggingStep({})
        step.log('error msg', file='stderr')
        events = parse_events(capsys.readouterr().out)
        assert events[0]['file'] == 'stderr'

    def test_log_invalid_file_defaults_stdout(self, capsys):
        step = LoggingStep({})
        step.log('msg', file='invalid')
        events = parse_events(capsys.readouterr().out)
        assert events[0]['file'] == 'stdout'


class TestStepPresent:
    def test_present_buttons(self, capsys, monkeypatch):
        monkeypatch.setattr('sys.stdin', io.StringIO('{"value": true}\n'))
        step = PassingStep({})
        result = step.present_buttons([['Pass', True], ['Fail', False]])
        assert result is True
        events = parse_events(capsys.readouterr().out)
        assert events[0]['type'] == 'present_buttons'
        assert events[0]['data'] == [['Pass', True], ['Fail', False]]

    def test_present_text_input(self, capsys, monkeypatch):
        monkeypatch.setattr('sys.stdin', io.StringIO('{"value": "typed"}\n'))
        step = PassingStep({})
        result = step.present_text_input('Enter serial:', size=30)
        assert result == 'typed'
        events = parse_events(capsys.readouterr().out)
        assert events[0]['type'] == 'present_text_input'
        assert events[0]['data'] == {'prompt': 'Enter serial:', 'size': 30}

    def test_present_radios(self, capsys, monkeypatch):
        monkeypatch.setattr('sys.stdin', io.StringIO('{"value": "b"}\n'))
        step = PassingStep({})
        result = step.present_radios('Pick one', [['A', 'a'], ['B', 'b']])
        assert result == 'b'
        events = parse_events(capsys.readouterr().out)
        assert events[0]['type'] == 'present_radios'

    def test_present_checkboxes(self, capsys, monkeypatch):
        monkeypatch.setattr('sys.stdin', io.StringIO('{"value": ["a", "c"]}\n'))
        step = PassingStep({})
        result = step.present_checkboxes('Check', [['A', 'a'], ['B', 'b'], ['C', 'c']])
        assert result == ['a', 'c']

    def test_present_select(self, capsys, monkeypatch):
        monkeypatch.setattr('sys.stdin', io.StringIO('{"value": "x"}\n'))
        step = PassingStep({})
        result = step.present_select('Choose', [['X', 'x'], ['Y', 'y']], allow_multiple=False)
        assert result == 'x'
        events = parse_events(capsys.readouterr().out)
        assert events[0]['data']['allow_multiple'] is False

    def test_update_heading_non_blocking(self, capsys):
        step = PassingStep({})
        step.update_heading('New heading')
        events = parse_events(capsys.readouterr().out)
        assert events[0]['type'] == 'update_heading'
        assert events[0]['data'] == 'New heading'

    def test_present_link_non_blocking(self, capsys):
        step = PassingStep({})
        step.present_link('https://example.com', text='Click here')
        events = parse_events(capsys.readouterr().out)
        assert events[0]['type'] == 'present_link'
        assert events[0]['data'] == {'url': 'https://example.com', 'text': 'Click here'}

    def test_present_image_non_blocking(self, capsys):
        step = PassingStep({})
        step.present_image('board.png')
        events = parse_events(capsys.readouterr().out)
        assert events[0]['type'] == 'present_image'
        assert events[0]['data'] == {'filename': 'board.png'}


class TestRun:
    def test_single_passing_step(self, capsys):
        run([PassingStep])
        events = parse_events(capsys.readouterr().out)
        assert events[0] == {'type': 'start', 'class': 'PassingStep', 'name': 'Passing'}
        assert events[1]['type'] == 'done'
        assert events[1]['data'] is True
        complete = events[2]
        assert complete['type'] == 'lager-factory-complete'
        assert complete['result'] is True
        assert complete['success'] == 1
        assert complete['failure'] == 0
        assert complete['failed_step'] == ''

    def test_single_failing_step(self, capsys):
        run([FailingStep])
        events = parse_events(capsys.readouterr().out)
        complete = events[-1]
        assert complete['result'] is False
        assert complete['failure'] == 1
        assert complete['failed_step'] == 'Failing'

    def test_stop_on_fail_halts_execution(self, capsys):
        run([FailingStopStep, PassingStep])
        events = parse_events(capsys.readouterr().out)
        class_names = [e.get('class') for e in events if e['type'] == 'start']
        assert class_names == ['FailingStopStep']
        complete = events[-1]
        assert complete['success'] == 0
        assert complete['failure'] == 1

    def test_continue_on_fail(self, capsys):
        run([FailingContinueStep, PassingStep])
        events = parse_events(capsys.readouterr().out)
        class_names = [e.get('class') for e in events if e['type'] == 'start']
        assert class_names == ['FailingContinueStep', 'PassingStep']
        complete = events[-1]
        assert complete['success'] == 1
        assert complete['failure'] == 1
        assert complete['failed_step'] == 'FailContinue'

    def test_error_step_emits_error_event(self, capsys):
        run([ErrorStep])
        events = parse_events(capsys.readouterr().out)
        error_events = [e for e in events if e['type'] == 'error']
        assert len(error_events) == 1
        assert error_events[0]['message'] == 'something broke'
        complete = events[-1]
        assert complete['result'] is False

    def test_none_return_treated_as_pass(self, capsys):
        run([NoneReturnStep])
        events = parse_events(capsys.readouterr().out)
        done_events = [e for e in events if e['type'] == 'done']
        assert done_events[0]['data'] is True

    def test_shared_state_between_steps(self, capsys):
        run([StateWriterStep, StateReaderStep])
        events = parse_events(capsys.readouterr().out)
        done_events = [e for e in events if e['type'] == 'done']
        assert done_events[0]['data'] is True
        assert done_events[1]['data'] is True

    def test_logging_step(self, capsys):
        run([LoggingStep])
        events = parse_events(capsys.readouterr().out)
        log_events = [e for e in events if e['type'] == 'lager-log']
        assert len(log_events) == 2
        assert log_events[0]['content'] == 'hello'
        assert log_events[0]['file'] == 'stdout'
        assert log_events[1]['content'] == 'oops'
        assert log_events[1]['file'] == 'stderr'

    def test_display_name_fallback_to_class_name(self, capsys):
        class NoDisplayName(Step):
            def run(self):
                return True

        run([NoDisplayName])
        events = parse_events(capsys.readouterr().out)
        assert events[0]['name'] == 'NoDisplayName'


class TestFinalizer:
    def test_finalizer_runs_on_success(self, capsys):
        state_holder = {}

        class TrackingFinalizer(Step):
            def run(self):
                self.state['finalized'] = True
                return True

        run([PassingStep], finalizer_cls=TrackingFinalizer)
        events = parse_events(capsys.readouterr().out)
        complete = events[-1]
        assert complete['result'] is True

    def test_finalizer_runs_on_failure(self, capsys):
        run([FailingStep], finalizer_cls=FinalizerStep)
        events = parse_events(capsys.readouterr().out)
        complete = events[-1]
        assert complete['result'] is False

    def test_finalizer_error_swallowed(self, capsys):
        run([PassingStep], finalizer_cls=FailingFinalizerStep)
        events = parse_events(capsys.readouterr().out)
        complete = events[-1]
        assert complete['type'] == 'lager-factory-complete'
        assert complete['result'] is True

    def test_finalizer_runs_after_stop_on_fail(self, capsys):
        run([FailingStopStep, PassingStep], finalizer_cls=FinalizerStep)
        events = parse_events(capsys.readouterr().out)
        complete = events[-1]
        assert complete['type'] == 'lager-factory-complete'
        assert complete['failure'] == 1
        assert complete['success'] == 0
