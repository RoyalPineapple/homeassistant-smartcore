"""The Atios SmartCore integration."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_LINE,
    DEFAULT_LINE,
    DOMAIN,
    ISSUE_DALI_IP_DISABLED,
    ISSUE_LUNATONE_SILENT,
    SERVICE_RECALL_SCENE,
    SERVICE_SEND_FRAME,
)
from .dali import Frame, Target, goto_scene
from .hub import AtiosHub
from .nvram import parse_control_devices, parse_input_devices
from .panel import async_register_panel, async_remove_panel
from .rgbw import RGBWController

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.UPDATE,
]

type AtiosConfigEntry = ConfigEntry[AtiosHub]


async def async_setup_entry(hass: HomeAssistant, entry: AtiosConfigEntry) -> bool:
    """Set up Atios SmartCore from a config entry."""
    session = async_get_clientsession(hass)
    hub = AtiosHub(entry.data[CONF_HOST], entry.data.get(CONF_LINE, DEFAULT_LINE), session)
    await hub.async_fetch_info()  # serial + firmware version for device info / update entity

    # Button/sensor events need the device's "DALI IP Interface" setting on;
    # surface it as a repair issue that clears itself once it is switched on.
    entry.async_on_unload(
        hub.add_ip_interface_listener(
            lambda enabled: _async_ip_interface_changed(hass, entry, hub, enabled)
        )
    )
    # ...and a SmartCore firmware that actually talks on the Lunatone socket.
    entry.async_on_unload(
        hub.add_stream_listener(lambda ok: _async_stream_changed(hass, entry, hub, ok))
    )
    await hub.async_fetch_settings()

    # Read the configured device model from NVRAM (same endpoint the web
    # configurator uses). On failure the platforms fall back to their legacy
    # behaviour (options address list / discover-on-first-event).
    raw_control = await hub.async_fetch_nvm_section("control_devices")
    raw_inputs = await hub.async_fetch_nvm_section("input_devices")
    if raw_control is not None:
        hub.control_devices = parse_control_devices(raw_control)
    if raw_inputs is not None:
        hub.input_devices = parse_input_devices(raw_inputs)

    hub.rgbw = RGBWController(hub.transport)
    await hub.rgbw.discover(
        hub.control_devices,
        complete=hub.control_inventory_complete and not any(d.get("collision") for d in raw_control or ()),
    )
    hub.rgbw_targets = hub.rgbw.members

    await hub.async_start()
    entry.runtime_data = hub

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _register_services(hass)
    await async_register_panel(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: AtiosConfigEntry) -> None:
    """Reload on options change (recreates light entities and re-applies panel)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AtiosConfigEntry) -> bool:
    """Unload a config entry."""
    async_remove_panel(hass, entry)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
        for key in (ISSUE_DALI_IP_DISABLED, ISSUE_LUNATONE_SILENT):
            ir.async_delete_issue(hass, DOMAIN, _issue_id(entry, key))
    return unloaded


def _issue_id(entry: AtiosConfigEntry, key: str) -> str:
    return f"{key}_{entry.entry_id}"


@callback
def _async_ip_interface_changed(
    hass: HomeAssistant, entry: AtiosConfigEntry, hub: AtiosHub, enabled: bool
) -> None:
    """Raise or clear the "DALI IP Interface is off" repair issue."""
    if enabled:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(entry, ISSUE_DALI_IP_DISABLED))
        return
    _LOGGER.warning(
        "Atios %s: the SmartCore's \"DALI IP Interface\" setting is off, so no "
        "button or sensor events reach Home Assistant. Turn it on under "
        "System → DALI in the SmartCore web interface.",
        hub.host,
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(entry, ISSUE_DALI_IP_DISABLED),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_DALI_IP_DISABLED,
        translation_placeholders={"host": hub.host},
        # a plain GET on / redirects to the web UI's system page
        learn_more_url=f"http://{hub.host}/",
    )


def _register_services(hass: HomeAssistant) -> None:
    """Register domain services once."""
    if hass.services.has_service(DOMAIN, SERVICE_SEND_FRAME):
        return

    def _first_hub() -> AtiosHub:
        entries: list[AtiosConfigEntry] = hass.config_entries.async_entries(DOMAIN)
        loaded = [e for e in entries if getattr(e, "runtime_data", None)]
        if not loaded:
            raise RuntimeError("No Atios SmartCore configured")
        return loaded[0].runtime_data

    async def _send_frame(call: ServiceCall) -> None:
        hub = _first_hub()
        await hub.send_frame(
            Frame(
                data=list(call.data["data"]),
                bits=call.data.get("bits", 16),
                send_twice=call.data.get("send_twice", False),
                wait_for_answer=call.data.get("wait_for_answer", False),
            )
        )

    async def _recall_scene(call: ServiceCall) -> None:
        hub = _first_hub()
        scene = call.data["scene"]
        addr = call.data.get("address")
        group = call.data.get("group")
        if group is not None:
            target = Target.group(group)
        elif addr is not None:
            target = Target.short(addr)
        else:
            target = Target.broadcast()
        await hub.send_frame(goto_scene(target, scene))

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_FRAME,
        _send_frame,
        schema=vol.Schema(
            {
                vol.Required("data"): [vol.All(int, vol.Range(min=0, max=255))],
                vol.Optional("bits", default=16): vol.In([16, 24, 25]),
                vol.Optional("send_twice", default=False): cv.boolean,
                vol.Optional("wait_for_answer", default=False): cv.boolean,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RECALL_SCENE,
        _recall_scene,
        schema=vol.Schema(
            {
                vol.Required("scene"): vol.All(int, vol.Range(min=0, max=15)),
                vol.Exclusive("address", "target"): vol.All(int, vol.Range(min=0, max=63)),
                vol.Exclusive("group", "target"): vol.All(int, vol.Range(min=0, max=15)),
            }
        ),
    )


@callback
def _async_stream_changed(
    hass: HomeAssistant, entry: AtiosConfigEntry, hub: AtiosHub, ok: bool
) -> None:
    """Raise or clear the "SmartCore does not stream DALI events" repair issue."""
    if ok:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(entry, ISSUE_LUNATONE_SILENT))
        return
    _LOGGER.warning(
        "Atios %s: connected to the DALI IP websocket, but the SmartCore "
        "(firmware %s) never sent its greeting, so no button or sensor events "
        "will arrive. Known SmartCore firmware bug (3.2.x built on ESP-IDF v5.5.5/v6.0.1+); "
        "lights are not affected.",
        hub.host,
        hub.sw_version or "unknown",
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(entry, ISSUE_LUNATONE_SILENT),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_LUNATONE_SILENT,
        translation_placeholders={
            "host": hub.host,
            "version": hub.sw_version or "unknown",
        },
    )
