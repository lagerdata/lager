# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Pin the request shape of DirectHTTPSession.run_exec (the `lager rust` upload).

It must POST the multipart body to the box's /exec endpoint, mirroring how
run_python targets /python.
"""

from __future__ import annotations

import os
import sys
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
sys.path.insert(0, _REPO_ROOT)

from cli.context.session import DirectHTTPSession  # noqa: E402


def _session():
    s = DirectHTTPSession.__new__(DirectHTTPSession)
    s.box_ip = "1.2.3.4"
    s.base_url = "http://1.2.3.4:5000"
    s.session = mock.Mock()
    return s


def test_run_exec_posts_to_exec_endpoint():
    s = _session()
    files = [('binary', ('prog', b'\x7fELF', 'application/octet-stream'))]
    with mock.patch.object(s, '_post_multipart_stream', return_value='resp') as post:
        out = s.run_exec("ignored", files=files)
    assert out == 'resp'
    args, kwargs = post.call_args
    assert args[0] == "http://1.2.3.4:5000/exec"
    assert args[1] is files


def test_run_python_still_posts_to_python_endpoint():
    # Guard that the shared multipart helper didn't change the python target.
    s = _session()
    with mock.patch.object(s, '_post_multipart_stream', return_value='resp') as post:
        s.run_python("ignored", files=[])
    assert post.call_args[0][0] == "http://1.2.3.4:5000/python"
