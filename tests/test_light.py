"""HA error boundaries, using real entities without starting Home Assistant."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.atios.dali import Target  # noqa: E402
from custom_components.atios.light import AtiosLight  # noqa: E402
from custom_components.atios.transport import DaliError  # noqa: E402


@pytest.mark.parametrize("rgbw", [False, True])
@pytest.mark.parametrize("action,kwargs", [
    ("async_turn_on", {}),
    ("async_turn_on", {"brightness": 128}),
    ("async_turn_off", {}),
])
def test_light_actions_report_transport_failure_without_updating_state(rgbw, action, kwargs):
    async def check():
        error = DaliError("bus_busy")
        owner = SimpleNamespace(
            turn_on=AsyncMock(side_effect=error), turn_off=AsyncMock(side_effect=error),
        )
        target = Target.short(5)
        hub = SimpleNamespace(
            host="gateway.invalid", serial=None, sw_version=None,
            rgbw=owner, rgbw_targets={target: ()} if rgbw else {},
            send_frame=AsyncMock(side_effect=error),
        )
        light = AtiosLight(hub, SimpleNamespace(unique_id="test"), target)
        light._attr_is_on = action == "async_turn_off"
        light._attr_brightness = 64
        light.async_write_ha_state = Mock()
        before = (light.is_on, light.brightness)
        with pytest.raises(HomeAssistantError, match="bus_busy") as caught:
            await getattr(light, action)(**kwargs)
        assert caught.value.__cause__ is error
        assert (light.is_on, light.brightness) == before
        light.async_write_ha_state.assert_not_called()
    asyncio.run(check())


def test_dimmer_poll_reports_transport_failure_without_updating_state():
    async def check():
        error = DaliError("timeout")
        hub = SimpleNamespace(
            host="gateway.invalid", serial=None, sw_version=None,
            rgbw=None, rgbw_targets={}, send_frame=AsyncMock(side_effect=error),
        )
        light = AtiosLight(hub, SimpleNamespace(unique_id="test"), Target.short(5))
        light._attr_is_on = True
        light._attr_brightness = 64
        with pytest.raises(HomeAssistantError, match="timeout") as caught:
            await light.async_update()
        assert caught.value.__cause__ is error
        assert light.is_on and light.brightness == 64
    asyncio.run(check())
