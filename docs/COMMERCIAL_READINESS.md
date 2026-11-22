# Commercial readiness

K5's native Windows recorder service is under qualification in
[PR #405](https://github.com/mkurtgerald/K5-Vision/pull/405). It is not a
commercially qualified release. The historical public
[`k5-windows-beta-2d74334` installer](https://github.com/mkurtgerald/K5-Vision/releases/tag/k5-windows-beta-2d74334)
does not include this service.

A surveillance recorder must save footage without an open client or a signed-in
operator. That is a release requirement. A graphical installer, a configured
automatic-start service, or a passing test suite does not establish that behavior.

## Order of work

| Priority | Required outcome | Acceptance evidence |
| --- | --- | --- |
| 1 | Unattended recording and recovery | The installed recorder writes with both clients closed, after actual Windows sign-out, and after a real reboot before interactive login. Force the owned backend and wrapper to fail; verify automatic recovery, retained camera configuration and decoded footage. |
| 1 | Safe existing-site upgrade | The actual graphical migration authorizes the original administrator and MFA, supports cancellation, preserves users/cameras/footage/exports, rewraps secrets for the service identity, and completes the documented reversal without resetting the site. |
| 1 | Safe recorder stop | Administrative stop flushes owned media work and releases every owned process and handle. An unidentified process or failed cleanup is a failed gate. |
| 2 | Visible recording faults | Authorized operators can see storage, media and unexpected no-write faults, with an actionable explanation and recovery state. Intentional pause and quiet motion recording must not generate false failure alerts. |
| 2 | Storage and outage recovery | At the proposed capacity, recording survives ordinary archive growth, quota pressure, a camera/network outage and restart. Interrupted inventory must not permanently strand recording. Partial inventories never authorize deletion. |
| 2 | Site backup and restore | A supported administrative flow restores configuration, users, permissions and protected secrets on the supported target. Test the restore and retained footage independently; copying a database or retaining a migration source is insufficient. |
| 3 | Qualified operating envelope | Publish the actual Windows hardware, camera brands/firmware, camera count, codec, resolution, frame rate, bitrate, retention, measured load and known limits from the endurance run. |

Camera enrollment, administrative controls, analytics adapters and video-wall
features do not replace these reliability requirements. Generated-camera tests
are useful automated evidence; physical-camera compatibility and sustained
capacity need their own results. The 64-camera enrollment ceiling is not a
qualified recording or decoding capacity.

## Time integrity

UTC correctness and recording continuity remain required throughout this work.
The installed candidate must pass real forward/backward clock changes while
preserving retained-file hashes, playback and analytics timestamps, then restore
UTC and time services. Physical camera/NTP accuracy, DST display and independent
time-reference checks remain separate acceptance items. See the
[time acceptance matrix](WINDOWS_BETA_FIELD_TEST.md#time-acceptance-matrix).

## Release decision

Use one exact source revision and its matching installer bytes for the final
Windows qualification. Keep failed, skipped and unobserved outcomes explicit;
results from an older installer do not qualify a later build. Complete the
[unattended gates](WINDOWS_UNATTENDED_RECORDING.md#qualification-required-before-release)
and the [capacity/endurance field checks](WINDOWS_BETA_FIELD_TEST.md) before
claiming commercial readiness.

The operator installs and uses K5 graphically. These acceptance requirements do
not ask a customer to run PowerShell, Command Prompt, Docker or WSL.
