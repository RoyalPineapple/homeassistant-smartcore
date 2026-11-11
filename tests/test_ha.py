"""Real HA entity/service and aiohttp-adapter tests; no fake HA framework.

Install requirements-test.txt to run these. Offline runner reports an explicit
skip if HA/aiohttp are absent; pure tests remain runnable without packages.
"""

import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

if importlib.util.find_spec("homeassistant") is None:
    raise unittest.SkipTest("Home Assistant is not installed; real entity/service tests not run")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp
from homeassistant.components import light as ha_light
from homeassistant.components import number as ha_number
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry, device_registry, entity_registry

from custom_components.atios import _register_services
from custom_components.atios.dali import Target, off, query_actual_level
from custom_components.atios.hub import AtiosHub
from custom_components.atios.light import AtiosLight
from custom_components.atios.number import AtiosWhiteLevel
from custom_components.atios.nvram import parse_control_devices
from custom_components.atios.rgbw import RGBWController
from custom_components.atios.transport import DaliError, DaliTransport
from test_rgbw import Gateway, device


class HAEntityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hass = HomeAssistant(self.directory.name)
        self.addAsyncCleanup(self.hass.async_stop, force=True)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        await asyncio.gather(
            area_registry.async_load(self.hass),
            device_registry.async_load(self.hass),
            entity_registry.async_load(self.hass),
        )
        # Use the real integration's exception class rather than the pure-test namespace.
        self.gateway = Gateway(error_type=DaliError)
        self.hub = AtiosHub("gateway.invalid", 0, MagicMock(spec=aiohttp.ClientSession))
        self.addAsyncCleanup(self.hub.async_stop)
        self.hub.transport = DaliTransport(self.gateway.request)
        self.hub.rgbw = RGBWController(self.hub.transport)
        self.hub.control_devices = parse_control_devices([device()])
        await self.hub.rgbw.discover(self.hub.control_devices, complete=True)
        self.hub.rgbw_targets = self.hub.rgbw.members
        self.entry = SimpleNamespace(unique_id="test_smartcore", options={}, runtime_data=self.hub)
        self.target = Target.short(5)
        self.light = AtiosLight(self.hub, self.entry, self.target, "Spots")
        self.number = AtiosWhiteLevel(self.hub, self.entry, self.target, "Spots")
        # Use real HA component service processing, including HS/XY -> RGB.
        self.assertTrue(await ha_light.async_setup(self.hass, {}))
        self.assertTrue(await ha_number.async_setup(self.hass, {}))
        from homeassistant.components.light.const import DATA_COMPONENT as LIGHT_COMPONENT
        from homeassistant.components.number import DATA_COMPONENT as NUMBER_COMPONENT
        self.light_component = self.hass.data[LIGHT_COMPONENT]
        self.number_component = self.hass.data[NUMBER_COMPONENT]
        await self.light_component.async_add_entities([self.light], update_before_add=True)
        await self.number_component.async_add_entities([self.number])
        self.assertIsNotNone(self.hass.states.get(self.light.entity_id))
        self.assertIsNotNone(self.hass.states.get(self.number.entity_id))

    async def asyncTearDown(self):
        for component in (self.light_component, self.number_component):
            for entity in tuple(component.entities):
                await component.async_remove_entity(entity.entity_id)

    async def light_action(self, **data):
        await self.hass.services.async_call("light", "turn_on", {
            "entity_id": self.light.entity_id, **data,
        }, blocking=True)

    async def test_rgbw_action_number_slider_and_identity(self):
        self.assertEqual(self.light.unique_id, "test_smartcore_addr_5")
        self.assertEqual(self.number.unique_id, "test_smartcore_addr_5_white_level")
        self.assertEqual(self.number.mode, ha_number.NumberMode.SLIDER)
        await self.light_action(rgbw_color=[255, 0, 0, 128], brightness=255)
        await self.hass.services.async_call("number", "set_value", {
            "entity_id": self.number.entity_id, "value": 200,
        }, blocking=True)
        self.assertEqual(self.light.rgbw_color, (255, 0, 0, 200))
        self.assertEqual(self.number.native_value, 200)
        self.assertEqual(self.gateway.channels[5], [254, 0, 0, 199])
        self.assertTrue(self.light.assumed_state)

    async def test_real_ha_rgb_hs_xy_conversion_preserves_w(self):
        await self.light_action(rgbw_color=[255, 0, 0, 180])
        for action in (dict(rgb_color=[0,255,0]), dict(hs_color=[240,100]), dict(xy_color=[0.4,0.4])):
            await self.light_action(**action)
            self.assertEqual(self.number.native_value, 180)
            self.assertEqual(self.light.rgbw_color[3], 180)

    async def test_brightness_off_on_and_shared_external_readback(self):
        await self.light_action(rgbw_color=[255, 0, 0, 128], brightness=128)
        await self.light.async_turn_off()
        await self.number.async_set_native_value(220)
        self.assertFalse(self.light.is_on)
        await self.light_action()
        self.assertTrue(self.light.is_on)
        self.assertEqual(self.gateway.arc[5], 127)
        self.gateway.channels[5] = [0, 254, 0, 0]
        # External changes are imported after the shared two-second cache expires.
        with patch("custom_components.atios.rgbw.time") as clock:
            clock.monotonic.return_value = time.monotonic() + 3
            await self.light.async_update()
        self.assertEqual(self.number.native_value, 0)
        self.assertEqual(self.light.rgbw_color, (0, 255, 0, 0))
        self.assertFalse(self.light.assumed_state)

    async def test_explicit_rgbw_action_replaces_pending_white(self):
        await self.light.async_turn_off()
        await self.number.async_set_native_value(220)
        await self.light_action(rgbw_color=[255, 0, 0, 0], brightness=255)
        self.assertEqual(self.gateway.channels[5], [254, 0, 0, 0])
        self.assertEqual(self.light.rgbw_color, (255, 0, 0, 0))
        self.assertEqual(self.number.native_value, 0)

    async def test_mixed_white_group_accepts_real_ha_number_action(self):
        controller = self.hub.rgbw
        group = Target.group(0)
        await controller.discover(parse_control_devices([
            dict(address=128, is_group=True), device(), device(6),
        ]), complete=True)
        await controller.poll(group, force=True)
        number = AtiosWhiteLevel(self.hub, self.entry, group, "Group spots")
        await self.number_component.async_add_entities([number])
        self.assertIsNone(number.native_value)
        self.assertTrue(number.available)
        await self.hass.services.async_call("number", "set_value", {
            "entity_id": number.entity_id, "value": 200,
        }, blocking=True)
        self.assertEqual(number.native_value, 200)
        self.assertEqual(self.gateway.channels[5], [254, 0, 64, 199])
        self.assertEqual(self.gateway.channels[6], [0, 254, 0, 199])

    async def test_legacy_brightness_scene_and_raw_services(self):
        legacy = AtiosLight(self.hub, self.entry, Target.short(6))
        legacy.async_write_ha_state = MagicMock()
        self.assertEqual(legacy.supported_color_modes, {ha_light.ColorMode.BRIGHTNESS})
        await legacy.async_turn_on(brightness=255)
        self.assertEqual(self.gateway.frames[-1], [12, 254])
        await legacy.async_turn_on()
        self.assertEqual(self.gateway.frames[-1], [13, 10])
        await legacy.async_turn_off()
        self.assertEqual(self.gateway.frames[-1], [13, 0])
        _register_services(self.hass)
        with patch.object(self.hass.config_entries, "async_entries", return_value=[self.entry]):
            await self.hass.services.async_call("atios", "recall_scene", {"scene": 3, "address": 5}, blocking=True)
            self.assertEqual(self.gateway.frames[-1], [11, 0x13])
            await self.hass.services.async_call("atios", "send_dali_frame", {"data": [11, 0]}, blocking=True)
            self.assertEqual(self.gateway.frames[-1], [11, 0])

    async def test_failure_is_not_a_successful_action(self):
        previous = self.light.rgbw_color
        self.gateway.frames.clear()
        self.gateway.fail_at = 20
        with self.assertRaises(HomeAssistantError):
            await self.number.async_set_native_value(240)
        self.assertEqual(self.light.rgbw_color, previous)
        self.assertFalse(self.light.available)
        self.assertNotIn([11, 0xE2], self.gateway.frames)


class HTTPAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = MagicMock(spec=aiohttp.ClientSession)
        self.response = MagicMock()
        self.response.json = AsyncMock(return_value=dict(
            success=True, bus_busy=False, collision_detected=False, data=0))
        self.session.post.return_value.__aenter__ = AsyncMock(return_value=self.response)
        self.hub = AtiosHub("gateway.invalid", 0, self.session)

    async def test_payload_and_actual_json_body_for_non_query_writes(self):
        await self.hub.send_frame(off(Target.short(5)))
        self.response.json.assert_awaited_once()
        self.assertEqual(self.session.post.call_args.kwargs['json'], dict(
            repeat_twice=False, wait_response=False, bits=16, data=[11, 0]))
        self.assertEqual(await self.hub.send_frame(query_actual_level(Target.short(5))), [0])

    async def test_gateway_busy_collision_and_malformed_responses(self):
        for value, reason in ((dict(success=True, bus_busy=True, collision_detected=False), 'bus_busy'),
                              (dict(success=True, bus_busy=False, collision_detected=True), 'collision'),
                              ({}, 'malformed_response')):
            self.response.json.return_value = value
            with self.assertRaises(DaliError) as caught:
                await self.hub.send_frame(off(Target.short(5)))
            self.assertEqual(caught.exception.reason, reason)

    async def test_http_error_timeout_and_invalid_json(self):
        for error, reason in ((aiohttp.ClientConnectionError(), 'http_error'),
                              (TimeoutError(), 'timeout'), (ValueError(), 'malformed_response')):
            self.response.json.side_effect = error
            with self.assertRaises(DaliError) as caught:
                await self.hub.send_frame(off(Target.short(5)))
            self.assertEqual(caught.exception.reason, reason)

    async def test_unload_drains_and_closes_transport(self):
        await self.hub.async_stop()
        with self.assertRaises(DaliError) as caught:
            await self.hub.send_frame(off(Target.short(5)))
        self.assertEqual(caught.exception.reason, 'closed')
