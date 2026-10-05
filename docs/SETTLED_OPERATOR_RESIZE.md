# Settled operator-shell resize

The running Windows multi-camera shell projects canonical viewport geometry into
its actual client area after each bounded message-pump batch. It reads
`GetClientRect`; it does not infer usable dimensions from decorated outer-window
size or depend only on queued `WM_SIZE` messages.

## Coordinate and ownership contract

- Editor history, saved catalog views, camera slots, stream assignments and media
  generations remain in canonical coordinates
- The reference canvas is the requested opening size expanded, when necessary,
  to contain the current canonical layout. Outer resizing never changes it
- One shared projection assigns strictly ordered integer client edges to all
  canonical placement edges. Adjacent tiles share a boundary; very small logical
  intervals retain at least one client pixel. The source-free layout bound of 64
  placements requires no more than 129 edge intervals per axis
- Pointer input uses the inverse of those exact edges, preserving half-open hit
  ownership. Drag deltas are computed from canonical endpoints, not cumulatively
  rounded resize steps. Raw Win32 pointer input remains signed-16-bit bounded;
  the internal projected event accepts the existing wider canonical geometry
- A geometry change fences pointer input before asynchronous target relayout,
  discards old queued events even after capture was released, and delivers CANCEL
  to clear pending drag and click-selection state. A wrap-safe `MSG.time` /
  `GetTickCount` fence also drops older viewport pointer messages still in the OS
  queue beyond the current pump batch. Same-tick input is conservatively discarded.
  Native catalog-control messages and other windows are not subjected to that
  viewport fence. Existing logical selection and accepted canonical commands remain valid
- Automatic resize uses the existing same-slot host relayout. It does not enqueue
  an editor RELAYOUT command, rebase undo/redo, restart streams, or count a paint

## Minimum and minimized geometry

A positive settled client is clamped once to a usable minimum: 192 by 192 for the
plain shell, 608 by 232 for catalog shells. Catalog commands occupy a fixed
40-pixel strip; video uses the remaining area. The full selector's 600-pixel
width remains usable with its margin. Native nonclient dimensions are measured
before the single `SetWindowPos` adjustment and the actual result is rechecked;
a failed adjustment is reported rather than retried in a loop.

Minimized or zero client extent cancels stale pointer state without restoring the
window, replacing targets, or stopping media. The accepted presentation boundary
continues its nonfatal paint deferral. Restore observes fresh client geometry.
Identical extents do not cause repeated relayout. Resize/move intent observed for
this shell still invalidates input even when a border drag ends at the original
size. An unobserved external A-to-B-to-A cycle is not continuously tracked. Target replacement preserves
surfaces and routing, and fixed catalog controls are raised again afterward.

## Explicit limits

All HWND creation, pumping, geometry operations and cleanup remain on the same
creating thread. Windows' native interactive move/size modal loop can still block
that synchronous pump until border dragging finishes. This change establishes
settled resizing; it does not establish uninterrupted playback during border drag
or implement a UI-thread redesign.

Projection is updated only for a running host. A terminal host retains its last
accepted target projection until replacement/start; stale input is invalidated.
No installer, dependencies, recording policy, source retention or workflow changes
are part of this repair. Overlapping-window native z-order is a pre-existing
separate acceptance boundary; non-overlapping tiled cameras are the resize target.
