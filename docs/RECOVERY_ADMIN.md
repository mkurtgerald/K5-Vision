# Local recovery administrator foundation

The default account name is `recovery-admin`. There is **no default password**.
The installation owner chooses a unique password locally. This is an ordinary
enabled `administrator`, authenticated by the existing password/session boundary
and subject to the same RBAC and account lifecycle as other administrators.

## New, persistent installation

This CLI foundation is separate from the Windows Alpha media test launcher.
Use the installed `k5-vision` command in a private local terminal:

```text
k5-vision setup-admin --identity-dir <new-absolute-private-directory>
k5-vision serve --identity-dir <same-absolute-private-directory>
```

Choose a directory beneath an existing private per-user parent, for example a
new `K5VisionIdentity` directory beneath your Windows `%LOCALAPPDATA%`. Expand
the path to an absolute local path. Network paths, links, junctions, existing
directories and partial installations are refused. On POSIX the new identity
directory is owner-only. On Windows the application requires a fixed local volume
and reads native owner/DACL information before accepting the private parent,
identity directory, manifest and database (including existing SQLite sidecars).
Private state must be limited to the current user, Administrators and SYSTEM;
unverifiable security information, unsafe inherited access and replaceable
ancestors are refused. Remote/mapped network drives and reparse paths are refused.
For higher system ancestors only, the locally resolved Windows TrustedInstaller
service SID is also permitted; this does not extend private-state access.
The checks do not change or repair ACLs, ownership or an existing installation.

[Python 3.12.4 and later apply a current-user/Administrators ACL when creating a
Windows directory with `mode=0o700`](https://docs.python.org/3.12/library/os.html#os.mkdir);
earlier Python 3.12 patches ignore that mode.
This runtime behavior alone does not establish safe ancestry, file inheritance or
existing-state permissions. The application's independent admission checks still
apply and may refuse an earlier runtime's inherited directory permissions. Choose
an already private local parent; do not weaken ACLs to make setup pass.
Do not put identity state beneath `%TEMP%`, inside a package directory that an
upgrade replaces, or in a shared/synced folder.

Setup asks twice for a 12–256-character password with terminal echo disabled.
It refuses redirected input or a terminal that cannot hide input. Passwords are
not accepted through flags or environment variables and are never printed.
Keep the password in your own password manager. K5 stores only its normal salted
scrypt verifier, ordinary user metadata, a stable site ID, and field-only audit.

Keep the identity directory outside disposable or replaceable application files.
Startup reads it without creating, repairing or resetting missing data. It refuses
a conflicting configured site/user database and any Alpha synthetic/public-test
source. It binds only to loopback in this mode. Password authentication uses the same persisted verifier
when the CLI server starts again; opaque sessions are process-local, so sign in
again through `/api/v1/auth/login` after restart.

This option configures identity only. Existing device database, private operator
runtime and media/recording settings remain separate and must be configured through
their normal deployment paths. No device is enrolled and no recording is enabled
by account setup. `serve --operator --identity-dir ...` retains the existing
private-operator configuration requirements and authorization.

## Existing installations and use

Setup refuses any existing identity directory, user, credential or audit history;
it cannot overwrite a password, re-enable a disabled user or take over an existing
installation. If setup is interrupted, preserve the directory for diagnosis rather
than retrying over it or deleting a possibly initialized account.

For an already initialized service, sign in as a current administrator and use the
existing `POST /api/v1/users` creation flow with role `administrator`, enabled `true`
and username `recovery-admin` (or another explicit account name if occupied).
Complete the returned short-lived bootstrap credential using the existing
`/api/v1/auth/bootstrap-password` flow, entering the new password locally. Creation
and activation use the existing audit path. A duplicate name is an error, never
permission to reset or take over that account. An existing administrator must use
the normal authorized user-management workflow; there is no unauthenticated
recovery endpoint or universal credential.

Use a separate administrator for everyday work and retain the recovery credential
privately. Verify a normal recovery-account sign-in before relying on it, and
verify again after a service restart. Keep the identity directory backed up under
the owner's normal protected backup procedure. No password is recoverable from K5.

## Native Windows qualification boundary

The existing `Windows Alpha Script Smoke` workflow retains its hosted
`windows-latest` job and 15-minute limit. Its recovery selection checks the exact
PR head, uses disposable per-user local fixtures and starts real CLI processes
with the clean installed runtime probe. Native tests cover:

- numeric owner/current-user SIDs, explicit and inherited DACL evidence, fixed
  local volume, private state and non-replaceable ancestors
- lexical path refusal before filesystem access, real junctions and hardlinks
- two sequential CLI server processes, ordinary administrator login/RBAC, stable
  account/site state, old-session refusal and no duplicated setup events
- two-process setup and registry bootstrap races with one committed winner
- redirected setup input refusal and absence of fixture passwords/session tokens
  from retained CLI output, manifest, database plaintext and setup audit records

The fixture records the exact Python patch, Windows version and hosted-image
metadata. Existing job logs retain a credential-free metadata/scope summary;
raw ACL evidence is only in the per-job XML, with no artifact upload. The fixture
does not repair ACLs, create OS accounts, alter network mappings or use an
installation owner's credentials. Security refusal unit tests use synthetic
native-reader results rather than weakening real ACLs. A skipped Linux test is
not native qualification; native acceptance requires a green hosted run at the
exact reviewed revision. The workflow is a qualification route, not evidence that
a particular revision has already passed.

## Delivery boundary

This is source-level CLI and durable-identity groundwork. The native scope is
local CLI process restart, not Windows service installation, machine reboot or
upgrade survival. Mapped-volume refusal is exercised with synthetic drive-type
results, without creating a mapped share. Real terminal echo-hidden entry and a
packaged graphical setup/sign-in experience remain outside this qualification.
Setup audit records are not a comprehensive authentication or usage audit.
This change alone is not an owner-ready installer or completed deployment.
The existing Windows Alpha launchers, generated/public RTSP tests, temporary
device/user state, and media cleanup are unchanged and remain disposable. Do not
create a fallback account inside an Alpha test session and expect it to persist.
