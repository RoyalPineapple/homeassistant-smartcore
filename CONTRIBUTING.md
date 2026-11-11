# Contributing

Developer notes for working on the Atios SmartCore Home Assistant integration.

## Hardware bring-up checklist

Steps to run through when validating the integration against a physical
SmartCore. Steps 4 and 5 have since landed (light addresses are configurable via
the options flow, and the zeroconf match entries are in `manifest.json`) — they
are kept here as the verification procedure.

1. **Confirm the WS endpoint** — turn on **DALI IP Interface** (System → DALI in the
   SmartCore web UI; without it `ws://<host>/` connects but never streams
   `daliMonitor`). Start HA with debug logging for `custom_components.atios`;
   confirm `websocket connected`. If not, the native HTTP path still drives lights.
2. **Verify QUERY answers** — call `atios.send_dali_frame` with `data: [1, 160]`
   (`QUERY ACTUAL LEVEL` to address 0), `wait_for_answer: true`, and check the
   returned byte matches the fixture level.
3. **Confirm buttons** — run `python3 tools/monitor.py <smartcore-ip>` and press
   each push button (single / double / long). Device-scheme buttons decode
   directly to named gestures; if any print `gesture=?` (Device/Instance
   scheme), note the `info=` value and confirm it against `PUSHBUTTON_EVENTS`.
   In HA, each button auto-appears as an `event` entity on first press.
   `ws://<host>/` serves one client at a time, so while the script runs HA
   receives nothing; reload the integration afterwards.
4. **Wire real lights** — replace the broadcast-only list in `light.py` with the
   discovered short addresses (or add an options flow).
5. **mDNS** — the zeroconf flow is already wired. Confirm the SmartCore's real
   advertisement with `avahi-browse -rt _http._tcp.local` (also try `_dali._tcp`,
   `_atios._tcp`). Check the service type, the instance name, and the TXT keys
   (`uid`, `serial`, `device`, `manufacturer`, `type`). If it differs from the
   three match entries in `manifest.json`, adjust them; the `async_step_zeroconf`
   handler already tolerates missing/renamed TXT keys and falls back to the host.

## Tests

```bash
python3 -m pytest tests/
```
