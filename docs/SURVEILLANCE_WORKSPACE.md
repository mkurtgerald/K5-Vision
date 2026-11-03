# Watch cameras, find footage and save a clip

Open K5 Vision from the Start menu or desktop, choose **Open camera wall**, and
sign in. The native window launches and supervises the engine; the browser
workspace contains the live views, recordings and camera controls. Engine ready
does not mean a camera is already recording.

For installation, ONVIF account/profile setup and direct RTSP stream setup, follow the
[Windows desktop guide](WINDOWS_DESKTOP_BETA.md). Download installers from
[public GitHub Releases](https://github.com/mkurtgerald/K5-Vision/releases) and
check the release's matching Windows qualification receipt for this workflow.

## Watch the site

Administrators can choose **Administration** to create and manage K5 accounts,
assign roles and cameras, enable or disable access, issue one-time password setup
credentials and review account activity. Viewers can watch and play assigned
cameras; operators can also export clips. Camera and recording configuration and
user administration require an administrator account. The
[Windows desktop guide](WINDOWS_DESKTOP_BETA.md#administer-users-and-camera-access)
describes account setup and the last-administrator protection.

Choose **Camera wall** and a layout: **1 camera**, **2 × 2** or **3 × 3**.
Use **Previous** and **Next** to page through additional cameras. Each tile names
its camera and shows connection and recording state. Choose **Watch** for the
larger single-camera view or **Recordings** to review that camera's footage.
Leaving a page or view stops its browser players, while background recording
continues according to each camera's recording choice.

If video is disconnected, check the camera's power and network, then retry.
Use **Reconnect camera** to retest its ONVIF account/profile or direct RTSP stream
when necessary.
Keep the original camera name and host when restoring a removed camera's identity
and retained history.

## Check recording, then pause or resume when needed

Expand **Recorder overview** to see camera, recording and storage totals. The
overview separates cameras online from recording confirmed. A live picture
or saved camera setting alone does not prove new footage is reaching disk.

| State | Meaning and next action |
| --- | --- |
| Recording | K5 has observed new recording data on disk; review retained footage when verifying coverage |
| Waiting for video / checking recording | The camera is not yet sending video, or K5 is waiting to confirm new disk activity |
| Recording stalled / unavailable | New footage is not being confirmed; check camera connection and storage guidance |
| Recording paused | An administrator paused this camera; select Resume recording when ready |
| Storage paused | Recording is blocked by disk space, quota or a storage check; follow the displayed storage guidance |

For a selected camera, administrators can choose **Pause recording** or
**Resume recording**. That choice persists across application restart. Pausing
leaves live video and retained history available; it does not delete earlier
footage. After resuming, wait for new recording data to be confirmed.

## Find the moment

Choose a tile's **Recordings**, or select the camera and choose **Latest
recording**. In **History**, **Play latest recording** opens the newest playable
footage in the loaded window. Use **15 min**, **1 hour** or **24 hours** to change
the rolling window, or choose a day or explicit **From / To · UTC** interval.

The timeline shows available recording ranges and gaps. Click a recorded point,
or drag and release, to seek and start playback. You can focus the timeline and
use arrow keys, Shift+arrows, Home or End. Selecting a gap reports that no footage
is available there; it does not join recordings across the missing time.

All timeline, history and clip times are UTC. Rolling windows use the recorder's
reported UTC rather than the browser's assumed current time. If recorder time
cannot be checked, use an explicit UTC interval. A clock-step warning requires
investigation; playback success does not prove accurate or continuous chronology.

## Save an MP4

1. Open an available interval and move playback to the desired beginning.
   Choose **Mark clip start**.
2. Move to the desired end and choose **Mark clip end**. You can also edit
   **Start · UTC** and **Seconds**.
3. Check the displayed clip interval and select **Save MP4 clip**. Open the
   downloaded MP4 in an independent player to check its scene and duration.

A clip must remain inside one available recording interval and last from one
second to five minutes. The save action downloads recorded video; it does not
create footage across a gap. Storage retention still applies to source recordings.
Keep downloaded footage private and follow the site's recording policy.

## Leave the recorder running

Close the browser or native window to leave K5 in the Windows notification area.
Reopen the Start/Desktop shortcut or use the tray's **Open camera wall** action
to return. **Stop and quit**, Windows sign-out and shutdown end recording.
Administrator pause/resume choices are preserved when the engine restarts.

Camera clock readings describe a relative offset with uncertainty. Reported NTP
mode is not verified synchronization or absolute UTC accuracy. K5 does not alter
Windows time, camera time or NTP settings; configure the approved source through
the normal Windows and camera graphical settings.

This workspace requires the exact release's Windows qualification. Physical
camera compatibility, independent NTP accuracy, measured capacity and endurance
remain separate [beta gates](WINDOWS_BETA_FIELD_TEST.md). This increment does not
add a boot service, bundled AI, PTZ, events or storage configuration.
