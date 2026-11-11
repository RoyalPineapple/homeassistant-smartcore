"""Raw DALI-2 frame construction and decoding.

This module is deliberately pure (no I/O, no Home Assistant imports) so it can
be unit-tested on its own. The Atios SmartCore does *not* expose the Lunatone
REST device API, so every light action is expressed as a raw 16-bit forward
frame and every status read is a QUERY; hub.py sends both over the HTTP
``/api/dali/iface`` endpoint. Bus traffic (incl. input events) comes back as
``daliMonitor`` frames on the websocket and is decoded here.

DALI-2 IEC 62386 forward-frame reference (16-bit):

  Address byte (first byte):
    short address, direct arc power  ->  0AAAAAA0   (level in 2nd byte)
    short address, command           ->  0AAAAAA1   (opcode in 2nd byte)
    group address, direct arc power  ->  100AAAA0
    group address, command           ->  100AAAA1
    broadcast, direct arc power      ->  11111110   (0xFE)
    broadcast, command               ->  11111111   (0xFF)

Send-twice applies to *configuration* commands (STORE/SET/REMOVE, 0x20..0x80),
not to the plain control commands used here (OFF/RECALL/GOTO SCENE), which are
single-shot. QUERY commands set wait-for-answer to receive a backward frame.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

# ---- opcodes (2nd byte of a 16-bit command frame) -------------------------

OP_OFF = 0x00
OP_UP = 0x01
OP_DOWN = 0x02
OP_STEP_UP = 0x03
OP_STEP_DOWN = 0x04
OP_RECALL_MAX = 0x05
OP_GOTO_LAST_ACTIVE = 0x0A  # GO TO LAST ACTIVE LEVEL (IEC 62386-102)
OP_RECALL_MIN = 0x06
OP_GOTO_SCENE_BASE = 0x10  # 0x10..0x1F -> scenes 0..15
OP_QUERY_STATUS = 0x90
OP_QUERY_CONTROL_GEAR_PRESENT = 0x91
OP_QUERY_ACTUAL_LEVEL = 0xA0

MASK = 0xFF
BROADCAST_DAPC = 0xFE
BROADCAST_CMD = 0xFF

# Brightness levels: 0 = off, 254 = max. 255 is valid DALI MASK, not brightness.
# DAPC MASK stops fading at the current level; DT8 channel MASK preserves it.
DALI_MIN = 0
DALI_MAX = 254


class TargetType(IntEnum):
    """Which addressing form a frame uses."""

    SHORT = 0
    GROUP = 1
    BROADCAST = 2


@dataclass(frozen=True)
class Target:
    """A DALI control-gear target: broadcast, a group 0..15, or a short address 0..63."""

    type: TargetType
    number: int = 0  # short address (0..63) or group (0..15); ignored for broadcast

    @classmethod
    def broadcast(cls) -> "Target":
        return cls(TargetType.BROADCAST)

    @classmethod
    def short(cls, address: int) -> "Target":
        if not 0 <= address <= 63:
            raise ValueError(f"short address out of range: {address}")
        return cls(TargetType.SHORT, address)

    @classmethod
    def group(cls, group: int) -> "Target":
        if not 0 <= group <= 15:
            raise ValueError(f"group out of range: {group}")
        return cls(TargetType.GROUP, group)

    def _address_byte(self, *, command: bool) -> int:
        """Build the first byte. ``command`` selects the S bit (1=command, 0=level)."""
        s = 1 if command else 0
        if self.type is TargetType.BROADCAST:
            return BROADCAST_CMD if command else BROADCAST_DAPC
        if self.type is TargetType.GROUP:
            return 0x80 | (self.number << 1) | s
        return (self.number << 1) | s

    @property
    def unique_suffix(self) -> str:
        """Stable identifier fragment for entity unique_ids."""
        if self.type is TargetType.BROADCAST:
            return "broadcast"
        if self.type is TargetType.GROUP:
            return f"group_{self.number}"
        return f"addr_{self.number}"


@dataclass(frozen=True)
class Frame:
    """A DALI forward frame ready for transport (WS ``daliFrame`` or HTTP ``/api/dali/iface``)."""

    data: list[int]
    bits: int = 16
    send_twice: bool = False
    wait_for_answer: bool = False
    line: int = 0
    priority: int = 3

    def __post_init__(self) -> None:
        for b in self.data:
            if not 0 <= b <= MASK:
                raise ValueError(f"frame byte out of range: {b}")


# ---- frame builders -------------------------------------------------------


def level(target: Target, value: int) -> Frame:
    """Direct Arc Power Control: set an absolute level (0..254)."""
    value = max(DALI_MIN, min(DALI_MAX, int(value)))
    return Frame(data=[target._address_byte(command=False), value])


def command(target: Target, opcode: int, *, send_twice: bool = False) -> Frame:
    """A control command (OFF, RECALL MAX, GOTO SCENE, ...)."""
    return Frame(
        data=[target._address_byte(command=True), opcode & MASK],
        send_twice=send_twice,
    )


def query(target: Target, opcode: int) -> Frame:
    """A QUERY command; the backward-frame answer is returned by hub.send_frame."""
    return Frame(
        data=[target._address_byte(command=True), opcode & MASK],
        wait_for_answer=True,
    )


def off(target: Target) -> Frame:
    return command(target, OP_OFF)


def recall_max(target: Target) -> Frame:
    return command(target, OP_RECALL_MAX)


def goto_last_active(target: Target) -> Frame:
    """Turn on restoring the level the gear had before it was switched off."""
    return command(target, OP_GOTO_LAST_ACTIVE)


def goto_scene(target: Target, scene: int) -> Frame:
    if not 0 <= scene <= 15:
        raise ValueError(f"scene out of range: {scene}")
    return command(target, OP_GOTO_SCENE_BASE + scene)


def query_actual_level(target: Target) -> Frame:
    return query(target, OP_QUERY_ACTUAL_LEVEL)


# ---- percentage <-> DALI level -------------------------------------------
# HA brightness is 0..255; DALI arc power is 0..254. This is a linear map, NOT
# the DALI logarithmic dim curve — fixtures apply their own curve on top, so a
# linear byte map is the correct thing to send.


def ha_brightness_to_dali(brightness: int) -> int:
    """HA brightness (0..255) -> DALI level (1..254). 0 stays 0 (handled as OFF upstream)."""
    if brightness <= 0:
        return 0
    return max(1, min(DALI_MAX, round(brightness / 255 * DALI_MAX)))


def dali_level_to_ha_brightness(dali_level: int) -> int:
    """DALI level (0..254) -> HA brightness (0..255)."""
    if dali_level <= 0:
        return 0
    return max(1, min(255, round(dali_level / DALI_MAX * 255)))


# IEC 62386-209:2011. Definitions independently checked against python-dali
# b1881c008aca0b269fd09b8834dd9bdc0f1a735b; see docs/rgbw.md.
OP_DT8_ACTIVATE = 0xE2
OP_DT8_RGB = 0xEB
OP_DT8_WAF = 0xEC
OP_DT8_CONTROL = 0xED
OP_DT8_FEATURES = 0xF9
OP_DT8_VALUE = 0xFA
OP_DT8_STATUS = 0xF8
OP_DT8_ASSIGNED_COLOUR = 0xFC
OP_DT8_QUERY_CONTROL = 0xFB
RGBW_CONTROL = 0x80  # normalised colour control, all channels unlinked
PRESERVE = None  # channel intent; distinct from every explicit HA byte, incl. 255


def byte(value: int) -> int:
    """Validate a protocol byte, without silently rounding or wrapping."""
    if type(value) is not int or not 0 <= value <= 255:
        raise ValueError(f"Expected an integer byte, got {value!r}")
    return value


def channel_to_dali(value: int) -> int:
    """HA channel intensity -> real DALI channel value; never MASK."""
    return round(byte(value) * 254 / 255)


def channel_from_dali(value: int) -> int | None:
    """MASK means unknown, whereas zero is a real channel value."""
    return None if byte(value) == MASK else round(value * 255 / 254)


def dtr(register: int, value: int) -> Frame:
    if type(register) is not int or register not in (0, 1, 2):
        raise ValueError("DTR register must be 0, 1 or 2")
    return Frame([(0xA3, 0xC3, 0xC5)[register], byte(value)])


def dt8(target: Target, opcode: int, *, read: bool = False) -> list[Frame]:
    """Enable immediately before each DT8 operation (runtime, single-send)."""
    return [Frame([0xC1, 8]), query(target, byte(opcode)) if read else command(target, byte(opcode))]


def query_colour(target: Target, selector: int) -> list[Frame]:
    """One-byte colour/control selectors only; no 16-bit DTR readback."""
    if selector not in (*range(9, 16), *range(201, 208)):
        raise ValueError("Not a one-byte RGBWAF selector")
    return [dtr(0, selector), *dt8(target, OP_DT8_VALUE, read=True)]


def rgbw_frames(target: Target, channels: tuple[int | None, int | None, int | None, int | None]) -> list[Frame]:
    """Stage all channel intents and control, then activate; DAPC is separate.

    None means preserve, encoded as MASK. Explicit 0..255 are always real levels.
    Overwrite every temporary RGBW field, including preserved ones, so stale
    temporary values cannot be activated accidentally. Unused A/F are MASK.
    """
    if len(channels) != 4:
        raise ValueError("RGBW needs four channels")
    red, green, blue, white = (MASK if v is PRESERVE else channel_to_dali(v) for v in channels)
    return [
        dtr(0, RGBW_CONTROL), *dt8(target, OP_DT8_CONTROL),
        dtr(0, red), dtr(1, green), dtr(2, blue), *dt8(target, OP_DT8_RGB),
        dtr(0, white), dtr(1, MASK), dtr(2, MASK), *dt8(target, OP_DT8_WAF),
        *dt8(target, OP_DT8_ACTIVATE),
    ]


def rgbw_level(channels: tuple[int, int, int, int], brightness: int) -> int:
    """Compensate normalised control's division by the largest channel.

    Channel bytes are staged unchanged. Only the final arc level includes the
    largest channel: output X * arc / max(X) = X * brightness / 254.
    This is protocol scaling, not a photometric transfer function.
    """
    return round(max(channel_to_dali(v) for v in channels)
                 * ha_brightness_to_dali(byte(brightness)) / 254)


# ---- DALI-2 input (control device) event decoding -------------------------
# Button/occupancy events from DALI-2 input devices arrive as 24-bit forward
# frames on the bus (event scheme, instance-addressed). The precise semantic
# mapping (iT1 push button / iT2 occupancy / iT3 light sensor and the
# short/double/long sub-codes) depends on how each coupler is configured, so we
# decode the envelope here and leave the semantic layer to be calibrated against
# real monitor traces from the actual hardware. See notes in event.py.


INSTANCE_TYPE_PUSHBUTTON = 1  # IEC 62386-301

# Push-button event information (low 10 bits) -> gesture name (IEC 62386-301,
# Table for event codes; verified against python-dali's pushbutton classes).
PUSHBUTTON_EVENTS: dict[int, str] = {
    0x00: "button_released",
    0x01: "button_pressed",
    0x02: "short_press",
    0x05: "double_press",
    0x09: "long_press_start",
    0x0B: "long_press_repeat",
    0x0C: "long_press_stop",
    0x0E: "button_free",
    0x0F: "button_stuck",
}


class EventScheme(IntEnum):
    """Event-source addressing scheme of a 24-bit control-device event frame."""

    DEVICE = 0  # short address + instance TYPE carried in the frame
    DEVICE_INSTANCE = 1  # short address + instance NUMBER carried in the frame
    OTHER = 2  # group / instance / instance-type / broadcast addressing (bit23=1)


@dataclass(frozen=True)
class InputEvent:
    """A decoded 24-bit DALI-2 control-device event frame (IEC 62386-103)."""

    raw: list[int]
    scheme: EventScheme
    short_address: int | None
    instance_type: int | None
    instance_number: int | None
    event_info: int | None

    @property
    def gesture(self) -> str | None:
        """Push-button gesture name, when this frame is a part-301 event.

        Only push-button (instance type 1) event_info maps to these names.
        In DEVICE scheme the instance type is in the frame, so we can be sure.
        In DEVICE_INSTANCE scheme the type is not transmitted; callers that know
        the instance is a button can look up PUSHBUTTON_EVENTS[event_info].
        """
        if self.instance_type == INSTANCE_TYPE_PUSHBUTTON and self.event_info is not None:
            return PUSHBUTTON_EVENTS.get(self.event_info)
        return None

    @property
    def signature(self) -> str:
        """Stable id fragment for HA device-trigger / entity wiring."""
        a = "x" if self.short_address is None else self.short_address
        t = "x" if self.instance_type is None else self.instance_type
        i = "x" if self.instance_number is None else self.instance_number
        return f"a{a}_t{t}_i{i}"


def decode_input_event(data: list[int], bits: int) -> InputEvent | None:
    """Decode a monitored frame into an InputEvent, or None if not a 24-bit event.

    24-bit event frame (device-addressed, the Lunatone default), bits [23..0]:

        byte0: 0 AAAAAA 0   short address 0..63 in bits 22..17; bit16 is 0 for
                            every event message (1 = 24-bit command)
        byte1: S TTTTT DD   S=bit15 scheme (0=Device,1=Device/Instance)
                            TTTTT=bits 14..10 (instance TYPE or NUMBER)
                            DD=bits 9..8 (top of event info)
        byte2: DDDDDDDD     bits 7..0 (rest of event info)

    Group/instance/broadcast-addressed events set bit23; we decode the event
    info but not the source address for those (rare with Lunatone couplers).
    Verified byte-for-byte against python-dali across all addresses/events.
    """
    if bits != 24 or len(data) < 3:
        return None
    b0, b1, b2 = data[0] & 0xFF, data[1] & 0xFF, data[2] & 0xFF
    if b0 & 0x01:
        # bit16 set -> a 24-bit command to control devices (e.g. from another
        # bus master), not an event; the SmartCore firmware splits them the
        # same way (dali_transport_layer.c, raw_frame & 0x10000).
        return None
    event_info = ((b1 & 0x03) << 8) | b2  # bits 9..0
    field = (b1 >> 2) & 0x1F  # bits 14..10

    if b0 & 0x80:  # bit23 set -> not a device-short-addressed event
        return InputEvent(list(data), EventScheme.OTHER, None, None, None, event_info)

    short_address = (b0 >> 1) & 0x3F
    if b1 & 0x80:  # bit15 -> Device/Instance scheme
        return InputEvent(
            list(data), EventScheme.DEVICE_INSTANCE, short_address, None, field, event_info
        )
    return InputEvent(
        list(data), EventScheme.DEVICE, short_address, field, None, event_info
    )
