# Stage 20 Concrete Decoder Adapter Review

Status: **APPROVED FOR EXACT-REVISION QUALIFICATION.**

Stage 20 adds no third-party Python package, codec runtime, or rendering dependency. The concrete playback decoder reuses only the already accepted isolated GStreamer `1.28.7` runtime and the Stage-19 qualified native C ABI.

## Runtime surface

The decoder pipeline is intentionally limited to the Stage-18 accepted elements:

- `appsrc`
- `rtph264depay`
- `h264parse`
- `d3d11h264dec`
- `videoconvert`
- `appsink`

Native calls remain inside the Stage-19 reviewed GStreamer core/GstApp export allowlist. Python standard-library `ctypes` is used for the bridge; no Python/GObject binding is introduced.

## Data handling

RTP packet bytes exist only for the bounded native push operation. Decoded BGRx frame bytes exist only long enough to cross the existing project-owned `DecodedVideoFrame` consumer boundary. Adapter state retains counters and lifecycle only. No packet, frame, source URI, address, credential, runtime path, storage path, or raw runner identity is retained as evidence.

## Commercial/provenance conclusion

No new externally distributed component is adopted. The governing external runtime remains the exact Stage-18/19 reviewed GStreamer `1.28.7` surface and existing LGPL distribution posture. Any later addition of another codec, decoder, binding package, renderer, or GStreamer element requires separate provenance/commercial review before acceptance.

This is an engineering dependency review, not legal advice.
