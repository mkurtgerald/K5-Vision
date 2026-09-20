# Current Gate Dependency Review

Status: **APPROVED FOR EXACT-REVISION QUALIFICATION.**

This successor introduces no new third-party package, donor code, model, binary, external service, persisted storage location, network surface, customer filesystem decision, or licensing obligation. It reuses the already accepted Win32/user32 reusable-view controls, overlap-safe z-order, fixed source-free feedback, command dispatch, and validated `ViewportCatalog` boundary.

The native surface adds only two bounded project-owned BUTTON children, `Prev` and `Next`, to the accepted reusable-view control row. Together with the previously accepted label/editor/Save/Apply/Delete/feedback children, the surface remains within the existing eight-control overlap-safe z-order ceiling. The selector receives only the canonical sorted occupancy of logical catalog view IDs 0 through 63. It never receives or displays camera/source identity, credentials, RTSP/network data, payload/media data, recording identity, private paths, native handles/pointers, runner identity, analytics values, or user-provided names.

Prev/Next updates only the already accepted numeric view-ID editor using the existing Win32 text boundary; it does not create, apply, delete, move, resize, normalize, or otherwise rewrite viewport geometry. Exact arbitrary/non-grid x/y/width/height/z geometry, sparse logical slots through 4095, media/source bindings, pointer/selection/stack state, and presentation generation remain governed by the accepted downstream command and relayout paths. Empty occupancy is rejected without reflecting private input. Native selector state and button handles remain ephemeral and are cleared during teardown.

The operator-control layer publishes only source-free catalog occupancy to the native selector and refreshes it after accepted catalog mutation. Retained observability remains aggregate-only; catalog view identifiers are not added to snapshots. Native selector/update failures map to existing sanitized application/control failure paths without exposing supplied content or native details.

No new third-party license or distribution obligation is introduced. Existing dependency and provenance controls remain governing.

This is an engineering dependency review, not legal advice.
