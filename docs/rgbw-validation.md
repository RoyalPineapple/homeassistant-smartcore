# Local RGBW validation — 2026-11-11

## Executed checks

Python executable:

```text
/Users/alex/.local/share/uv/python/cpython-3.13.18-macos-aarch64-none/bin/python3.13
```

Commands (from the repository root):

```bash
/Users/alex/.local/share/uv/python/cpython-3.13.18-macos-aarch64-none/bin/python3.13 tests/run_offline.py
/Users/alex/.local/share/uv/python/cpython-3.13.18-macos-aarch64-none/bin/python3.13 -m compileall -q custom_components tests
git diff --check
```

Results: **50 tests passed, one module skipped** (51 unittest results total).
Compilation and whitespace checks passed. The runner executes the existing
14 function tests unchanged, plus 36 new tests. Golden expected bytes are literal
independent reference data. The simulated gateway is a software peer, not a
hardware trace or firmware emulator.

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

## Checks not executed

`tests/test_ha.py` contains **eleven real Home Assistant/aiohttp adapter tests**:
entity identities and slider actions, actual HA RGB/HS/XY conversion, shared
state, restoration, legacy dimming/scenes/raw services, action failure, JSON
payload/body validation, HTTP/timeout/malformed replies and unload closure.
Two regressions also exercise explicit RGBW replacement of pending white and a
real number service action on a group whose members have different W values.
The module was explicitly skipped because Home Assistant is not installed.
These tests have been added but have not passed an execution check here.

The intended environment is Python 3.13 and `requirements-test.txt` (HA
2025.11.3). Installation was attempted with:

```bash
/Users/alex/.local/share/uv/python/cpython-3.13.18-macos-aarch64-none/bin/python3.13 -m pip install --target /tmp/atios-test-deps --index-url https://pypi.org/simple --no-cache-dir --retries 0 --timeout 5 pytest aiohttp homeassistant==2025.11.3 -v
```

It failed. A further isolated verbose pip attempt confirmed the cause:
`pypi.org` could not be resolved. Pip itself is installed and operational. The
full pytest/HA suite is therefore an explicit remaining local verification
blocker, not a hardware dependency. Run the documented venv/pytest commands in
CONTRIBUTING.md when dependency installation is available.

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
