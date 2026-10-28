# Windows surveillance workspace sprint

The product goal is to watch cameras, confirm recording, find footage and save a
clip without a terminal. This increment adds that workflow to the native Windows
installer and its browser workspace. Obtain installers from
[public GitHub Releases](https://github.com/mkurtgerald/K5-Vision/releases);
the [desktop guide](WINDOWS_DESKTOP_BETA.md) covers installation and ONVIF setup.

## Product scope

| Task | Workspace behavior |
| --- | --- |
| Watch several cameras | Camera wall with 1, 4 or 9 tiles, paging, and per-camera Watch / Recordings actions |
| Know whether footage is being saved | Recording confirmation from observed disk activity, separate from camera connectivity and enabled recording |
| Control a camera's recording | Persistent administrator pause/resume while live viewing and retained history continue |
| Find footage | Latest recording, 15-minute / one-hour / 24-hour windows, explicit UTC intervals, and a visual timeline that preserves gaps |
| Save a clip | Playback, start/end marks and a real MP4 download of up to five minutes inside one available interval |
| Keep the recorder running | Native launcher/tray with a primary Open camera wall action; closing viewing windows leaves the engine running |

The native window starts and supervises K5; viewing, recordings and camera
controls live in the browser workspace. Engine readiness does not claim cameras
are recording. Setup remains native Windows, with no external Python, Docker,
WSL or manual stream URL required.

The [workspace guide](SURVEILLANCE_WORKSPACE.md) describes the operator journey
and recording states. Person detection remains a separately provisioned
engineering capability, outside this bundled workspace increment.

## Release gates

A release claiming these features needs a passing Windows receipt tied to its
exact source revision and installer hash. It must exercise the installed GUI,
ONVIF onboarding, concurrent wall video, recording pause/resume and persistence,
honest recording states, timeline seeks, decoded playback, and a saved MP4 that
opens independently. Switching cameras, paging, gaps and sign-out must preserve
the expected camera and authorization boundaries.

The same installer must pass its isolated Windows clock gate: real +48-hour and
-48-hour guest clock corrections, session/live/recording checks, explicit UTC
history and retained-file hashes, followed by restoration of guest UTC and time
services. This automation uses a disposable VM and an independent recovery
watchdog; testers do not run it themselves.

Historical clock qualification exists for source
[`c6dbf70abc9a7bede7a07602e52e5d5a7fb753a0`](https://github.com/mkurtgerald/K5-Vision/commit/c6dbf70abc9a7bede7a07602e52e5d5a7fb753a0).
That proof applies to its own installer. It does not qualify this later camera
wall, recording-control, timeline or clip-export increment. Publish only the
features established by the new release's matching receipts.

Clock compensation is read-only and relative. K5 does not set clocks or NTP
configuration, establish time-server health or certify absolute UTC accuracy.
The [time matrix](WINDOWS_BETA_FIELD_TEST.md#time-acceptance-matrix) remains the
reference for physical-camera clock behavior, UTC/DST and independent accuracy.

## Remaining beta gaps

Physical-camera models and firmware still need compatibility and recovery
qualification. Hardware capacity needs an eight-hour run at the intended camera
count and stream settings; endurance needs at least 24 hours, including an outage
and restart. Retention and retained playback must be checked under the site's
actual storage conditions. Automated generated-media results do not replace
these [field results](WINDOWS_BETA_FIELD_TEST.md).

The application runs for the signed-in Windows user; a boot service is outside
this increment. Bundled AI, PTZ, events/event search and storage-location or quota
configuration are also outside it. The recording default remains a 20 GiB quota
with conservative elapsed-age retention; this workspace does not add a storage
settings screen. A beta decision must name the tested cameras, hardware, stream
settings, count, duration and known limits.
