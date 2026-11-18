# Motion recording and zones

These features are implemented in the candidate source. Native Windows installer
qualification is pending. Use the matching release notes and recording-motion
evidence to establish which installer includes them.

Select an enrolled camera and choose **Recording and zones**. Recording-policy
changes require an administrator with recording-management permission and access
to that camera. Capturing frames or editing zones also requires live-view
permission. Camera encoder controls remain under **Camera settings**.

## Choose recording behavior

Choose **Continuous** or **Motion-triggered**, then select **Save recording
settings**. Motion means general scene movement; it is separate from person
detection and external Neural/Analytics results.

- **Continuous** keeps an enabled camera recording while its stream and storage
  are available.
- **Motion-triggered** waits for movement in enabled regions, opens recording
  when movement is detected and holds it after movement stops.
- **Pause recording / Resume recording** remains the master control. Changing
  modes does not resume a paused camera or override a storage block.

Higher **Motion sensitivity** detects smaller brightness changes. **Minimum
changed area (%)** measures changed pixels inside enabled regions as a percentage
of the full frame. Increase the area threshold to ignore smaller movements.
**Record after movement stops** accepts 1–120 seconds. The hold uses elapsed
time, so correcting Windows time does not extend or shorten it.

**Pre-event capture is zero.** The candidate does not buffer footage before a
trigger. New recording begins after detection and recorder activation; it does
not promise to include the first moment of an event.

## Draw inclusion and exclusion zones

1. Choose **Capture current frame** to load a frozen frame captured by K5 from
   this camera. The image is for drawing; the browser does not supply motion
   evidence.
2. Choose **Draw inclusion** or **Draw exclusion**, then select at least three
   points on the frame. Choose **Finish zone** to close the outline. Blue
   outlines are inclusions; dashed orange outlines are exclusions.
3. Use **Undo point**, **Cancel drawing**, or the zone's **Delete** button to
   correct an outline. **Clear zones** removes all zones; **Use whole frame**
   explicitly selects the complete current view.
4. Choose **Save recording settings** to apply the policy. **Cancel** discards
   unsaved edits. **Refresh** discards edits and reads the saved policy again.

No inclusion zones means the whole frame. Multiple inclusions are combined;
exclusions subtract last, including where they overlap inclusions. K5 rejects a
policy that leaves no enabled area. A camera supports eight zones in total and
up to sixteen points per zone. Crossing or empty outlines must be corrected.

To draw with a keyboard, focus the frame. Arrow keys move the cursor by 1%;
Shift plus an arrow moves it by 0.1%. Enter adds a point, Backspace removes the
last point, and Escape cancels the outline being drawn. Use the **Finish zone**
button to complete it.

Frozen frames expire. Capture a new frame before saving zone edits after
expiry. Existing unsaved outlines can be retained when the new frame has the
same source and geometry. Loss of live-view permission clears the frame and
unsaved zone edits.

**These are trigger zones, not privacy masks.** Excluding a region from motion
detection does not obscure that region in live video, recordings or exports.

## Read the recorder state

| State | Meaning |
| --- | --- |
| Armed · Waiting for motion | The detector is active; an idle camera is not expected to create new footage. |
| Motion recording | Detected movement has opened the recording gate. |
| Finishing motion recording | The saved hold time keeps the gate open after movement stops. |
| Recording paused | The master recording control is paused. |
| Storage blocked | Storage protection takes priority over recording mode. |
| Zones need review | The source, selected profile or verified frame geometry changed. Review the new view before rearming motion recording. |
| Motion unavailable | Motion input or processing is unavailable; recording is not confirmed. |

The camera's recording badge separately confirms observed disk activity. An open
motion gate alone is not proof that footage was written. A motion-input failure
does not silently enable continuous recording.

After a source/profile or frame-geometry change, capture the current view,
clear or redraw the old zones and save them. If no polygons are needed, capture
the view and choose **Use whole frame** before saving. Camera repositioning,
optical zoom and changes made through the camera's own controls may also change
the scene without changing its dimensions; review zones after such adjustments.

## Background operation and footage

The native motion worker reads camera video independently of recording files.
It can detect movement while a motion-armed recorder is idle. Saved recording
mode, thresholds and zones persist across restarts and continue when the dialog
or browser closes.

Keep K5 running in the Windows tray and keep the Windows user signed in.
**Stop and quit**, sign-out and shutdown stop the engine. Motion processing uses
local CPU resources; an unavailable detector is reported rather than counted as
working.

Motion mode creates real gaps between recorded periods. History, playback and
export use the footage that actually exists and its original UTC timestamps.
Choose an available interval when exporting; K5 does not invent footage across
an idle period.
