# Deferred on-site RGBW validation

These checks have **not** been executed. Do not run them automatically.

1. Back up HA and export Atios configuration. Identify the intended HA instance;
   a historical VM number or address alone is not sufficient identification.
2. Record current HA, SmartCore and driver firmware versions, actual short
   addresses, feature bytes, assigned colours, current control and representation.
   Confirm group membership. Historical group 0 contained address 5, not
   necessarily address 0.
3. Coordinate monitor ownership before capturing Cockpit's known-working W
   slider. The emulated Lunatone websocket may support only one monitor client;
   do not silently take HA's stream. Record the actual trace and device settings.
4. Compare the capture with implemented control, RGB/WAF staging, MASK fields,
   enables, ACTIVATE and DAPC. The research example is not a captured trace.
5. Check actual `/api/dali/iface` JSON for successful writes, zero replies,
   missing replies, busy and collisions. Confirm how firmware represents no
   backward answer. Confirm sequential request timing and enable placement.
6. Select one explicitly identified short address before any group testing.
   Start with a complete RGBW tuple and explicit brightness. Check that control
   `0x80` is implemented as expected, without changing commissioned driver mode.
7. Move W through 0, midpoint and 255 while RGB is nonzero. Observe physical RGB
   preservation, correct W, maximum not MASK and zero not preservation. Also
   test W-only low/mid/full tuples with master brightness varied independently.
8. Change RGB using HA's action, colour wheel, HS and XY paths. Confirm physical
   W preservation. A four-channel tuple with W=0 must actually remove W.
9. Check master brightness, all-zero tuples, off/on restoration, pending W while
   off, low-level rounding, fade behaviour and any ACTIVATE/DAPC transient.
10. Change colour/white externally using Cockpit and normal Matter controls.
    Verify readback, canonical decomposition, failure of incompatible partial
    edits, and recovery using an explicit full tuple. Do not presume the earlier
    colour-temperature darkness symptom has been diagnosed.
11. Exercise busy/collision/error responses, cancellation, rapid dragging,
    reload/unload and restart. Confirm no false confirmation after failures.
    Pending off-state W is intentionally not persistent across restart/reload.
12. Test only verified groups, including different member RGB and W values.
    Confirm common-value reporting and independent preservation per member.
13. Check normal coexistence with Matter and other controllers. Integration
    locking cannot protect shared registers from those independent bus masters.
14. Record results and any firmware-specific corrections. Remove “untested”
    qualifications only for the exact firmware/driver combinations observed.
