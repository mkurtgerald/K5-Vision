# Native Windows operator pilot

This pilot runs K5, MediaMTX and Caddy as native Windows processes. The dashboard
opens in Microsoft Edge or Chrome on the same computer. It supports password
sign-in, manually adding an RTSP camera, live HLS video, continuous recording,
recording history with gaps, and MP4 playback or download of up to five minutes.
Saved cameras and recordings survive an application restart. Person detection
is unconfigured by default. The sprint adds per-user installation, camera removal
and source-free recording/service status. Optional person detection is a separately
admitted sampled-view candidate, described in `OPERATOR_PERSON_DETECTION_REVIEW.md`.

## Start

Use 64-bit Windows 10/11 with an installed **64-bit CPython 3.12.4 or newer
3.12.x**, a current Edge or Chrome browser, and at least 1 GiB of free disk space
beyond the recordings. First launch needs Internet access to install Python
dependencies and download the pinned native MediaMTX and Caddy releases.
Neither Docker nor WSL is required.

Download the `k5-operator-windows-pilot` artifact from a successful **Native
Windows Operator Product** Actions run and extract the entire folder. Open
PowerShell in that folder and install the reusable Start menu application:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-K5Operator.ps1 -DesktopShortcut
```

Open **K5 Vision Operator** from Start or the Desktop shortcut. Installation,
safe upgrades, optional user logon startup and profile-preserving uninstall are
described in `docs/WINDOWS_OPERATOR_INSTALL.md` in the package. Installed 64-bit
Python 3.12 remains required. The supervisor console must remain open.

To run directly from the extracted package instead:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Start-K5Operator.ps1 -PackagePath .\dist\k5_vision-0.1.0-py3-none-any.whl
```

From a complete checkout of this product branch, the equivalent command is:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Start-K5Operator.ps1
```

The launcher creates a private per-user runtime, verifies the downloaded archive,
executable and license hashes, and prompts for the password and confirmation for
the first administrator, **`recovery-admin`**. Keep the PowerShell window open.
The dashboard opens at
`http://127.0.0.1:8000/operator/`; use that exact address if opening it yourself.
If Python is not found automatically, pass `-PythonExecutable` with the full
path to its `python.exe`. Use `-Port 8001` if port 8000 is already occupied.

## Connect and record

Sign in with the administrator account. Select **Add camera**, enter a name,
the camera's numeric IP address, and its complete RTSP source URL. For example,
an address and source without credentials can look like `192.0.2.10` and
`rtsp://192.0.2.10:554/live`. Use the camera manufacturer's actual RTSP path;
the example address is reserved for documentation. A source containing a
username or password stays in protected local state and is not returned by the
operator API. Percent-encode special characters in the source credentials.
Select **Update source** to correct a saved camera's RTSP path or password;
the form starts with a blank source field and reconnects that camera.

Use an **H.264 camera stream** for this pilot. Source enrollment is manual;
automatic discovery, audio and H.265 browser compatibility are not qualified.
Live playback uses HLS and can lag the camera by several seconds.

Recording starts when the saved source connects. Close the browser without
stopping recording; reopen the dashboard and sign in again when needed. Select
a camera's **History** view to find available intervals and source-outage gaps.
Choose an interval, a UTC start time and a duration of 1–300 seconds to play or
download an MP4 clip. History becomes available as recording segments are closed.
Signing out revokes media access. Press **Ctrl+C** in the PowerShell window to
stop the application and its native workers.

The sidebar shows the last sampled recording bytes, configured allowance and free
space. It distinguishes low space, capacity pressure and unsafe storage inspection.
Unresponsive native workers are detected and recovered independently of camera
connection state. A disconnected camera does not restart all other cameras.

An administrator can select **Remove camera**, review the confirmation and stop
that camera's live/recording path. Existing files are retained subject to rolling
retention; that camera's history closes. Re-adding the same original name and IP
with a valid source preserves its camera identity and restores retained history.

## Local state and limits

The default runtime is `%LOCALAPPDATA%\K5VisionOperatorRuntime`; identity, camera
credentials and recordings are in `%LOCALAPPDATA%\K5VisionOperatorState`.
Run with the same Windows account to reuse them. Interrupted or unsafe identity
state is refused rather than reset. One running operator may own a profile at
a time. Do not put these private directories into a repository or evidence ZIP.

Recordings use a rolling 20 GiB allowance and a one-day age limit. Closed older
segments can be removed automatically. The newest segment for each camera and
recent writes are protected. Recording pauses if storage remains above its
allowance, less than 1 GiB is free, or storage cannot be safely inspected. Live
video and existing history remain available when the disk pause is active.
The dashboard shows the pause; free space and the supervisor will retry.
Use `-RecordingLimitGb 50` on the starter or installer to select a 50 GiB allowance.
The installer saves that setting for subsequent starts.

This is a foreground, local operator pilot. It does not start automatically on
Windows boot or continue after Windows signs the user out. Automated acceptance
uses a generated H.264 stream and checks recording, playback, source reconnect,
logout and whole-product restart. Real-camera compatibility, multi-camera
capacity and long-duration operation still require field qualification. The sprint
separately exercises two generated camera streams in actual native Windows Edge;
that result does not qualify arbitrary camera firmware, resolution or capacity.
A signed installer and boot service remain outside this per-user candidate.

MediaMTX remains under its MIT license and Caddy under Apache-2.0. The package
includes their notices; native binaries are obtained from their pinned official
releases at first launch. FFmpeg is used only by development acceptance fixtures
and is not installed by this launcher. K5-owned files retain the repository
license.
