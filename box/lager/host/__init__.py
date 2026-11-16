# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Native host agent (``lager-host``).

Runs the Box's hardware-owning services directly on the host operating
system, for hosts where the Box container cannot reach hardware itself. The
container talks to it through ``lager.hostlink``.

The agent does not reimplement anything. It imports the same hardware
service and the same drivers a Linux Box runs, and adds only what running
outside the container requires: authentication on every request, the
handshake, and the location of the Box's configuration directory.

A Linux Box never starts this package.
"""

import os

# The release pipeline stamps the bundle; a source checkout reports "dev".
AGENT_VERSION = os.environ.get("LAGER_HOST_VERSION", "dev")
