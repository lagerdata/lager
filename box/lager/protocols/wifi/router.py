# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import enum
import asyncio
import logging

try:
    import aiohttp
    from asusrouter import AsusRouter, AsusData
    from asusrouter.modules.parental_control import PCRuleType, ParentalControlRule
except ImportError:
    pass

"""
:meta private:
"""
logger = logging.getLogger(__name__)

async def toggle_internet_access(router, mac, enabled):
    rule_type = PCRuleType.BLOCK if not enabled else PCRuleType.DISABLE
    rule = ParentalControlRule(mac=mac, type=rule_type)
    return await router.async_set_state(state=rule)


async def set_internet_access(hostname, username, password, mac, is_enabled):
    async with aiohttp.ClientSession() as session:
        router = AsusRouter(
            hostname=hostname,
            username=username,
            password=password,
            session=session,
        )

        await router.async_connect()

        success = await toggle_internet_access(router, mac, is_enabled)

        await router.async_disconnect()
        return success

class Wifi:
    """
        Class for managing access to wifi
    """

    def __init__(self, name, pin, location):
        self._name = name
        self._pin = pin
        self._location = location

    @property
    def name(self):
        return self._name

    @property
    def pin(self):
        return self._pin

    def __str__(self):
        return f'<lager.Wifi name="{self.name}" pin={self.pin}>'

    def enable(self):
        location = self._location
        return asyncio.run(set_internet_access(location['hostname'], location['username'], location['password'], location['mac'], True))

    def disable(self):
        location = self._location
        return asyncio.run(set_internet_access(location['hostname'], location['username'], location['password'], location['mac'], False))
