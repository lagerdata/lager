# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Link between a Box container and a native host agent.

On Linux the Box container owns its hardware: the drivers run inside it and
open USB devices through ``/dev``. Where the container cannot reach hardware
at all -- Docker Desktop or OrbStack on macOS -- the drivers run in a native
process on the host instead (``lager.host``), and the container reaches them
over this link.

The link is deliberately thin. It carries the Box's existing internal HTTP
interfaces unchanged and adds the three things that crossing a machine
boundary requires:

``protocol``
    Version negotiation and the error envelope.
``config``
    The shared endpoint record (address, port, token) both sides read.
``auth``
    Bearer-token check for every request the agent serves.
``relay``
    The process that runs inside the container in place of the hardware
    service. Existing Box code keeps calling ``localhost:8080``; the relay
    forwards those calls to the agent.

Nothing in this package is imported by a Linux Box. It is platform
independent: no macOS API appears here, so another host operating system can
reuse it as is.
"""
