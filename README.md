# Atios SmartCore Home Assistant integration

![Atios SmartCore: Matter on one side, DALI-2 bus, 12 inputs and 12 relay outputs on the other](images/smartcore-matter-dali-io.png)

The [Atios SmartCore](https://atios.ch/products/smartcore) is a Matter-certified
controller with 12 built-in IOs and a DALI-2 Master. It's powered by 24V DC or
via PoE, and also powers the DALI bus.

## Standard setup: Matter (this integration is not needed)

Because the SmartCore is a certified Matter device, it pairs directly with Home
Assistant. Most users therefore should follow the documentation provided with
the device, which in short is:

1. Visit [setup.atios.ch](https://setup.atios.ch/) to find your SmartCore on
   your local network, and connect to its local web interface
   ([see online demo here](https://r12.atios.ch/app/)).
2. Create devices in the **Accessory Manager**, e.g. a light bulb from one
   input and one output.
3. DALI devices: scan the bus in the **DALI Configurator** tab.
4. Back in the Accessory Manager, use DALI single addresses or groups as
   outputs, and DALI-2 sensors as inputs for any device type.
5. Pair the SmartCore with Home Assistant via **Matter**. Every light, blind
   and sensor from the Accessory Manager shows up as its own entity.

## Who this is for

This integration is for expert users who want **raw DALI access** from Home
Assistant: send and receive arbitrary DALI commands, for example to process
live DALI-2 events from sensors that the SmartCore and its Matter integration
do not support yet.

It ships with examples for controlling lights (control gear), reading DALI-2
sensors (control devices), a general DALI monitor, DALI scene recall, a
firmware update entity, and the SmartCore web UI as a sidebar panel. Fully
local (push over LAN), no cloud, no external Python dependencies. Works
standalone or alongside a Matter-paired SmartCore.

## Installation

**Step 1: Install HACS** (skip if you already have it)

1. Go to **Settings → Apps → App store**, open the **⋮** menu (top right) →
   **Repositories**, and add `https://github.com/hacs/addons`. Then install
   the **Get HACS** app and press **Start**.
2. Restart Home Assistant (**Settings → System → Restart**).
3. Go to **Settings → Devices & services**, click **Add integration**
   (bottom right), search for **HACS**, and follow the GitHub sign-in
   (a free GitHub account is required; you'll enter a short code at
   github.com/login/device).

**Step 2: Install the Atios SmartCore integration**

4. Search for **Atios SmartCore** in HACS, click **Download**, and restart
   Home Assistant once more.

   [![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Atios-AG&repository=homeassistant-smartcore&category=integration)

5. Go to **Settings → Devices & services**, click **Add integration**
   (bottom right), select **Atios SmartCore**, and enter your SmartCore's
   IP address. Done.

   [![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=atios)

6. **For buttons and sensors:** in the SmartCore web interface, go to
   **System → DALI** and turn on **DALI IP Interface**. Without it the
   SmartCore sends no DALI bus events to Home Assistant (lights still work).
   The integration raises a repair notice while it is off.

   SmartCore firmware 3.2.2 (built on ESP-IDF v5.5.5/v6.0.1+) sends no events at all on
   this interface, whatever the setting: its websocket never registers
   clients. The integration detects this and raises a repair notice; it needs
   a SmartCore firmware with the fix.

We plan to submit this integration to Home Assistant core in a bit.

## Status

| Piece | State |
|---|---|
| DALI frame encode/decode (`dali.py`) | ✅ verified against reference frames (`FF 10` goto-scene, `01 91` query-gear-present) |
| Transport (`hub.py`) | ✅ native aiohttp; HTTP `/api/dali/iface` confirmed on device; `daliMonitor` stream on `ws://<host>/` (needs **DALI IP Interface** on) |
| Broadcast light + brightness/on/off | ✅ |
| Native DT8 RGBW + independent White level | Implemented locally; software tests and hardware-validation status in [RGBW documentation](docs/rgbw.md) |
| NVRAM device model (`nvram.py`) | ✅ reads `/api/dali/nvm` (control + input devices, paginated); parser verified against captured fixtures |
| Named lights from NVRAM | ✅ every configured short address and group becomes a named light; legacy options list as fallback |
| QUERY status read | ✅ HTTP answer shape confirmed on device (`{success,bus_busy,collision_detected,data}`) |
| `send_dali_frame` / `recall_scene` services | ✅ |
| DALI-2 event decode (`dali.py`) | ✅ 62386-103/-301, verified byte-for-byte vs python-dali on 585 frames |
| Button events (`event.py`) | ✅ pre-created from the NVRAM model with configured names; unknown buttons still auto-appear on first press |
| Occupancy sensors (62386-303) | 🟨 entities from NVRAM, state from bus events; bit mapping still to be verified on hardware |
| Light sensors (62386-304) | 🟨 entities from NVRAM, raw 10-bit level; lux conversion pending hardware verification |
| Zeroconf discovery | 🟨 flow built; confirm the SmartCore's actual mDNS/TXT on device and tighten the manifest |
| Firmware `update` entity | ✅ installed version from `/ota_status`; latest-version source still needed for update *notifications* |
| Web-UI iframe panel | ✅ sidebar panel via options flow; probes for X-Frame-Options / CSP / mixed-content and warns |

Existing baseline features were confirmed on SmartCore firmware 3.2.3. The new
RGBW path has **not** been validated on SmartCore or Lunatone hardware.

## RGBW and physical white control

Verified-capable four-channel DT8 RGBW targets receive a native RGBW light and a
separate **White level** slider. RGB edits preserve W; white edits preserve RGB;
brightness is independent. Both entities share one controller and driver reports.
DT6, tunable-white-only, unknown and mixed targets stay brightness-only.

See [RGBW semantics, limitations and removal](docs/rgbw.md),
[local validation](docs/rgbw-validation.md), and the
[deferred hardware checklist](docs/rgbw-hardware-checklist.md).

## Contributing

Developer notes and the hardware bring-up checklist live in
[CONTRIBUTING.md](CONTRIBUTING.md).
