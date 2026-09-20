# Current Gate Dependency Review

Status: **APPROVED FOR EXACT-REVISION QUALIFICATION.**

This successor introduces no new third-party package, donor code, model, binary, external service, persisted format, network surface, or platform API. It hardens the accepted project-owned source-free Win32 pointer interaction boundary with the operating system's existing mouse-capture lifecycle and reuses the accepted serialized viewport-edit control.

A left-button drag is captured to the already-owned visible operator shell only after the initial bounded client-relative input is accepted. Capture ownership is verified through the existing Win32 user32 surface; release, capture-loss, cancel-mode, repeated-down, and close paths deterministically terminate ephemeral drag state. A completed release continues through the accepted move/resize edit queue against the latest accepted arbitrary `ViewportLayout`; cancellation publishes no candidate geometry. Sparse slots through 4095, overlap/non-grid geometry, z-order, source/media bindings, and presentation generation remain unchanged. Existing message, input, control-queue, controls-per-cycle, geometry, and session limits remain governing resource bounds.

Retained control state contains only accepted viewport geometry/logical slots and aggregate replacement/relayout/edit/completed-interaction/cancelled-interaction/stop counters. Native capture state and pointer coordinates/traces are ephemeral and are not retained. No camera source, credential, private path, payload, recording identity, native handle/pointer value, or runner identity is added to retained observability.

No new third-party license or distribution obligation is introduced. Existing dependency and provenance controls remain governing.

This is an engineering dependency review, not legal advice.
