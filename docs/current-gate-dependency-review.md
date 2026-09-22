# Current Gate Dependency Review

Status: **APPROVED FOR EXACT-REVISION QUALIFICATION.**

This successor introduces no new third-party package, donor code, model, binary, external service, persisted storage location, network surface, customer filesystem decision, or licensing obligation. It reuses the accepted project-owned arbitrary viewport geometry, viewport editor, bounded undo/redo history, and application relayout boundary.

The change adds non-mutating undo/redo candidates to the accepted bounded history object and a project-owned transactional control boundary. A candidate layout is presented to the existing visible relayout boundary first; history state is committed only after that relayout succeeds. Rejected relayouts therefore leave the last accepted layout and undo/redo depths unchanged. History remains bounded by the existing maximum depth and operation budget, and divergent edits retain the previously accepted redo-invalidation behavior.

Retained state contains only logical viewport geometry and aggregate undo/redo/operation counts. It contains no camera/source identity, credentials, RTSP/network data, payload/media data, recording identity, private paths, native handles/pointers, runner identity, analytics values, or customer media. Downstream relayout exceptions are mapped to sanitized control failures without reflecting native or supplied private detail.

The accepted audit/security hardening already present on the branch base remains unchanged. This gate does not alter device authentication, ONVIF certificate verification, stream metadata sanitization, device enrollment, Neural authority, recording, playback, or evidence controls.

No new third-party license or distribution obligation is introduced. Existing dependency and provenance controls remain governing.

This is an engineering dependency review, not legal advice.
