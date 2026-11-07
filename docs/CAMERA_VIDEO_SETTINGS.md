# Camera video settings

These controls are implemented in the candidate source. Use the matching Windows
release notes and receipts to establish which installer includes them.

Select an ONVIF camera and choose **Camera settings**. Read its video settings,
change the supported values and choose **Apply**. K5 reads the device again,
checks for conflicts, applies the changes and reads the configuration back.
Only the selected camera reconnects; its identity and retained recordings stay
the same. A camera-management administrator without live-view permission can
open these controls from the camera list without opening video.

The camera determines which controls are available: frame-rate limit,
resolution, bitrate, keyframe interval, quality and encoding interval. Missing
capabilities leave the corresponding control unavailable. ONVIF Media1 and
Media2 are supported. Ambiguous profile/channel identity prevents a write rather
than selecting a different stream from its name or position.

**H.264 is the writable product codec.** A camera may advertise H.265 or MJPEG,
but those choices require qualified K5 viewing, recording, playback and export
before K5 can enable them. If another tool has changed the selected encoder to
an unsupported codec, K5 can offer H.264 restoration when the device's reported
options permit it. K5 preserves unrelated encoder settings. If other profiles
share that encoder, applying a change requires explicit acknowledgement of the
shared impact.

Use the enrolled camera account first. If it cannot manage video settings, use
the temporary management-account fields and read again. Temporary credentials
are not saved as recording credentials. A direct RTSP connection needs an
ONVIF management connection before K5 can configure the camera; the Video
dialog does not guess a vendor-specific control URL.

Configuration readback and stream verification are separate. A successful
readback means the camera reports the requested configuration. K5 then checks
the reconnected H.264 stream for continuing incoming bytes. It does not claim
that a configured frame-rate limit is measured delivered FPS; that value remains
unmeasured. A successful configuration can still leave the stream connecting or
unavailable. Recording status depends on actual recorder progress.

An Apply handle is short-lived and usable once. After expiry, a conflicting
change or an uncertain outcome, read the settings again. If a camera reply is
lost after a write might have reached it, K5 reports an unverified outcome and
reacquires the saved stream. It does not automatically repeat the camera write
or claim that the camera was rolled back. Confirm the readback before deciding
whether to apply another change.

The recording Pause/Resume controls remain available. Motion-triggered recording
and graphical inclusion/exclusion zones are tracked in the
[camera-setup roadmap](CAMERA_SETUP_ROADMAP.md); this increment does not implement
them. Detection zones, when added, do not establish permanent privacy masking.

The wall's **Auto** layout uses one tile for one authorized enrolled camera,
four for two through four, and nine for five or more. Larger inventories use
paging. Offline enrolled cameras count so an outage does not rearrange the
wall. Manual 1/4/9 choices remain selected during inventory updates. This does
not add custom tile resizing or main/substream switching, and inventory limits
are not simultaneous decoder-capacity claims.
