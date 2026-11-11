"""Software contracts only: independent golden frames and a simulated gateway."""

import asyncio
import unittest

import pure  # noqa: F401
from _atios_pure import dali as d
from _atios_pure.nvram import parse_control_devices, rgbw_group_members
from _atios_pure.rgbw import RGBWController
from _atios_pure.transport import DaliError, DaliTransport, parse_response

T = d.Target.short(5)


class ProtocolTests(unittest.TestCase):
    def test_independent_golden_rgbw(self):
        # Part 209 opcodes and registers, independently checked against
        # python-dali b1881c008aca0b269fd09b8834dd9bdc0f1a735b.
        frames = d.rgbw_frames(T, (255, 0, 64, 128))
        self.assertEqual([f.data for f in frames], [
            [0xA3, 0x80], [0xC1, 8], [0x0B, 0xED],
            [0xA3, 254], [0xC3, 0], [0xC5, 64], [0xC1, 8], [0x0B, 0xEB],
            [0xA3, 127], [0xC3, 255], [0xC5, 255], [0xC1, 8], [0x0B, 0xEC],
            [0xC1, 8], [0x0B, 0xE2],
        ])
        self.assertTrue(all(not f.send_twice and not f.wait_for_answer for f in frames))

    def test_group_and_broadcast_encoding(self):
        self.assertEqual(d.dt8(d.Target.group(0), 0xEC)[1].data, [0x81, 0xEC])
        self.assertEqual(d.dt8(d.Target.broadcast(), 0xEB)[1].data, [0xFF, 0xEB])
        self.assertEqual(d.query_colour(T, 12)[-1].data, [0x0B, 0xFA])
        self.assertEqual([f.data for f in d.query_colour(T, 12)][:2], [[0xA3, 12], [0xC1, 8]])
        self.assertTrue(d.query_colour(T, 12)[-1].wait_for_answer)

    def test_preserve_is_separate_from_explicit_zero_and_maximum(self):
        # Independent expected DTR bytes: preserved RGB, explicit W off/full.
        for white, expected in ((0, 0), (255, 254)):
            frames = d.rgbw_frames(T, (d.PRESERVE, d.PRESERVE, d.PRESERVE, white))
            self.assertEqual([f.data for f in frames][3:8], [
                [0xA3, 255], [0xC3, 255], [0xC5, 255], [0xC1, 8], [11, 0xEB]])
            self.assertEqual(frames[8].data, [0xA3, expected])
        frames = d.rgbw_frames(T, (255, 0, 128, d.PRESERVE))
        self.assertEqual([f.data for f in frames][3:6], [[0xA3, 254], [0xC3, 0], [0xC5, 127]])
        self.assertEqual([f.data for f in frames][8:11], [[0xA3, 255], [0xC3, 255], [0xC5, 255]])

    def test_scaling_mask_and_normalisation_compensation(self):
        self.assertEqual([d.channel_to_dali(v) for v in (0, 128, 255)], [0, 127, 254])
        self.assertEqual(d.channel_from_dali(0), 0)
        self.assertEqual(d.channel_from_dali(254), 255)
        self.assertIsNone(d.channel_from_dali(255))
        self.assertTrue(all(d.channel_to_dali(v) < 255 for v in range(256)))
        self.assertEqual(d.rgbw_level((0, 0, 0, 128), 255), 127)
        self.assertEqual(d.rgbw_level((255, 0, 0, 128), 128), 127)
        self.assertEqual(d.rgbw_level((0, 0, 0, 0), 255), 0)
        for value in (-1, 256, 1.5, True):
            with self.assertRaises(ValueError):
                d.channel_to_dali(value)

    def test_validation(self):
        for data, bits in (([1], 16), ([1, 2, 3], 16), ([True, 0], 16)):
            with self.assertRaises(ValueError):
                d.Frame(data, bits=bits)
        with self.assertRaises(ValueError):
            d.query_colour(T, 2)  # a 16-bit selector needs different readback
        with self.assertRaises(ValueError):
            d.rgbw_frames(T, (1, 2, 3))


