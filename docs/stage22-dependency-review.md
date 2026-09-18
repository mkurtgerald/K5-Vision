# Stage 22 Presentation Frame Contract Review

Status: **APPROVED FOR THE ACTIVE GATE.**

Stage 22 adds a project-owned renderer-ready frame contract only. It introduces no third-party package, native export, codec, renderer, UI toolkit, storage format, or external service.

## Contract

The accepted contract is deliberately narrow:

- transient `memoryview` payload; no frame copy is required by the boundary
- bounded width and height
- bounded packed-row stride
- fixed `BGRx` pixel format matching the accepted Stage-20 decoder output surface
- bounded source-relative timing
- payload length must exactly match `stride_bytes * height`
- retained metadata contains geometry, timing, format and byte count only

## Privacy and evidence

Frame payloads are never serialized into metadata. The contract contains no camera/source identifier, URI, address, credential, file/storage path, host identity, or runner identity.

## Runtime and provenance

This stage uses Python standard-library dataclasses plus the project's existing Pydantic dependency. The external runtime remains unchanged. The already-qualified GStreamer caps/native surface is not exercised or expanded by this contract-only gate; concrete decoder population of the presentation metadata remains the immediate downstream integration step.

Any renderer, UI toolkit, image codec/export component, GPU interop layer, or new native symbol requires separate review before adoption.

This is an engineering dependency review, not legal advice.
