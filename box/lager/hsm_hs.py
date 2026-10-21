# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
hardware_service adapter for hsm nets (create_device factory).

Thin shim over the HSM dispatcher's driver; hardware_service serializes every
call under the net's ``device_id`` lock. /invoke carries JSON, so binary data
crosses as base64 and public keys as PEM text. The password arrives as the
``password`` kwarg, which hardware_service redacts from its log.
"""
from __future__ import annotations

import base64


class HsmHardwareAdapter:
    def __init__(self, netname: str) -> None:
        self._netname = netname

    def _driver(self):
        from lager.security.hsm import dispatcher as _disp
        return _disp.get_driver(self._netname)

    def info(self) -> dict:
        return self._driver().info()

    def blink(self, seconds=5, *, auth_key_id=1, password=None) -> None:
        self._driver().blink(int(seconds), auth_key_id=int(auth_key_id), password=password)

    def list_objects(self, type=None, label=None, *, auth_key_id=1, password=None) -> list:
        return self._driver().list_objects(type=type, label=label,
                                           auth_key_id=int(auth_key_id), password=password)

    def delete_object(self, object_id, object_type="asymmetric-key", *,
                      auth_key_id=1, password=None) -> None:
        self._driver().delete_object(int(object_id), object_type,
                                     auth_key_id=int(auth_key_id), password=password)

    def generate_key(self, object_id, algorithm, label="", domains=(1,), capabilities=None, *,
                     auth_key_id=1, password=None) -> dict:
        return self._driver().generate_key(int(object_id), algorithm, label, domains, capabilities,
                                           auth_key_id=int(auth_key_id), password=password)

    def get_public_key(self, object_id, *, auth_key_id=1, password=None) -> str:
        return self._driver().get_public_key(int(object_id),
                                             auth_key_id=int(auth_key_id), password=password)

    def sign(self, object_id, data_b64, mechanism, *, auth_key_id=1, password=None) -> str:
        data = base64.b64decode(data_b64, validate=True)
        signature = self._driver().sign(int(object_id), data, mechanism,
                                        auth_key_id=int(auth_key_id), password=password)
        return base64.b64encode(signature).decode("ascii")

    def reset(self, *, auth_key_id=1, password=None) -> None:
        self._driver().reset(auth_key_id=int(auth_key_id), password=password)


def create_device(net_info, **_):
    netname = (net_info or {}).get("name")
    return HsmHardwareAdapter(netname)
