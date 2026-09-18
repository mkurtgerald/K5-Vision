# Stage 23 Presentation Decoder Integration Review

Status: **APPROVED FOR EXACT-REVISION QUALIFICATION.**

Stage 23 introduces no new third-party package, GStreamer element, native library, native export, codec, renderer, UI toolkit, storage format, or external service.

## Inherited external surface

The runtime remains GStreamer `1.28.7` with the exact accepted Stage-20 decode pipeline:

`appsrc -> rtph264depay -> h264parse -> d3d11h264dec -> videoconvert -> BGRx appsink`

Stage 23 consumes only these native exports that were already part of the Stage-19 qualified core ABI:

- `gst_sample_get_caps`
- `gst_caps_get_structure`
- `gst_structure_get_int`
- the previously accepted buffer/sample map and appsink exports

The Stage-23 physical workflow requalifies the complete accepted native ABI on the exact candidate revision before camera qualification.

## Data handling

The decoder reads width and height from negotiated sample caps and derives packed BGRx stride from the bounded mapped buffer size. Frame payloads remain transient. Retained adapter state contains counters/lifecycle only; physical evidence may retain geometry, pixel format, byte counts and state, but no frame bytes or source identity.

Invalid/missing caps, invalid geometry, inconsistent stride, oversized frame buffers, malformed RTP, native failures and cleanup failures fail closed behind sanitized project-owned errors.

## Commercial/provenance conclusion

No new license surface is introduced. Existing GStreamer LGPL distribution controls remain governing. Any additional native symbol, GStreamer element, renderer/GPU interop dependency, image exporter or UI toolkit requires separate review before adoption.

This is an engineering dependency review, not legal advice.
