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
directory is owner-only; on Windows it inherits the chosen parent's ACL, so the
parent must already be private to the installing user. Setup does not change an
existing directory's permissions. Do not put identity state beneath `%TEMP%`,
inside a package directory that an upgrade replaces, or in a shared/synced folder.

Setup asks twice for a 12–256-character password with terminal echo disabled.
It refuses redirected input or a terminal that cannot hide input. Passwords are
not accepted through flags or environment variables and are never printed.
Keep the password in your own password manager. K5 stores only its normal salted
scrypt verifier, ordinary user metadata, a stable site ID, and field-only audit.

The identity directory must survive restart and upgrade. Startup reads it without
creating, repairing or resetting missing data. It refuses a conflicting configured
site/user database and any Alpha synthetic/public-test source. It binds only to
loopback in this mode. Password authentication survives service restart; opaque
sessions do not, so sign in again through `/api/v1/auth/login` after restart.

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

## Delivery boundary

This is source-level CLI and durable-identity groundwork. A packaged graphical
setup/sign-in experience and native Windows restart/upgrade acceptance remain
open; this change alone is not an owner-ready installer or completed deployment.
The existing Windows Alpha launchers, generated/public RTSP tests, temporary
device/user state, and media cleanup are unchanged and remain disposable. Do not
create a fallback account inside an Alpha test session and expect it to persist.
