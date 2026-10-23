# Open the camera wall on Windows

Use K5 Vision to watch cameras together, check whether recording is actually
advancing, find footage on a UTC timeline and save an MP4 clip. The native Windows
application starts the camera engine and stays in the notification area;
**Open camera wall** opens the viewing and recording workspace in your browser.

Download the Windows installer from [public GitHub Releases](https://github.com/mkurtgerald/K5-Vision/releases).
Use the release notes and matching source revision, installer hash and Windows
qualification receipt to identify its supported features. The new workspace
requires its own passing receipt; an earlier installer does not qualify it.

## Install and connect cameras

Use Windows 10/11 x64 and a current Edge or Chrome browser. The installer includes
its own runtime; first launch needs Internet access to download and verify the
native recording workers. The engine and workers run on this Windows PC.
Normal setup needs no terminal, separate Python
installation, manual packages, Docker or WSL. The testing installer is unsigned.

1. Double-click **K5VisionSetup.exe**, accept its software terms and choose
   **Install**. Leave **Launch K5 Vision** selected when choosing **Finish**.
2. In the first-run window, choose an administrator username and enter a password
   of at least 12 characters twice. Select **Create account**.
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

## Watch, review and save

On **Camera wall**, choose **1 camera**, **2 × 2** or **3 × 3** and use
**Previous** / **Next** for more cameras. A tile's **Watch** opens its single-camera
view; **Recordings** opens its history. Paging or changing views does not stop
the recorder.

Administrators can **Pause recording** or **Resume recording** for a selected
camera. The choice persists after restart; live viewing and retained history stay
available. **Recording** means K5 has observed new recording data on disk.
Waiting, stalled, operator-paused and storage-paused states require different
actions; the [workspace guide](SURVEILLANCE_WORKSPACE.md) explains them.

Choose **Latest recording**, or open **History** and use **15 min**, **1 hour**,
**24 hours** or an explicit UTC interval. Select a recorded interval or scrub the
timeline to play it; a gap contains no video. Use **Mark clip start** and
**Mark clip end**, or edit the start and length, then **Save MP4 clip**. Each clip
must stay inside one available interval and be at most five minutes.

## Time and background operation

Camera time readings show a relative camera-minus-PC offset and uncertainty.
A small offset or a camera reporting NTP mode does not verify correct UTC or
synchronization. K5 reads clocks for authentication and diagnostics; it does not
set Windows time, camera time or NTP settings. Configure approved time sources
through Windows **Date & time** and the camera's own settings.

History labels are UTC, and rolling windows follow the recorder's reported time.
Investigate clock-step warnings: recording timestamps can move when Windows time
changes. Successful playback alone does not establish accurate chronology.
See the [field time matrix](WINDOWS_BETA_FIELD_TEST.md#time-acceptance-matrix).

Closing the browser or native window leaves the engine running in the
notification area. Reopen the Start/Desktop shortcut or choose **Open camera
wall** from the tray. **Stop and quit**, Windows sign-out and shutdown stop the
engine. This is a signed-in-user application, not a boot service.

## Upgrade, removal and release limits

Choose **Stop and quit** before installing an update. Setup refuses a running
recorder and preserves the account, cameras and recordings. Removing K5 through
**Windows Settings → Apps → Installed apps** also preserves that private profile.
Reinstalling can use it again.

A release claiming this workspace must pass the real Windows installer and GUI
journey, wall viewing, recording controls, UTC timeline, decoded playback and MP4
download checks for that exact installer. Its clock gate must also cover real
forward/back Windows clock corrections and retained-file integrity. Earlier
clock-only evidence does not qualify the new workspace.

Physical-camera compatibility, independent NTP/UTC accuracy, measured capacity
and endurance still require [field qualification](WINDOWS_BETA_FIELD_TEST.md).
This increment does not deliver a boot service, bundled AI workflow, PTZ controls,
event search or recording-storage configuration.
