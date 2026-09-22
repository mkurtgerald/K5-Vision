# Current Gate Dependency Review

Status: **APPROVED FOR EXACT-REVISION QUALIFICATION.**

Issue #207 introduces no new third-party package, donor code, model, binary, external service, persisted storage location, network surface, customer filesystem decision, or licensing obligation. It reuses the accepted project-owned arbitrary viewport geometry, viewport editor, bounded undo/redo history, transactional history control, live Windows operator-control queue, selection/catalog chain, and application relayout boundary.

The successor wires the already-accepted transactional undo/redo boundary into the running Windows operator path. Incremental viewport edits, undo, and redo are serialized through the existing bounded live-control queue. Explicit replace or relayout establishes a new accepted history baseline and clears stale undo/redo branches rather than allowing an old edit chain to cross a new layout decision. History depth and lifetime operation count remain bounded.

The operation budget is now preflighted before a visible relayout side effect. If the history operation budget is exhausted, the request fails before the application is relaid out, preventing visible state from diverging from committed history. Application relayout rejection continues to leave the last accepted history state unchanged.

The existing selectable/catalog operator path now inherits the bounded history-enabled control so the capability reaches the product-facing Windows operator chain without duplicating media delivery, recording, playback, device, or source authority. No camera credential or source URI is introduced into viewport history. Retained history observability contains only logical viewport geometry and aggregate counts for undo depth, redo depth, operations, requests, and rebases.

Retained state contains no camera/source identity, credentials, RTSP/network data, payload/media data, recording identity, private paths, native handles/pointers, runner identity, analytics values, or customer media. Downstream relayout exceptions are mapped to sanitized control failures without reflecting native or supplied private detail.

The accepted audit/security hardening already present on the branch base remains unchanged. This gate does not alter device authentication, ONVIF certificate verification, stream metadata sanitization, authenticated device authority, recording, playback, evidence controls, analytics, or Neural authority.

No new third-party license or distribution obligation is introduced. Existing dependency and provenance controls remain governing.

This is an engineering dependency review, not legal advice.
