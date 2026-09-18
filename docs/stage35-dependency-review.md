# Stage 35 Windows Presentation Surface Review

Status: **APPROVED FOR EXACT-REVISION QUALIFICATION.**

Stage 35 introduces no new Python package, UI toolkit, codec runtime, renderer library, external service, storage format, or network surface.

## Native surface

The only new runtime surface is the operating-system-provided Windows GDI API loaded through Python standard-library `ctypes`:

- `gdi32!CreateDIBSection`
- `gdi32!DeleteObject`
- process-local memory copy through `ctypes.memmove`

The surface is a top-down 32-bit `BI_RGB` DIB compatible with the already accepted project-owned BGRx `PresentationVideoFrame` contract. Native handles and pointers are private implementation details; they are never returned to callers, snapshots, exceptions, or retained qualification evidence.

## Data handling

Presentation bytes are copied only into an in-memory DIB section for active presentation. Stage 35 does not write frames, clips, RTP payloads, source URIs, credentials, or native surface memory to disk or artifacts. Surface memory is released deterministically on close, replacement, or failure. Retained observability is limited to state, bounded geometry, counters, replacement count, and source-relative timing span.

Physical qualification consumes real camera-derived presentation frames through the already accepted live RTP/H.264 decode path, copies them into the private DIB surface, then releases the surface before evidence is retained. Evidence contains no media bytes, camera/source identity, credentials, storage paths, GDI handles/pointers, or runner identity.

## Commercial/provenance conclusion

No new third-party license obligation is introduced by this stage. Windows GDI is an operating-system API. Existing GStreamer LGPL distribution controls remain governing for the inherited media/decode surface. Concrete application-window chrome, layout controls, and UI toolkit selection remain separately gated.

This is an engineering dependency review, not legal advice.
