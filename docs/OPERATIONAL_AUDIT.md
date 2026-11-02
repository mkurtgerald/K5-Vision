# Operational activity audit

The installed workspace keeps a private, site-bound operational activity database
separate from the existing account-change audit. Administration can filter the
activity by action, outcome, actor, target, UTC interval and append cursor, and
download the matching bounded JSON through the normal authenticated browser.

The log covers camera enrollment/probing/removal/reconnection, recording changes,
export preparation and transfer, analytics settings, storage and time changes,
integration operations, authentication, security settings and user permissions.
Background operations can append their actual completion or failure explicitly.
An accepted asynchronous export request is distinct from a completed export.
Denied or failed operator reads also produce a finite security-access record;
routine successful status, video and history polling does not fill the log.

Each audited HTTP operation appends a durable `started` record before performing
its action, then a terminal `success`, `denied`, `failure` or `interrupted` record
with the same generated request ID. Success describes completion of that HTTP
operation, not proof that a remote client saved a file. A started record without
a completion exposes an interrupted or incompletely audited operation. The
first record must persist before an audited operation can execute. A failed
completion latches the logger unavailable and prevents subsequent audited
operations; ordinary viewing and existing camera recording remain independent.
HTTP 202 acceptance and federation redirects retain the `started` outcome.
They do not claim completed export preparation or a fully authenticated session.
The actual job, MFA and federation completions append their terminal results.

Times are observed host UTC, accompanied by a per-run identity and elapsed
milliseconds. Durable append IDs establish order across UTC corrections and
process restarts. UTC age never authorizes audit deletion. Retention holds the
latest 100,000 records and reports its first retained ID. The database has a
128 MiB size ceiling. Queries return at most 100 records; downloads return at
most 10,000 records and 8 MiB. The default download limit is 5,000. `has_more`
and `next_before` explicitly identify additional matching records.

The audit accepts only fixed actions, outcomes, target types and changed-field
names. Actors and targets are local UUIDs. Request bodies, cookies, authorization
headers, query strings, credentials, camera addresses, private paths and remote
reasoning text are never recorded. Export details can contain only an export
UUID, selected UTC start, duration and byte count. Integration metadata can name
its fixed provider class and bounded response count. These are validated fields,
not an arbitrary JSON extension surface.

Private directory and file ownership, Windows DACL/volume checks, file identity,
site binding and SQLite transactions protect the store. A retained hash chain
detects changed rows, truncated history and broken retention anchors. It is not
an externally witnessed or immutable compliance ledger: the Windows account
owner can replace both application and database. Existing account-change history
remains available independently.
After its first admitted initialization, a durable anchor in the account
authority database requires the audit file. File loss fails admission instead
of silently replacing historical activity with an empty log.

## Composition

Create `OperatorAuditStore(identity.directory, identity.site_id)` under the
profile lease and assign `app.state.operator_audit`. Install
`OperatorAuditMiddleware` after the other product middleware so it wraps their
outcomes. Install `install_operator_audit_api` with the product's current-human
`require_admin` and `authenticate` callbacks. Call `set_audit_actor(account)`
after authenticating an account, including successful login and federation.
Close the store after owned tasks and requests finish, before releasing the
profile lease.

Direct producers call `record(action, outcome=..., actor=account_or_uuid,
target_kind=..., target_id=uuid, changes=(...))`. The optional `details` mapping
is restricted to `export_id`, `clip_start`, `clip_duration_seconds`, `bytes`,
`provider_type` (`neural` or `analytics`) and `response_count` (0 through 128).
Use an actual job's verified metadata, never raw HTTP or provider payloads.
`record` returns its append ID. Storage failures raise the finite
`OperatorAuditError`; invalid producer metadata raises `ValueError`.

Attach the same store to the export manager with `manager.set_audit(store)`
before accepting requests. The manager records actual verified preparation as
`export.completed` and failures/cancellation as `export.failed`. It refuses to
offer a newly prepared file when its configured audit cannot commit. The file
API should call `manager.audit_download(job, outcome=...)` after its actual
transfer to include the selected evidence in that operation's audit. This hook
does not replace the API's current role, camera and credential checks.

The read API is `/api/v1/operator/admin/activity`; its attachment endpoint is
`/api/v1/operator/admin/activity/export`. Both require a currently enabled
administrator and recheck that authority after suspended database work. Download
links carry no credentials. Browser code requests the attachment with its bearer
session, then saves the returned JSON Blob normally.
