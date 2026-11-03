# Windows beta field qualification

The automated browser gate runs real K5, Caddy, MediaMTX and Microsoft Edge on
Windows with two generated H.264 streams. It checks the operator interface,
decoded live and recorded video, camera switching, concurrent recording, an
isolated camera outage, history gaps, sign-out and cleanup. Its receipt names the
actual browser version and platform. These results qualify the assembled
software at **two 320×240 streams, 15 fps, for less than three minutes**. They do
not establish physical-camera compatibility, production capacity or endurance.

Use the following field run to qualify an actual beta installation. Record
failures as defects with the exact package revision and step. A skipped or
unattempted step stays **pending**, even when the automated checks pass.

## Prepare one Windows beta station

1. Use a dedicated Windows 10/11 x64 station and the exact proposed beta package.
   Record the package SHA256, source revision, Windows version, CPU, RAM, free
   recording disk space, Python version and Edge version. For the current
   launcher, use Python 3.12.4 or newer within 3.12.x. Follow
   [the Windows pilot guide](WINDOWS_OPERATOR_PILOT.md) for installation and
   startup.
2. Use two cameras representative of the first users' hardware. Record each
   camera model, firmware, H.264 resolution, fps and configured bitrate. Use a
   numeric camera IP and its actual RTSP path in the setup dialog. Check that
   stream profiles remain within the capacity intended for this beta.
3. Use a new, dedicated K5 state and recording profile. Keep the launcher console
   open throughout the foreground pilot run. Recordings continue with the
   browser closed; closing the launcher or logging off Windows ends this pilot.
4. Keep passwords, IP addresses, RTSP addresses, serial numbers, source-store
   files, private logs and recordings out of the public repository and CI
   artifacts. Share an aggregate receipt and redacted defect descriptions.
   Consent and retention for real recordings follow the operator's site policy.

## First field gate: two cameras and ordinary failures

Complete the following on the target station with Edge. Use clearly different
camera scenes so a wrong-camera image is recognizable. Repeat browser playback
in Chrome when Chrome support is included in the beta promise.

| Action | Required observation |
| --- | --- |
| Start the package, sign in and add both cameras | Setup clears the RTSP input; both camera names and connection/recording states appear; no source credential is shown. |
| Switch cameras repeatedly during live viewing | The title and actual image always match; video advances; the second camera continues recording while the first is viewed. |
| Close all browser windows for five minutes, then reopen and sign in | Both cameras kept recording; history has footage for the closed-browser interval. |
| Disconnect one camera for at least 30 seconds | That camera becomes disconnected; the other camera stays live and recording. |
| Reconnect it without restarting K5 | Live video and recording recover; history identifies the missing interval instead of suggesting continuous footage. |
| Play and download intervals from both cameras before and after the outage | The correct camera scene appears; video advances; a downloaded MP4 plays in an independent player. |
| Update one camera's RTSP source through the interface | The source input starts empty and clears afterward; the camera reconnects using the new source; old recordings remain available. |
| Open Remove camera, then select Keep camera | The named camera remains enrolled; its history stays available. |
| Confirm removal, then re-add the same camera with its original name/IP/RTSP source | Removal stops new viewing/recording and denies saved media URLs for that camera; it preserves recording files and the other camera. Re-adding restores retained history. |
| Open Connection help and inspect service/storage status | The status matches actual service readiness and recording disk capacity; the help identifies camera-network, stream-format and credential setup checks. |
| Sign out with live/playback URLs already open | The workspace and players clear; new requests to saved media URLs are denied; signing in again restores permitted access. |
| Stop K5, then restart the same profile | Sign-in, saved cameras and previously recorded intervals survive; old sessions cannot open protected media. |
| Attempt a second launch against the same running profile | The duplicate is refused; the first instance remains usable and recording. |
| Stop the foreground launcher | K5 and its owned media/gateway workers exit; the profile can be started again cleanly. |

Record elapsed reconnect and restart times. Do not substitute refreshing the
page for automatic source recovery, or re-enrolling cameras for restart
persistence. If the package offers an unattended Windows service, qualify
reboot, logoff, service stop/start and removal separately using its own guide;
the foreground pilot result does not establish those behaviors.

## Capacity and recording retention gate

Run the intended beta camera count and actual stream profiles for **at least
eight hours** on the target station. Include concurrent live viewing and
recorded playback for the last hour. Record five-minute CPU/RAM samples, disk
growth, camera disconnect/reconnect counts and playback failures. Report the
maximum observed values and any sustained growth. A two-camera test is a
two-camera acceptance; it does not imply that the supported configuration limit
of 64 enrolled cameras can all be recorded on that hardware.

