"""Connection hub for a single Atios SmartCore.

Transport is native aiohttp — no external DALI library. The SmartCore exposes:

  * a confirmed HTTP endpoint ``POST /api/dali/iface`` that sends a raw DALI
    frame and (with ``wait_response``) returns the answer as
    ``{"success":true,"bus_busy":false,"collision_detected":false,"data":<byte>}``;
  * an emulated Lunatone DALI-2 IoT websocket at ``ws://<host>/`` that streams
    ``daliMonitor`` frames (bus traffic, incl. DALI-2 input/button events).
    Confirmed on fw 2.7.9: on connect it greets with ``{"type":"info"}``
    reporting name "dali-iot", protocolVersion 3.0.

Lights and status use the HTTP path (verified on device). The websocket is used
only to *receive* monitor frames for buttons; it is best-effort — if it can't be
reached, lights are unaffected and it retries quietly.

What the firmware requires for that stream (checked against the SmartCore
firmware source, lib-dali ``dali_transport_layer.c`` / ``dali_ip_websocket.c``):

  * ``daliMonitor`` JSON is sent only while the device setting
    "DALI IP Interface" (System → DALI in the web UI, ``dali_ip_interface`` in
    ``GET /settings``) is on. It is off by default. While it is off the socket
    still connects and greets, but no bus frame ever arrives. We read the flag
    on setup and on every connect, and report it via add_ip_interface_listener.
  * No subscribe message is needed. ``/cmd/dali_monitor_start`` only drives the
    web UI's plain-text ``DALI:...`` monitor on ``/ws`` (broadcast to every ws
    client, so it also shows up here); it has no effect on ``daliMonitor``.
    A text "ping" is not understood on ``/`` either — the device just logs a
    JSON parse error — so keepalive is left to the ws-level heartbeat.
  * The socket serves ONE client: the firmware keeps a single global fd and the
    most recent handshake takes the stream over. Another Lunatone client on
    ``/`` (tools/monitor.py, a second HA, DALI Cockpit) silences us without
    closing our socket, and we stay silent after it leaves until we reconnect.
  * Every client is greeted with ``{"type":"info"}`` right after the
    handshake. Firmware built on ESP-IDF v5.5.5/v6.0.1+ (seen on 3.2.2) never runs the
    handler's handshake branch, so it registers no client and silently drops
    the greeting and everything after it. We watch for any Lunatone JSON after
    connecting and report its absence via add_stream_listener.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

import aiohttp

from .const import LUNATONE_GREETING_TIMEOUT_S, RECONNECT_MAX, RECONNECT_MIN
from .dali import Frame
from .transport import DaliError, DaliTransport, parse_response

_LOGGER = logging.getLogger(__name__)

# Message types the Lunatone socket sends. The web UI's own JSON notifications
# (ota_status, system_event, ...) are broadcast to every ws client, this one
# included, but carry no "type", so they don't count as the device talking.
_LUNATONE_TYPES = frozenset({"info", "daliMonitor", "daliFrame", "daliAnswer"})


@dataclass
class MonitorFrame:
    """A raw frame observed on the bus (decoded further by dali.decode_input_event)."""

    data: list[int]
    bits: int
    line: int | None = None
    framing_error: bool = False


def _parse_ws_json(raw: str) -> dict | None:
    """Decode one ws text message as a JSON object, or None if it isn't one.

    Besides Lunatone-protocol JSON the socket carries the web UI's plain-text
    bus monitor (``DALI:[IN],DA24 Evt,008001,NA``) whenever someone has started
    the DALI Monitor in the web UI. Those lines repeat frames we already get as
    daliMonitor JSON, so they are dropped here — parsing both would deliver
    every button press twice and break gesture synthesis.
    """
    if not raw or not raw.lstrip().startswith("{"):
        return None
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return msg if isinstance(msg, dict) else None


def _monitor_frame(msg: dict) -> MonitorFrame | None:
    """Extract an inbound MonitorFrame from a daliMonitor message.

    Confirmed on fw 2.7.9, the SmartCore pushes each observed bus frame as::

        {"type":"daliMonitor","data":{"line":0,"bits":24,"externalSource":true,
         "data":[0,128,1], ...}, "timeSignature":{...}}

    ``externalSource`` is true for traffic that originated on the bus (input
    devices, gear answers) and false for the SmartCore's own output (our
    commands, and the arc-power frames it sends itself when a coupler is bound
    to a light). Only external frames are input-event candidates.
    """
    if msg.get("type") != "daliMonitor":
        return None
    data = msg.get("data")
    if not isinstance(data, dict):
        return None
    payload = data.get("data")
    if not isinstance(payload, list) or not payload:
        return None
    if not data.get("externalSource"):
        return None  # our own outgoing frame, not something on the bus
    try:
        octets = [int(b) & 0xFF for b in payload]
    except (TypeError, ValueError):
        return None
    bits = data.get("bits")
    return MonitorFrame(
        data=octets,
        bits=int(bits) if isinstance(bits, int) else len(octets) * 8,
        line=data.get("line"),
        framing_error=bool(data.get("framingError")),
    )


def _notify(cbs: list[Callable[[bool], None]], value: bool) -> None:
    for cb in list(cbs):
        try:
            cb(value)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Atios state listener failed")


class AtiosHub:
    """Manage HTTP control and the (optional) monitor websocket for one SmartCore."""

    def __init__(self, host: str, line: int, session: aiohttp.ClientSession) -> None:
        self._base_url = host if host.startswith("http") else f"http://{host}"
        self._host = self._base_url.removeprefix("http://").removeprefix("https://")
        # Lunatone-emulation ws lives at the root path (confirmed fw 2.7.9 and
        # firmware source). /ws also accepts a ws, but that is the web UI's
        # socket and only carries the plain-text monitor, never daliMonitor.
        self._ws_url = f"ws://{self._host}/"
        self._line = line
        self._session = session

        self._task: asyncio.Task | None = None
        self._closing = False
        self._ws_warned = False
        self._monitor_cbs: list[Callable[[MonitorFrame], None]] = []
        self._ip_interface: bool | None = None
        self._ip_interface_cbs: list[Callable[[bool], None]] = []
        self._greeted = False  # Lunatone JSON seen on the current connection
        self._stream_ok: bool | None = None
        self._stream_cbs: list[Callable[[bool], None]] = []
        self._info: dict = {}
        self.control_devices: list = []  # nvram.ControlDevice, set at setup
        self.input_devices: list = []  # nvram.InputDevice, set at setup
        self.rgbw_targets: dict = {}  # target -> shared RGBW control/state owner
        self.rgbw = None  # populated before platforms are forwarded
        self.transport = DaliTransport(self._request_frame)

    @property
    def host(self) -> str:
        return self._host

    @property
    def serial(self) -> str | None:
        return self._info.get("serial")

    @property
    def sw_version(self) -> str | None:
        return self._info.get("version_string")

    @property
    def info(self) -> dict:
        return self._info

    @property
    def latest_version(self) -> str | None:
        """Available firmware version, from the /ota_status 'latest' block.

        Populated after async_ota_check(); None until a check has run.
        """
        latest = self._info.get("latest")
        if isinstance(latest, dict):
            return latest.get("version_string")
        return None

    @property
    def ip_interface_enabled(self) -> bool | None:
        """The device's "DALI IP Interface" setting; None until it is known."""
        return self._ip_interface

    async def async_fetch_settings(self) -> None:
        """Read the "DALI IP Interface" flag from GET /settings.

        The daliMonitor stream only flows while it is on (see module
        docstring). Unauthenticated, like the web UI's own settings read.
        Best-effort: on failure the last known state is kept.
        """
        try:
            async with self._session.get(
                f"{self._base_url}/settings", timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                resp.raise_for_status()
                body = await resp.json(content_type=None)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Atios %s: settings fetch failed: %s", self._host, err)
            return
        if isinstance(body, dict) and isinstance(body.get("dali_ip_interface"), bool):
            self._set_ip_interface(body["dali_ip_interface"])

    def _set_ip_interface(self, enabled: bool) -> None:
        if enabled == self._ip_interface:
            return
        self._ip_interface = enabled
        _notify(self._ip_interface_cbs, enabled)

    @property
    def stream_ok(self) -> bool | None:
        """Whether the Lunatone socket talks to us; None until it is known."""
        return self._stream_ok

    def _set_stream_ok(self, ok: bool) -> None:
        if ok == self._stream_ok:
            return
        self._stream_ok = ok
        _notify(self._stream_cbs, ok)

    async def async_fetch_info(self) -> dict:
        """Read GET /ota_status (serial, firmware version, update flag)."""
        try:
            async with self._session.get(
                f"{self._base_url}/ota_status", timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                resp.raise_for_status()
                body = await resp.json(content_type=None)
                if isinstance(body, dict):
                    self._info = body
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Atios %s: ota_status fetch failed: %s", self._host, err)
        return self._info

    async def async_fetch_nvm_section(self, section: str) -> list[dict] | None:
        """Read one NVRAM section via GET /api/dali/nvm, following pagination.

        The web configurator pages with limit=4; we mirror that exactly since
        it is the only request shape confirmed against the firmware. Returns
        None when the endpoint is unreachable (older firmware), so callers can
        fall back.
        """
        devices: list[dict] = []
        offset = 0
        for _ in range(32):  # 32 * 4 = 128 > max 64 addresses + groups
            url = (
                f"{self._base_url}/api/dali/nvm"
                f"?section={section}&offset={offset}&limit=4"
            )
            try:
                async with self._session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    resp.raise_for_status()
                    body = await resp.json(content_type=None)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Atios %s: nvm fetch %s failed: %s", self._host, section, err
                )
                return None
            if not isinstance(body, dict) or not body.get("success"):
                return None
            devices.extend((body.get("data") or {}).get(section) or [])
            pagination = body.get("pagination") or {}
            if not pagination.get("has_more"):
                break
            offset += pagination.get("limit", 4)
        else:
            return None  # never promote a truncated inventory to RGBW groups
        return devices

    async def async_trigger_ota(self) -> bool:
        """Start a firmware update via POST /cmd/ota_update.

        Confirmed by Atios. Note: only works if the device has no web password.
        """
        try:
            async with self._session.post(
                f"{self._base_url}/cmd/ota_update", timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                resp.raise_for_status()
                return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Atios %s: OTA trigger failed: %s", self._host, err)
            return False

    async def async_ota_check(self) -> None:
        """Ask the device to check for updates (POST /cmd/ota_update_check).

        Afterwards /ota_status carries a 'latest' block with the available
        version. Best-effort: firmware without the endpoint just 404s.
        """
        try:
            async with self._session.post(
                f"{self._base_url}/cmd/ota_update_check",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Atios %s: ota_update_check failed: %s", self._host, err)

    # ---- lifecycle --------------------------------------------------------

    async def async_start(self) -> None:
        self._closing = False
        self._task = asyncio.create_task(self._run_monitor(), name=f"atios-{self._host}")

    async def async_stop(self) -> None:
        self._closing = True
        if self.rgbw is not None:
            await self.rgbw.close()
        await self.transport.close()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def add_monitor_listener(self, cb: Callable[[MonitorFrame], None]) -> Callable[[], None]:
        self._monitor_cbs.append(cb)
        return lambda: self._monitor_cbs.remove(cb)

    def add_ip_interface_listener(self, cb: Callable[[bool], None]) -> Callable[[], None]:
        """Call ``cb(enabled)`` whenever the "DALI IP Interface" state changes."""
        self._ip_interface_cbs.append(cb)
        return lambda: self._ip_interface_cbs.remove(cb)

    def add_stream_listener(self, cb: Callable[[bool], None]) -> Callable[[], None]:
        """Call ``cb(ok)`` when the Lunatone socket starts or stops talking to us."""
        self._stream_cbs.append(cb)
        return lambda: self._stream_cbs.remove(cb)

    # ---- monitor websocket (receive-only, best-effort) --------------------

    async def _run_monitor(self) -> None:
        backoff = RECONNECT_MIN
        while not self._closing:
            watchdog: asyncio.Task | None = None
            try:
                # heartbeat: ws-level PING, answered by the device's HTTP
                # server itself. Nothing else needs sending: the daliMonitor
                # stream needs no subscribe (see module docstring).
                async with self._session.ws_connect(
                    self._ws_url, heartbeat=30, timeout=aiohttp.ClientTimeout(total=10)
                ) as ws:
                    self._ws_warned = False
                    backoff = RECONNECT_MIN
                    _LOGGER.info("Atios %s: monitor websocket connected", self._host)
                    self._greeted = False
                    watchdog = asyncio.create_task(self._greeting_watchdog())
                    # Re-check on every connect, so a setting changed while we
                    # were away is picked up.
                    await self.async_fetch_settings()
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle_ws_text(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                if self._closing:
                    break
                # Quiet: lights work over HTTP regardless; warn once, then debug.
                if not self._ws_warned:
                    _LOGGER.info(
                        "Atios %s: monitor websocket unavailable (%s). Lights work "
                        "over HTTP; button events resume once it reconnects. "
                        "Retrying quietly.",
                        self._host,
                        err,
                    )
                    self._ws_warned = True
                else:
                    _LOGGER.debug("Atios %s: ws retry (%s)", self._host, err)
            finally:
                if watchdog is not None:
                    watchdog.cancel()
            if not self._closing:
                await asyncio.sleep(backoff)
                backoff = min(RECONNECT_MAX, backoff * 2)

    async def _greeting_watchdog(self) -> None:
        """Flag a connection on which the device never says anything."""
        await asyncio.sleep(LUNATONE_GREETING_TIMEOUT_S)
        if not self._greeted:
            self._set_stream_ok(False)

    def _handle_ws_text(self, raw: str) -> None:
        """Parse a ws text message and dispatch monitor frames.

        Only Lunatone-protocol JSON is used (see _parse_ws_json); the web UI's
        ``DALI:...`` text mirror of the same frames is ignored so each bus
        event is delivered exactly once.
        """
        msg = _parse_ws_json(raw)
        if msg is None or msg.get("type") not in _LUNATONE_TYPES:
            return
        # Any Lunatone message (the "info" greeting first) shows the device has
        # registered us as its client and will stream to us.
        self._greeted = True
        self._set_stream_ok(True)
        if msg.get("type") != "daliMonitor":
            return
        # The device sends no daliMonitor at all while "DALI IP Interface" is
        # off, so any one of them (ours included) proves it is on now.
        self._set_ip_interface(True)
        frame = _monitor_frame(msg)
        if frame is None:
            return
        for cb in list(self._monitor_cbs):
            try:
                cb(frame)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Atios monitor listener failed")

    # ---- sending / querying (HTTP, confirmed) -----------------------------

    async def send_frame(self, frame: Frame) -> list[int] | None:
        """Serialize raw services, scenes, lights and queries on one transport."""
        try:
            return await self.transport.send_frame(frame)
        finally:
            if self.rgbw is not None and not frame.wait_for_answer:
                self.rgbw.invalidate()

    async def send_sequence(self, frames: list[Frame]) -> list[list[int] | None]:
        """Run ordered frames, stopping at the first failure."""
        return await self.transport.send_sequence(frames)

    async def _request_frame(self, frame: Frame) -> list[int] | None:
        """One HTTP exchange, called only while the transport lock is held."""
        payload = {
            "repeat_twice": frame.send_twice,
            "wait_response": frame.wait_for_answer,
            "bits": frame.bits,
            "data": list(frame.data),
        }
        url = f"{self._base_url}/api/dali/iface"
        try:
            async with self._session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                resp.raise_for_status()
                body = await resp.json(content_type=None)
        except TimeoutError as err:
            raise DaliError("timeout") from err
        except aiohttp.ClientError as err:
            raise DaliError("http_error") from err
        except (ValueError, TypeError) as err:
            raise DaliError("malformed_response") from err
        return parse_response(body, frame)

    async def async_test_connection(self) -> bool:
        """Config-flow reachability check: gear-present query over HTTP."""
        from .dali import OP_QUERY_CONTROL_GEAR_PRESENT, Target, query

        try:
            result = await self.send_frame(query(Target.short(0), OP_QUERY_CONTROL_GEAR_PRESENT))
        except DaliError:
            return False
        if result is not None:
            return True
        # gear may not answer; treat a live HTTP endpoint as reachable
        try:
            async with self._session.get(
                f"{self._base_url}/api/dali/iface",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return resp.status < 500
        except Exception:  # noqa: BLE001
            return False
