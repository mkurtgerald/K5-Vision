# Camera-setup roadmap

Status: **Planned**, requested by the owner on 2026-11-01. This is the next core
camera-setup workstream, ahead of advanced video-wall customization. It does not
establish support in the current installer.

Provide **Camera settings** for each enrolled camera, with Video, Recording and
Zones sections. Keep ONVIF the brand-neutral management interface and retain the
graphical connection flow. Capability reads, zone authoring and source-policy
work can proceed independently where their contracts permit.

| Item | Tracking | Release boundary |
| --- | --- | --- |
| Frame rate, codec and supported encoder parameters | [#351](https://github.com/mkurtgerald/K5-Vision/issues/351) | Device-supported options, verified readback and actual delivered video; target-camera-only reconnection. |
| Motion-recording on/off and continuous mode | [#352](https://github.com/mkurtgerald/K5-Vision/issues/352) | Independent trigger source, real capture behavior and honest recorder states. |
| Graphical inclusion/exclusion zones | [#353](https://github.com/mkurtgerald/K5-Vision/issues/353) | Correct image coordinates and demonstrated effects in the actual trigger/detection path. |

## Video parameters

- Read the selected ONVIF channel/profile, current codec, resolution and frame
  rate. Offer camera-reported options/ranges, including bitrate/keyframe controls
  where supported; distinguish unavailable controls and management permissions.
- Retain private profile, video-source and encoder-configuration identities.
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

Expose **Continuous / Motion-triggered** recording separately from master
**Pause / Resume**. Disabling motion mode returns an enabled recorder to
continuous recording; pausing stops new writes. Show Armed / Waiting for motion,
actively recording, paused, unavailable and storage-blocked states accurately.

Before implementing the trigger policy, confirm whether motion means general
scene movement, person detections or camera-reported ONVIF events. The working
proposal is general movement, including people and vehicles; the owner's choice
is pending. These sources must remain explicitly distinguished.

The current person detector samples growing recording files. It cannot wake an
idle recorder. Motion mode therefore requires independent live sampling,
verified camera events or a continuously populated bounded private cache.
Advertised pre-event capture requires actual buffered footage; a record switch
alone cannot supply it. Provide bounded pre/post-event settings and an explicit
trigger-loss policy before activation.

Persist recording intent separately from the effective gate. Reuse MediaMTX's
per-camera record-only control, isolate camera failures/retries, prioritize disk
reserve/quota protection and use monotonic hold durations. Keep original footage
UTC, playback/export behavior and recording on other cameras intact. Browser
closure must not disarm recording.

## Inclusion and exclusion zones

Provide an authorized, aspect-correct frozen camera frame with Draw, Finish,
Undo, Delete, Clear, Save and Cancel, plus keyboard operation. Store bounded
normalized polygons tied to camera/source identity, reference geometry,
orientation and a revision. Account for letterboxing, browser zoom, resize and
Windows DPI; revalidate or request redraw when the actual view changes.

Proposed trigger semantics: no inclusion polygons means the full frame;
inclusions are combined and exclusions subtract last. Reject invalid geometry,
make an all-excluded result visible and reject stale detector results after a
zone edit. The saved policy must affect the actual backend detector/recording
trigger, not only the browser overlay. Preview pixels supplied by a browser are
for authoring, not trusted motion evidence.

The working scope is detection/recording-trigger zones. Whether permanent
privacy masking is also required awaits owner confirmation. Ignoring motion in
a region does not obscure live, recorded or exported pixels. Any privacy-mask
feature needs a real camera/recording implementation and pixel-level output
qualification before it can claim redaction.

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
