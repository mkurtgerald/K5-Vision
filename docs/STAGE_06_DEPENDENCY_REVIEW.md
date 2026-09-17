# Stage 06 Dependency Review

Status: **APPROVED FOR THE CURRENT STAGE 06 RTP LIVE-VIEW DELIVERY SURFACE.**

Stage 06 now exercises an actual ephemeral RTP delivery path from the accepted RTSP/UDP source through GStreamer to a K5-owned loopback UDP consumer boundary. It still adds no decoder, codec library, transcoder, recorder, persistence layer, browser transport, analytics runtime, UI toolkit, or new Python dependency.

## Exercised third-party surface

- GStreamer `1.28.7`
- `rtspsrc` from GStreamer Good Plug-ins, effective license `LGPL`
- `capsfilter` from GStreamer core elements, effective license `LGPL`
- `queue` from GStreamer core elements, effective license `LGPL`
- `udpsink` from GStreamer Good Plug-ins, effective license `LGPL`
- `fakesink` from GStreamer core elements remains part of earlier Stage-04/05 qualification paths, effective license `LGPL`
- psutil `7.2.2`, BSD-3-Clause, for bounded process-tree cleanup
- Pydantic, already present in the project dependency set, for source-free contract/evidence validation

The official GStreamer documentation identifies `rtspsrc` output as `application/x-rtp`, identifies `udpsink` as the UDP network sink in GStreamer Good Plug-ins, and identifies `capsfilter` as a GStreamer core element. GStreamer states that core code is LGPL and that Good Plug-ins use the project's preferred LGPL/LGPL-compatible licensing posture. The physical workflow verifies the exact runtime version and availability of each exercised element before qualification.

The governing baseline reviews remain `docs/STAGE_04_DEPENDENCY_REVIEW.md`, `docs/STAGE_03_GSTREAMER_REVIEW.md`, and `docs/STAGE_05_DEPENDENCY_REVIEW.md`, with distribution notices in `THIRD_PARTY_NOTICES.md`.

## Stage 06 delivery posture

The RTP relay is intentionally narrow:

- RTSP lower transport remains UDP.
- A caps constraint selects `application/x-rtp,media=video`; K5 does not decode or transcode the payload.
- `queue` is explicitly bounded to eight buffers and configured leaky downstream so a stalled K5 consumer cannot create unbounded GStreamer buffering.
- `udpsink` is restricted to `127.0.0.1` and an OS-assigned ephemeral port owned by K5.
- the Python receive socket has a bounded receive buffer and one-datagram receive allocation.
- packet payload exists only long enough to validate the RTP header and invoke the in-process consumer callback; K5 retains only counters and elapsed time after delivery.
- subprocess stdout/stderr remain discarded, and process-tree cleanup reuses the Stage-03/04 bounded psutil pattern.

No camera media, frames, clips, RTP payloads, RTSP URIs, camera addresses, credentials, or raw runner identity are permitted in retained Stage-06 evidence.

A change to the GStreamer version, element/plugin surface, Python dependency set, non-loopback delivery protocol, codec/decode surface, or distribution model requires another dependency review before acceptance.

This is an engineering dependency review, not legal advice.
