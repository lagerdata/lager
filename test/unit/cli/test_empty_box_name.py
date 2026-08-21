# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the empty ``--box`` guard in cli.box_storage.

``--box ""`` used to be indistinguishable from omitting ``--box`` entirely: an
empty string is falsy, so it fell into the "no box given" branch and silently
resolved to whatever the DEFAULT box was. The caller named one box (badly) and
got a different one, with nothing in the output saying so.

``lager boxes add --name ""`` already refused with "Box name cannot be empty",
so an empty name was validated on one path and quietly reinterpreted on the
other.

This was invisible for years because the bench suite's own check for it
(generic.sh Test 9.3) asserted an error and got one -- but only because an
earlier test in the same suite had wiped the box registry, leaving no default
to fall back to. Once the registry survived the run (issue #273), the fallback
worked and the assertion started failing, which is what surfaced this.

Both resolvers are covered because they duplicate the resolution logic rather
than one delegating to the other -- a guard added to only one would leave every
caller of the other still silently defaulting.
"""

from __future__ import annotations

from unittest import mock

import pytest

from cli.box_storage import (
    resolve_and_validate_box,
    resolve_and_validate_box_with_name,
)
from cli.errors import LagerError


RESOLVERS = (resolve_and_validate_box, resolve_and_validate_box_with_name)
RESOLVER_IDS = ('resolve_and_validate_box', 'resolve_and_validate_box_with_name')


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No unit test may open a socket.

    The guard under test runs before any I/O, so a test that reaches the
    network has already failed -- fail loudly rather than block on a connect
    timeout.
    """
    def _forbidden(*args, **kwargs):
        raise AssertionError(
            'a unit test attempted a real HTTP request; mock it explicitly'
        )

    for verb in ('get', 'post', 'put', 'delete', 'request'):
        monkeypatch.setattr(f'requests.{verb}', _forbidden, raising=False)


@pytest.mark.parametrize('resolver', RESOLVERS, ids=RESOLVER_IDS)
@pytest.mark.parametrize('value', ['', '   ', '\t', '\n'],
                         ids=['empty', 'spaces', 'tab', 'newline'])
def test_explicitly_empty_box_is_refused(resolver, value):
    """An explicit empty or whitespace-only --box is a user error."""
    with pytest.raises(LagerError) as excinfo:
        resolver(mock.Mock(), value)

    assert 'cannot be empty' in str(excinfo.value)


@pytest.mark.parametrize('resolver', RESOLVERS, ids=RESOLVER_IDS)
def test_omitted_box_still_falls_back_to_the_default(resolver):
    """``None`` means "not given" and must keep using the default box.

    This is the half the guard must NOT break: the distinction it draws is
    between "unset" and "set to empty", not between "empty" and "non-empty".
    """
    with mock.patch('cli.context.get_default_box', return_value='10.0.0.1') as default, \
         mock.patch('cli.box_storage._check_box_lock'):
        result = resolver(mock.Mock(default_box=None), None)

    assert default.called
    resolved = result[0] if isinstance(result, tuple) else result
    assert resolved == '10.0.0.1'


@pytest.mark.parametrize('resolver', RESOLVERS, ids=RESOLVER_IDS)
def test_named_box_is_unaffected(resolver):
    """A real saved name resolves exactly as before."""
    with mock.patch('cli.box_storage.get_box_ip', return_value='10.0.0.2'), \
         mock.patch('cli.box_storage._check_box_lock'):
        result = resolver(mock.Mock(), 'PRD-1')

    resolved = result[0] if isinstance(result, tuple) else result
    assert resolved == '10.0.0.2'
