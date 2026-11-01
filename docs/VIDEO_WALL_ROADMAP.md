# Video-wall roadmap

Status: **Planned**, requested by the owner on 2026-11-01. These are tracked work
items; this document does not establish installer support.

The released baseline is
[`34a77a5`](https://github.com/mkurtgerald/K5-Vision/releases/tag/k5-windows-admin-integrations-34a77a5).
It provides manual 1/4/9-camera layouts, paging, responsive tile sizing,
aspect-ratio preservation and individual fullscreen. Automatic layout selection,
individual tile editing and tile-size-driven stream switching remain planned.

## Delivery order

| Order | Item | Target and prerequisites |
| --- | --- | --- |
| 1 | [Automatic layout selection — #347](https://github.com/mkurtgerald/K5-Vision/issues/347) | Next wall-usability increment; reuse the current authorized inventory, presets, paging and player lifecycle. |
| 2 | [Reorder, resize and saved layouts — #348](https://github.com/mkurtgerald/K5-Vision/issues/348) | Following customization increment, once the Auto/manual layout contract and stable camera-to-slot mapping are established. |
| 3 | [Adaptive main/preview display streams — #349](https://github.com/mkurtgerald/K5-Vision/issues/349) | Investigate ONVIF capabilities alongside item 1; release switching after paired-source persistence, transport, authorization and capacity qualification. |

These are sequencing targets, not calendar commitments. Profile discovery and
its transport work can proceed independently of browser layout editing. Stream
adaptation can ship before custom resizing if measured wall capacity makes it
the more urgent product need; it must still meet its own acceptance criteria.

## 1. Automatic layouts

Add an **Auto** choice beside the existing manual layouts. Initially use the
existing presets: 0 cameras shows the empty state, 1 selects a one-slot layout,
2–4 selects a four-slot layout, and 5–9 selects a nine-slot layout. Larger
inventories use paging within the qualified visible-camera limit. Count
authorized enrolled cameras, including
offline cameras, so an outage does not rearrange the operator's wall.

Acceptance:

- Exercise 0/1/2/4/5/9/10/64 authorized inventory boundaries, enrollment, removal
  and permission changes. Inventory bounds do not establish simultaneous stream
  capacity.
- Preserve manual overrides, camera identity, the current page where possible,
  aspect ratio and fullscreen across ordinary polling and window resizing.
- Keep existing players for unchanged visible camera IDs and release players
  that leave the visible page.
- Prove usable controls at Windows DPI/browser zoom settings, with continuing
  recording growth and unchanged retained playback and exports.

## 2. Reorder, resize and saved layouts

Provide pointer and keyboard controls to move cameras and resize individual
tiles, plus Save, Reset and cancel/undo behavior. Layout preferences must be
bounded, versioned and isolated per user/site. Ordinary polls and reconnects
must preserve the saved arrangement.

Review the existing renderer-neutral
[geometry](../src/k5vision/media/viewport_geometry.py),
[editor](../src/k5vision/media/viewport_editor.py),
[edit history](../src/k5vision/media/viewport_editor_history.py),
[projection](../src/k5vision/media/viewport_client_projection.py) and
[catalog](../src/k5vision/media/viewport_catalog.py) contracts for reuse. Their
accepted behavior is a starting point for a thin browser adapter; the older
Win32 presentation/control implementation is not an implemented browser wall.

Acceptance:

- Reorder and resize through mouse and keyboard; exercise cancellation, capture
  loss, undo/reset, minimum sizes, bounds and the chosen snap/collision policy.
- Bind stable camera UUIDs to logical slots. A removed or revoked camera must
  trigger explicit repair/invalidation, never silently map its slot to another
  camera. Existing catalog restore requires identical slot sets, so reconciliation
  needs deliberate handling.
- Restore a saved layout after browser and native-app restart; validate malformed
  data, unsupported versions, sparse slots and cross-user/site isolation.
- Preserve camera labels, decoded video identity and authorization. Preferences
  contain no media URLs, credentials or session tokens.
- Preserve players for geometry-only edits, visible controls at Windows DPI
  sizes, background recording, original UTC, playback and exports.

## 3. Adaptive display streams

Keep the recording source fixed while choosing a supported live-display profile
from tile pixel size/DPI and fullscreen state. Provide **Auto / Main / Sub**
controls with bounded switching and hysteresis.

Current ONVIF profile metadata is temporary: enrollment stores one source per
camera and the media session exposes one live URL. First retain validated
main/preview choices, grouped by the same ONVIF video-source/channel identity.
Profile names or resolution alone cannot establish pairing on a multi-sensor
camera or NVR. Add a separately authorized, non-recorded preview binding and
review existing MediaMTX stream-token primitives, source persistence, admission
and path bounds for reuse. Recording has priority over display connections.

Acceptance:

- Distinguishable main/preview streams prove actual source switching on resize
  and fullscreen, rather than CSS scaling alone. Measure CPU, memory, bandwidth
  and decoder capacity at the intended wall size.
- Cover single-profile cameras, unsupported preview codecs, different-channel
  profiles, connection limits, stale completions, reconnects and timeouts. Keep a
  working display/fallback without repeated switching or hidden player growth.
- Keep ONVIF brand-neutral. For direct RTSP enrollment, an optional preview must
  be supplied explicitly in the GUI; never guess vendor/model URL patterns.
- Enforce current camera permissions for both variants and keep source URLs and
  credentials private. Release unused off-page previews.
- Preserve the recording source/resolution, analytics input, recording growth,
  retained-file hashes, footage/event UTC, playback and exports while switching.
- Complete representative physical ONVIF profile checks and matching native
  Windows GUI/clock qualification. Discovery alone is not compatibility proof.

## Release criteria

Each item receives its own implementation and review. Before advertising it in
an installer, require exact-source Windows GUI evidence for its interactions,
applicable failure/recovery and authorization tests, and existing recording,
playback/export and actual +/-48-hour clock-regression checks. End users install
and operate through the native EXE and graphical controls.

Retain the existing [capacity/endurance and field checks](WINDOWS_BETA_FIELD_TEST.md).
Adding grid options does not increase qualified decoder capacity. Any new donor
code or dependency still follows the normal dependency and attribution review.
