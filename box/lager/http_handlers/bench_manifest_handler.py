# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""``GET /bench``: the box's bench manifest, for clients that keep a copy.

The MCP server on :8100 describes the bench to an agent one question at a
time. A client that holds MANY boxes (a control plane, a fleet-level MCP
host, ``lager bench export``) wants the whole description at once, in a
versioned shape it can store and diff. This route serves that shape --
``lager.mcp.schemas.manifest.BenchManifest`` -- built from the same loaded
state the MCP tools read, so the two never disagree.

``ETag`` carries the manifest's ``content_hash``; a request that sends it
back in ``If-None-Match`` gets ``304`` and no body, so a poller that
fetches every minute costs the box nothing while nothing has changed.

Advertised in ``/status`` as ``capabilities.benchManifest`` so a client can
tell a box that serves this route from one that predates it, instead of
reading a 404 as "no bench".
"""

import logging

from flask import Flask, jsonify, make_response, request

logger = logging.getLogger(__name__)


def _build_manifest():
    # Imported here so a box whose MCP package fails to import still serves
    # every other :9000 route; only this one reports the failure.
    from lager.mcp import server_state
    from lager.mcp.engine.manifest import build_manifest

    server_state.ensure_loaded()
    bench, graph = server_state.get_bench_and_graph()
    return build_manifest(bench, graph)


def etag_matches(header_value, content_hash) -> bool:
    """Whether an ``If-None-Match`` value names ``content_hash``.

    Accepts the quoted form the route emits, a weak ``W/`` prefix, a bare
    hash, a comma-separated list, and ``*``.
    """
    if not header_value:
        return False
    for tag in str(header_value).split(','):
        tag = tag.strip()
        if tag == '*':
            return True
        if tag.startswith('W/'):
            tag = tag[2:].strip()
        if tag.strip('"') == content_hash:
            return True
    return False


def register_bench_manifest_routes(app: Flask) -> None:
    """Register the bench-manifest REST route with the Flask app."""

    @app.route('/bench', methods=['GET'])
    def get_bench_manifest():
        """The bench manifest, or ``304`` when the caller already has it."""
        try:
            manifest = _build_manifest()
        except Exception as e:  # never let a bad bench.json take the route down
            logger.exception("bench manifest build failed")
            return jsonify({'error': 'bench manifest unavailable: %s' % e}), 500

        etag = '"%s"' % manifest.content_hash
        if etag_matches(request.headers.get('If-None-Match'), manifest.content_hash):
            resp = make_response('', 304)
        else:
            resp = make_response(jsonify(manifest.model_dump(mode='json')), 200)
        resp.headers['ETag'] = etag
        # The hash, not a lifetime, decides freshness: always revalidate.
        resp.headers['Cache-Control'] = 'no-cache'
        return resp
