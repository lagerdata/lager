# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Run by every Python process in a Box container that has no USB.

Python imports a module called ``sitecustomize`` at startup if it finds one.
``start-services-hostlink.sh`` -- the macOS start script, and only it -- puts
this directory on PYTHONPATH, so a Linux Box never runs this file.

It does one thing: make ``serial.Serial("/dev/ttyUSB0")`` reach the port on
the host, so that a user's script which opens a port with pySerial directly
works as it does on a Linux Box. The stand-in is loaded from its file rather
than imported as ``lager.hostlink.serial_proxy``, so that a script which
never uses Lager does not pay for importing it.
"""
import os


def _install():
    import importlib.util

    source = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "serial_proxy.py")
    spec = importlib.util.spec_from_file_location("lager_hostlink_serial_proxy", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.install_pyserial()


if os.environ.get("LAGER_HOSTLINK") == "1":
    try:
        _install()
    except Exception:  # noqa: BLE001 - a script without pySerial must still start
        pass
