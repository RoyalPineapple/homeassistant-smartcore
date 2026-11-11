# Local RGBW validation — 2026-11-11

## Executed checks

Verified environment:

```text
Python 3.13.18
Home Assistant 2025.11.3
pytest 8.4.2
aiohttp 3.13.2
pycares 4.11.0
```

Commands (from the repository root):

```bash
.venv/bin/python -m pytest tests/test_ha.py -q --tb=short
.venv/bin/python -m pytest tests/ -q --tb=short
.venv/bin/python -m compileall -q custom_components tests
git diff --check
```

Results: **11 HA/HTTP tests passed; the complete suite passed all 61 tests, with
no skips.** Compilation and whitespace checks also passed. The complete suite
contains the existing 14 function tests, 36 protocol/controller/transport tests,
and 11 real HA/HTTP adapter tests. Both pytest runs reported one upstream
DeprecationWarning from HA's HTTP application subclassing aiohttp's application.
Golden expected bytes are literal independent reference data. The simulated
gateway is a software peer, not a hardware trace or firmware emulator.

Covered contracts include addressing and DT8 enable/order; explicit zero/full
versus preservation MASK; brightness scaling; RGB/W preservation; unknown and
mixed capability fallback; per-member group state; report/request distinction;
external state and canonical decomposition; stale temporary values; busy,
collision, malformed results; serialization; no ACTIVATE after staging failure;
bounded admission; slider coalescing; cancellation and unload lock release.
The existing button-event decoding and input-device parser tests also passed.

Review regressions were reproduced before the fixes: an explicit W=0 or W=255
was overridden by saved off-state W, later on commands overtook a waiting off,
and slider values merged across off. Those regressions now pass. Additional
tests verify the bounded power-command queue and cancellation of queued power
commands and edits during unload.

## Home Assistant integration checks

`tests/test_ha.py` contains **eleven real Home Assistant/aiohttp adapter tests**:
entity identities and slider actions, actual HA RGB/HS/XY conversion, shared
state, restoration, legacy dimming/scenes/raw services, action failure, JSON
payload/body validation, HTTP/timeout/malformed replies and unload closure.
Two regressions also exercise explicit RGBW replacement of pending white and a
real number service action on a group whose members have different W values.
All eleven tests now execute and pass against the installed HA package. HA runs
in-process with a temporary configuration and real registries, entities and
service processing; the gateway and HTTP responses are simulated.

The first real run exposed test-harness defects: registries and the config-entry
manager were not initialized, teardown used a removed method, the external-change
test did not allow the documented polling cache to expire, and the shared fake
gateway raised the pure-test namespace's exception instead of the integration's
real exception type. The harness now initializes HA's native registries, uses
public entity removal and proper shutdown, advances only the controller's test
clock, and supplies the correct simulated failure type. No production integration
code changes were needed for these harness corrections.

Dependency installation initially could not run inside the task because PyPI
DNS resolution was blocked. The dependencies were subsequently installed from
the user's terminal. `requirements-test.txt` now constrains pycares to 4.x: HA's
pinned aiodns 3.5.0 cannot import with pycares 5.x. The verified environment above
includes that compatibility constraint. Follow CONTRIBUTING.md to reproduce it.

No tests used private addresses, SSH, live device requests or a deployed HA.
No deployment, restart, pairing change, firmware update, DALI command, group
change or operating-mode commissioning action was performed.

## What these results mean

The executed checks validate the software contracts exercised by the simulation.
They do not prove driver behaviour, firmware compatibility, HTTP reply shapes
on an installed gateway, timing, dimming curves or coexistence with other bus
masters. Normalised-mode mapping and literal MASK semantics have protocol
references in [rgbw.md](rgbw.md); compatibility with the actual drivers remains
unverified. The [on-site checklist](rgbw-hardware-checklist.md) records that work.

## Checkout

Started on `main` at `472ad18d5be7908fae25f4b20fdd208c2916bb28`, with only
the user's untracked `todo.txt`. The workspace permits source edits but denies
writes to its `.git`, so branch creation there failed before any edit. A writable
local clone at `/tmp/atios-dt8-rgbw` contains the feature branch
`codex/dt8-rgbw`, with the same source changes as the original working directory.
The original working directory remains on `main` with those changes uncommitted;
`todo.txt` was preserved and excluded from the implementation commit. Nothing
was pushed and no PR was opened.
