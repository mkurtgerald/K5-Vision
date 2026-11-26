# Camera-setup roadmap

Status: **Video controls, motion recording and trigger zones are implemented in
the candidate source; combined native Windows installer qualification is
pending.** Requested by
the owner on 2026-11-01. Camera setup takes priority over advanced video-wall
customization. Source changes do not establish support in a released installer.

Use **Camera settings** for ONVIF video parameters and **Recording and zones**
for recorder behavior and trigger regions. Keep ONVIF the brand-neutral
management interface and retain the graphical connection flow.

| Item | Tracking | Release boundary |
| --- | --- | --- |
| Frame rate, codec and supported encoder parameters | [#351](https://github.com/mkurtgerald/K5-Vision/issues/351) | Candidate Video controls implemented with H.264-only writes; native and representative physical-camera qualification remain release/field gates. See the [control guide](CAMERA_VIDEO_SETTINGS.md). |
| Motion-recording on/off and continuous mode | [#352](https://github.com/mkurtgerald/K5-Vision/issues/352) | Candidate source implemented with independent live sampling, per-camera record gating, saved thresholds, monotonic post-capture and zero pre-capture. Native Windows qualification remains required. See the [operator guide](CAMERA_MOTION_RECORDING.md). |
| Graphical inclusion/exclusion zones | [#353](https://github.com/mkurtgerald/K5-Vision/issues/353) | Candidate source implemented with authorized frozen-frame drawing, normalized source-bound polygons and backend trigger enforcement. Native Windows and representative camera qualification remain required. |

## Video parameters

- Read the selected ONVIF channel/profile, current codec, resolution and frame
  rate. Offer camera-reported options/ranges, including bitrate/keyframe controls
  where supported; distinguish unavailable controls and management permissions.
- Bind each short-lived private inspection to its exact profile, video-source
  and encoder-configuration identities; re-read before writing.
  Never select a channel from profile names, resolution or list position alone.
- Account for Media1 encoding interval and Media2 fractional/discrete rates.
  Distinguish a requested/configured limit from measured delivered FPS.
- Preserve unrelated encoder settings, detect shared configurations, and show
  affected profiles before Apply. Re-read for conflicts, write bounded changes,
  read back, obtain a fresh URI and verify actual video from the affected stream.
- Preserve the existing stream account when temporary management credentials are
  supplied. Clear temporary credentials after the operation and retain the
  camera UUID, recording identity and historical footage.
- Keep camera capability separate from K5 codec compatibility. The current
  product accepts H.264; H.265/MJPEG choices need qualified display, recording,
  playback and export paths before they become writable product options.
- A direct-RTSP-only source needs a valid ONVIF management connection for device
  configuration. A timeout after dispatch must report an uncertain outcome,
  rather than claim the camera was unchanged or blindly repeat the write.

Reuse the current bounded ONVIF client, Digest/clock handling and management
credential patterns, with the actual Media1/Media2 operations. A camera encoder
change must not restart every camera's media paths.

## Recording behavior

The candidate exposes **Continuous / Motion-triggered** recording separately from master
**Pause / Resume**. Disabling motion mode returns an enabled recorder to
continuous recording; pausing stops new writes. Show Armed / Waiting for motion,
actively recording, paused, unavailable and storage-blocked states accurately.

The candidate detects general scene movement, including changes caused by
people or vehicles. Person detections and camera-reported ONVIF events remain
separate sources and do not silently substitute for this motion trigger.

The person detector samples growing recording files and cannot wake an idle
recorder. The motion worker instead samples the private live relay independently
and applies the saved inclusion/exclusion mask before opening a camera's record
gate. Unavailable or stale inputs fail closed. Post-capture is bounded to
1–120 seconds and uses monotonic elapsed time. Pre-capture is explicitly zero;
adding it requires a real bounded buffer and separate footage qualification.

Persist recording intent separately from the effective gate. Reuse MediaMTX's
per-camera record-only control, isolate camera failures/retries, prioritize disk
reserve/quota protection and use monotonic hold durations. Keep original footage
UTC, playback/export behavior and recording on other cameras intact. Browser
closure must not disarm recording.

## Inclusion and exclusion zones

The candidate provides an authorized, aspect-correct frozen camera frame with Draw, Finish,
Undo, Delete, Clear, Save and Cancel, plus keyboard operation. Store bounded
normalized polygons tied to camera/source identity, reference geometry,
orientation and a revision. Account for letterboxing, browser zoom, resize and
Windows DPI; revalidate or request redraw when the actual view changes.

Trigger semantics: no inclusion polygons means the full frame;
inclusions are combined and exclusions subtract last. Reject invalid geometry,
make an all-excluded result visible and reject stale detector results after a
zone edit. The saved policy must affect the actual backend detector/recording
trigger, not only the browser overlay. Preview pixels supplied by a browser are
for authoring, not trusted motion evidence.

The implemented scope is recording-trigger zones. Ignoring motion in a region
does not obscure live, recorded or exported pixels. Permanent privacy masking
remains a separate future feature requiring actual camera/recording processing
and pixel-level output qualification.

Use current camera/recording/analytics permissions according to each control's
effect. A frame preview additionally requires live-view permission and current
camera access. Retain private credentials, revision checks, delayed-response
guards and operational auditing.

## Acceptance and reuse

Require actual device-option/readback and decoded-video tests, real
idle/motion/post-capture behavior, inside/outside/overlapping zone activity,
restart persistence, permission revocation, stale settings, unavailable inputs,
partial writes and per-camera failure isolation. Include excluded-only motion
and boundary crossings after detector mask/contour processing. Preserve footage hashes,
recording growth, original event/footage UTC, playback/export and the actual
Windows +/-48-hour clock qualification. Match each feature claim to its exact
native Windows installer evidence and representative physical-camera checks.

Prefer the existing ONVIF client, native MediaMTX worker, bounded worker
lifecycle and installed OpenCV/NumPy capabilities. Evaluate continuous-decode,
short-cache and selective-retention donor architecture where useful. Additional
donor code or dependencies still require provenance and licensing review.

The same private source/profile model should support the planned
[adaptive wall display streams](VIDEO_WALL_ROADMAP.md#3-adaptive-display-streams).
