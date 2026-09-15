# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""One rule decides whether a box lock is ours, everywhere on the CLI.

Holder matching was fixed one comparison at a time: the pre-command check
first, then the three acquire decisions, and `lager boxes unlock` never. Each
fix passed its own tests and left the other comparisons disagreeing. A CI job
could be told by `lager hello` that a lock was its own, be refused by
`lager boxes unlock`, and wait on that same lock in the next auto-lock.

`box_storage.holder_is_ours` is now the only comparison. These tests pin its
truth table, the shape of a holder another tool writes, the holder a resumed
lock renews under, and an AST scan that fails if a raw holder comparison
comes back.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
import requests

from cli import box_storage

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
CLI_DIR = REPO_ROOT / 'cli'

CI_HOLDER = 'ci:github:org/repo#123-1/hardware-tests@runner-1:{}'
OTHER_JOB = 'ci:github:org/repo#123-1/unit-tests@runner-1:{}'
GENERIC_HOLDER = 'ci:generic:runner-host:{}'
TOOL_HOLDER = 'tool:5f0c:Ada Lovelace:ada@example.com'


class _FakeResp:
    def __init__(self, status_code, json_body=None):
        self.status_code = status_code
        self._json = json_body

    def json(self):
        if self._json is None:
            raise ValueError('no body')
        return self._json


@pytest.fixture(autouse=True)
def _clean_lock_env(monkeypatch):
    for key in (
        'LAGER_LOCK_HOLDER', 'LAGER_USER', 'LAGER_LOCK_WAIT', 'CI',
        'GITHUB_RUN_ID', 'DRONE', 'GITLAB_CI', 'BITBUCKET_BUILD_NUMBER',
        'JENKINS_URL', 'CI_SERVER_NAME', 'BUILD_TAG',
    ):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# holder_is_ours
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('stored, holder, user, expected', [
    # the exact holder
    ('alice', 'alice', 'alice', True),
    (CI_HOLDER.format(42), CI_HOLDER.format(42), 'runner', True),
    # the same CI scope, from a later process of the job
    (CI_HOLDER.format(42), CI_HOLDER.format(99), 'runner', True),
    # a different job of the same run
    (CI_HOLDER.format(42), OTHER_JOB.format(99), 'runner', False),
    # the plain user a `lager boxes lock` reservation records, seen from CI
    ('runner', CI_HOLDER.format(99), 'runner', True),
    # a holder another tool wrote, matched by its email, in any case
    (TOOL_HOLDER, CI_HOLDER.format(99), 'ada@example.com', True),
    (TOOL_HOLDER, 'ada', 'ADA@Example.com', True),
    # never by the display name, and never for another email
    (TOOL_HOLDER, 'Ada Lovelace', 'Ada Lovelace', False),
    (TOOL_HOLDER, 'bob', 'bob@example.com', False),
    # a CI holder whose tail looks like an email is not read as one
    ('ci:gitlab:group/p#9/job:42@runner.example.com', 'x', 'runner.example.com', False),
    # the three-part shape is not matched by email
    ('tool:5f0c:ada@example.com', 'x', 'ada@example.com', False),
    # nothing stored, or a foreign plain user
    ('', 'alice', 'alice', False),
    (None, 'alice', 'alice', False),
    ('bob', 'alice', 'alice', False),
    # inherited from lock_scope, which strips a trailing pid from any holder;
    # pinned so that a change to it is deliberate
    ('pytest:bench:11', 'pytest:bench:22', 'x', True),
])
def test_holder_is_ours_truth_table(stored, holder, user, expected):
    assert box_storage.holder_is_ours(stored, holder, user) is expected


def test_a_generic_ci_scope_does_not_count_when_coarse_scopes_are_refused():
    # Every CI job on one host shares ci:generic:<host>, so unlock must not
    # treat a sibling job's live lock as its own.
    stored = GENERIC_HOLDER.format(1)
    later = GENERIC_HOLDER.format(2)
    assert box_storage.holder_is_ours(stored, later, 'x') is True
    assert box_storage.holder_is_ours(stored, later, 'x', coarse_scope_ok=False) is False
    assert box_storage.holder_is_ours(stored, stored, 'x', coarse_scope_ok=False) is True


def test_a_specific_ci_scope_still_counts_when_coarse_scopes_are_refused():
    assert box_storage.holder_is_ours(
        CI_HOLDER.format(1), CI_HOLDER.format(2), 'x', coarse_scope_ok=False) is True


def test_the_pre_command_check_uses_the_same_rule(monkeypatch):
    monkeypatch.setattr(box_storage, 'get_lager_user', lambda: 'ada@example.com')
    assert box_storage._lock_held_by_self(TOOL_HOLDER)


