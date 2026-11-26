# Open the camera wall on Windows

Use K5 Vision to watch cameras together, check whether recording is actually
advancing, find footage on a UTC timeline and save an MP4 clip. The native Windows
application connects to the Windows recording service; **Open camera wall**
opens the viewing and recording workspace in your browser. The service owns
recording independently of the desktop client.

This guide describes the unattended-service candidate source. Its exact installer
still needs matching Windows qualification, including actual reboot, sign-out
and legacy migration results. Earlier public per-user installers do not gain
these capabilities from this source change; see the
[unattended recording status](WINDOWS_UNATTENDED_RECORDING.md).

Download the Windows installer from [public GitHub Releases](https://github.com/mkurtgerald/K5-Vision/releases).
Use the release notes and matching source revision, installer hash and Windows
qualification receipt to identify its supported features. The new workspace
requires its own passing receipt; an earlier installer does not qualify it.

## Install and connect cameras

Use Windows 10/11 x64 and a current Edge or Chrome browser. The installer includes
its own runtime; internal candidates bundle and verify the
native recording workers. The engine and workers run on this Windows PC.
Normal setup needs no terminal, separate Python
installation, manual packages, Docker or WSL. The testing installer is unsigned.

1. Double-click **K5VisionSetup.exe**, accept its software terms and choose
   **Install**. Leave **Launch K5 Vision** selected when choosing **Finish**.
2. On a new site, select **Set up administrator** and approve the Windows
   permission prompt if offered. In the setup window, choose an administrator
   username, enter a password of at least 12 characters twice, and select
   **Create account**. An existing per-user site must use **Move existing K5
   site** instead; follow the upgrade instructions below.
3. Choose **Open camera wall** if the workspace has not opened, then sign in.
   Engine readiness means K5 is available; check each camera's recording state
   before relying on new footage.
4. Select **Add camera**, then **Find cameras on this network**. Choose a camera,
   give it a name, and enter its ONVIF account. If discovery finds nothing, enter
   its numeric IP in the same screen. Set the camera settings port or HTTPS
   option if required.
5. Select **Test connection**, choose an H.264 **Video stream**, then **Connect
   camera**. K5 obtains the stream address automatically; no RTSP URL is needed.

The camera must be reachable from this PC and have ONVIF and H.264 enabled.
Camera credentials may differ from your K5 account. An accepted camera account
does not establish that video or recording has begun.

Administrators can select an enrolled ONVIF camera and choose **Camera settings**
to read and change its supported resolution, frame-rate limit, bitrate, keyframe
interval, quality and encoding interval. **Apply video settings** writes H.264
only and can interrupt that camera's live video and new recording.
Available controls come from the camera; direct RTSP enrollment does not provide
ONVIF management access. See [camera video settings](CAMERA_VIDEO_SETTINGS.md).

### Connect a direct RTSP stream

For a camera or encoder with a known stream address, select **Add camera** and
choose **RTSP stream** as the connection type. Enter a name and the full
`rtsp://` or `rtsps://` address using the device's numeric IP address. Enter its
username and password if the stream requires an account. Select **Test
connection**, then **Connect camera** after K5 confirms an H.264 stream.
Different stream paths on the same device and port can be added separately.

Direct streams use the same camera wall, recording controls, UTC history and MP4
export as ONVIF cameras. Use **Reconnect camera** to test a replacement stream
address or account while preserving the selected camera's history and recording
preference. RTSP does not expose the camera's management clock; check its time
and NTP settings through the camera's own settings. K5 continues to show the
recorder's time diagnostics.

## Administer users and camera access

Sign in as an administrator and choose **Administration**. The screen lists K5
accounts, their role, account status, password setup status and camera access.
Create an account with a display name and username, choose its role and assign
all cameras or a selected set. The temporary credential appears once; give it
privately to the new user.

On the sign-in screen, the user chooses the password setup option, enters their
username and temporary credential, and sets their own password. They can then
sign in normally. Temporary credentials expire and work once. Administrators can
issue a replacement when needed, enable or disable an account, change its role
or camera access, and review the account activity log. Resetting a password or
changing access ends the affected user's current session.

| Role | Available controls |
| --- | --- |
| Viewer | Live viewing and recorded playback for assigned cameras |
| Operator | Viewer access plus MP4 clip export |
| Administrator | All cameras, recording and camera configuration, user management and account activity |

Camera assignments also apply to direct media requests. Administrators always
retain access to all cameras. K5 protects the last enabled administrator with an
activated password; activate another administrator before disabling, demoting or
resetting that account. The account activity log records administrative changes
without showing passwords or temporary credentials.

## Watch, review and save

On **Camera wall**, choose **Auto** for one tile with one enrolled camera,
four tiles with two through four cameras, or nine tiles with five or more.
Authorized offline cameras still count. Choose **1 camera**, **2 × 2** or
**3 × 3** for a manual layout, and use **Previous** / **Next** for more cameras.
A tile's **Watch** opens its single-camera view; **Recordings** opens its history.
Paging or changing views does not stop the recorder.

Administrators can **Pause recording** or **Resume recording** for a selected
camera. The choice persists after restart; live viewing and retained history stay
available. **Recording** means K5 has observed new recording data on disk.
Waiting, stalled, operator-paused and storage-paused states require different
actions; the [workspace guide](SURVEILLANCE_WORKSPACE.md) explains them.

Use **Administration → Recorder settings → Storage** to manage recording quota,
free-space reserve, retention and location. A new recording location requires a
successful restart migration before it becomes active; see
[storage administration](WINDOWS_STORAGE_ADMINISTRATION.md).

Choose **Latest recording**, or open **History** and use **15 min**, **1 hour**,
**24 hours** or an explicit UTC interval. Select a recorded interval or scrub the
timeline to play it; a gap contains no video. Use **Mark clip start** and
**Mark clip end**, or edit the start and length, then **Save MP4 clip**. Each clip
must stay inside one available interval and be at most five minutes.

The installer includes the local person detector and its model. Administrators
can enable **Detect people on this camera**, set **Minimum confidence** and choose
**Save detection settings**. Keep recording enabled: background detection analyzes
recorded frames. **Recorded person events** follow the selected history range;
choose **Play recording** to open the corresponding footage. External Neural or
Analytics services need a separately configured
[integration](OPERATOR_INTEGRATIONS.md).

## Time and background operation

Camera time readings show a relative camera-minus-PC offset and uncertainty.
A small offset or a camera reporting NTP mode does not verify correct UTC or
synchronization. Ordinary sign-in, discovery and recording do not silently
change either clock. Administrators can use **Administration → Recorder settings
→ Time** to configure Windows Time, request resynchronization and manage an
ONVIF camera's NTP or UTC settings. Windows changes use the normal UAC consent
dialog and require no terminal. Review configuration, measured offset and
verification status separately; see
[time administration](WINDOWS_TIME_ADMINISTRATION.md).

History labels are UTC, and rolling windows follow the recorder's reported time.
Investigate clock-step warnings: recording timestamps can move when Windows time
changes. Successful playback alone does not establish accurate chronology.
See the [field time matrix](WINDOWS_BETA_FIELD_TEST.md#time-acceptance-matrix).

Closing the browser leaves recording with the Windows service. Closing the native
window hides the client in the notification area; **Close K5 Vision** exits that
client while recording continues. Reopen the Start/Desktop shortcut or choose
**Open camera wall** from the tray to return. Locking or signing out of Windows
must also leave the service recording. Shutdown stops recording while the PC is
off; automatic startup must restore the saved site before anyone signs in.
These are candidate requirements until the exact installer passes the
[service, reboot and sign-out gates](WINDOWS_UNATTENDED_RECORDING.md#qualification-required-before-release).

The client reports service readiness and recovery. Check each camera's observed
recording state as well. If recovery does not complete and the client requests a
repair, rerun the graphical installer; preserve the existing site data.

## Upgrade, removal and release limits

Close the desktop client before installing an update. The graphical installer
stops and verifies its service before replacing application files, then starts it
again. Schedule this maintenance because new recording stops during the update.
Setup preserves the machine site's accounts, cameras and recordings. Removing K5
through **Windows Settings → Apps → Installed apps** removes the service and
application while preserving site data; reinstalling can use that data again.

For an older per-user installation, sign in to Windows with the account that owns
that K5 site, close the previous K5 app, and install the new candidate. This same
Windows account must have administrator permission; approving elevation with a
different Windows account is refused. In K5, select **Move existing K5 site**,
approve the Windows permission prompt, and enter the existing **K5 administrator**
username, password and authenticator or recovery code if MFA is enabled. Select
**Move existing site** and keep the migration window open.

Migration copies and verifies the site, recordings and exports before activating
the service copy; the original per-user data is preserved. Allow free space for
the full copy plus K5's reserve. Insufficient capacity or failed verification must
leave the existing site intact instead of creating an empty replacement. Large
sites can take time. **Cancel** waits while K5 checks the transaction; if activation
has already completed, cancellation does not undo the new site. Reopen K5 to check
its actual state before retrying. An interrupted retry can need a new full copy;
partial-copy reuse is not a supported promise. See
[existing-site migration and recovery limits](WINDOWS_UNATTENDED_RECORDING.md#existing-installations).

Do not delete private state, copy protected credentials manually, or run both
copies as independent active sites to work around a migration refusal. A return
to the old version requires a controlled maintenance procedure; it does not merge
new service recordings or account changes back into the old profile. Actual
native migration and reversal qualification remains required before release.

A release claiming this workspace must pass the real Windows installer and GUI
journey, wall viewing, recording controls, UTC timeline, decoded playback and MP4
download checks for that exact installer. User-administration releases also
require graphical account creation, password setup, role and camera restrictions,
active-session revocation, account activity and restart persistence. Its clock gate must also cover real
forward/back Windows clock corrections and retained-file integrity. Earlier
clock-only evidence does not qualify the new workspace.

The dedicated clock VM repeats graphical setup, camera enrollment, recording,
analytics and playback before stepping Windows time by 48 hours in each
direction and restoring it. Its receipt requires the full GUI journey's raw
report and receipt, plus the actual installer bundle, from the same source,
workflow run and attempt. The full GUI journey remains mandatory. The clock
journey retains its 360-second limit, one 90-second watchdog across both steps,
and a hold of at least 10 seconds at each corrected time. A separate Windows
Time administration gate verifies configuration, resynchronization and restore.

Physical-camera compatibility, independent NTP/UTC accuracy, measured capacity
and endurance still require [field qualification](WINDOWS_BETA_FIELD_TEST.md).
Motion recording and inclusion/exclusion zones are implemented in the candidate
source; their dedicated installed-Windows motion receipt must qualify the exact
installer before a release claims support. See the [motion recording guide](CAMERA_MOTION_RECORDING.md).
The boot service and existing-site migration are implemented in candidate source
and remain subject to the unattended qualification gates above. PTZ controls,
custom tile layouts and adaptive main/sub streams remain on the
[camera-setup](CAMERA_SETUP_ROADMAP.md) and [video-wall](VIDEO_WALL_ROADMAP.md) roadmaps.
