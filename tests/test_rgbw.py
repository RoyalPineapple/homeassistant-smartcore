"""Controller/transport contracts using scripted replies, with no device emulator."""

import asyncio
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock

import pytest

# Same pure-module loading as test_nvram.py; Home Assistant is not required.
_root = Path(__file__).resolve().parent.parent
for _name, _path in (
    ("custom_components", _root / "custom_components"),
    ("custom_components.atios", _root / "custom_components" / "atios"),
):
    if _name not in sys.modules:
        _pkg = types.ModuleType(_name)
        _pkg.__path__ = [str(_path)]
        sys.modules[_name] = _pkg

from custom_components.atios.dali import Frame, Target, goto_scene, level, off, query_actual_level  # noqa: E402
from custom_components.atios.rgbw import RGBWController, RGBWState  # noqa: E402
from custom_components.atios.transport import DaliError, DaliTransport, parse_response  # noqa: E402

T = Target.short(5)
# Literal frames: IEC 62386-209/python-dali reference used by test_dali.py.
READ = [
    [0x0B, 0xA0], [0xC1, 8], [0x0B, 0xF8],
    [0xA3, 15], [0xC1, 8], [0x0B, 0xFA],
    [0xA3, 9], [0xC1, 8], [0x0B, 0xFA],
    [0xA3, 10], [0xC1, 8], [0x0B, 0xFA],
    [0xA3, 11], [0xC1, 8], [0x0B, 0xFA],
    [0xA3, 12], [0xC1, 8], [0x0B, 0xFA],
]
WHITE_200 = [
    [0xA3, 0x80], [0xC1, 8], [0x0B, 0xED],
    [0xA3, 255], [0xC3, 255], [0xC5, 255], [0xC1, 8], [0x0B, 0xEB],
    [0xA3, 199], [0xC3, 255], [0xC5, 255], [0xC1, 8], [0x0B, 0xEC],
    [0xC1, 8], [0x0B, 0xE2], [0x0A, 254],
]


def replies(arc=254, channels=(254, 0, 64, 127)):
    """Actual level, colour status, control and four actual channel answers."""
    return [
        None if arc is None else [arc], None, [0x80], None, None, [0x80],
        *(value for channel in channels
          for value in (None, None, None if channel is None else [channel])),
    ]


def frames(request):
    return [call.args[0].data for call in request.await_args_list]


@pytest.fixture
def controller():
    request = AsyncMock()
    owner = RGBWController(DaliTransport(request))
    owner.states[T] = RGBWState(T)
    owner.members[T] = (T,)
    return owner, request


@pytest.mark.parametrize("reason", [
    "bus_busy", "collision", "timeout", "http_error", "malformed_response", "gateway_rejected",
])
@pytest.mark.parametrize("failed", range(16))
def test_failed_frame_stops_sequence_without_confirming_request(controller, reason, failed):
    async def check():
        owner, request = controller
        request.side_effect = replies() + [None] * failed + [DaliError(reason)]
        with pytest.raises(DaliError, match=reason):
            await owner.set_white(T, 200)
        assert frames(request) == READ + WHITE_200[:failed + 1]
        if failed < 14:  # ACTIVATE is the fifteenth colour frame.
            assert [0x0B, 0xE2] not in frames(request)
        state = owner.state(T)
        assert state.requested is None
        assert state.rgbw == (255, 0, 64, 128)
        assert state.assumed and not state.available
        request.side_effect = None
        request.return_value = None
        await asyncio.wait_for(owner.transport.send_frame(off(T)), 1)
        await owner.close()
    asyncio.run(check())


def test_colour_sequence_excludes_scene_brightness_and_poll(controller):
    async def check():
        owner, request = controller
        entered, release = asyncio.Event(), asyncio.Event()
        answers = iter(replies() + [None] * 18 + replies(channels=(254, 0, 64, 199)))

        async def respond(frame):
            if frame.data == [0xA3, 0x80]:
                entered.set()
                await release.wait()
            return next(answers)

        request.side_effect = respond
        active = asyncio.create_task(owner.set_white(T, 200))
        await asyncio.wait_for(entered.wait(), 1)
        others = [
            asyncio.create_task(owner.transport.send_frame(goto_scene(T, 3))),
            asyncio.create_task(owner.transport.send_frame(level(T, 100))),
            asyncio.create_task(owner.poll(T, force=True)),
        ]
        await asyncio.sleep(0)
        assert frames(request) == READ + WHITE_200[:1]
        release.set()
        await asyncio.wait_for(asyncio.gather(active, *others), 1)
        assert frames(request) == READ + WHITE_200 + [[0x0B, 0x13], [0x0A, 100]] + READ
        await owner.close()
    asyncio.run(check())