# ---------------------------------------------------------------------------
# holder_email
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('stored, email', [
    (TOOL_HOLDER, 'ada@example.com'),
    ('tool:5f0c:ada@example.com', None),
    ('tool:5f0c:Name:With:Colons:ada@example.com', None),
    ('tool:5f0c:ada@x.com:ada@example.com', None),
    ('ci:gitlab:group/p#9/job:42@runner.example.com', None),
    ('alice', None),
    ('', None),
    (None, None),
])
def test_holder_email_reads_only_the_four_part_shape(stored, email):
    assert box_storage.holder_email(stored) == email


def test_the_email_holder_is_the_one_displayed_by_name():
    # format_lock_user parses the same shape to show the name.
    assert box_storage.format_lock_user(TOOL_HOLDER) == 'Ada Lovelace'


# ---------------------------------------------------------------------------
# heartbeat_holder
# ---------------------------------------------------------------------------


def test_a_resumed_lock_renews_under_the_holder_the_box_stored():
    lock = {'user': CI_HOLDER.format(111), 'ttl_seconds': 1800}
    assert box_storage.heartbeat_holder(
        'already_ours', lock, CI_HOLDER.format(222)) == CI_HOLDER.format(111)


def test_an_acquired_lock_renews_under_this_processs_holder():
    assert box_storage.heartbeat_holder(
        'acquired', {'user': CI_HOLDER.format(222)}, CI_HOLDER.format(222),
    ) == CI_HOLDER.format(222)


def test_a_resumed_lock_with_no_stored_user_falls_back():
    assert box_storage.heartbeat_holder('already_ours', {}, 'me') == 'me'
    assert box_storage.heartbeat_holder('already_ours', None, 'me') == 'me'


# ---------------------------------------------------------------------------
# acquire_box_lock uses the same rule as the check
# ---------------------------------------------------------------------------


class TestAcquireUsesTheSameRule:
    @pytest.fixture(autouse=True)
    def _no_gateway(self, monkeypatch):
        monkeypatch.setattr(box_storage, '_gateway_kwargs', lambda ip: {})
        monkeypatch.setattr(box_storage, '_check_gateway', lambda resp, ip, **k: resp)

    @staticmethod
    def _no_post(monkeypatch):
        def refuse(*a, **k):
            raise AssertionError('must not POST over a lock this job may use')
        monkeypatch.setattr(requests, 'post', refuse)

    def test_a_runner_accounts_reservation_is_used_by_its_ci_job(self, monkeypatch):
        # The pre-command check accepted this lock; the acquire used to wait
        # LAGER_LOCK_WAIT (1800 s in CI) on it and then exit 1.
        monkeypatch.setattr(box_storage, 'get_lager_user', lambda: 'ci-bench')
        monkeypatch.setattr(
            requests, 'get',
            lambda *a, **k: _FakeResp(200, {'locked': True, 'user': 'ci-bench'}))
        self._no_post(monkeypatch)
        state, data = box_storage.acquire_box_lock(
            '10.0.0.1', 'bench', CI_HOLDER.format(9), wait_seconds=0)
        assert state == 'already_ours'
        assert data['user'] == 'ci-bench'

    def test_a_409_naming_the_runner_accounts_reservation_does_not_wait(self, monkeypatch):
        monkeypatch.setattr(box_storage, 'get_lager_user', lambda: 'ci-bench')
        monkeypatch.setattr(
            requests, 'get', lambda *a, **k: _FakeResp(200, {'locked': False}))
        monkeypatch.setattr(
            requests, 'post',
            lambda *a, **k: _FakeResp(409, {'lock': {'user': 'ci-bench'}}))

        def no_sleep(_):
            raise AssertionError('must not wait on a lock this job may use')
        monkeypatch.setattr('time.sleep', no_sleep)

        state, _ = box_storage.acquire_box_lock(
            '10.0.0.1', 'bench', CI_HOLDER.format(9), wait_seconds=1800)
        assert state == 'already_ours'

    def test_a_lock_another_tool_took_under_my_email_is_mine(self, monkeypatch):
        monkeypatch.setattr(box_storage, 'get_lager_user', lambda: 'ada@example.com')
        monkeypatch.setattr(
            requests, 'get',
            lambda *a, **k: _FakeResp(200, {'locked': True, 'user': TOOL_HOLDER}))
        self._no_post(monkeypatch)
        state, _ = box_storage.acquire_box_lock(
            '10.0.0.1', 'bench', 'ada@example.com', wait_seconds=0)
        assert state == 'already_ours'

    def test_a_foreign_lock_is_still_refused(self, monkeypatch):
        monkeypatch.setattr(box_storage, 'get_lager_user', lambda: 'alice')
        monkeypatch.setattr(
            requests, 'get',
            lambda *a, **k: _FakeResp(200, {'locked': True, 'user': 'bob'}))
        monkeypatch.setattr(
            requests, 'post',
            lambda *a, **k: _FakeResp(409, {'lock': {'user': 'bob'}}))
        with pytest.raises(SystemExit):
            box_storage.acquire_box_lock(
                '10.0.0.1', 'bench', 'alice', wait_seconds=0, quiet=True)


