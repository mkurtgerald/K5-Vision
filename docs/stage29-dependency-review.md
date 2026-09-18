# Stage 29 Renderer-Neutral Viewport Dispatch Review

Status: **APPROVED FOR EXACT-REVISION QUALIFICATION.**

Stage 29 introduces no new third-party package, GStreamer element, native library, native export, codec, renderer, UI toolkit, image encoder, storage format, external service, or network surface.

## Inherited surface

Physical qualification composes the accepted Stage-28 mixed live/playback presentation path. Live and playback children continue to terminate in the accepted BGRx `PresentationVideoFrame` boundary using the existing reviewed GStreamer H.264 decode surface.

Stage 29 adds only project-owned Python dispatch logic: logical slot-to-consumer routing, frame/byte/time bounds, sanitized failures, deterministic consumer-reference release, and aggregate payload/source/path/identifier-free counters.

## Data handling

The dispatcher does not cache or persist frame payloads. Each decoded frame is handed directly to the registered async viewport consumer and no payload reference is stored after that dispatch returns. Physical qualification creates one temporary camera-derived recording only inside private runner scratch; it is deleted before retained evidence is written.

Retained evidence is aggregate only and must not contain source connection data, camera media, decoded frame bytes, RTP payloads, storage identifiers, private storage paths, or runner identity.

## Commercial/provenance conclusion

No new license surface is introduced. Existing GStreamer LGPL distribution controls remain governing. Concrete renderer/UI-toolkit selection remains a later separately reviewed decision.

This is an engineering dependency review, not legal advice.
