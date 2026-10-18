# Test K5 Vision on Windows

Download **K5VisionSetup.exe** from the qualified desktop build. Double-click it,
accept the displayed software terms, and choose **Install**. Leave **Launch K5
Vision** selected on the final screen and choose **Finish**. The installer creates
a Start menu entry and offers a desktop shortcut.

The application includes its own runtime. First launch downloads and verifies its
native recording workers automatically, so it needs Internet access once. Later
launches reuse the verified private cache. Setup and
normal use require no PowerShell, Command Prompt, separate Python installation,
manual package downloads, Docker or WSL. Use Windows 10/11 on an x64 computer and a
current Edge or Chrome browser. This candidate is an unsigned testing build.

On first launch, choose an administrator username, enter a password of at least
12 characters twice, and select **Create account**. K5 Vision opens the camera
dashboard in your browser. Sign in with that account, then:

1. Open **Add a camera**, choose **Find cameras on this network**, and select
   your camera from the results.
2. Choose a camera name and enter the camera's ONVIF username and password.
   These are the camera's
   credentials, which may differ from your K5 Vision account.
3. Select **Test connection** to check the camera account and load its video
   profiles. K5 reads the camera's time to handle a different clock without
   changing the camera or Windows clock.
4. Choose an H.264 **Video stream** and select **Connect camera**.

K5 Vision obtains the stream address from the camera and checks for video.
Saving the camera settings does not establish that video or recording has started;
check the camera's live view and recording status. You do not need to look up or
type an RTSP URL. RTSP remains the standard camera transport used by the recording
worker.

The computer and camera must be on a reachable local network, and ONVIF must be
enabled in the camera's settings. If discovery does not find it, enter the
camera's numeric IP address in the camera connection screen and continue with
its credentials and profile. This also obtains the stream address automatically.
If the camera uses a different settings port or HTTPS, choose those options in
the same screen.
If the camera only offers H.265, enable an H.264 profile in its settings and
retry.

If you used the earlier operator candidate, K5 Vision uses its existing account,
cameras and recordings. It does not replace or reset an existing account profile.

## Camera and recording time

The time-critical increment is pending qualification on its exact Windows build.
Its connection test reports the camera's time difference from this PC, when it
can read the camera's UTC time. A positive difference means the camera is ahead;
a negative difference means it is behind. The report includes an uncertainty
allowance for the camera's whole-second clock and network round trip. An
unavailable reading stays unavailable.

This is relative clock alignment. A small difference does not prove that either
clock has the correct UTC time. A camera reporting NTP mode only reports its
configuration; K5 does not verify its time server or declare NTP synchronization.
K5 uses the measured camera time for the camera account check and can re-read it
once if authentication fails after a clock change. An incorrect password still
fails. K5 does not set either clock or change NTP settings.

Configure automatic time through **Windows Settings → Time & language → Date &
time** and the camera's own date/time or NTP settings. Use the site's approved
time source. If absolute recording time matters, verify Windows against that
source independently; an accepted camera account is not proof of accurate
recording timestamps or a correct camera overlay clock.

History uses UTC. The new increment anchors its rolling history window to the
recorder's reported UTC and reports a detected PC clock step. Investigate that
warning even when video continues. The recording worker's timestamps can move
when Windows time changes; do not infer continuous or accurate chronology from
successful playback alone. Required clock, history and retention checks are in
the [field time matrix](WINDOWS_BETA_FIELD_TEST.md#time-acceptance-matrix).

## Keep recording while the window is closed

Closing the browser or the K5 Vision window keeps the recorder running. K5 Vision
stays in the Windows notification area. Double-click the Start or desktop shortcut
again to reopen the application, or use its tray menu to open the camera dashboard.

Select **Stop and quit** in the application or tray menu to stop recording and
close the owned workers. Windows sign-out and shutdown also end recording. This
candidate runs for the signed-in Windows user; it is not a boot service.

## Upgrade or remove the application

Choose **Stop and quit** before running another K5 Vision setup. The installer
refuses a running recorder and holds an installation lease while application
files change. It preserves the account and recording profile.

Remove K5 Vision through **Windows Settings → Apps → Installed apps**. Removal
preserves accounts, saved cameras and recordings. Reinstalling can use that same
profile. The default private data directory is
`%LOCALAPPDATA%\K5VisionOperatorState`; application files live separately in
`%LOCALAPPDATA%\K5VisionDesktop`.

## Qualification scope

The desktop gate must exercise the real installer, account screen, bundled
runtime, ONVIF discovery, camera credentials, H.264 profile selection, automatic
stream resolution, Edge video, recording while closed, single-instance reopening,
stop, restart and uninstall on Windows. A skipped test does not qualify a
download. Earlier manual-enrollment desktop evidence does not establish that the
new ONVIF flow passed; use the qualified build's exact commit and receipt.
The clock-skew gate uses a generated camera whose clock differs from Windows and
requires successful authenticated enrollment and decoded video from the installed
application. It exercises the read-only clock lookup and rejects a wrong camera
password. That gate alone does not establish actual clock synchronization, NTP
availability, absolute UTC accuracy, DST behavior or recording retention across
a PC clock correction. The time-critical increment requires its own new passing
Windows receipt; earlier candidate evidence does not qualify it.

The recording candidate includes the reviewed ONVIF connector; separately
provisioned person detection remains outside the bundled desktop runtime.
Physical-camera compatibility, measured capacity and long-duration recording
remain the [field beta gates](WINDOWS_BETA_FIELD_TEST.md).
