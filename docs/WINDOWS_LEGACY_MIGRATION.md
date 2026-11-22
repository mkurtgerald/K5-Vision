# Native Windows legacy migration

This implementation connects the existing non-activatable identity snapshot to
an authenticated graphical import and a service-owned copy-first transaction.
Portable tests establish the store and transaction behavior. A release still
requires the matching Windows installer, real-token DPAPI, ACL, graphical
upgrade, retained playback/export and interruption qualification results.

## Supported authorization boundary

The initial native flow requires the Windows administrator who owns the old
per-user K5 profile. Elevation carries only that user's public SID and refuses a
different Windows account. The existing K5 administrator must supply their
current password and configured MFA or recovery factor in the graphical form.
Cross-user imports and imports by a Windows standard user are refused; there is
no replacement-site or empty-reset option.

The installed service records the SIDs of detected legacy profiles in private
machine data before enabling automatic boot. Its startup and initialization
paths refuse a fresh site while migration is required. A pending transaction
also blocks backend startup, including a crash after moving the validated
identity directory but before writing the committed journal.

## Transfer and activation

1. The sealed native GUI creates a private first-instance local pipe and starts
   its exact source helper in an owned Windows Job. The helper checks its actual
   creator, token, session, executable, process handle and creation identity.
2. The source helper admits the fixed old profile before acquiring its stopped
   profile lease. It authenticates the existing administrator and retains a live
   password/MFA-version-bound permit throughout source preparation and copying.
3. The native service independently admits the GUI's elevated pipe token,
   sealed image and exact live process. Its own Job-owned helper uses the actual
   recorder account to protect and verify MFA and OIDC secrets. Cleartext travels
   only in bounded private frames; credentials never enter argv, environment,
   logs or migration files.
4. The old-user helper copies admitted identity, completed recording segments
   and completed export metadata/files to the fixed private service stage. It
   first checks service-volume capacity for the full copy, identity validation
   clone, metadata allowance and the retained storage policy's free-space reserve.
   verifies source/destination identities, hashes and modification times.
   The copy primitive accepts only exact complete files when resuming the same
   transfer binding; foreign and partial files refuse. A new graphical attempt
   uses a new binding and preserves, rather than reuses, the old staged copy.
   Source paths and file contents do not pass through graphical controls.
5. The target verifies every retained byte, validates copied stores and exports,
   and rebases only the copied storage policy to the fixed service recording
   root. The global identity remains absent during preparation.
6. After preparation, the source independently reseals its authorities and media
   inventory and rechecks the live original administrator. Only then does it
   request the separate explicit commit. The target journals directory moves and
   publishes the validated identity last. The committed journal is the sole
   success authority.

Original profiles, footage, exports and their ACLs are preserved. Successful MFA
authentication consumes the original replay counter or recovery factor before
the source snapshot is fingerprinted. Staged ciphertext is service-token DPAPI;
the old-user ciphertext remains in the original profile.

## Failure and recovery

Cancellation and helper loss close only owned handles and Jobs. A fresh
authenticated attempt may roll back an interrupted target transaction only for
the same site and Windows owner. It preserves the old stage and original data,
then begins a new transfer. Foreign sites, owners, journal tampering and a
committed site are refused. A lost final reply is reconciled against the private
journal rather than interpreted as permission to roll back a committed site.

Frames have a fixed 128 KiB ceiling, including the largest supported Unicode
OIDC secret plus bounded DPAPI ciphertext. Service RPCs remain bounded to 30
seconds; long preparation and explicit commit are polled. Bulk import has a
fixed 24-hour monotonic lifetime with cancellable owned helpers. These migration
bounds do not change recording, motion, decoder freshness or clock deadlines.
The number of retained transaction journals is bounded; staged data is never
silently deleted to make a failed import appear successful.
