# Activity history

Administrators can use **Audit trail** in the Windows workspace to review recorder
and security activity. Filter by action, outcome, actor, target or UTC interval.
**Administration → Account activity** provides the separate account-change view.

## Interpreting results

A *Started / incomplete* entry records an attempted operation, not a completed
one. A terminal entry with the same request identifier records its result.
An accepted export request is not proof that export preparation or a download
finished.

Timestamps reflect the recorder's observed UTC and can be affected by clock
corrections. The activity list preserves entry order across those corrections.

## Downloading activity

Use **Download audit JSON** to save matching activity through the browser.
History and downloads are bounded, so older activity may no longer be available
and one download may not include every matching entry. Check the included
pagination information before treating a download as complete.

Handle downloaded activity as private operational information. This local
history is not an independently witnessed or immutable compliance record.

See [Administration in the Windows workspace](ADMIN_SETTINGS_WORKSPACE.md) for
the related operator controls.
