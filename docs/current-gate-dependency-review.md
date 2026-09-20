# Current Gate Dependency Review

Status: **APPROVED FOR EXACT-REVISION QUALIFICATION.**

This successor introduces no new third-party package, donor code, model, binary, external service, persisted format, network surface, or platform API. It adds a project-owned source-free pointer interaction boundary on the already-accepted visible Win32 shell and routes completed interactions into the accepted serialized viewport-edit control.

Native input capture is limited to bounded left-button/move events already present in the Win32 message pump. Client-relative coordinates are normalized to the existing shell coordinate space and immediately reduced to one logical-slot move or resize edit after deterministic hit testing against the latest accepted arbitrary `ViewportLayout`. Overlap is resolved by existing z-order, sparse slots/non-grid geometry remain valid, and the accepted relayout path continues to preserve source/media bindings and presentation generation. Existing message, input, control-queue, controls-per-cycle, geometry, and session limits remain the resource boundaries; overflow or invalid geometry fails closed before unaccepted geometry is published.

Retained control state contains only accepted viewport geometry/logical slots and aggregate replacement/relayout/edit/interaction/stop counters. Pointer event coordinates and traces are ephemeral and are not retained. No camera source, credential, private path, payload, recording identity, native handle/pointer value, or runner identity is added to retained observability.

No new third-party license or distribution obligation is introduced. Existing dependency and provenance controls remain governing.

This is an engineering dependency review, not legal advice.
