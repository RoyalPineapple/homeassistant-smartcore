"""Independent physical white-channel sliders backed by the light's state."""

from __future__ import annotations

import math

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AtiosConfigEntry
from .const import DOMAIN
from .dali import Target
from .hub import AtiosHub
from .transport import DaliError


async def async_setup_entry(
    hass: HomeAssistant, entry: AtiosConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    names = {dev.target: dev.name for dev in hub.control_devices}
    async_add_entities([
        AtiosWhiteLevel(hub, entry, target, names.get(target))
        for target in hub.rgbw_targets
    ])


class AtiosWhiteLevel(NumberEntity):
    """A convenient slider; no independent cache or polling loop."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_native_min_value = 0
    _attr_native_max_value = 255
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:lightbulb-on-outline"

    def __init__(
        self, hub: AtiosHub, entry: AtiosConfigEntry, target: Target, name: str | None = None
    ) -> None:
        self._controller = hub.rgbw
        self._target = target
        # Match the existing light's device and identity convention.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)}, manufacturer="Atios",
            model="SmartCore", name=f"SmartCore ({hub.host})", serial_number=hub.serial,
            sw_version=hub.sw_version,
        )
        self._attr_unique_id = f"{entry.unique_id}_{target.unique_suffix}_white_level"
        self._attr_name = f"{name or target.unique_suffix} White level"

    @property
    def native_value(self) -> float | None:
        return self._controller.white_level(self._target)

    @property
    def available(self) -> bool:
        return self._controller.state(self._target).available

    @property
    def assumed_state(self) -> bool:
        return self._controller.state(self._target).assumed

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._controller.subscribe(self.async_write_ha_state))

    async def async_set_native_value(self, value: float) -> None:
        if not math.isfinite(value) or not 0 <= value <= 255 or value != int(value):
            raise HomeAssistantError("White level must be an integer from 0 to 255")
        try:
            await self._controller.set_white(self._target, int(value))
        except DaliError as err:
            raise HomeAssistantError(str(err)) from err
