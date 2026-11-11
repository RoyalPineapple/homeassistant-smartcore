"""Parse the SmartCore NVRAM device model.

The SmartCore web configurator reads its device list from
``GET /api/dali/nvm?section=<control_devices|input_devices>&offset=N&limit=4``
(captured from the r12.atios.ch demo, firmware 2.7.x). This module turns those
payloads into the small dataclasses the entity platforms consume. Pure module,
no I/O, no Home Assistant imports — unit-tested against captured fixtures.

Instance types follow IEC 62386 parts: 1 = push button (-301), 2 = absolute
input (-302), 3 = occupancy sensor (-303), 4 = light sensor (-304),
6 = general purpose sensor.

``event_scheme`` uses the part-103 numbering: 0 = Instance, 1 = Device,
2 = Device/Instance, 3 = Device group, 4 = Instance group. Schemes 1 and 2 are
the ones our 24-bit event decoder resolves to a source (see dali.py); for the
others the entity keeps listening but cannot be matched upfront.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .dali import Target

INSTANCE_PUSHBUTTON = 1
INSTANCE_ABSOLUTE_INPUT = 2
INSTANCE_OCCUPANCY = 3
INSTANCE_LIGHT = 4
INSTANCE_GENERAL_PURPOSE = 6

SCHEME_DEVICE = 1
SCHEME_DEVICE_INSTANCE = 2

# group entries carry the DALI group number offset into the address field
GROUP_ADDRESS_BASE = 128


@dataclass(frozen=True)
class ControlDevice:
    """One configured control gear target (a short address or a group)."""

    name: str
    is_group: bool
    number: int  # short address 0..63 or group 0..15
    gear_groups: tuple[int, ...] = ()
    device_types: tuple[int, ...] = ()
    device_type_selected: int = 0
    min_level: int = 1
    max_level: int = 254
    color_type_features: int | None = None
    groups_known: bool = False

    @property
    def target(self) -> Target:
        return Target.group(self.number) if self.is_group else Target.short(self.number)

    @property
    def dt8_candidate(self) -> bool:
        """Selected DT6 must stay brightness-only, even if DT8 is also listed."""
        return (self.device_type_selected in (0, 8)
                and (8 in self.device_types or self.device_type_selected == 8))

    @property
    def rgbw_channels(self) -> int:
        """Part 209 bits 5..7 are a count, not a boolean RGBW flag."""
        value = self.color_type_features
        return (value >> 5) & 7 if type(value) is int and 0 <= value < 255 else 0


@dataclass(frozen=True)
class InputInstance:
    """One instance of a DALI-2 input device."""

    number: int
    type: int
    active: bool
    event_scheme: int

    def expected_signature(self, short_address: int) -> str | None:
        """The InputEvent.signature this instance's events decode to, if known.

        Device scheme carries the instance TYPE in the frame; Device/Instance
        scheme carries the instance NUMBER. Other schemes aren't resolvable
        upfront.
        """
        if self.event_scheme == SCHEME_DEVICE:
            return f"a{short_address}_t{self.type}_ix"
        if self.event_scheme == SCHEME_DEVICE_INSTANCE:
            return f"a{short_address}_tx_i{self.number}"
        return None


@dataclass(frozen=True)
class InputDevice:
    """One configured DALI-2 input device (24-bit control device)."""

    name: str
    address: int
    instances: tuple[InputInstance, ...] = field(default_factory=tuple)


def parse_control_devices(devices: list[dict]) -> list[ControlDevice]:
    """Parse the ``control_devices`` list; skips collision pseudo-entries."""
    out: list[ControlDevice] = []
    for d in devices:
        if not isinstance(d, dict):
            continue
        if d.get("collision"):
            continue
        addr = d.get("address")
        if type(addr) is not int:
            continue
        is_group = bool(d.get("is_group"))
        number = addr - GROUP_ADDRESS_BASE if is_group else addr
        if is_group and not 0 <= number <= 15:
            continue
        if not is_group and not 0 <= number <= 63:
            continue
        out.append(
            ControlDevice(
                name=d.get("name") or ("Group" if is_group else "Light") + f" {number}",
                is_group=is_group,
                number=number,
                gear_groups=tuple(g for g in (d.get("gear_groups") or ())
                                  if type(g) is int and 0 <= g <= 15)
                if isinstance(d.get("gear_groups"), (list, tuple)) else (),
                device_types=tuple(t for t in (d.get("device_types") or ()) if type(t) is int)
                if isinstance(d.get("device_types"), (list, tuple)) else (),
                device_type_selected=d.get("device_type_selected", 0),
                min_level=d.get("min_level", 1),
                max_level=d.get("max_level", 254),
                color_type_features=d.get("color_type_features"),
                groups_known=isinstance(d.get("gear_groups"), list)
                and all(type(g) is int and 0 <= g <= 15 for g in d["gear_groups"]),
            )
        )
    return out


def rgbw_group_members(
    devices: list[ControlDevice], verified: set[Target], *, complete: bool
) -> dict[Target, tuple[Target, ...]]:
    """Only groups with a complete known inventory and verified RGBW members."""
    shorts = [d for d in devices if not d.is_group]
    if not complete or not shorts or not all(d.groups_known for d in shorts):
        return {}
    groups = {}
    for group in (d for d in devices if d.is_group):
        members = tuple(d.target for d in shorts if group.number in d.gear_groups)
        if members and all(t in verified for t in members):
            groups[group.target] = members
    return groups


def parse_input_devices(devices: list[dict]) -> list[InputDevice]:
    """Parse the ``input_devices`` list."""
    out: list[InputDevice] = []
    for d in devices:
        addr = d.get("address")
        if addr is None or not 0 <= addr <= 63:
            continue
        instances = tuple(
            InputInstance(
                number=i.get("number", 0),
                type=i.get("type", 0),
                active=bool(i.get("active")),
                event_scheme=i.get("event_scheme", SCHEME_DEVICE_INSTANCE),
            )
            for i in (d.get("instances") or ())
        )
        out.append(
            InputDevice(name=d.get("name") or f"Input {addr}", address=addr, instances=instances)
        )
    return out
