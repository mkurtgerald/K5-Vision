# Administration in the Windows workspace

The workspace provides graphical settings; operators do not run terminal commands.
Select **Administration → Recorder settings** to open the settings pages. **Account
security** in the top bar opens the signed-in user's authenticator settings.
Pages and controls follow current account permissions; backend checks remain
mandatory for every action.

## Storage

**Storage** shows the active recording directory, observed recording usage and
free disk space. Save a quota, minimum free space and closed-footage retention
period. Lowering limits can remove older safely closed recordings. A retention
value of zero disables age pruning while capacity safeguards remain active.
Retention uses elapsed stable-file observations rather than wall-clock ages.

Changing the directory creates a pending change. Restart K5 normally so its
startup migration can copy and verify retained footage before activating the
new location. Original files are preserved. The page distinguishes active,
restart-required and migration-required states; a saved pending directory is
not presented as active. See [storage administration](WINDOWS_STORAGE_ADMINISTRATION.md).

## Time

**Time** separates configured Windows Time sources, service observations and
independent reference measurements. Saving a source configuration does not mark
synchronization verified. **Synchronize Windows Time** requests native Windows
Time resynchronization; Windows can display its normal administrator prompt.

Select a camera to read its ONVIF clock and NTP settings. If its stream account
has insufficient management rights, optionally provide a separate management
account for the next action. Both fields are cleared after use and on leaving
this page. **Set camera UTC to recorder UTC** requires the explicit checkbox;
its verification concerns relative camera/recorder offset. Absolute UTC accuracy
depends on the recorder's reference. See [time administration](WINDOWS_TIME_ADMINISTRATION.md).

## Audit trail

**Audit trail** filters recorder and security activity. Results use stable append
order across clock changes. A *Started / incomplete* row is retained attempt
evidence; a terminal row with the same request identifier records its result.
**Download audit JSON** uses a normal browser download, excludes credentials,
and includes pagination fields when more than 5,000 matching rows exist.
**Administration → Account activity** retains the narrower account-change view.
See [operational auditing](OPERATIONAL_AUDIT.md).

## Integrations and events

**Integrations** configures K5's authenticated provider handoff and explicit
camera grants. A generated provider token appears once. Save it privately before
leaving the page; it cannot be recovered from the connection list. A declared
provider origin does not contact or start an inference engine. Readiness is
based on an actual compatible provider handshake, and Neural analysis does not
authorize device actions. Built-in person detection remains available separately
in camera **History**.

Accepted external analytics observations appear as **Recent provider events** in
camera History, preserving candidate/confirmed/cleared status and reported UTC.
Selecting an event opens footage at its reported time, if that footage exists.
This recent list spans recorded history independently of the selected
built-in-person event interval. See [provider handoff](OPERATOR_INTEGRATIONS.md).

## Roles and organization sign-in

**Custom roles** starts from a built-in role and narrows its allowed permissions.
In an existing account's editor, choose a profile and select **Apply role
profile**. The proposed base role is shown explicitly, and the target's previous
sessions are revoked. Removing a custom profile keeps its existing base role;
then edit the built-in role normally. Camera grants remain independent.
Delegated administrators cannot grant permissions they do not hold or take over
stronger accounts. K5 preserves at least one enabled administrator with an active
password and every product permission; account-management permissions alone do
not make a recovery administrator.

**Organization sign-in** saves an OpenID Connect provider, shows the exact local
redirect URI to register, and tests issuer, PKCE and signing compatibility.
Explicitly link each provider subject to an existing K5 account, with an optional
required group. A saved configuration or successful metadata test is not proof
of a particular organization's completed sign-in. Local password sign-in remains
available. See [account security](WINDOWS_ACCOUNT_SECURITY.md).
Changing the site-wide provider requires every product permission because that
provider can authenticate all linked accounts. Linking or unlinking an identity
requires authority over the target account's current permissions. Restricted
administrators see these limits in the graphical controls.

## Authenticator and recovery codes

In **Account security**, enter your current password and select **Set up
authenticator**. Add the displayed secret to an authenticator and confirm its
current six-digit code within five minutes. After confirmation, existing
sessions are revoked. Save the one-time recovery codes and select **I saved my
recovery codes** to return to sign-in. Background UI refresh pauses while these
codes are displayed so revocation cannot erase them before they are saved.

Future sign-ins accept a current authenticator code or an unused recovery code
only after the password succeeds. Codes and setup secrets are not stored in
browser storage. Disabling the authenticator or replacing recovery codes
requires the current password and a valid factor. Each recovery code works once.