@pytest.mark.parametrize("failed_request", [False, True])
def test_cancellation_drains_request_before_unlocking(controller, failed_request):
    async def check():
        owner, request = controller
        entered, release = asyncio.Event(), asyncio.Event()

        async def respond(frame):
            if frame.data == [0xA3, 0x80]:
                entered.set()
                await release.wait()
                if failed_request:
                    raise DaliError("timeout")

        request.side_effect = respond
        async def colour():
            async with owner.transport.transaction() as send:
                await send(Frame([0xA3, 0x80]))
                await send(Frame([0x0B, 0xE2]))

        active = asyncio.create_task(colour())
        await asyncio.wait_for(entered.wait(), 1)
        active.cancel()
        scene = asyncio.create_task(owner.transport.send_frame(goto_scene(T, 3)))
        await asyncio.sleep(0)
        active.cancel()
        await asyncio.sleep(0)
        assert frames(request) == [[0xA3, 0x80]]
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(active, 1)
        await asyncio.wait_for(scene, 1)
        assert frames(request) == [[0xA3, 0x80], [0x0B, 0x13]]
        await owner.close()
    asyncio.run(check())


def test_unload_cancels_pending_edits_and_drains_active_request(controller):
    async def check():
        owner, request = controller
        entered, release = asyncio.Event(), asyncio.Event()
        answers = iter(replies() + [None])

        async def respond(frame):
            if frame.data == [0xA3, 0x80]:
                entered.set()
                await release.wait()
            return next(answers)

        request.side_effect = respond
        active = asyncio.create_task(owner.set_white(T, 200))
        await asyncio.wait_for(entered.wait(), 1)
        pending = asyncio.create_task(owner.turn_off(T))
        await asyncio.sleep(0)
        closing = asyncio.create_task(owner.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await asyncio.wait_for(closing, 1)
        results = await asyncio.gather(active, pending, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert frames(request) == READ + WHITE_200[:1]
        assert not owner._workers and not owner._pending
        request.side_effect = None
        request.return_value = None
        await asyncio.wait_for(owner.transport.send_frame(off(T)), 1)
        await owner.transport.close()
        with pytest.raises(DaliError, match="closed"):
            await owner.transport.send_frame(off(T))
    asyncio.run(check())


def test_white_while_off_restores_without_turning_on_early(controller):
    async def check():
        owner, request = controller
        request.side_effect = replies() + [None] + replies(arc=0) * 2 + [None] * 16
        await owner.poll(T)
        await owner.turn_off(T)
        request.reset_mock()
        await owner.set_white(T, 200)
        state = owner.state(T)
        assert frames(request) == READ
        assert state.rgbw == (255, 0, 64, 200)
        assert not state.is_on and state.pending_white == 200
        request.reset_mock()
        await owner.turn_on(T)
        assert frames(request) == READ + WHITE_200
        assert state.is_on and state.brightness == 255
        assert state.pending_white is None and state.assumed
        await owner.close()
    asyncio.run(check())


def test_rgb_edit_preserves_fresh_white_and_master_brightness(controller):
    async def check():
        owner, request = controller
        request.side_effect = replies(arc=127, channels=(254, 0, 64, 199)) + [None] * 16
        await owner.turn_on(T, rgb=(0, 255, 0))
        assert frames(request)[21:31] == [
            [0xA3, 0], [0xC3, 254], [0xC5, 0], [0xC1, 8], [0x0B, 0xEB],
            [0xA3, 255], [0xC3, 255], [0xC5, 255], [0xC1, 8], [0x0B, 0xEC],
        ]
        assert frames(request)[-1] == [0x0A, 127]
        assert owner.state(T).rgbw == (0, 255, 0, 200)
        assert owner.state(T).brightness == 128
        await owner.close()
    asyncio.run(check())


def test_slider_coalescing_respects_power_order_and_cancelled_waiter(controller):
    async def check():
        owner, request = controller
        entered, release = asyncio.Event(), asyncio.Event()
        answers = iter(replies() + [None] * 16
                       + replies(channels=(254, 0, 64, 199)) + [None] * 17
                       + replies(arc=0, channels=(254, 0, 64, 254)))

        async def respond(frame):
            if frame.data == [0xA3, 0x80]:
                entered.set()
                await release.wait()
            return next(answers)

        request.side_effect = respond
        active = asyncio.create_task(owner.set_white(T, 200))
        await asyncio.wait_for(entered.wait(), 1)
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active
        pending = [
            asyncio.create_task(owner.set_white(T, 100)),
            asyncio.create_task(owner.set_white(T, 255)),
            asyncio.create_task(owner.turn_off(T)),
            asyncio.create_task(owner.set_white(T, 80)),
        ]
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(*pending), 1)
        full_white = WHITE_200[:8] + [[0xA3, 254]] + WHITE_200[9:]
        assert frames(request) == READ + WHITE_200 + READ + full_white + [[0x0B, 0]] + READ
        state = owner.state(T)
        assert not state.is_on
        assert state.pending_white == 80 and state.rgbw == (255, 0, 64, 80)
        await owner.close()
    asyncio.run(check())


@pytest.mark.parametrize("mix", [(255, 0, 0, 0), (0, 0, 0, 0)])
def test_explicit_zero_replaces_pending_white(controller, mix):
    async def check():
        owner, request = controller
        request.side_effect = replies(arc=0) * 2 + [None] * 16
        await owner.set_white(T, 200)
        request.reset_mock()
        await owner.turn_on(T, rgbw=mix, brightness=255)
        assert frames(request)[26] == [0xA3, 0]  # Explicit W=0, never MASK.
        assert frames(request)[-1] == [0x0A, 254 if any(mix) else 0]
        assert owner.state(T).rgbw == mix
        assert owner.state(T).is_on == bool(any(mix))
        assert owner.state(T).pending_white is None
        await owner.close()
    asyncio.run(check())


def test_readback_preserves_compatible_intent_then_imports_external_change(controller):
    async def check():
        owner, request = controller
        request.side_effect = (replies() + [None] * 16
                               + replies(arc=64, channels=(0, 0, 0, 127))
                               + replies(arc=127, channels=(0, 254, 0, 0)))
        await owner.turn_on(T, rgbw=(0, 0, 0, 128), brightness=128)
        assert frames(request)[-1] == [0x0A, 64]
        assert owner.state(T).requested is not None and owner.state(T).assumed
        await owner.poll(T, force=True)
        assert owner.state(T).rgbw == (0, 0, 0, 128)
        assert owner.state(T).brightness == 128
        assert not owner.state(T).assumed
        await owner.poll(T, force=True)
        assert owner.state(T).rgbw == (0, 255, 0, 0)
        assert owner.state(T).brightness == 128
        assert owner.state(T).requested is None
        assert not owner.state(T).assumed
        await owner.close()
    asyncio.run(check())


@pytest.mark.parametrize("white", [0, 255, None])
def test_zero_mask_and_missing_channel_readback_are_distinct(controller, white):
    async def check():
        owner, request = controller
        request.side_effect = replies(channels=(254, 0, 64, white))
        if white == 0:
            await owner.poll(T)
            assert owner.white_level(T) == 0
            assert not owner.state(T).assumed
        else:
            with pytest.raises(DaliError, match="no_colour_response"):
                await owner.poll(T)
            assert owner.white_level(T) is None
            assert owner.state(T).available and owner.state(T).assumed
        await owner.close()
    asyncio.run(check())


@pytest.mark.parametrize("channels,reason", [
    ((64, 0, 0, 0), "incompatible_decomposition_use_full_rgbw"),
    ((254, 0, 64, None), "no_colour_response"),
])
def test_unsafe_partial_edit_requires_complete_tuple(controller, channels, reason):
    async def check():
        owner, request = controller
        request.side_effect = replies(arc=127, channels=channels) * 2 + [None] * 16
        with pytest.raises(DaliError, match=reason):
            await owner.set_white(T, 200)
        assert frames(request) == READ
        assert owner.state(T).available and owner.state(T).assumed
        await owner.turn_on(T, rgbw=(255, 0, 0, 200), brightness=128)
        assert frames(request)[-1] == [0x0A, 127]
        assert owner.state(T).rgbw == (255, 0, 0, 200)
        assert owner.state(T).brightness == 128
        assert owner.state(T).assumed
        await owner.close()
    asyncio.run(check())


@pytest.mark.parametrize("answer", [0, 255, None])
def test_documented_gateway_reply_keeps_zero_mask_and_no_answer_distinct(answer):
    # Shape documented in hub.py; no raw write/no-answer captures are available.
    body = dict(success=True, bus_busy=False, collision_detected=False, data=answer)
    assert parse_response(body, query_actual_level(T)) == (None if answer is None else [answer])


@pytest.mark.parametrize("changes,reason", [
    ({"bus_busy": True}, "bus_busy"),
    ({"collision_detected": True}, "collision"),
    ({"success": False}, "gateway_rejected"),
    ({"success": 1}, "malformed_response"),
    ({"data": True}, "malformed_response"),
    ({"data": [0, 1]}, "malformed_response"),
])
def test_gateway_failures_cannot_be_success(changes, reason):
    body = dict(success=True, bus_busy=False, collision_detected=False, data=0)
    body.update(changes)
    with pytest.raises(DaliError, match=reason):
        parse_response(body, query_actual_level(T))
    if "data" not in changes:
        with pytest.raises(DaliError, match=reason):
            parse_response(body, off(T))


@pytest.mark.parametrize("body", [
    None, [], {},
    dict(success=True, bus_busy=False, collision_detected=False),
    dict(success=True, bus_busy=False, data=0),
    dict(success=True, bus_busy=False, collision_detected=False, data=[]),
    dict(success=True, bus_busy=False, collision_detected=False, data=-1),
    dict(success=True, bus_busy=False, collision_detected=False, data=256),
])
def test_malformed_reply_is_not_a_missing_backward_answer(body):
    with pytest.raises(DaliError, match="malformed_response"):
        parse_response(body, query_actual_level(T))