def device(address=5, *, features=128, types=(6, 8), selected=8, groups=None):
    return dict(address=address, device_types=list(types), device_type_selected=selected,
                color_type_features=features, gear_groups=groups if groups is not None else [0])


class CapabilityTests(unittest.TestCase):
    def test_metadata_count_and_fallback(self):
        devices = parse_control_devices([
            device(features=109), device(6, features=128), device(7, features=None),
            device(8, features=255), device(9, selected=6),
        ])
        self.assertEqual([x.rgbw_channels for x in devices], [3, 4, 0, 0, 4])
        self.assertFalse(devices[-1].dt8_candidate)
        self.assertEqual(devices[1].color_type_features, 128)

    def test_mixed_unknown_and_empty_groups(self):
        group = dict(address=128, is_group=True)
        devices = parse_control_devices([group, device(), device(6, selected=6)])
        self.assertEqual(rgbw_group_members(devices, {T}, complete=True), {})
        devices = parse_control_devices([group, device()])
        self.assertEqual(rgbw_group_members(devices, {T}, complete=True), {d.Target.group(0): (T,)})
        self.assertEqual(rgbw_group_members(devices, {T}, complete=False), {})
        self.assertEqual(rgbw_group_members(parse_control_devices([group]), {T}, complete=True), {})
        unknown = device(6)
        del unknown['gear_groups']
        self.assertEqual(rgbw_group_members(parse_control_devices([group, device(), unknown]), {T}, complete=True), {})


class Gateway:
    """A small simulated protocol peer, not an alternative encoder or HA fake."""

    def __init__(self):
        self.frames = []
        self.registers = [0, 0, 0]
        self.channels = {5: [254, 0, 64, 127], 6: [0, 254, 0, 64]}
        self.staged = {}
        self.arc = {5: 254, 6: 254}
        self.control = 0x80
        self.status = 0x80
        self.features = 128
        self.assigned = [1, 2, 3, 4]
        self.fail_at = None
        self.missing_selector = None
        self.mask_selector = None

    async def request(self, frame):
        self.frames.append(list(frame.data))
        if len(self.frames) == self.fail_at:
            raise DaliError('bus_busy')
        first, second = frame.data
        if first in (0xA3, 0xC3, 0xC5):
            self.registers[(0xA3, 0xC3, 0xC5).index(first)] = second
        elif first == 0xC1:
            pass
        else:
            addr = first >> 1
            if frame.wait_for_answer:
                if second == 0xA0:
                    return [self.arc[addr]]
                if second == 0xF9:
                    return [self.features] if self.features is not None else None
                if second == 0xFC:
                    return [self.assigned[self.registers[0]]]
                if second == 0xF8:
                    return [self.status]
                if second == 0xFB:
                    return [self.control]
                if second == 0xFA:
                    selector = self.registers[0]
                    if selector == self.missing_selector:
                        return None
                    if selector == self.mask_selector:
                        return [255]
                    if selector == 15:
                        return [self.control]
                    return [self.channels[addr][selector - 9]]
                raise AssertionError(f'Unexpected query {frame.data}')
            if first & 1 == 0:
                self.arc[addr] = second
            elif second == 0xEB:
                self.staged.setdefault(addr, list(self.channels[addr]))[:3] = self.registers
            elif second == 0xEC:
                self.staged.setdefault(addr, list(self.channels[addr]))[3] = self.registers[0]
            elif second == 0xE2:
                staged = self.staged.pop(addr)
                self.channels[addr] = [old if value == 255 else value
                                       for old, value in zip(self.channels[addr], staged)]
                self.arc[addr] = max(self.channels[addr])
            elif second == 0:
                self.arc[addr] = 0
        return None


class StateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = Gateway()
        self.transport = DaliTransport(self.gateway.request)
        self.controller = RGBWController(self.transport)
        await self.controller.discover(parse_control_devices([device()]), complete=True)
        self.gateway.frames.clear()

    async def asyncTearDown(self):
        await self.controller.close()

    async def test_white_preserves_rgb_and_rgb_preserves_white(self):
        observed = []
        remove = self.controller.subscribe(lambda: observed.append(self.controller.state(T).rgbw))
        await self.controller.set_white(T, 200)
        self.assertEqual(self.gateway.channels[5], [254, 0, 64, 199])
        rgb_index = self.gateway.frames.index([11, 0xEB])
        self.assertEqual(self.gateway.frames[rgb_index-4:rgb_index-1], [[0xA3,255], [0xC3,255], [0xC5,255]])
        self.assertEqual(self.controller.state(T).rgbw[3], 200)
        self.gateway.frames.clear()
        await self.controller.turn_on(T, rgb=(0, 255, 0))
        self.assertEqual(self.gateway.channels[5], [0, 254, 0, 199])
        waf_index = self.gateway.frames.index([11, 0xEC])
        self.assertEqual(self.gateway.frames[waf_index-4], [0xA3,255])
        self.assertEqual(observed[-1], (0, 255, 0, 200))
        remove()
        self.assertEqual(len(self.controller._listeners), 0)

    async def test_brightness_preserves_mix_and_all_zero(self):
        await self.controller.turn_on(T, brightness=128)
        self.assertEqual(self.gateway.channels[5], [254, 0, 64, 127])
        self.assertEqual(self.gateway.arc[5], 127)
        await self.controller.turn_on(T, rgbw=(0, 0, 0, 0))
        self.assertEqual(self.gateway.channels[5], [0, 0, 0, 0])
        self.assertFalse(self.controller.state(T).is_on)

    async def test_white_zero_does_not_turn_rgb_off(self):
        await self.controller.set_white(T, 0)
        self.assertEqual(self.gateway.channels[5], [254, 0, 64, 0])
        self.assertTrue(self.controller.state(T).is_on)

    async def test_white_while_off_and_plain_on_restore(self):
        await self.controller.turn_on(T, brightness=128)
        await self.controller.turn_off(T)
        self.gateway.frames.clear()
        await self.controller.set_white(T, 220)
        self.assertFalse(self.controller.state(T).is_on)
        self.assertEqual(self.gateway.arc[5], 0)
        self.assertNotIn([11, 0xE2], self.gateway.frames)
        self.assertEqual(self.controller.state(T).rgbw[3], 220)
        await self.controller.turn_on(T)
        self.assertEqual(self.gateway.channels[5], [254, 0, 64, 219])
        self.assertEqual(self.gateway.arc[5], 127)

    async def test_explicit_rgbw_white_overrides_off_state_pending_white(self):
        for white, expected in ((0, 0), (255, 254)):
            with self.subTest(white=white):
                await self.controller.turn_off(T)
                await self.controller.set_white(T, 220)
                await self.controller.turn_on(T, rgbw=(255, 0, 0, white), brightness=255)
                self.assertEqual(self.gateway.channels[5], [254, 0, 0, expected])
                self.assertEqual(self.controller.state(T).rgbw, (255, 0, 0, white))
                self.assertIsNone(self.controller.state(T).pending_white)
                await self.controller.poll(T, force=True)
                self.assertEqual(self.controller.state(T).rgbw, (255, 0, 0, white))

    async def test_external_change_and_readback_zero_mask_missing(self):
        await self.controller.poll(T)
        self.assertFalse(self.controller.state(T).assumed)
        self.gateway.channels[5] = [0, 254, 0, 0]
        await self.controller.poll(T, force=True)
        self.assertEqual(self.controller.state(T).rgbw, (0, 255, 0, 0))
        for attr in ('mask_selector', 'missing_selector'):
            setattr(self.gateway, attr, 12)
            with self.assertRaises(DaliError):
                await self.controller.poll(T, force=True)
            self.assertIsNone(self.controller.state(T).rgbw)
            self.assertTrue(self.controller.state(T).available)  # gear replied; colour is unknown
            setattr(self.gateway, attr, None)
        await self.controller.poll(T)
        self.assertTrue(self.controller.state(T).available)

    async def test_unknown_colour_blocks_partial_but_full_tuple_establishes(self):
        self.gateway.status = 0x20
        with self.assertRaises(DaliError):
            await self.controller.set_white(T, 100)
        self.assertNotIn([11, 0xE2], self.gateway.frames)
        await self.controller.turn_on(T, rgbw=(255, 0, 0, 128), brightness=255)
        self.assertEqual(self.controller.state(T).rgbw, (255, 0, 0, 128))
        self.assertTrue(self.controller.state(T).assumed)

    async def test_stale_temporary_values_are_replaced_by_preserve_masks(self):
        self.gateway.staged[5] = [0, 254, 0, 254]
        await self.controller.set_white(T, 0)
        self.assertEqual(self.gateway.channels[5], [254, 0, 64, 0])

    async def test_external_rescaling_fails_partial_edit_when_mask_cannot_preserve_mix(self):
        self.gateway.channels[5] = [64, 0, 0, 0]
        self.gateway.arc[5] = 127
        with self.assertRaises(DaliError) as caught:
            await self.controller.set_white(T, 200)
        self.assertEqual(caught.exception.reason, 'incompatible_decomposition_use_full_rgbw')
        self.assertNotIn([11, 0xE2], self.gateway.frames)
        await self.controller.turn_on(T, rgbw=(255, 0, 0, 200), brightness=128)
        self.assertEqual(self.gateway.channels[5], [254, 0, 0, 199])

    async def test_partial_failure_never_confirms_requested_state(self):
        await self.controller.poll(T)
        self.gateway.frames.clear()
        self.gateway.fail_at = 20  # after readback, during staging
        previous = self.controller.state(T).rgbw
        with self.assertRaises(DaliError):
            await self.controller.set_white(T, 240)
        self.assertNotIn([11, 0xE2], self.gateway.frames)
        self.assertEqual(self.controller.state(T).rgbw, previous)
        self.assertFalse(self.controller.state(T).available)
        self.assertTrue(self.controller.state(T).assumed)

    async def test_groups_use_shared_members_and_preserve_different_rgb(self):
        await self.controller.discover(parse_control_devices([
            dict(address=128, is_group=True), device(), device(6)
        ]), complete=True)
        group = d.Target.group(0)
        await self.controller.set_white(group, 150)
        self.assertEqual(self.gateway.channels[5], [254, 0, 64, 149])
        self.assertEqual(self.gateway.channels[6], [0, 254, 0, 149])
        self.assertIsNone(self.controller.state(group).rgbw)
        self.assertEqual(self.controller.state(T).rgbw[3], 150)
        self.assertEqual(self.controller.white_level(group), 150)
        self.assertTrue(self.controller.state(group).assumed)
        self.assertFalse(any(f[0] in (0x81, 0xFF) for f in self.gateway.frames))

    async def test_capability_runtime_overrides_stale_cache(self):
        for features, assigned in ((2, [1,2,3,4]), (96, [1,2,3,4]), (128, [1,2,3,0]), (None, [1,2,3,4])):
            self.gateway.features = features
            self.gateway.assigned = assigned
            controller = RGBWController(self.transport)
            await controller.discover(parse_control_devices([device()]), complete=True)
            self.assertEqual(controller.members, {})
        self.gateway.features, self.gateway.assigned = 128, [1,2,3,4]
        controller = RGBWController(self.transport)
        await controller.discover(parse_control_devices([device(features=None)]), complete=True)
        self.assertIn(T, controller.members)

    async def test_concurrent_changes_and_poll_preserve_both_channels(self):
        await asyncio.gather(self.controller.set_white(T, 180),
                             self.controller.turn_on(T, rgb=(0, 255, 64)),
                             self.controller.poll(T))
        self.assertEqual(self.gateway.channels[5], [0, 254, 64, 179])

    async def test_report_request_and_external_canonical_decomposition(self):
        self.assertIsNone(self.controller.state(T).reported)
        self.assertIsNone(self.controller.state(T).rgbw)
        await self.controller.turn_on(T, rgbw=(0, 0, 0, 128), brightness=255)
        state = self.controller.state(T)
        self.assertEqual(state.requested.rgbw, (0, 0, 0, 128))
        self.assertEqual(state.reported.channels, (254, 0, 64, 127))  # prior query, not requested state
        self.assertEqual(state.confirmation, 'assumed')
        await self.controller.poll(T, force=True)
        self.assertEqual((state.rgbw, state.brightness), ((0, 0, 0, 128), 255))
        self.assertEqual(state.confirmation, 'confirmed')
        self.gateway.channels[5] = [0, 0, 0, 64]
        self.gateway.arc[5] = 127
        await self.controller.poll(T, force=True)
        self.assertEqual((state.rgbw, state.brightness), ((0, 0, 0, 255), 128))
        self.assertIsNone(state.requested)
        self.assertEqual(state.reported.channels, (0, 0, 0, 64))

    async def test_group_and_short_poll_share_one_physical_refresh(self):
        await self.controller.discover(parse_control_devices([
            dict(address=128, is_group=True), device()
        ]), complete=True)
        await self.controller.poll(d.Target.group(0))
        self.gateway.frames.clear()
        await self.controller.poll(T)
        self.assertEqual(self.gateway.frames, [])

    async def test_rapid_slider_changes_keep_only_latest_pending_patch(self):
        staging, release = asyncio.Event(), asyncio.Event()
        original = self.gateway.request
        first = True
        async def request(frame):
            nonlocal first
            if frame.data == [0xA3, 0x80] and first:
                first = False
                staging.set()
                await release.wait()
            return await original(frame)
        self.transport._request = request
        active = asyncio.create_task(self.controller.set_white(T, 10))
        await staging.wait()
        sliders = [asyncio.create_task(self.controller.set_white(T, value)) for value in range(11, 256)]
        rgb = asyncio.create_task(self.controller.turn_on(T, rgb=(0, 255, 64)))
        await asyncio.sleep(0)
        self.assertEqual(len(self.controller._pending), 1)
        self.assertEqual(len(self.controller._workers), 1)
        release.set()
        await asyncio.gather(active, *sliders, rgb)
        self.assertEqual(self.gateway.channels[5], [0, 254, 64, 254])
        self.assertEqual(self.gateway.frames.count([11, 0xE2]), 2)
        self.assertEqual(self.controller._pending, {})

    async def test_power_commands_keep_order_during_slider_transaction(self):
        staging, release = asyncio.Event(), asyncio.Event()
        original = self.gateway.request
        first = True
        async def request(frame):
            nonlocal first
            if frame.data == [0xA3, 0x80] and first:
                first = False
                staging.set()
                await release.wait()
            return await original(frame)
        self.transport._request = request
        active = asyncio.create_task(self.controller.set_white(T, 10))
        await staging.wait()
        off = asyncio.create_task(self.controller.turn_off(T))
        await asyncio.sleep(0)
        on = asyncio.create_task(self.controller.turn_on(T, brightness=255))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(active, off, on)
        self.assertGreater(self.gateway.arc[5], 0)
        self.assertTrue(self.controller.state(T).is_on)
        off_index = self.gateway.frames.index([11, 0])
        self.assertIn([11, 0xE2], self.gateway.frames[off_index + 1:])

    async def test_white_edits_do_not_coalesce_across_off(self):
        staging, release = asyncio.Event(), asyncio.Event()
        original = self.gateway.request
        first = True
        async def request(frame):
            nonlocal first
            if frame.data == [0xA3, 0x80] and first:
                first = False
                staging.set()
                await release.wait()
            return await original(frame)
        self.transport._request = request
        active = asyncio.create_task(self.controller.set_white(T, 10))
        await staging.wait()
        before_off = asyncio.create_task(self.controller.set_white(T, 100))
        await asyncio.sleep(0)
        off = asyncio.create_task(self.controller.turn_off(T))
        await asyncio.sleep(0)
        after_off = asyncio.create_task(self.controller.set_white(T, 200))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(active, before_off, off, after_off)
        self.assertEqual(self.gateway.channels[5][3], 100)
        self.assertEqual(self.gateway.arc[5], 0)
        self.assertEqual(self.controller.state(T).pending_white, 200)
        self.assertEqual(self.controller.white_level(T), 200)
        off_index = self.gateway.frames.index([11, 0])
        self.assertNotIn([11, 0xE2], self.gateway.frames[off_index + 1:])

    async def test_pending_power_commands_are_bounded(self):
        staging, release = asyncio.Event(), asyncio.Event()
        original = self.gateway.request
        async def request(frame):
            if frame.data == [0xA3, 0x80]:
                staging.set()
                await release.wait()
            return await original(frame)
        self.transport._request = request
        active = asyncio.create_task(self.controller.set_white(T, 10))
        await staging.wait()
        queued = [asyncio.create_task(self.controller.turn_off(T)) for _ in range(8)]
        await asyncio.sleep(0)
        with self.assertRaises(DaliError) as raised:
            await self.controller.turn_off(T)
        self.assertEqual(raised.exception.reason, 'queue_full')
        release.set()
        await asyncio.gather(active, *queued)
        self.assertEqual(self.gateway.frames.count([11, 0]), 8)
        self.assertFalse(self.controller.state(T).is_on)
        self.assertEqual(self.controller._pending, {})

    async def test_unload_cancels_queued_power_commands_and_edits(self):
        staging, release = asyncio.Event(), asyncio.Event()
        original = self.gateway.request
        async def request(frame):
            if frame.data == [0xA3, 0x80]:
                staging.set()
                await release.wait()
            return await original(frame)
        self.transport._request = request
        active = asyncio.create_task(self.controller.set_white(T, 10))
        await staging.wait()
        queued = [asyncio.create_task(action) for action in (
            self.controller.turn_off(T), self.controller.turn_on(T),
            self.controller.set_white(T, 200),
        )]
        await asyncio.sleep(0)
        closing = asyncio.create_task(self.controller.close())
        await asyncio.sleep(0)
        release.set()
        await closing
        results = await asyncio.gather(active, *queued, return_exceptions=True)
        self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in results))
        self.assertNotIn([11, 0xE2], self.gateway.frames)
        self.assertNotIn([11, 0], self.gateway.frames)
        self.assertEqual(self.controller._workers, {})
        self.assertEqual(self.controller._pending, {})

    async def test_full_tuple_replaces_pending_white_and_later_white_wins(self):
        # Submissions coalesce before their worker starts.
        await asyncio.gather(
            self.controller.set_white(T, 200),
            self.controller.turn_on(T, rgbw=(0, 255, 0, 0)),
        )
        self.assertEqual(self.gateway.channels[5], [0, 254, 0, 0])
        await asyncio.gather(
            self.controller.turn_on(T, rgbw=(255, 0, 0, 0)),
            self.controller.set_white(T, 220),
        )
        self.assertEqual(self.gateway.channels[5], [254, 0, 0, 219])

    async def test_cancelled_slider_waiter_does_not_cancel_accepted_sequence(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.gateway.request
        async def request(frame):
            if frame.data == [0xA3, 0x80]:
                entered.set()
                await release.wait()
            return await original(frame)
        self.transport._request = request
        task = asyncio.create_task(self.controller.set_white(T, 200))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        release.set()
        await self.controller._flush(T)
        self.assertEqual(self.gateway.channels[5], [254, 0, 64, 199])

    async def test_unload_cancels_workers_without_activating_and_releases_lock(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.gateway.request
        async def request(frame):
            if frame.data == [0xA3, 0x80]:
                entered.set()
                await release.wait()
            return await original(frame)
        self.transport._request = request
        task = asyncio.create_task(self.controller.set_white(T, 200))
        await entered.wait()
        closing = asyncio.create_task(self.controller.close())
        await asyncio.sleep(0)
        release.set()
        await closing
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn([11, 0xE2], self.gateway.frames)
        await self.transport.send_frame(d.off(T))
        self.assertEqual(self.controller._workers, {})


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def test_response_contract(self):
        query = d.query_actual_level(T)
        good = dict(success=True, bus_busy=False, collision_detected=False)
        self.assertEqual(parse_response(dict(good, data=0), query), [0])
        self.assertEqual(parse_response(dict(good, data=255), query), [255])
        self.assertIsNone(parse_response(dict(good, data=None), query))
        self.assertIsNone(parse_response(good, d.off(T)))
        for body in (None, [], {}, good, dict(good, data=True), dict(good, data=-1),
                     dict(good, data=[]), dict(good, data=[1,2]), dict(good, success=1)):
            with self.assertRaises(DaliError):
                parse_response(body, query)
        for change, reason in ((dict(bus_busy=True), 'bus_busy'),
                               (dict(collision_detected=True), 'collision'),
                               (dict(success=False), 'gateway_rejected')):
            with self.assertRaises(DaliError) as raised:
                parse_response(dict(good, **change), d.off(T))
            self.assertEqual(raised.exception.reason, reason)

    async def test_transactions_do_not_interleave_with_raw_or_query(self):
        seen = []
        async def request(frame):
            seen.append(frame.data)
            await asyncio.sleep(0)
        transport = DaliTransport(request)
        await asyncio.gather(transport.send_sequence(d.rgbw_frames(T, (255,0,0,128))),
                             transport.send_frame(d.goto_scene(T, 3)),
                             transport.send_frame(d.query_actual_level(T)))
        self.assertEqual(seen[-2:], [[11, 0x13], [11, 0xA0]])
        self.assertEqual(seen[14], [11, 0xE2])

    async def test_failure_stops_activation_and_releases_lock(self):
        for fail in range(1, 15):
            gateway = Gateway()
            gateway.fail_at = fail
            transport = DaliTransport(gateway.request)
            with self.assertRaises(DaliError):
                await transport.send_sequence(d.rgbw_frames(T, (255,0,0,128)))
            self.assertNotIn([11, 0xE2], gateway.frames)
            gateway.fail_at = None
            await transport.send_frame(d.off(T))

    async def test_cancel_waits_for_inflight_and_sends_no_activate(self):
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []
        async def request(frame):
            seen.append(frame.data)
            if len(seen) == 1:
                entered.set()
                await release.wait()
        transport = DaliTransport(request)
        task = asyncio.create_task(transport.send_sequence(d.rgbw_frames(T, (255,0,0,128))))
        await entered.wait()
        task.cancel()
        other = asyncio.create_task(transport.send_frame(d.off(T)))
        await asyncio.sleep(0)
        self.assertEqual(len(seen), 1)
        task.cancel()  # repeated cancellation still must not release early
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await other
        self.assertEqual(seen, [[0xA3, 0x80], [11, 0]])

    async def test_bounded_admission_and_close(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def request(frame):
            entered.set()
            await release.wait()
        transport = DaliTransport(request)
        tasks = [asyncio.create_task(transport.send_frame(d.off(T))) for _ in range(8)]
        await entered.wait()
        with self.assertRaises(DaliError) as raised:
            await transport.send_frame(d.off(T))
        self.assertEqual(raised.exception.reason, 'queue_full')
        for task in tasks[1:]:
            task.cancel()
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await transport.close()
        with self.assertRaises(DaliError) as raised:
            await transport.send_frame(d.off(T))
        self.assertEqual(raised.exception.reason, 'closed')

    async def test_timeout_and_exception_release_lock(self):
        for error in (DaliError('timeout'), DaliError('http_error'), DaliError('malformed_response')):
            async def request(frame):
                raise error
            transport = DaliTransport(request)
            for _ in range(2):
                with self.assertRaises(DaliError):
                    await transport.send_frame(d.off(T))
