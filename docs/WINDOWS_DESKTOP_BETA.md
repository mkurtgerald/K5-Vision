# Test K5 Vision on Windows

Download **K5VisionSetup.exe** from the qualified desktop build. Double-click it,
accept the displayed software terms, and choose **Install**. Leave **Launch K5
Vision** selected on the final screen and choose **Finish**. The installer creates
a Start menu entry and offers a desktop shortcut.

The application includes its own runtime and native recording workers. Setup and
normal use require no PowerShell, Command Prompt, separate Python installation,
package downloads, Docker or WSL. Use Windows 10/11 on an x64 computer and a
current Edge or Chrome browser. This candidate is an unsigned testing build.

On first launch, choose an administrator username, enter a password of at least
12 characters twice, and select **Create account**. K5 Vision opens the camera
dashboard in your browser. Sign in with that account and add an H.264 camera
using its numeric IP address and actual RTSP stream address.

If you used the earlier operator candidate, K5 Vision uses its existing account,
cameras and recordings. It does not replace or reset an existing account profile.

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
runtime, Edge video, recording while closed, single-instance reopening, stop,
restart and uninstall on Windows. A skipped test does not qualify a download.

This recording candidate uses manual RTSP enrollment. ONVIF discovery and
separately provisioned person detection are outside the bundled desktop runtime.
Physical-camera compatibility, measured capacity and long-duration recording
remain the [field beta gates](WINDOWS_BETA_FIELD_TEST.md).
