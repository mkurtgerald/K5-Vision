# Native Windows beta sprint

The delivered recording candidate is a double-click Windows installer with
graphical account setup and ONVIF camera onboarding. Testers find a camera or
enter its IP address, enter the camera account, choose an H.264 stream and
connect. K5 obtains the stream address automatically. Setup and normal use
require no terminal, separate Python installation or manual RTSP URL.

Use [the desktop guide](WINDOWS_DESKTOP_BETA.md) for current installation and
camera connection steps. Earlier script-based pilot instructions are historical
engineering evidence, not the tester workflow.

## Delivered and verified

The delivered ONVIF candidate was verified at exact source revision
`a581098a1510d3b214c11fbfa3d28625b9419d49`, under
[PR #344](https://github.com/mkurtgerald/K5-Vision/pull/344).

| Workstream | Delivered behavior | Evidence at that revision |
| --- | --- | --- |
| Windows installation | Native EXE installer, bundled runtime, account window, Start/Desktop shortcuts and profile-preserving uninstall | Actual Windows graphical lifecycle passed; no console windows or external Python required |
| Camera onboarding | ONVIF credentials, H.264 profile selection and automatic stream resolution | Real SOAP fixture, wrong-password rejection, profile/connect flow and Edge live video passed without entering a stream URL |
| Discovery | Bounded search across active local IPv4 interfaces, with numeric-IP fallback | GUI discovery executed but did not find its fixture, so that run used IP fallback. Two separate actual multicast tests passed |
| Recording and lifecycle | Recording while windows are closed, tray controls, single-instance reopening, stop/restart and retained history | Native receipt verified recording growth while closed, decoded recordings, owned-process cleanup, restart persistence and uninstall preserving media |
| Camera identity | Removal/restoration and ONVIF endpoint changes preserve camera identity/history | API/state tests and native browser regression passed, including atomic registry/private-source updates |
| Optional sampled detection | Separate engineering model/runtime and transient sampled-view checks | Person regression lane passed; detection is outside the bundled desktop runtime and recording-beta field acceptance |

The [native desktop run](https://github.com/mkurtgerald/K5-Vision/actions/runs/39159696655)
passed its actual, non-skipped GUI test and 232 focused tests, with nine focused
tests skipped. Its receipt records Windows execution, generated media, no manual
stream URI, no tester terminal, and no physical-camera, capacity or endurance
acceptance. The GUI run lasted 67.33 seconds.

The [five-lane Windows regression run](https://github.com/mkurtgerald/K5-Vision/actions/runs/39159647805)
passed browser, person, install, launcher and product lanes. The
[repository CI run](https://github.com/mkurtgerald/K5-Vision/actions/runs/39159647820)
passed 3,786 tests, with 151 skipped and 84% coverage. Skipped tests supply no
acceptance evidence. The installer and finite metadata are in the
[qualified desktop artifact](https://github.com/mkurtgerald/K5-Vision/actions/runs/39159696655/artifacts/12148079412).

These results establish the tested software behavior with generated streams and
emulated camera responses on native Windows. They do not establish camera
firmware compatibility, sustained hardware capacity or long-duration recording.

## ONVIF interoperability hardening

The next increment handles camera clock differences through the standard,
read-only `GetSystemDateAndTime` operation. K5 uses the returned UTC time for
authentication without changing either clock. An unavailable or malformed time
response leaves most of the connection budget for normal authentication.
The qualification requires actual authenticated cameras with positive and
negative clock offsets, wrong-password rejection, and an installed Windows GUI
run with a skewed camera clock. The earlier artifact linked above predates this
increment; require the new revision's passing receipt before distributing it.

Setup also distinguishes an accepted camera account from available video.
Saving an ONVIF profile shows that K5 is waiting for video until the worker
reports the stream available, with recording status shown separately.

## Remaining beta gates

The next work is qualification on the intended Windows station and representative
cameras. Keep these gates pending until the required observations exist.

| Priority | Gate | Required result |
| --- | --- | --- |
| 1 | Physical-camera compatibility | Install through the GUI, connect representative ONVIF cameras, select their H.264 profiles, and verify advancing live video and recorded playback |
| 2 | Ordinary recovery | Outage/reconnect, sign-out, window-to-tray behavior, restart, removal/restoration and upgrade preserve the expected access and history |
| 3 | Measured capacity | Run the intended camera count and actual stream settings for eight hours; measure CPU, memory, recording growth and playback failures |
| 4 | Storage retention | Exercise the installed quota and age retention using consented test media; verify retained playback and report pauses or gaps |
| 5 | Endurance | Complete at least a 24-hour recording run with an outage and application restart; investigate drift, gaps and playback failures, extending the run when needed |

The [field procedure](WINDOWS_BETA_FIELD_TEST.md) uses the desktop application,
browser and ordinary Windows tools. Access to the actual station and cameras
is the prerequisite. Schedule estimates follow field observations; test counts
and available compute do not establish a completion date.

## Operating and release limits

The candidate is unsigned, requires Windows 10/11 x64, and needs Internet access
on first launch to obtain its verified native recording workers. It runs for the
signed-in Windows user. Closing either window keeps recording active; **Stop
and quit**, Windows sign-out and shutdown end recording. It is not a boot
service. Cameras require reachable ONVIF support and an H.264 stream.

Preserve the exact installer revision, hashes and receipts for a reviewable
release. A beta decision names the field-verified hardware, camera models,
stream settings, camera count, duration and known limits. Until the required
physical, capacity, retention and endurance results exist, this delivery remains
a recording candidate. Optional person detection requires its own camera-scene,
accuracy and distribution qualification.
