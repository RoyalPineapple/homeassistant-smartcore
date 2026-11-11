"""Tests for the NVRAM device-model parser against captured fixtures.

Fixtures were captured from the r12.atios.ch demo SmartCore (2026-08-13);
scene tables regenerated (all 16 slots disabled, as observed).
"""

import json
from pathlib import Path

# Load pure modules in an isolated namespace without replacing HA's real
# integration package (the real entity/service tests need that package intact).
import pure  # noqa: F401

from _atios_pure.dali import TargetType  # noqa: E402
from _atios_pure.nvram import (  # noqa: E402
    INSTANCE_LIGHT,
    INSTANCE_OCCUPANCY,
    INSTANCE_PUSHBUTTON,
    parse_control_devices,
    parse_input_devices,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name):
    with open(FIXTURES / name) as f:
        return json.load(f)


def control_devices():
    body = _load("nvm_control_devices.json")
    return parse_control_devices(body["data"]["control_devices"])


def input_devices():
    body = _load("nvm_input_devices.json")
    return parse_input_devices(body["data"]["input_devices"])


def test_collision_entries_are_skipped():
    devs = control_devices()
    assert [d.name for d in devs] == ["Group 1", "Short 0", "Control 1"]


def test_group_address_decoding():
    group = control_devices()[0]
    assert group.is_group
    assert group.number == 1  # raw address 129 -> group 1
    assert group.target.type is TargetType.GROUP
    assert group.target.number == 1


def test_short_address_passthrough():
    short0 = control_devices()[1]
    assert not short0.is_group
    assert short0.number == 0
    assert short0.target.type is TargetType.SHORT
    assert short0.device_types == (6, 8)
    assert short0.gear_groups == (1,)


def test_input_devices_parsed():
    devs = input_devices()
    assert [d.name for d in devs] == ["Input 1", "Input 2", "Input 3", "Input 4"]
    assert [d.address for d in devs] == [0, 1, 3, 4]
    # Input 1 is the application controller: no instances
    assert devs[0].instances == ()


def test_instance_types():
    devs = input_devices()
    input2 = devs[1]
    assert input2.instances[0].type == INSTANCE_PUSHBUTTON
    input3 = devs[2]
    assert input3.instances[0].type == INSTANCE_OCCUPANCY
    assert input3.instances[1].type == INSTANCE_LIGHT


def test_expected_signature_device_instance_scheme():
    # Input 2 instance 0: push button, event_scheme 2 (Device/Instance)
    inst = input_devices()[1].instances[0]
    assert inst.event_scheme == 2
    assert inst.expected_signature(1) == "a1_tx_i0"


def test_expected_signature_device_scheme():
    # Input 4 instance 0: occupancy, event_scheme 1 (Device) -> type in frame
    inst = input_devices()[3].instances[0]
    assert inst.event_scheme == 1
    assert inst.expected_signature(4) == "a4_t3_ix"
