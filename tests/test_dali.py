"""Unit tests for the pure DALI layer.

Golden frames were captured from python-dali (the canonical DALI library) and
are hard-coded here so the suite runs without that dependency.
Run: pytest tests/  (or: python3 tests/test_dali.py)
"""

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "atios_dali", Path(__file__).resolve().parent.parent / "custom_components" / "atios" / "dali.py"
)
dali = importlib.util.module_from_spec(_spec)
sys.modules["atios_dali"] = dali
_spec.loader.exec_module(dali)

T = dali.Target


def test_frame_builders():
    assert dali.goto_scene(T.broadcast(), 0).data == [0xFF, 0x10]
    assert dali.query(T.short(0), dali.OP_QUERY_CONTROL_GEAR_PRESENT).data == [1, 145]
    assert dali.off(T.broadcast()).data == [0xFF, 0x00]
    assert dali.level(T.short(5), 254).data == [0x0A, 254]
    assert dali.recall_max(T.group(3)).data == [0x87, 0x05]
    assert dali.query(T.short(0), 0).wait_for_answer is True


def test_brightness_mapping():
    assert dali.ha_brightness_to_dali(255) == 254
    assert dali.ha_brightness_to_dali(0) == 0
    assert dali.ha_brightness_to_dali(1) >= 1
    assert dali.dali_level_to_ha_brightness(254) == 255
    assert dali.dali_level_to_ha_brightness(0) == 0


# golden 24-bit event frames from python-dali (device scheme, addr=3)
_DEVICE_ADDR3 = {
    "button_released": [6, 4, 0],
    "button_pressed": [6, 4, 1],
    "short_press": [6, 4, 2],
    "double_press": [6, 4, 5],
    "long_press_start": [6, 4, 9],
    "long_press_repeat": [6, 4, 11],
    "long_press_stop": [6, 4, 12],
    "button_free": [6, 4, 14],
    "button_stuck": [6, 4, 15],
}


def test_decode_device_scheme():
    for gesture, frame in _DEVICE_ADDR3.items():
        d = dali.decode_input_event(frame, 24)
        assert d is not None
        assert d.scheme is dali.EventScheme.DEVICE
        assert d.short_address == 3
        assert d.instance_type == dali.INSTANCE_TYPE_PUSHBUTTON
        assert d.gesture == gesture, (gesture, d.gesture)


def test_decode_addr0():
    d = dali.decode_input_event([0, 4, 1], 24)
    assert d.short_address == 0 and d.gesture == "button_pressed"


def test_decode_device_instance_scheme():
    # ShortPress, addr=3, instance_number=2 -> [6, 136, 2]
    d = dali.decode_input_event([6, 136, 2], 24)
    assert d.scheme is dali.EventScheme.DEVICE_INSTANCE
    assert d.short_address == 3 and d.instance_number == 2
    assert d.event_info == 2
    assert d.gesture is None  # type not carried in this scheme
    assert dali.PUSHBUTTON_EVENTS[d.event_info] == "short_press"


def test_non_event_frame():
    assert dali.decode_input_event([0x0A, 0x0B], 16) is None
    assert dali.decode_input_event([1, 2], 24) is None


def test_24bit_command_is_not_event():
    # bit16 set = 24-bit command, not an event. Same address/instance bytes as
    # the golden button frames, which would otherwise decode as presses.
    assert dali.decode_input_event([7, 4, 1], 24) is None  # device 3, instance byte 0x04
    assert dali.decode_input_event([7, 136, 2], 24) is None
    assert dali.decode_input_event([0xFF, 0xFE, 0x00], 24) is None  # broadcast command
    # the event twin (bit16 clear) still decodes
    assert dali.decode_input_event([6, 4, 1], 24).gesture == "button_pressed"


def test_rgbw_channel_values_and_preservation():
    assert dali.channel_to_dali(0) == 0
    assert dali.channel_to_dali(255) == 254
    assert all(dali.channel_to_dali(v) != dali.MASK for v in range(256))
    assert dali.channel_from_dali(0) == 0
    assert dali.channel_from_dali(255) is None
    # RGB-only: explicit maximum/zero remain levels; W/A/F use MASK.
    frames = dali.rgbw_frames(T.short(5), (255, 0, 128, dali.PRESERVE))
    assert [f.data for f in frames][3:13] == [
        [0xA3, 0xFE], [0xC3, 0x00], [0xC5, 0x7F], [0xC1, 8], [0x0B, 0xEB],
        [0xA3, 0xFF], [0xC3, 0xFF], [0xC5, 0xFF], [0xC1, 8], [0x0B, 0xEC],
    ]
    frames = dali.rgbw_frames(T.short(5), (None, None, None, 255))
    assert [f.data for f in frames][3:13] == [
        [0xA3, 0xFF], [0xC3, 0xFF], [0xC5, 0xFF], [0xC1, 8], [0x0B, 0xEB],
        [0xA3, 0xFE], [0xC3, 0xFF], [0xC5, 0xFF], [0xC1, 8], [0x0B, 0xEC],
    ]


def test_rgbw_golden_sequence():
    # Literal register/opcode reference: python-dali colour.py at b1881c008aca.
    frames = dali.rgbw_frames(T.short(5), (255, 0, 64, 128))
    assert [f.data for f in frames] == [
        [0xA3, 0x80], [0xC1, 8], [0x0B, 0xED],
        [0xA3, 0xFE], [0xC3, 0x00], [0xC5, 0x40], [0xC1, 8], [0x0B, 0xEB],
        [0xA3, 0x7F], [0xC3, 0xFF], [0xC5, 0xFF], [0xC1, 8], [0x0B, 0xEC],
        [0xC1, 8], [0x0B, 0xE2],
    ]
    assert dali.rgbw_level((0, 0, 0, 128), 255) == 127
    assert dali.rgbw_level((0, 0, 0, 0), 255) == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
