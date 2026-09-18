# Stage 28 Mixed Live/Playback Presentation Review

Status: **APPROVED FOR EXACT-REVISION QUALIFICATION.**

Stage 28 introduces no new third-party package, GStreamer element, native library, native export, codec, renderer, UI toolkit, storage format, external service, or network surface.

## Inherited external surface

The live child is the accepted Stage-25 `BoundedLivePresentationDelivery`. The playback child is the accepted Stage-24 `BoundedPresentationPlaybackDelivery`. Both terminate in the accepted presentation-ready BGRx frame boundary and use the already reviewed GStreamer H.264 decode surface.

Stage 28 adds only project-owned Python coordination for a mixed set of live and playback children: unique logical slots, bounded concurrency, serialized frame consumption, aggregate frame/byte/time limits, sibling cancellation, and source/path/identifier-free aggregate lifecycle/counters.

## Data handling

Physical qualification creates one temporary camera-derived recording only inside private runner scratch so one accepted playback child can run concurrently with one accepted live child. The temporary recording is deleted before retained evidence is written. Decoded BGRx payloads remain transient inside accepted child boundaries and the caller-provided consumer.

Retained evidence is aggregate only and must not contain source connection data, camera media, decoded frame bytes, RTP payloads, storage identifiers, private storage paths, or runner identity.

## Commercial/provenance conclusion

No new license surface is introduced. Existing GStreamer LGPL distribution controls remain governing. Concrete renderer/UI-toolkit selection remains a later separately reviewed decision.

This is an engineering dependency review, not legal advice.
