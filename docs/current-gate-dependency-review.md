# Current Gate Dependency Review

Status: **APPROVED FOR EXACT-REVISION QUALIFICATION.**

This successor introduces no new third-party package, donor code, model, binary, external service, persisted storage location, network surface, customer filesystem decision, or licensing obligation. It reuses the already accepted Win32/user32 boundary and adds only a project-owned bounded z-order guard for the accepted reusable-view command controls.

The guard calls the existing Win32 `SetWindowPos` API for at most eight ephemeral catalog-control handles. It changes sibling z-order only: `SWP_NOMOVE`, `SWP_NOSIZE`, and `SWP_NOACTIVATE` preserve exact control and viewport position/dimensions while placing the command controls above child presentation targets. The refresh occurs before each bounded native message-pump cycle so newly created or replaced arbitrary presentation targets cannot permanently cover the command surface. Exact arbitrary/non-grid x/y/width/height/z viewport geometry and sparse logical slots through 4095 are never rewritten or normalized.

Native control handles remain only inside the ephemeral shell boundary and are cleared during teardown. No handle, z-order argument, pointer coordinate, camera/source identity, credential, RTSP/network data, payload/media data, recording identity, private path, runner identity, analytics value, or user naming is added to retained observability. A native z-order failure maps to the existing sanitized native/application failure path and does not expose native details.

No new third-party license or distribution obligation is introduced. Existing dependency and provenance controls remain governing.

This is an engineering dependency review, not legal advice.
