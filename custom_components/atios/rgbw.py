"""Shared RGBW state/control, independent of HA and network libraries."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
import logging
import time

from .dali import (
    MASK, OP_DT8_ASSIGNED_COLOUR, OP_DT8_FEATURES, OP_DT8_STATUS, OP_DT8_QUERY_CONTROL,
    RGBW_CONTROL, PRESERVE, Target, byte, channel_from_dali, channel_to_dali,
    dali_level_to_ha_brightness, dt8, dtr, level, off, query_actual_level,
    query_colour, rgbw_frames, rgbw_level,
)
from .nvram import ControlDevice, rgbw_group_members
from .transport import DaliError, DaliTransport

_LOGGER = logging.getLogger(__name__)
_STATE_ERRORS = {
    "unsupported_colour_state", "unsupported_control_state", "no_colour_response",
    "unknown_colour", "unknown_brightness", "incompatible_decomposition_use_full_rgbw",
}


async def _sequence(send, frames):
    result = None
    for frame in frames:
        result = await send(frame)
    return result


async def _value(send, target: Target, selector: int) -> int | None:
    answer = await _sequence(send, query_colour(target, selector))
    return answer[0] if answer is not None and answer[0] != MASK else None


@dataclass
class RGBWSettings:
    """A requested/restorable HA decomposition, not a measured light output."""

    rgbw: tuple[int, int, int, int]
    brightness: int
    is_on: bool


@dataclass(frozen=True)
class RGBWReport:
    """The last complete successful set of actual driver queries."""

    channels: tuple[int, int, int, int]
    arc: int
    control: int
    observed_at: float


@dataclass
class RGBWState:
    """One short address; groups reference these same objects."""

    target: Target
    rgbw: tuple[int, int, int, int] | None = None
    brightness: int | None = None
    is_on: bool | None = None
    assumed: bool = True
    available: bool = True
    pending_white: int | None = None
    reported: RGBWReport | None = None
    requested: RGBWSettings | None = None
    last_usable: RGBWSettings | None = None


class RGBWController:
    """One owner for all RGBW lights and white numbers on one SmartCore."""

    def __init__(self, transport: DaliTransport) -> None:
        self.transport = transport
        self.states: dict[Target, RGBWState] = {}
        self.members: dict[Target, tuple[Target, ...]] = {}
        self._listeners: list[Callable[[], None]] = []
        self._pending: dict[Target, deque[tuple[dict, asyncio.Future, bool]]] = {}
        self._workers: dict[Target, asyncio.Task] = {}
        self._closed = False

    async def close(self) -> None:
        """Unload owns the workers, including callers that have gone away."""
        self._closed = True
        workers = tuple(self._workers.values())
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self._workers.clear()
        for queue in self._pending.values():
            for _, future, _ in queue:
                future.cancel()
        self._pending.clear()

    async def _submit(self, target: Target, *, barrier: bool = False, **patch) -> None:
        """Coalesce slider edits, without crossing an ordered power command.

        Each target admits at most eight pending entries. Consecutive slider
        edits share one patch and Future. On/off entries are barriers: later
        edits cannot overtake them or merge with edits that came before them.
        Cancelling a caller never abandons an accepted sequence halfway through.
        """
        if self._closed:
            raise DaliError("closed")
        if target not in self.members:
            raise ValueError("Target was not verified for RGBW")
        queue = self._pending.setdefault(target, deque())
        if queue and not barrier and not queue[-1][2]:
            pending, future, _ = queue[-1]
        else:
            if len(queue) >= 8:
                raise DaliError("queue_full")
            future = asyncio.get_running_loop().create_future()
            # A caller may cancel after acceptance; still retrieve failures.
            future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            pending = {}
            queue.append((pending, future, barrier))
        if patch.get("rgbw") is not None:
            pending.pop("rgb", None)
            pending.pop("white", None)
        if patch.get("rgb") is not None and pending.get("rgbw") is not None:
            pending["rgbw"] = (*patch.pop("rgb"), pending["rgbw"][3])
        pending.update(patch)
        if target not in self._workers:
            self._workers[target] = asyncio.create_task(self._run_pending(target))
        await asyncio.shield(future)

    async def _run_pending(self, target: Target) -> None:
        try:
            while queue := self._pending.get(target):
                patch, future, _ = queue.popleft()
                if not queue:
                    del self._pending[target]
                try:
                    await self._change(target, **patch)
                except asyncio.CancelledError:
                    future.cancel()
                    raise
                except Exception as err:
                    future.set_exception(err)
                else:
                    future.set_result(None)
        finally:
            self._workers.pop(target, None)

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:
                _LOGGER.exception("RGBW state listener failed")

    def invalidate(self) -> None:
        """A raw write or scene may affect any target; readback will reconcile."""
        for state in self.states.values():
            state.assumed = True
        self._notify()

    def state(self, target: Target) -> RGBWState:
        members = [self.states[t] for t in self.members[target]]
        if len(members) == 1:
            return members[0]
        def common(attr):
            values = [getattr(s, attr) for s in members]
            return values[0] if all(v == values[0] for v in values) else None
        return RGBWState(
            target=target, rgbw=common("rgbw"), brightness=common("brightness"),
            is_on=True if any(s.is_on for s in members) else common("is_on"),
            assumed=True, available=all(s.available for s in members),
        )

    def white_level(self, target: Target) -> int | None:
        """A group may share W even when its members have different RGB."""
        mixes = [self.states[t].rgbw for t in self.members[target]]
        if any(mix is None for mix in mixes):
            return None
        whites = [mix[3] for mix in mixes]
        return whites[0] if all(w == whites[0] for w in whites) else None

    async def discover(self, devices: list[ControlDevice], *, complete: bool) -> None:
        """Verify four assigned RGBW outputs; cached NVRAM alone is insufficient.

        No mode/configuration writes. A failed candidate stays brightness-only
        until reload. Groups never issue backward queries to a group address.
        """
        for dev in devices:
            if dev.is_group or not dev.dt8_candidate:
                continue
            target = dev.target
            try:
                async with self.transport.transaction() as send:
                    answer = await _sequence(send, dt8(target, OP_DT8_FEATURES, read=True))
                    if answer is None or answer[0] == MASK or answer[0] >> 5 != 4:
                        continue
                    assigned = []
                    for channel in range(4):
                        answer = await _sequence(send, [dtr(0, channel), *dt8(
                            target, OP_DT8_ASSIGNED_COLOUR, read=True)])
                        assigned.append(answer[0] if answer else None)
                    if assigned != [1, 2, 3, 4]:
                        continue
                    answer = await _sequence(send, dt8(target, OP_DT8_QUERY_CONTROL, read=True))
                    control = answer[0] if answer is not None and answer[0] != MASK else None
                    # Legacy control formats only; reserved/extended encodings
                    # require an explicit implementation, never a guessed mode.
                    if control is None or control & 0xC0 == 0xC0:
                        continue
            except DaliError as err:
                _LOGGER.warning("RGBW capability check %s failed: %s", target.unique_suffix, err.reason)
                continue
            self.states[target] = RGBWState(target)
            self.members[target] = (target,)
        self.members.update(rgbw_group_members(devices, set(self.states), complete=complete))

    async def _read(self, send, state: RGBWState) -> None:
        """Read actual values; don't manufacture zero on missing/MASK replies."""
        answer = await send(query_actual_level(state.target))
        if answer is None or answer[0] == MASK:
            raise DaliError("no_level_response")
        arc = answer[0]
        state.is_on = arc > 0
        answer = await _sequence(send, dt8(state.target, OP_DT8_STATUS, read=True))
        if answer is None or answer[0] & 0xF0 != 0x80:
            state.rgbw = None
            raise DaliError("unsupported_colour_state")
        control = await _value(send, state.target, 15)
        if control != RGBW_CONTROL:
            state.rgbw = None
            raise DaliError("unsupported_control_state")
        channels = [await _value(send, state.target, n) for n in range(9, 13)]
        if any(v is None for v in channels):
            state.rgbw = None
            raise DaliError("no_colour_response")
        peak = max(channels)
        state.reported = RGBWReport(tuple(channels), arc, control, time.monotonic())
        previous = state.requested or state.last_usable
        expected_channels = tuple(channel_to_dali(v) for v in previous.rgbw) if previous else None
        # Preserve the exact caller decomposition only when the report agrees,
        # including the effective master level. Off retains the last brightness.
        compatible = previous is not None and expected_channels == tuple(channels)
        if compatible:
            expected_arc = rgbw_level(previous.rgbw, previous.brightness) if previous.is_on else 0
            compatible = arc == expected_arc
        if previous is not None and arc == 0 and state.pending_white is not None:
            compatible = expected_channels[:3] == tuple(channels[:3])
        if compatible:
            state.rgbw, state.brightness = previous.rgbw, previous.brightness
        else:
            # External intent cannot be recovered. Prefer raw channel levels
            # with an inferred master; if that would exceed 255, use a full
            # scale mix and the actual master. Both are deterministic forms.
            if peak and arc <= peak:
                state.rgbw = tuple(channel_from_dali(v) for v in channels)
                state.brightness = (dali_level_to_ha_brightness(round(arc * 254 / peak))
                                    if arc else previous.brightness if previous else 255)
            else:
                state.rgbw = tuple(channel_from_dali(round(v * 254 / peak)) for v in channels) if peak else (0, 0, 0, 0)
                state.brightness = (dali_level_to_ha_brightness(arc) if arc else
                                    previous.brightness if previous else 255)
            state.requested = None
        state.is_on = arc > 0 and peak > 0
        state.assumed = False
        state.available = True
        if state.pending_white is not None and not state.is_on:
            state.rgbw = (*state.rgbw[:3], state.pending_white)
            state.assumed = True
        else:
            state.requested = None
            state.pending_white = None
        state.last_usable = RGBWSettings(state.rgbw, state.brightness, state.is_on)

    def _invalidate(self, targets: tuple[Target, ...], *, unavailable: bool = True) -> None:
        for target in targets:
            self.states[target].available = not unavailable
            self.states[target].assumed = True

    async def poll(self, target: Target, *, force: bool = False) -> None:
        if self._closed:
            raise DaliError("closed")
        targets = self.members[target]
        try:
            async with self.transport.transaction() as send:
                for member in targets:
                    state = self.states[member]
                    # Group and individual entities share the same refresh.
                    # Commands always read freshly, independent of this limit.
                    if not force and not state.assumed and state.available and state.reported is not None and time.monotonic() - state.reported.observed_at < 2:
                        continue
                    await self._read(send, state)
        except DaliError as err:
            self._invalidate(targets, unavailable=err.reason not in _STATE_ERRORS)
            raise
        except asyncio.CancelledError:
            self._invalidate(targets)
            raise
        finally:
            self._notify()

    async def turn_on(
        self, target: Target, *, brightness: int | None = None,
        rgbw: tuple[int, int, int, int] | None = None,
        rgb: tuple[int, int, int] | None = None,
    ) -> None:
        if brightness is not None:
            byte(brightness)
        for values, count in ((rgbw, 4), (rgb, 3)):
            if values is not None:
                if len(values) != count:
                    raise ValueError("Invalid RGB/RGBW channel count")
                for value in values:
                    byte(value)
        if rgbw is not None and rgb is not None:
            raise ValueError("Specify RGB or RGBW, not both")
        if brightness is None and rgbw is None and rgb is None:
            await self._submit(target, barrier=True, turn_on=True)
        else:
            patch = {key: value for key, value in dict(brightness=brightness, rgbw=rgbw, rgb=rgb).items() if value is not None}
            await self._submit(target, **patch, turn_on=True)

    async def set_white(self, target: Target, value: int) -> None:
        await self._submit(target, white=byte(value))

    async def turn_off(self, target: Target) -> None:
        await self._submit(target, barrier=True, turn_off=True)

    async def _change(
        self, target: Target, *, brightness=None, rgbw=None, rgb=None,
        white=None, turn_off=False, turn_on=False,
    ) -> None:
        if self._closed:
            raise DaliError("closed")
        targets = self.members[target]
        try:
            async with self.transport.transaction() as send:
                # Read every member before writing any. Partial changes must
                # preserve today's physical mix, not yesterday's HA cache.
                if not turn_off:
                    for member in targets:
                        state = self.states[member]
                        try:
                            await self._read(send, state)
                        except DaliError as err:
                            # A full tuple can establish RGBW from another
                            # representation. Partial edits must fail safely.
                            if rgbw is None or err.reason not in (
                                "unsupported_colour_state", "unsupported_control_state", "no_colour_response"
                            ):
                                raise
                            if brightness is None:
                                raise DaliError("unknown_brightness") from err
                for member in targets:
                    state = self.states[member]
                    if turn_off:
                        await send(off(member))
                        state.is_on = False
                        state.assumed = True
                        state.available = True
                        if state.rgbw is not None:
                            state.requested = RGBWSettings(state.rgbw, state.brightness or 255, False)
                            state.last_usable = state.requested
                        continue
                    mix = tuple(rgbw) if rgbw is not None else state.rgbw
                    if mix is None:
                        raise DaliError("unknown_colour")
                    intents = list(mix) if rgbw is not None else [PRESERVE] * 4
                    if rgb is not None:
                        mix = (*rgb, mix[3])
                        intents[:3] = rgb
                    if state.pending_white is not None and turn_on and rgbw is None:
                        intents[3] = state.pending_white
                    if white is not None:
                        mix = (*mix[:3], white)
                        intents[3] = white
                        if not state.is_on and not turn_on:
                            # Editing white while off is a pending preference,
                            # never an ACTIVATE that could light the room.
                            state.pending_white = white
                            state.rgbw = mix
                            state.assumed = True
                            state.requested = RGBWSettings(mix, state.brightness or 255, False)
                            state.last_usable = state.requested
                            continue
                    bri = brightness if brightness is not None else state.brightness or 255
                    if (any(v is not PRESERVE for v in intents) and state.reported is not None
                            and any(intent is PRESERVE and channel_to_dali(mix[i]) != state.reported.channels[i]
                                    for i, intent in enumerate(intents))):
                        # An externally rescaled mix cannot be combined with
                        # literal MASK and full-range explicit channels safely.
                        raise DaliError("incompatible_decomposition_use_full_rgbw")
                    await _sequence(send, rgbw_frames(member, tuple(intents)))
                    await send(level(member, rgbw_level(mix, bri)))
                    state.rgbw = mix
                    state.brightness = bri
                    state.is_on = bri > 0 and any(mix)
                    state.pending_white = None
                    state.assumed = True
                    state.available = True
                    state.requested = RGBWSettings(mix, bri, state.is_on)
                    state.last_usable = state.requested
        except DaliError as err:
            self._invalidate(targets, unavailable=err.reason not in _STATE_ERRORS)
            raise
        except asyncio.CancelledError:
            self._invalidate(targets)
            raise
        finally:
            self._notify()
