# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``lager.binaries.store`` and the :9000 ``/binaries/*`` +
``/download-file`` handlers.

The store is the shared disk logic behind both the :5000 python-exec service
and the :9000 Flask handlers; these tests redirect its directory constants
and download allowlist into a temp tree so no real box paths are touched.
"""

import importlib.util
import io
import os
import shutil
import stat
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")

if BOX_DIR not in sys.path:
    sys.path.insert(0, BOX_DIR)


def _load_module(dotted, filepath):
    if dotted in sys.modules:
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


# Import the REAL lager package (its __init__ stubs missing third-party deps)
# rather than installing a bare namespace module: this file collects before
# test_box_http_server_capabilities.py, whose `from lager import ...` would
# otherwise resolve against the attribute-less bare module and fail.
import lager  # noqa: E402,F401
from lager.binaries import store  # noqa: E402


class _BinariesBase(unittest.TestCase):
    """Redirect the store's paths + allowlist into a temp tree."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lager-binaries-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.host_dir = os.path.join(self.tmp, "host-binaries")
        self.container_dir = os.path.join(self.tmp, "container-binaries")
        self.download_root = os.path.join(self.tmp, "lager-output")
        os.makedirs(self.download_root)

        self._orig = (store.HOST_BINARIES_DIR, store.CONTAINER_BINARIES_DIR,
                      store.ALLOWED_DOWNLOAD_ROOTS)
        store.HOST_BINARIES_DIR = self.host_dir
        store.CONTAINER_BINARIES_DIR = self.container_dir
        store.ALLOWED_DOWNLOAD_ROOTS = (self.download_root,)
        self.addCleanup(self._restore)

    def _restore(self):
        (store.HOST_BINARIES_DIR, store.CONTAINER_BINARIES_DIR,
         store.ALLOWED_DOWNLOAD_ROOTS) = self._orig


class StoreTests(_BinariesBase):
    def test_list_state_empty(self):
        state = store.list_state()
        self.assertEqual(state["binaries"], [])
        self.assertEqual(state["host_path"], self.host_dir)
        self.assertEqual(state["container_path"], self.container_dir)
        self.assertFalse(state["mounted"])

    def test_add_binary_writes_executable_file(self):
        result = store.add_binary("my_tool", b"#!/bin/sh\necho hi\n")
        self.assertTrue(result["success"])
        self.assertEqual(result["name"], "my_tool")
        # Response path is always the container path (where lager python sees it).
        self.assertEqual(result["path"], os.path.join(self.container_dir, "my_tool"))
        self.assertEqual(result["size"], 18)
        # Container dir doesn't exist -> wrote to host dir, restart required.
        self.assertTrue(result["restart_required"])
        on_disk = os.path.join(self.host_dir, "my_tool")
        self.assertTrue(os.path.isfile(on_disk))
        self.assertTrue(os.stat(on_disk).st_mode & stat.S_IXUSR)

    def test_add_prefers_container_dir_when_mounted(self):
        os.makedirs(self.container_dir)
        result = store.add_binary("t", b"x")
        self.assertFalse(result["restart_required"])
        self.assertTrue(os.path.isfile(os.path.join(self.container_dir, "t")))

    def test_add_rejects_path_separators(self):
        for bad in ("../evil", "a/b", "a\\b"):
            with self.assertRaises(store.StoreError) as ctx:
                store.add_binary(bad, b"x")
            self.assertEqual(ctx.exception.status, 400)

    def test_add_requires_name(self):
        with self.assertRaises(store.StoreError) as ctx:
            store.add_binary("", b"x")
        self.assertEqual(ctx.exception.status, 400)

    def test_list_reflects_added_binaries(self):
        store.add_binary("tool_a", b"aaaa")
        state = store.list_state()
        self.assertEqual(len(state["binaries"]), 1)
        entry = state["binaries"][0]
        self.assertEqual(entry["name"], "tool_a")
        self.assertEqual(entry["size"], 4)
        self.assertTrue(entry["executable"])

    def test_remove_binary(self):
        store.add_binary("gone", b"x")
        result = store.remove_binary("gone")
        self.assertEqual(result, {"success": True, "name": "gone"})
        self.assertEqual(store.list_state()["binaries"], [])

    def test_remove_missing_is_404(self):
        with self.assertRaises(store.StoreError) as ctx:
            store.remove_binary("nope")
        self.assertEqual(ctx.exception.status, 404)

    def test_remove_rejects_traversal(self):
        with self.assertRaises(store.StoreError) as ctx:
            store.remove_binary("../etc/passwd")
        self.assertEqual(ctx.exception.status, 400)

    # ------------------------------------------------------------------ #
    # /download-file allowlist                                            #
    # ------------------------------------------------------------------ #

    def test_resolve_download_path_ok(self):
        target = os.path.join(self.download_root, "result.bin")
        with open(target, "wb") as f:
            f.write(b"data")
        path, size = store.resolve_download_path(target)
        self.assertEqual(path, target)
        self.assertEqual(size, 4)

    def test_resolve_rejects_outside_allowlist(self):
        outside = os.path.join(self.tmp, "secret.txt")
        with open(outside, "w") as f:
            f.write("x")
        with self.assertRaises(store.StoreError) as ctx:
            store.resolve_download_path(outside)
        self.assertEqual(ctx.exception.status, 403)

    def test_resolve_rejects_traversal_out_of_root(self):
        sneaky = os.path.join(self.download_root, "..", "secret.txt")
        with self.assertRaises(store.StoreError) as ctx:
            store.resolve_download_path(sneaky)
        self.assertEqual(ctx.exception.status, 403)

    def test_resolve_rejects_sibling_prefix_dir(self):
        # /tmp/lager-output-evil must not match the /tmp/lager-output root.
        evil_dir = self.download_root + "-evil"
        os.makedirs(evil_dir)
        target = os.path.join(evil_dir, "f.txt")
        with open(target, "w") as f:
            f.write("x")
        with self.assertRaises(store.StoreError) as ctx:
            store.resolve_download_path(target)
        self.assertEqual(ctx.exception.status, 403)

    def test_resolve_missing_file_is_404(self):
        with self.assertRaises(store.StoreError) as ctx:
            store.resolve_download_path(os.path.join(self.download_root, "nope"))
        self.assertEqual(ctx.exception.status, 404)

    def test_resolve_directory_is_400(self):
        with self.assertRaises(store.StoreError) as ctx:
            store.resolve_download_path(self.download_root)
        self.assertEqual(ctx.exception.status, 400)


