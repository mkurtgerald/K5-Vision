# Legacy identity migration: isolated preparation primitive

`legacy_identity_snapshot.prepare_identity_snapshot` prepares a separate private
copy of a stopped legacy identity. It is not wired into Setup, the desktop GUI,
or the recorder service. Existing migration-required refusals remain in place.
Its result always says `activation_allowed: false`; this is not an upgrade
qualification receipt.

The copied manifest is deliberately named `identity.json.snapshot`, so normal
product identity admission rejects the staging directory. A later native
activation must publish its final manifest only after all migration gates pass.

The caller must hold the existing `OperatorProfileLock` for the exact original
identity directory throughout the operation. The primitive validates the real
lease, directory identities, private single-link files, local path custody,
stable reads and hashes. It never stops a process by name, changes an original
file or ACL, or activates an old recording directory. Journals and interrupted
saves are refused before an ordinary SQLite reader can modify the source by
recovering a journal or creating a shared-memory sidecar. Reads are bounded by
the admitted file size (maximum 128 MiB each), with a 300-second monotonic budget
checked during blocks and between steps. The future native sink must also bound
its own operations; this primitive cannot forcibly interrupt a blocked callback.
Closed WAL-mode `users.sqlite3` is also refused from its on-disk SQLite header
before any ordinary identity reader runs, even when no sidecars currently exist.
Its valid data is left intact; support for immutable WAL-mode source admission
requires a separate migration-controller change rather than changing the source.

The known authority set is copied byte for byte: `identity.json`,
`users.sqlite3`, `devices.sqlite3`, `operator-cameras.sqlite3`,
`operator-account-security.sqlite3`, `operator-audit.sqlite3`,
`operator-events.sqlite3`, `operator-integrations.sqlite3`,
`role-profiles.json`, `operator-storage.json`, and
`operator-recording-policy.json`. Optional absent authorities remain absent;
this primitive does not initialize missing stores. Unknown entries refuse.
Normal full store/schema admission is still mandatory before activation.

The original site UUID, accounts, password verifiers, camera sources, grants,
security generations, MFA replay counters and recovery-code hashes, OIDC links,
audit/event timestamps, component anchors and recording-policy anchors are
retained. Only encrypted MFA/OIDC columns in the new security database change.
Its original schema and every nonsecret row, including SQLite storage classes,
are compared with the source. All other staged authority hashes must still
equal their source hashes at completion. Existing camera credentials retain
their existing private-store format; no extra decoded credential export is
created.

MFA and OIDC secrets are unwrapped through current-user DPAPI in the legacy
GUI user's context. One bounded writable buffer at a time is passed to the
`SecretSink` contract and cleared after use. The sink must protect and verify
the same value using the exact recorder account's current-user DPAPI. Neither
endpoint may log or persist an unwrapped value. The primitive never selects
machine-scope DPAPI, writes a plaintext transfer file, or puts credentials in
arguments or environment variables. Python/DPAPI may make transient immutable
memory copies; this implementation does not claim complete memory erasure.

`SecretSink` is an explicit integration contract, not a native transport or a
claim that a callback authenticated itself. Its admission must bind the live
legacy GUI principal, site, transfer UUID and source digest, and reject revoked,
expired or disconnected peers. Admission is checked before copying, around
each secret and before completion. A boolean admission result is refused.
The future native channel must independently establish these properties.

A fixed private marker binds the transfer and source hashes. An interrupted
rewrap rolls back as one SQLite transaction. Resuming completed preparation
requires the same transfer and unchanged source and repeats service-side
verification of every protected secret without re-encrypting it. The staged
security database has a private bookkeeping table; it is not an activated
production database. An incomplete file, interrupted SQLite journal, changed
schema/ciphertext, unrelated target or modified marker refuses without
overwriting it. Recovery after an arbitrary power loss in the middle of a copy
still needs a native controller that can safely retire its own partial stage.

The receipt lists omitted nonauthority entries. `operator.lock` stays held at
its original location. Worker configuration is regenerated later. Pending time
capabilities are never imported; a nonempty `time-admin` directory refuses.
Completed exports, analytics scratch files and footage are not copied by this
identity-only primitive, and remain intact at the source. The unchanged storage
JSON may still contain absolute per-user or other-drive paths. It must not be
used by the service until a separate copy and path-activation stage completes.

Remaining native integration is explicit:

1. Authenticate the existing product administrator, including MFA, in the
   original Windows user's GUI context; retain live credential/authority checks.
2. Bind that GUI and any elevated helper across UAC to the actual old Windows
   SID and native process identity. Implement the admitted private channel and
   service-owned staging without credential arguments, environment or files.
3. Copy and hash-verify footage and completed exports into new admitted storage,
   preserving site/device/stream identifiers, filenames, timezone offsets, MP4
   bytes, audit chains and history timestamps. Keep old storage/ACLs intact.
4. Validate every staged authority under the actual recorder account, finish
   bookkeeping, then durably activate a complete new site with resumable
   commit/rollback. Existing target sites must never be overwritten.
5. Prove stopped upgrade, failure recovery, retained-history playback, old-user
   DPAPI refusal in the service and successful rewrap on real Windows. Unit
   tests use separate authenticated-encryption contexts; they do not qualify
   native DPAPI, GUI/UAC, service transport, activation, or the whole upgrade.
