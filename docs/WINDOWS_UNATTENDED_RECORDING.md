# Unattended Windows recording

This work replaces the desktop-owned recorder with the Windows service
`K5VisionRecorder`. It is under development; the published
[`k5-windows-beta-2d74334` installer](https://github.com/mkurtgerald/K5-Vision/releases/tag/k5-windows-beta-2d74334)
does not contain this architecture. A source change or a passing unit test does
not qualify an installer for unattended operation.

## Required behavior

The graphical installer installs the recorder for the machine. Windows starts
it automatically without an interactive sign-in. Closing the camera wall,
quitting the desktop client, locking Windows, or signing out must leave
recording active. An unexpected recorder failure must trigger Windows service
recovery and resume recording from the saved camera configuration. A normal
administrative stop must finish safely without being treated as a crash.

The desktop app opens the local operator workspace and displays recorder
status. Its process lifetime does not control recording. Camera pause/resume,
users, roles, camera permissions, recording settings, playback, exports, and
audit remain authenticated product operations. Installation and first
administrator setup use graphical Windows elevation; operators do not run a
terminal or supply service-account credentials.

## Ownership and recovery

The service uses the dedicated virtual account
`NT SERVICE\K5VisionRecorder`, rather than an interactive user's identity.
Immutable application files live under Program Files. The Windows ProgramData
known folder supplies `K5Vision`, with private `identity`, `recordings`, and
`logs` directories. Only the recorder account, Windows Administrators, and
SYSTEM may access private site state. First-account creation executes under
the recorder identity so subsequent secret protection and file admission use
the same identity.

ProgramData remains a shared Windows container. Machine-state admission permits
its standard built-in Users attribute/extended-attribute rights only at that
exact native known-folder ancestor. Delete, delete-child, ownership and ACL
mutation remain forbidden there. The exception does not apply to K5's private
directories, other principals, arbitrary paths, or executable code. Windows'
global ProgramData permissions are never rewritten.

The pinned WinSW service wrapper supplies Windows Service Control Manager
integration. Recovery restarts are configured for 5, 15, and 60 seconds, with a
one-hour failure-count reset. The host owns its embedded Python backend and
worker job. Loss of that backend must end the host unsuccessfully so Windows
can recover the whole recorder. The desktop client cannot stop the service
through its ordinary status channel.

Local status exposes finite readiness information. Setup and administrative
control use a separate authenticated local pipe. Neither pipe accepts arbitrary
executables, profile paths, recording paths, or listening addresses. The
operator workspace remains local to this Windows station; this change does not
establish a supported remote-client or multi-recorder deployment.

## Existing installations

A machine-service upgrade must preserve the existing site. Until an actual
migration is qualified, an existing legacy per-user profile must produce a
visible migration-required state rather than initialize an empty replacement
site. The original profile and footage must remain intact.

The eventual migration needs a stopped, leased, copy-first snapshot. It must
preserve the site UUID, accounts and password verifiers, role/camera grants,
camera sources, recording policy, security generations, recovery-code hashes,
OIDC links, storage anchors, and recorded-file names and timestamps. Both MFA
secrets and OIDC client secrets use the old Windows user's DPAPI protection;
they must be decrypted in that user's context and protected again in the
recorder account's context through an authenticated transfer. Copying their
ciphertext or changing to machine-wide DPAPI is insufficient. Footage must be
hash-verified before committing changed absolute storage paths.

## Time integrity

Recording UTC, monotonic durations, clock-change compensation, camera-time
inspection, and retained-history integrity remain release requirements. A
Session 0 service cannot display an interactive Windows elevation prompt.
Host NTP configuration and resynchronization therefore need an explicit native
client approval path, while retaining authenticated administrator requests,
bounded private capabilities, exact live-issuer checks, cancellation, and
configuration readback. A successful configuration change alone does not prove
that UTC is accurate or that an NTP reference is reachable.

## Qualification required before release

The complete native workflow must pass, including its separate
`service-recording`, `time-administration`, camera, motion, upgrade, and clock
lanes. A graphical-installation witness alone does not qualify the service or
time controls. The service and time lanes use the original candidate artifact
from the same run, with matching source, installer, launcher, and manifest
identities; a failed GUI lane must not prevent independent diagnosis of those
boundaries. Older desktop-only publication checks are insufficient for this
architecture.

The actual installer and its source and payload hashes must be tied to a
Windows acceptance receipt that establishes:

- Native installation, exact service account, automatic startup, configured
  recovery actions, and a live recorder in Session 0.
- Actual recording growth after closing both browser and desktop client.
- Termination of the exact owned backend, Windows service recovery to a new
  process, renewed recording, retained site/users/cameras, and unchanged closed
  recording hashes.
- Authorized login, camera administration, decoded retained playback, and an
  exported MP4 that exists and opens after recovery.
- Recording after a real Windows sign-out and after a real reboot before an
  interactive sign-in. Configuration readback does not establish either.
- Matching native camera, administration, motion, storage, and clock gates,
  including the existing forward/backward clock-correction protections.

Hosted Windows CI can prove service configuration, Session 0, desktop-client
independence, and crash recovery. Reboot and real sign-out require an observed
session/boot transition with an independent witness. Receipts must keep these
outcomes false until observed, rather than infer them from automatic startup.
Physical mixed-brand compatibility, intended-capacity endurance, outage and
disk-pressure recovery, and independent time-reference checks remain necessary
for commercial readiness.