Use a disposable profile with generated or consented test video to exercise
quota retention. The current launcher accepts `-RecordingLimitGb 1`; choose a
test bitrate and run duration that actually fills that quota. Verify that old,
closed segments disappear from disk and history, while recent recordings remain
playable. Record the observed maximum disk use and whether a pause occurred.
Do not fill or delete unrelated disk content to simulate failure. A low-space
run, if needed, belongs on a dedicated disposable Windows test disk or VM.

After the run, restart K5 and recheck earlier retained recordings and the final
hour. The default age retention is one day, so confirming age-based deletion
requires a separate run exceeding 24 hours. Eight hours qualifies an eight-hour
pilot only; mark the 24-hour retention gate pending until it has executed.

## Receipt and beta decision

Store this aggregate record with the tested revision. Use actual observations;
do not set a gate to passed based on a synthetic CI receipt. The public receipt
contains no camera address, password, recording, private file path or session.

```json
{
  "schema_version": "1",
  "source_revision": "<exact 40-character Git SHA>",
  "package_sha256": "<exact package SHA256>",
  "platform": "Windows",
  "windows_version": "<observed version>",
  "browser_version": "<observed Edge version>",
  "physical_camera_count": 2,
  "stream_profiles": ["<model, firmware, H264 resolution/fps/bitrate>"],
  "duration_hours": 0,
  "field_ui_and_media_gate": "pending",
  "source_reconnect_gate": "pending",
  "restart_and_signout_gate": "pending",
  "capacity_gate": "pending",
  "quota_retention_gate": "pending",
  "age_retention_24h_gate": "pending",
  "unattended_service_gate": "pending",
  "cpu_percent_max": null,
  "ram_mib_max": null,
  "recording_disk_gib_max": null,
  "unexpected_disconnects": null,
  "playback_failures": null,
  "blocking_defects": []
}
```

A limited recording beta can proceed for the qualified hardware and camera
count when the first field gate, eight-hour capacity gate and quota retention
gate pass with no blocking defect. Publish the remaining pending gates and the
foreground-operation requirement with that beta. Person detection needs its
own model, runtime, accuracy and camera-scene qualification; this recording
receipt makes no analytics claim.

## Reproduce the automated browser gate

This is a developer check. It needs Python development dependencies, installed
Edge, `playwright==1.58.0`, and explicitly supplied MediaMTX, Caddy, FFmpeg and
ffprobe executables. The native CI workflow verifies the donor/development tool
manifests before running them. Playwright and FFmpeg are not product runtime
dependencies or bundled product binaries.

```powershell
python -m pip install -e ".[dev]" playwright==1.58.0
$env:K5_OPERATOR_BROWSER_ACCEPTANCE = "1"
$env:K5_OPERATOR_BROWSER_CHANNEL = "msedge"
# Set the following to independently verified native development-tool paths:
$env:K5_MEDIAMTX_DONOR_EXE = "C:\K5 test tools\mediamtx.exe"
$env:K5_CADDY_DONOR_EXE = "C:\K5 test tools\caddy.exe"
$env:K5_DONOR_FFMPEG = "C:\K5 test tools\ffmpeg.exe"
$env:K5_DONOR_FFPROBE = "C:\K5 test tools\ffprobe.exe"
python -m pytest tests/integration/test_operator_beta_browser.py --no-cov --junitxml=operator-beta-browser-windows.xml
```

The `operator_beta_browser_receipt` must exist in the JUnit report and the test
must have executed successfully. The Windows receipt requires installed Edge,
`native_windows_execution: true` and `portable_ancestor_adjustment: false`. An
ordinary suite run without the opt-in skips this expensive fixture and supplies
no beta evidence. The dedicated CI receipt gate rejects that skipped outcome.

The optional engineering inference check is
`tests/integration/test_operator_beta_capacity.py`. It admits the pinned
OpenVINO/NumPy runtime and independently licensed official OMZ model/image,
executes real positive and blank-frame inference, checks two-view admission,
single-flight backpressure, owner/idle cleanup and native-thread shutdown. Its
`operator_beta_person_receipt` explicitly leaves product analytics enablement,
live-camera suitability, accuracy and endurance unqualified. The first beta
remains a recording product unless its separate analytics integration and field
gates have passed.