# ---------------------------------------------------------------------------
# One rule in the tree
# ---------------------------------------------------------------------------

#: The rule and its parts may compare holders directly.
RULE_FUNCTIONS = frozenset({'holder_is_ours', 'holder_email', 'heartbeat_holder', 'lock_scope'})

#: Names that carry a lock holder string in the lock code.
HOLDER_NAMES = frozenset({
    'locked_by', 'holder', 'resolved_holder', 'previous_holder', 'stored', 'other',
})

#: Calls that produce a holder or a holder's scope.
HOLDER_CALLS = frozenset({'get_lager_user', 'get_lock_holder', 'lock_scope'})

#: The files that carry lock-holder code. The name-based rule is limited to
#: these, where the names above always mean a holder.
LOCK_FILES = (
    CLI_DIR / 'box_storage.py',
    CLI_DIR / 'commands' / 'box' / 'lock.py',
    CLI_DIR / 'commands' / 'development' / 'python.py',
)


def _callee(call):
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _enclosing_functions(tree):
    """Map every node to the name of the innermost function around it."""
    owner = {}

    def visit(node, current):
        for child in ast.iter_child_nodes(node):
            name = (child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    else current)
            owner[child] = name
            visit(child, name)

    visit(tree, None)
    return owner


def _is_holder(node):
    if isinstance(node, ast.Name):
        return node.id in HOLDER_NAMES
    return isinstance(node, ast.Call) and _callee(node) in HOLDER_CALLS


def _raw_holder_comparisons(source, label):
    tree = ast.parse(source)
    owner = _enclosing_functions(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
            continue
        if any(_is_holder(n) for n in (node.left, *node.comparators)) \
                and owner.get(node) not in RULE_FUNCTIONS:
            found.append(f'{label}:{node.lineno} in {owner.get(node)}()')
    return found


def _stray_lock_scope_calls(source, label):
    tree = ast.parse(source)
    owner = _enclosing_functions(tree)
    return [
        f'{label}:{node.lineno}'
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _callee(node) == 'lock_scope'
        and owner.get(node) != 'holder_is_ours'
    ]


def _heartbeats_not_using_heartbeat_holder(source, label):
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee(node) in {'HeartbeatThread', '_HeartbeatThread'}:
            holder = node.args[1] if len(node.args) > 1 else None
            if not (isinstance(holder, ast.Call) and _callee(holder) == 'heartbeat_holder'):
                found.append(f'{label}:{node.lineno}')
    return found


def _read(path):
    return path.read_text(encoding='utf-8'), str(path.relative_to(REPO_ROOT))


def test_no_holder_comparison_bypasses_holder_is_ours():
    offenders = [hit for path in LOCK_FILES for hit in _raw_holder_comparisons(*_read(path))]
    assert offenders == [], (
        'These compare lock holders directly. Call box_storage.holder_is_ours: '
        'fixing one comparison at a time left the others disagreeing.')


def test_lock_scope_is_called_only_inside_holder_is_ours():
    offenders = [
        hit for path in sorted(CLI_DIR.rglob('*.py'))
        for hit in _stray_lock_scope_calls(*_read(path))
    ]
    assert offenders == []


def test_every_heartbeat_renews_under_heartbeat_holder():
    offenders = [
        hit for path in LOCK_FILES
        for hit in _heartbeats_not_using_heartbeat_holder(*_read(path))
    ]
    assert offenders == [], (
        'A heartbeat must send the holder string the box stored; pass '
        'heartbeat_holder(state, lock_data, holder).')


def test_the_scans_see_each_shape_they_forbid():
    # A scanner that silently matches nothing would pass forever.
    source = (
        "def check(locked_by, holder):\n"
        "    if locked_by == get_lager_user():\n"
        "        return True\n"
        "    return lock_scope(locked_by) != lock_scope(holder)\n"
        "\n"
        "def renew(ip, holder):\n"
        "    return HeartbeatThread(ip, holder, 60)\n"
    )
    assert _raw_holder_comparisons(source, 'x') == ['x:2 in check()', 'x:4 in check()']
    assert _stray_lock_scope_calls(source, 'x') == ['x:4', 'x:4']
    assert _heartbeats_not_using_heartbeat_holder(source, 'x') == ['x:7']
