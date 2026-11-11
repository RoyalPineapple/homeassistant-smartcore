# DALI DT8 RGBW

This fork adds native RGBW lights and a separate **White level** number entity.
The number is the convenient independent slider: UI support for `RGBW` alone
does not guarantee a physical-white slider in every HA card. White means the
driver's W output, not colour temperature, desaturation, or RGB-generated white.

## Detection and identity

NVRAM retains `color_type_features`. Bits 5–7 contain the RGBWAF **channel count**;
DT8 or a name containing “RGBW” is insufficient. DT6 selections remain dimmers.
During integration setup, DT8 candidates are checked using real short-address
queries for colour features, assignments of channels 0–3, and RGBWAF control.
Only four-channel gear assigned R/G/B/W is enabled. Other representations,
ambiguous assignments, six-channel gear, reserved/extended control encodings,
missing answers and failed checks stay brightness-only. A reload retries checks.
Cached metadata is a candidate hint, never sufficient proof of RGBW capability.

Groups qualify only with a complete NVRAM inventory, known group memberships,
and verified RGBW support on every known member. Failed pagination or collision
entries suppress RGBW groups. Membership is not changed; reload after changing
it externally. Broadcast stays brightness-only. Group operations address members
individually and are not simultaneous or rollback-capable. Group replies are
never treated as authoritative. Unknown/unlisted members cannot be discovered
from a stale inventory; confirm membership during hardware validation.

Existing light unique IDs are unchanged. Numbers append `_white_level` to the
same target identity. RGB and RGBW are accepted HA input modes; the full reported
colour mode is RGBW. Accepting RGB directly prevents HA's HS/XY conversion from
inventing or replacing W. The HA tests exercise RGB, HS and XY service inputs.

## Command contract

| Input | Behaviour |
|---|---|
| Brightness | Preserve the mix; change the overall level |
| `rgbw_color` | Explicitly replace all four channels, including W=0 |
| RGB / converted HS or XY | Replace RGB, preserve W |
| White level 0–255 | Replace W, preserve RGB and overall brightness |
| Off | Turn output off, retain usable settings for this session |
| White while off | Remember pending W; send no colour-control frames; stay off |
| Plain on | Restore the usable settings, including pending W |
| All channels zero | Stay dark; retain zeros; never invent a white component |
| Unknown preserved channels | Read first, then fail if preservation cannot be established |

Example (replace the entity ID with the existing light):

```yaml
action: light.turn_on
target:
  entity_id: light.living_room_rgbw_1
data:
  rgbw_color: [255, 0, 0, 128]
  brightness: 255
```

Use the matching `number` entity's White level slider for W alone.

## Explicit values versus preservation

An explicit HA channel is an integer 0–255. It is scaled to a real DALI value
0–254. HA 255 becomes 254; HA zero stays zero. `PRESERVE` is a separate intent
(`None` internally), encoded as DALI MASK (255).

Every colour sequence writes control, all temporary RGB fields and all temporary
WAF fields before ACTIVATE. RGB-only sets W/A/F to MASK. White-only sets RGB and
A/F to MASK. Brightness-only preserves all channel fields. Replacing temporary
fields with MASK also discards stale pending channel values left by a previous
sequence. ACTIVATE interprets each MASK as no change to the corresponding actual
value. No partial update relies on a previous temporary value being harmless.

Each extended command is immediately preceded by ENABLE DEVICE TYPE 8. Runtime
commands are single-send; no configuration commands, persistent operating-mode
changes, channel reassignments, scene writes or calibration are performed.

## Master brightness and RGBWAF control

The implementation explicitly selects **normalised colour control with all
channels unlinked**, control byte `0x80`. “Linked” means linked to arc power,
not “enable this colour”: `0x4F` would not provide the required master dimming.
Reserved/extended encodings are not assumed to follow these legacy semantics.
This runtime control selection can affect another controller's expectations.

Let `d[i] = round(HA_channel[i] * 254 / 255)`, `p = max(d)`, and `b` be the
existing HA-to-DALI master brightness conversion. Stage channel values without
multiplying them by brightness. The final DAPC level is `round(p * b / 254)`.
This compensates the driver's normalisation by the largest channel: a white-only
HA tuple `[0,0,0,128]` at master 255 must not become full white. Zero peak means
DAPC zero. This is a byte-domain mapping; the active DALI dimming curve, minimum
levels, calibration, power limits and perceptual output require hardware checks.
Low values can round to zero. ACTIVATE followed by DAPC may have visible timing
effects; no claim of flicker-free or simultaneous execution is made.

## State, external controllers and restoration

One controller owns one record per physical address. Light and number subscribe
to that owner; groups reference those same records. Records distinguish complete
driver reports, accepted requested settings, last usable settings, pending W,
availability and assumed/confirmed/unknown status. A successful write updates an
**assumed** view, never a confirmed report. Failures mark uncertainty and cannot
confirm the requested value. Reports are observations from queries, not optical
measurements.

Polling queries actual level, active representation, control and actual RGBW
selectors. Zero is valid; MASK and absent answers are not converted to zero.
Only normalised RGBW state can be interpreted. Incompatible representations keep
colour unknown; establishing RGBW requires a complete tuple with explicit
brightness. Partial edits read freshly while holding the same transaction lock.