class BinariesHttpTests(_BinariesBase):
    """The :9000 /binaries/* + /download-file routes wrap lager.binaries.store."""

    def setUp(self):
        super().setUp()
        from flask import Flask
        handler = _load_module(
            "lager.http_handlers.binaries_handler",
            os.path.join(BOX_DIR, "lager", "http_handlers", "binaries_handler.py"))
        app = Flask(__name__)
        handler.register_binaries_routes(app)
        self.client = app.test_client()

    def test_list_route(self):
        store.add_binary("tool", b"abc")
        r = self.client.get("/binaries/list")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["binaries"][0]["name"], "tool")
        self.assertEqual(data["host_path"], self.host_dir)
        self.assertIn("mounted", data)

    def test_add_route_multipart(self):
        r = self.client.post("/binaries/add", data={
            "binary": (io.BytesIO(b"#!/bin/sh\n"), "upload.bin"),
            "name": "my_tool",
        }, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["name"], "my_tool")
        self.assertEqual(data["size"], 10)
        self.assertTrue(os.path.isfile(os.path.join(self.host_dir, "my_tool")))

    def test_add_route_missing_binary_is_400(self):
        r = self.client.post("/binaries/add", data={"name": "x"},
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertIn("binary file is required", r.get_json()["error"])

    def test_add_route_missing_name_is_400(self):
        r = self.client.post("/binaries/add", data={
            "binary": (io.BytesIO(b"x"), "f.bin"),
        }, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertIn("name is required", r.get_json()["error"])

    def test_add_route_bad_name_is_400(self):
        r = self.client.post("/binaries/add", data={
            "binary": (io.BytesIO(b"x"), "f.bin"),
            "name": "../evil",
        }, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Invalid binary name", r.get_json()["error"])

    def test_remove_route(self):
        store.add_binary("gone", b"x")
        r = self.client.post("/binaries/remove", json={"name": "gone"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"success": True, "name": "gone"})

    def test_remove_route_missing_is_404(self):
        r = self.client.post("/binaries/remove", json={"name": "nope"})
        self.assertEqual(r.status_code, 404)

    def test_remove_route_invalid_json_is_400(self):
        r = self.client.post("/binaries/remove", data="not json",
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_download_route_streams_file(self):
        target = os.path.join(self.download_root, "out.bin")
        with open(target, "wb") as f:
            f.write(b"payload")
        r = self.client.get(f"/download-file?filename={target}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"payload")
        self.assertEqual(r.headers["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", r.headers["Content-Disposition"])

    def test_download_route_missing_param_is_400(self):
        r = self.client.get("/download-file")
        self.assertEqual(r.status_code, 400)
        self.assertIn("filename", r.get_json()["error"])

    def test_download_route_outside_allowlist_is_403(self):
        outside = os.path.join(self.tmp, "secret.txt")
        with open(outside, "w") as f:
            f.write("x")
        r = self.client.get(f"/download-file?filename={outside}")
        self.assertEqual(r.status_code, 403)

    def test_download_route_missing_file_is_404(self):
        r = self.client.get(
            f"/download-file?filename={os.path.join(self.download_root, 'nope')}")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
