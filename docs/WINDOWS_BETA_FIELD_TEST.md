# Windows beta field qualification

Use this procedure with the native desktop installer and representative physical
cameras. All tester steps use the installer, dashboard, Windows Settings, File
Explorer or Task Manager. Follow [the desktop guide](WINDOWS_DESKTOP_BETA.md) for
setup; there is no terminal, separate Python installation or manual stream URL
step.

This procedure describes the unattended-service candidate source. Begin only with
an installer whose matching native qualification is supplied. Actual reboot,
sign-out and existing-site migration gates remain pending until observed for
that installer. Earlier per-user releases do not contain this service
architecture; see [unattended recording](WINDOWS_UNATTENDED_RECORDING.md).

The released administration baseline at
[`34a77a5`](https://github.com/mkurtgerald/K5-Vision/releases/tag/k5-windows-admin-integrations-34a77a5)
passed [native graphical Windows acceptance, time administration and actual
+/-48-hour host clock corrections](https://github.com/mkurtgerald/K5-Vision/actions/runs/41220620116)
and [native Windows regression](https://github.com/mkurtgerald/K5-Vision/actions/runs/41220620112).
The GUI test used generated H.264 media and a real SOAP camera fixture for
credentials, profiles and automatic stream resolution. It ran discovery but
used numeric-IP fallback because its fixture was not discovered. Separate
actual multicast tests passed. Physical-camera compatibility, capacity and
endurance remain pending.

Record failures with the exact candidate revision and step. Leave every
unattempted or incomplete gate **pending**.

Every new candidate must repeat the exact-build Windows and clock gates. The
camera-video-settings and automatic-wall increment additionally needs its own
installed camera-controls receipt; the older baseline does not qualify those
new controls. Generated-media automation does not complete the physical-camera
and reference-time checks below.

## Prepare the Windows station

1. Use Windows 10/11 x64 on the intended test hardware and a dedicated Windows
   account or test station. Record the source revision and installer SHA-256
   from its supplied release metadata, Windows version, CPU, RAM, available
   recording disk space and browser version. Keep the metadata with the report.
2. Double-click **K5VisionSetup.exe**, accept its software terms, choose
   **Install**, and leave **Launch K5 Vision** selected when choosing
   **Finish**. The supplied download may include an ONVIF qualification suffix
   in its filename.
   Internal beta candidates contain hash-verified native workers. Disconnected-network
   installation and startup still require separate field qualification.
   Later launches reuse the verified private cache.
3. For a new site, select **Set up administrator**, approve the Windows permission
   prompt, then enter an administrator username and a password of at least 12
   characters twice and select **Create account**. Sign in to the dashboard.
   For an existing per-user site, follow the desktop guide's
   [authenticated migration](WINDOWS_DESKTOP_BETA.md#upgrade-removal-and-release-limits)
   instead of creating a replacement account. Record migration as a separate
   gate. This candidate is unsigned; report a Windows installation block as a
   delivery issue instead of inventing a setup workaround.
4. Choose two representative ONVIF cameras with clearly different scenes.
   Record each model, firmware and H.264 resolution/frame rate/bitrate. Enable
   ONVIF and an H.264 profile in each camera's own settings if required. Camera
   accounts may differ from the K5 administrator account.
5. In **Add a camera**, select **Find cameras on this network**, choose the
   camera, enter its name and camera credentials, then select **Test connection**.
   Choose an H.264 **Video stream** and select **Connect camera**. If discovery
   finds nothing, enter the numeric camera IP in the same screen. Set its camera
   settings port and HTTPS option when needed. K5 retrieves the stream address.
6. Check that recording is confirmed before testing recovery. Closing the browser,
   hiding the native window, or choosing **Close K5 Vision** must leave the service
   recording. Windows lock and sign-out must also preserve recording. Shutdown
   stops recording while the PC is off; startup must resume the saved site before
   sign-in. These outcomes need actual observations, not a configured startup
   setting. Test video must follow the site's consent and retention policy.

Keep camera addresses, credentials, serial numbers, private source-store files,
logs and recordings out of the public repository and CI artifacts. Public reports
contain aggregate observations and redacted defects only.

## Time acceptance matrix

Complete the time gate before making a time-accuracy claim. Relative camera/PC
agreement does not establish that either clock has correct UTC. Choose the
site's required UTC tolerance before the run, configure the approved time source
through Windows and camera graphical settings, and verify Windows against that
source independently. Record the reference and observed error privately. K5's
camera comparison includes uncertainty; it does not certify the time source.

Use a disposable qualification station and generated or consented test media for
PC clock-step cases. Windows Date & time settings provide the graphical controls;
camera time/NTP changes use that camera's settings. Restore the approved
automatic-time configuration afterward. No tester terminal is required.

Maintainer automation uses a separate disposable GitHub-hosted Windows VM
gate with the exact same EXE artifact as desktop qualification. An independent
watchdog is armed before guest time services are temporarily controlled and
the guest's UTC clock is moved +48 hours and -48 hours. The gate must check
session lifetime, live video, recording, closed-file SHA-256 hashes and UTC
history, then restore UTC and the time services before evidence upload. This
passed for the linked released baseline and must pass again for each new
candidate. A generated-media VM result does
not complete physical-camera time/NTP accuracy, UTC/DST field checks, capacity
or endurance qualification. Testers do not run this automation themselves.

| Case | Required observation and evidence |
| --- | --- |
| Windows and camera agree with the approved reference | Record the independently observed Windows error and the camera-relative offset/uncertainty. A camera-reported NTP mode alone must not produce a verified-synchronization claim |
| Camera ahead and behind Windows | Test known offsets in both directions, including a date boundary. Correct credentials connect and decode video; the relative difference has the correct sign and is consistent with its uncertainty |
| Camera time changes during the account check | A bounded re-read can retry authentication when the measured change exceeds the uncertainty allowance. The original connection deadline remains bounded; no clock-setting operation occurs |
| Incorrect camera password with aligned or skewed clocks | Authentication fails in both cases, the K5 session remains signed in, and a clock re-read does not turn wrong credentials into success |
| Camera time unavailable, malformed or access restricted | Time remains unavailable; normal authentication can still use its remaining budget. No missing measurement is labelled aligned, NTP-verified or UTC-accurate |
| Approved time server becomes unavailable | A camera-time sample may still be measurable, but synchronization remains unverified; K5 must not claim to have detected NTP source health. Restore the source and recheck independently; configuration alone is insufficient evidence of recovery |
| UTC, local time-zone change and DST boundary | Queries identify the intended UTC interval, ambiguous local hours remain distinguishable, and the correct recordings play. Changing a display time zone must not shift the footage's UTC meaning |
| Windows clock steps forward and backward | The new increment reports the step; previously retained recordings remain available through explicit UTC history queries. Check an uninterrupted stream and a camera/application reconnect. Reauthenticate if a legitimate session expiry requires it |
| Windows clock jumps beyond the age-retention interval | Under controlled storage conditions without quota pressure, age deletion must not purge recent test footage solely because the wall clock moved. Compare retained test-file hashes and decoded playback before and after the step |
| Application restarts after a clock step | Account/camera identity survives, retained footage remains playable, and elapsed-age retention restarts conservatively instead of trusting a changed wall-clock age |
| Browser clock differs from the recorder's clock | The rolling history window follows the recorder's reported UTC rather than the browser's assumed current time. Record any warning, explicit interval and playback result |

Maintainer automation records retained-file SHA-256 comparisons for generated
test video and the clock-step/reference measurements; the public field report
contains aggregate pass/fail results only. The operator checks history and
playback through the GUI. A missing hash comparison or an untested actual Windows
clock-step case remains pending; link any passing automated clock receipt
separately from the physical station's observations. Fake clocks in one Python
test do not establish the native recorder's behavior across an actual PC clock
correction.

Record genuine time discontinuities as discontinuities. Successful decoding is
not proof of absolute timestamp accuracy or continuous chronology. The recording
worker can re-anchor timestamps after Windows time changes. K5's read-only
camera compensation does not change the camera's overlay clock, Windows time,
NTP reachability or a trusted source's accuracy.

## Physical cameras and ordinary recovery

Complete these checks in Edge. Repeat browser playback in Chrome before making
a field-qualified Chrome compatibility claim.

| Action | Required observation |
| --- | --- |
| Discover and connect both cameras through the account/profile wizard | Both names and connection/recording states appear; no password or stream URL is displayed; record whether discovery or IP fallback was used |
| Test an incorrect camera password, then correct it | The camera error is clear, the K5 administrator stays signed in, and the correct account connects |
| Switch cameras repeatedly during live viewing | The selected name matches the actual scene, video advances, and both cameras keep recording |
| Close all browser windows and choose Close K5 Vision for five minutes | The client exits while the service records; reopening the Start/Desktop shortcut restores the same site and playable footage for that interval |
| Disconnect one camera for at least 30 seconds | That camera becomes disconnected while the other stays live and recording |
| Reconnect it without restarting K5 | Live video and recording recover; history represents the missing interval accurately |
| Play and download intervals from both cameras around the outage | The correct scene appears, video advances, and an independent player opens the downloaded MP4 |
| Use the camera reconnect control, retest its account and choose its H.264 profile | The wizard updates the connection while preserving the original camera identity and retained history |
| Open Remove camera, then select Keep camera | The named camera stays enrolled and its history stays available |
| Confirm removal, then connect the camera again using its original name and host | Removal stops new viewing/recording; the other camera is unaffected; reconnecting restores the original identity and retained history, subject to retention |
| Inspect connection help and service/storage status | Readiness and disk use match the station; guidance identifies network, ONVIF account, profile and storage checks |
| Sign out of the K5 workspace with live or playback already open | The workspace and players clear, protected access ends for that K5 session, and signing in restores authorized access while recording continues |
| Sign out of Windows, wait five minutes, then sign in and reopen K5 | The same site returns and its history contains playable new footage from the signed-out interval; retain the separate native session-transition witness |
| Restart Windows and wait five minutes before interactive sign-in | After sign-in, the same site has playable new footage from the pre-sign-in interval; retain the independent native boot-transition witness |
| Open the Start/Desktop shortcut while K5 is already running | The existing client reopens; no duplicate recorder starts |
| Close the client, install the same candidate again and reopen it | Setup safely stops the service for maintenance, preserves accounts/cameras/footage, and starts recording afterward; it must not replace a still-running recorder's files |

Record outage recovery and restart times, with shutdown and network gaps shown
honestly in history. Automatic camera recovery must work without re-enrollment.
A graphical look at retained history is useful field evidence; it does not replace
the native witness proving actual boot/sign-out transitions, the absence of an
interactive sign-in, and newly recorded bytes in that interval. Keep each pending
until the matching [unattended gate](WINDOWS_UNATTENDED_RECORDING.md#qualification-required-before-release)
passes.

For removal testing, close the client, then remove K5 through **Windows Settings
→ Apps → Installed apps**. The uninstaller must safely stop and remove the service
while preserving private machine-site data. Reinstall through the EXE and confirm
that the same accounts, cameras and retained footage return. Perform this on the
dedicated test station. A legacy migration requires its own cancellation,
MFA/credential preservation, retained media and controlled reversal observations;
ordinary reinstall testing does not qualify it.

## Measured capacity and retention

Run the intended camera count and actual H.264 profiles for **at least eight
hours**, including concurrent live viewing and recorded playback during the
last hour. Use Task Manager to record station CPU and memory every five minutes.
Use dashboard storage status and File Explorer to record recording growth.
Report maximum observed values, sustained growth, unexpected disconnects and
playback failures. A two-camera result qualifies that tested configuration;
the 64-camera enrollment limit is not a hardware capacity guarantee.

The default recording quota is **20 GiB**; administrators can change it, the
free-space reserve and retention under **Administration → Recorder settings →
Storage**. Record the actual policy used for this run. The default one-day
elapsed-age policy uses K5's observation of unchanged, closed files instead of
native wall-clock age deletion. Freshly started K5 must wait conservatively before
age-expiring previously unobserved recordings; restarting can extend retention.
Each camera's newest segment stays protected, so a final clip can remain beyond
one day.
The quota and free-space policy can still remove eligible old recordings before
the age interval, so keep clock-retention tests separate from quota-pressure tests.
This new behavior remains pending exact-build qualification.

With consented test media, run long enough to exercise quota
retention and verify that old closed segments disappear from disk and history
while recent recordings remain playable. If the run does not reach the quota,
leave quota retention pending. Do not fill or delete unrelated disk content to
manufacture a low-space condition; a dedicated low-space station requires its
own controlled test plan.

The service's default private recording directory is
`%ProgramData%\K5Vision\recordings`; the Storage panel names the actual active
location. Ordinary users must not gain direct access to this private site data.
Use dashboard storage totals and authorized playback for the operator checks;
any administrator filesystem evidence remains private. After the capacity run,
restart the recorder through a scheduled Windows restart and recheck retained
footage from the beginning and final hour. Confirming one-day age deletion needs
more than 24 hours of eligible elapsed observation, not only advancing the
Windows clock.

## Endurance and beta decision

Complete **at least a 24-hour recording run** at the proposed qualified capacity,
including a camera outage, a recorder restart and a Windows sign-out. Check
timestamps, unexpected gaps, retained playback and sustained memory/disk growth. Extend the
run when defects or drift require more evidence; eight hours alone does not
satisfy this gate.

Keep this aggregate record with the tested installer. It can be completed as a
normal field report; the structure below is a reporting template, not a command
to run. Use actual observations and keep unfinished gates pending.

```json
{
  "schema_version": "4",
  "source_revision": "<exact installer source revision>",
  "setup_sha256": "<installer hash from release metadata>",
  "platform": "Windows",
  "windows_version": "<observed version>",
  "browser_version": "<observed version>",
  "physical_camera_count": 0,
  "stream_profiles": [
    "<model, firmware, H264 resolution/fps/bitrate>"
  ],
  "discovery_or_ip_fallback": "<observed result>",
  "duration_hours": 0,
  "time_reference_and_tolerance": "<private reference record and chosen tolerance>",
  "relative_clock_report_gate": "pending",
  "camera_clock_change_auth_gate": "pending",
  "unavailable_time_source_gate": "pending",
  "utc_timezone_dst_gate": "pending",
  "host_clock_step_history_gate": "pending",
  "clock_step_retained_hash_gate": "pending",
  "independent_utc_accuracy_gate": "pending",
  "physical_ui_and_media_gate": "pending",
  "camera_account_and_profile_gate": "pending",
  "source_reconnect_gate": "pending",
  "client_exit_and_single_instance_gate": "pending",
  "service_recovery_gate": "pending",
  "windows_signout_recording_gate": "pending",
  "pre_signin_reboot_recording_gate": "pending",
  "preserved_site_upgrade_gate": "pending",
  "legacy_migration_and_reversal_gate": "pending",
  "capacity_8h_gate": "pending",
  "quota_retention_gate": "pending",
  "age_retention_24h_gate": "pending",
  "endurance_24h_gate": "pending",
  "cpu_percent_max": null,
  "ram_mib_max": null,
  "recording_disk_gib_max": null,
  "unexpected_disconnects": null,
  "playback_failures": null,
  "blocking_defects": []
}
```

Keep the delivery a recording candidate until the required physical-camera,
recovery, capacity, retention and endurance results pass without a blocking
defect. A beta decision names the observed hardware, camera models, stream
settings, count, duration and remaining limits. Automated workflows and finite
receipts must identify the exact candidate and remain linked with the
[unattended release gates](WINDOWS_UNATTENDED_RECORDING.md#qualification-required-before-release);
they do not substitute for field results. Bundled detector availability does not
qualify model accuracy for the site's scenes.