The driver's original HA decomposition is not uniquely recoverable:

* If the raw channels and effective arc level match our usable/requested
  settings, retain the exact caller decomposition, including byte quantisation.
* Otherwise discard the old request. Prefer raw channel levels with an inferred
  master brightness when the actual arc does not exceed their peak.
* If the actual arc exceeds that peak, use a full-scale mix and actual arc as
  the canonical master. A partial colour edit is rejected if literal MASK for
  untouched channels cannot preserve that representation; send a complete RGBW
  tuple and brightness to establish it explicitly. Nothing guesses lost intent.

Group values are common only when members agree; group on means any member on.
W can agree even when RGB differs. Group state stays assumed. Short/group polls
share the physical refresh path and reuse a complete confirmed report for up to
two seconds; commands always read freshly. The number has no separate poll loop.

State restoration is session-local. Reload/restart discards pending W and
unconfirmed intent, rediscovers capabilities and imports actual driver state.
While off, last master brightness is not generally recoverable from actual-level
zero alone; without a retained setting, subsequent on uses master 255. This is
explicitly a fallback, not a claim to have recovered pre-restart intent.

## Transport, concurrency and lifecycle

All raw-frame services, scenes, light actions and queries share one gateway lock.
Dependent sequences stop on the first failure, with no later ACTIVATE. JSON must
explicitly indicate success, no busy bus and no collision, even for writes.
Queries accept one byte or explicit `data: null` for no backward response. Zero,
MASK, null, rejected execution, busy, collision, malformed replies, HTTP errors,
timeouts and cancellation stay distinct. This strict reply contract must still
be checked against the installed firmware's no-answer and write responses.

Slider commands have one active sequence and one coalesced pending patch per
target. Later values replace pending values; independent RGB, W and brightness
edits merge. A complete tuple supersedes earlier pending component edits. On/off
wait for that target's accepted edits. General transport admission is bounded to
eight transactions and fails explicitly when full; there is no unlimited queue.
Cancelled UI waiters do not interrupt accepted commands. Unload cancels owned
workers, waits for an in-flight request to finish, aborts remaining frames, removes
entity listeners, drains the transport, and rejects subsequent work.

The lock excludes **only this integration**. It cannot exclude Matter, Cockpit,
firmware actions or another bus master. Shared DTRs and enable frames can still
be disturbed externally. Public documentation/current integration source did
not establish a supported gateway batching endpoint, so none was invented.
Sequential HTTP requests are not bus-atomic. There is no automatic transaction
retry and no rollback of already-applied members or frames.

## Compatibility, references and verification

No SmartCore firmware or Lunatone driver has been hardware-validated for this
new path. Historical firmware observations are not RGBW compatibility claims.
The Home Assistant API baseline for the integration tests is 2025.11.3 / Python
3.13; API source was inspected, but execution requires the real package.

Protocol constants and golden frames were independently checked using
[python-dali colour definitions at b1881c008aca0b269fd09b8834dd9bdc0f1a735b](https://github.com/sde1000/python-dali/blob/b1881c008aca0b269fd09b8834dd9bdc0f1a735b/dali/gear/colour.py)
and [general registers at that revision](https://github.com/sde1000/python-dali/blob/b1881c008aca0b269fd09b8834dd9bdc0f1a735b/dali/gear/general.py).
Only protocol facts were consulted; no LGPL implementation was copied or added
as a dependency. Expected frames are literal test data, not encoder output.

Other references:

* [Beckhoff variables, channel counts and control bits](https://infosys.beckhoff.com/content/1033/tcplclib_tc2_dali/1007747723.html)
* [Beckhoff WAF operation](https://infosys.beckhoff.com/content/1033/tcplclib_tc2_dali/1012110475.html)
* [Beckhoff colour queries and selectors](https://infosys.beckhoff.com/content/1033/tcplclib_tc2_dali/1009001483.html)
* IEC 62386-209:2011, clauses 9.1.2, 9.12.5 and 11.3.4: normalisation,
  linked/unlinked semantics, MASK and activation.
* [HA native light contract](https://contributors.home-assistant.io/core/entities/light/)
* [HA 2025.11.3 light action conversion](https://github.com/home-assistant/core/blob/2025.11.3/homeassistant/components/light/__init__.py)
* [Atios DALI interface settings](https://docs.atios.ch/smartcore/settings/system)

See [validation results](rgbw-validation.md) for exact executed commands and
[on-site checklist](rgbw-hardware-checklist.md) for deferred physical tests.

## Revert or remove

No firmware, addressing, groups, scenes or Matter configuration is changed.
To revert the code, restore the previous `custom_components/atios` directory and
reload/restart HA during a later authorised on-site maintenance session. Added
number entities may remain in the entity registry until removed. To remove the
integration entirely, remove its config entry under Devices & services, then
remove the custom component through HACS or delete its directory. Existing
Matter pairing is independent. The last runtime RGBWAF control selection can
remain active in the gear; changing code does not restore an earlier control
selection. Reconcile that with the normal bus controller during on-site testing.
