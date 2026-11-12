"""Light platform for Atios SmartCore (raw DALI control gear).

Lights are created from the device model stored in the SmartCore's NVRAM (the
same list the DALI Configurator shows): every configured short address and
group becomes a named light, plus the bus-wide broadcast light. If the NVRAM
endpoint is unavailable (older firmware) the platform falls back to the legacy
per-address list from the options.

Brightness-only gear uses DAPC / GO TO LAST ACTIVE LEVEL / OFF. Verified RGBW
targets use a shared controller, explicit channel intents and member readback;
broadcast and unverified group states remain optimistic.
"""

from __future__ import annotations

from homeassistant.components.light import (
    ATTR_BRIGHTNESS, ATTR_RGB_COLOR, ATTR_RGBW_COLOR, ColorMode, LightEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AtiosConfigEntry
from .const import CONF_LIGHTS, DOMAIN
from .dali import (
    Target,
    TargetType,
    dali_level_to_ha_brightness,
    ha_brightness_to_dali,
    level,
    off,
    query_actual_level,
    goto_last_active,
)
from .hub import AtiosHub
from .transport import DaliError


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AtiosConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = entry.runtime_data

    entities: list[AtiosLight] = [AtiosLight(hub, entry, Target.broadcast())]
    if hub.control_devices:
        # named lights from the NVRAM device model (groups + short addresses)
        for dev in hub.control_devices:
            entities.append(AtiosLight(hub, entry, dev.target, name=dev.name))
    else:
        # legacy fallback: per-address list from the options
        for addr in entry.options.get(CONF_LIGHTS, []):
            try:
                entities.append(AtiosLight(hub, entry, Target.short(int(addr))))
            except (ValueError, TypeError):
                continue
    async_add_entities(entities)


class AtiosLight(LightEntity):
    """A DALI control-gear target exposed as an HA light."""

    _attr_has_entity_name = True
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(
        self,
        hub: AtiosHub,
        entry: AtiosConfigEntry,
        target: Target,
        name: str | None = None,
    ) -> None:
        self._hub = hub
        self._target = target
        self._attr_unique_id = f"{entry.unique_id}_{target.unique_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            manufacturer="Atios",
            model="SmartCore",
            name=f"SmartCore ({hub.host})",
            serial_number=hub.serial,
            sw_version=hub.sw_version,
        )
        if name:
            self._attr_name = name
        elif target.type is TargetType.BROADCAST:
            self._attr_name = "All lights"
        elif target.type is TargetType.GROUP:
            self._attr_name = f"Group {target.number}"
        else:
            self._attr_name = f"Light {target.number}"
        # broadcast/group can't be reliably queried -> optimistic
        self._attr_assumed_state = target.type is not TargetType.SHORT
        self._attr_is_on = False
        self._attr_brightness: int | None = None
        self._rgbw = hub.rgbw if target in hub.rgbw_targets else None
        if self._rgbw is not None:
            # Accept RGB directly so HA's HS/XY conversion doesn't invent W=0
            # (or synthesize W from RGB) before it reaches our state owner.
            self._attr_supported_color_modes = {ColorMode.RGB, ColorMode.RGBW}
            self._attr_color_mode = ColorMode.RGBW

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._rgbw is not None:
            self.async_on_remove(self._rgbw.subscribe(self._rgbw_changed))
            self._rgbw_changed()

    def _rgbw_changed(self) -> None:
        state = self._rgbw.state(self._target)
        self._attr_rgbw_color = state.rgbw
        self._attr_brightness = state.brightness
        self._attr_is_on = state.is_on
        self._attr_assumed_state = state.assumed
        self._attr_available = state.available
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        try:
            if self._rgbw is not None:
                await self._rgbw.turn_on(
                    self._target, brightness=kwargs.get(ATTR_BRIGHTNESS),
                    rgbw=kwargs.get(ATTR_RGBW_COLOR), rgb=kwargs.get(ATTR_RGB_COLOR),
                )
                return
            if ATTR_BRIGHTNESS in kwargs:
                dali = ha_brightness_to_dali(kwargs[ATTR_BRIGHTNESS])
                await self._hub.send_frame(level(self._target, dali))
                self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
            else:
                # Plain "on": restore the level the gear had before it was switched
                # off (GO TO LAST ACTIVE LEVEL), rather than forcing full brightness.
                await self._hub.send_frame(goto_last_active(self._target))
                # Actual level is read back by async_update for single addresses;
                # optimistic only for broadcast/group.
                if self._target.type is not TargetType.SHORT and self._attr_brightness is None:
                    self._attr_brightness = 255
        except DaliError as err:
            raise HomeAssistantError(str(err)) from err
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        try:
            if self._rgbw is not None:
                await self._rgbw.turn_off(self._target)
                return
            await self._hub.send_frame(off(self._target))
        except DaliError as err:
            raise HomeAssistantError(str(err)) from err
        self._attr_is_on = False
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Poll actual level for single-address lights only."""
        if self._rgbw is not None:
            try:
                await self._rgbw.poll(self._target)
            except DaliError:
                pass  # shared owner marks unavailable; next poll can recover
            return
        if self._target.type is not TargetType.SHORT:
            return
        try:
            answer = await self._hub.send_frame(query_actual_level(self._target))
        except DaliError as err:
            raise HomeAssistantError(str(err)) from err
        if not answer:
            return
        lvl = answer[0]
        if lvl in (255, None):  # MASK / no answer
            return
        self._attr_is_on = lvl > 0
        self._attr_brightness = dali_level_to_ha_brightness(lvl) if lvl > 0 else None
