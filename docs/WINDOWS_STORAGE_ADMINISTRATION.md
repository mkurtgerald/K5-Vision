# Windows storage administration

The administrator Storage panel reads aggregate recording usage and free space,
changes the recording quota and minimum free-space reserve, and changes the
retention period. Users without administrator authority cannot read the recording
directory or change these settings. Policy and location settings are private,
site-bound durable state; revision checks prevent silently replacing a newer
administrator change.

Quota, minimum free space and retention apply to the existing recording manager
without restarting its monotonic observations. Lowering a limit can delete old,
safely closed recording segments. The recorder protects currently changing and
newest segments and never removes unknown files to satisfy its policy. A retention
period of zero disables age-based deletion; quota and free-space protection still
apply. Age means elapsed time observed with an unchanged file, not a timestamp in
its name or the Windows wall clock. A cold restart conservatively starts this
observation again. Clock corrections do not make footage suddenly eligible for
deletion.

## Recording location changes

Saving a new absolute local directory stages the location change. The response
continues to name the old active directory and reports `restart_required`.
Network shares, linked/reparse paths, ancestors writable by untrusted users,
identity roots and locations nested within the current recording tree are refused.

A folder or parent that other local users can read may be usable without
changing its permissions. On an explicit save, K5 can create a fresh protected
per-site container with a private `recordings` child. Windows sets its protected
DACL at directory creation; K5 does not repair the selected parent's ACL or adopt
its unknown files. The response and settings page name the actual prepared child
path, which can differ from the selected folder. Failure of ownership, fixed-volume,
reparse, mutation or Windows write-permission checks is not bypassed by creating
an alternate location. Ordinary startup and settings loading remain read-only.

Under the application's exclusive profile lease, with the recorder stopped,
startup calls `OperatorStorageSettings.migrate_pending()` before constructing
the recording service or background event sampler. The migration:

1. Admits a new private target, or a target previously marked for this exact site
   and source directory.
2. Copies every recognized retained recording and checks its SHA-256. Original
   files stay in their old location.
3. Rechecks the complete source inventory, destination inventory and directory
   ownership before atomically activating the new directory.

The copy is bounded by an elapsed deadline and existing recording-inventory
limits. A failed, interrupted, changed-source or conflicting-destination attempt
keeps the original location active, preserving history and recording capability.
The panel reports `migration_required` and a finite error. A later ordinary
restart can reuse copies whose complete bytes still match. An administrator can
cancel the pending change or choose a different empty directory without deleting
either copy. This operation never removes the old recordings or overwrites a
conflicting destination file.

If Windows terminates K5 during a file copy, its temporary file may survive
without running normal cleanup. A filename pattern cannot establish ownership of
an existing file. Such a target remains inactive with the finite
`interrupted_copy_preserved` reason; neither its partial files nor old footage are
deleted. Choose a new empty destination in Storage to complete migration through
the graphical application. Ordinary handled interruptions clean their own open
temporary file and can reuse already verified complete copies on retry.

The new active directory must be selected before constructing the MediaMTX
configuration, playback service and recorded-frame sampler, so saved person-event
UTC links continue to resolve to identical retained footage.
