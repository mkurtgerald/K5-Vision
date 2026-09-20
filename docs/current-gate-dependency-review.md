# Current Gate Dependency Review

Status: **APPROVED FOR EXACT-REVISION QUALIFICATION.**

This successor introduces no new third-party package, donor code, model, binary, external service, persisted format, network surface, or platform API. It changes only project-owned Windows viewport coordination so an already-active logical-slot set can accept arbitrary x/y/width/height/z geometry changes without changing source/media bindings or rebuilding a fixed grid.

Relayout remains source-free: candidate state contains only `ViewportLayout` geometry/logical slots, while existing surfaces/bindings stay active. Replacement targets become authoritative only after the full candidate target set opens; slot-set changes are rejected, and relayout failure is sanitized and fails closed. No camera source, credential, private path, payload, native handle/pointer, or runner identity is added to retained observability.

No new third-party license or distribution obligation is introduced. Existing dependency and provenance controls remain governing.

This is an engineering dependency review, not legal advice.
