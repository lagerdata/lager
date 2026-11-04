# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""``python -m lager.mcp.engine_service``.

Environment:

- ``LAGER_ENGINE_HOST``: bind address, default ``127.0.0.1``. A container
  sets ``0.0.0.0`` and publishes the port on the host's loopback.
- ``LAGER_ENGINE_PORT``: default ``8200``.
- ``LAGER_ENGINE_TOKEN``: when set, required in ``X-Lager-Engine-Token``.
- ``LAGER_ENGINE_VERSION`` / ``LAGER_ENGINE_REVISION``: reported by
  ``/healthz``; the image sets them at build time.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    import uvicorn

    from .app import build_app

    host = os.environ.get("LAGER_ENGINE_HOST", "127.0.0.1")
    port_text = os.environ.get("LAGER_ENGINE_PORT", "8200")
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        print(f"LAGER_ENGINE_PORT must be a port number, got {port_text!r}", file=sys.stderr)
        return 2
    app = build_app(os.environ.get("LAGER_ENGINE_TOKEN") or None)
    uvicorn.run(app, host=host, port=int(port_text), log_level="warning", proxy_headers=False, server_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
