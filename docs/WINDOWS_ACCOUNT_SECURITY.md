# Windows account security and single sign-on

Account security is available from the graphical workspace. Users do not need a
terminal, a Python installation, or an environment-variable configuration.

## Authenticator and recovery codes

Open **Account security**, enter the current password, and begin authenticator
enrollment. Add the displayed `otpauth` account or secret to an authenticator,
then confirm with its six-digit code. Enrollment does not take effect until
confirmation succeeds. K5 then displays ten recovery codes once and requires a
new sign-in. Save those codes before leaving the confirmation screen. The
confirmation code has already been consumed; use the next authenticator code or
a recovery code when signing in again.

An enabled second factor applies to both password and single sign-on. Successful
primary authentication returns only a five-minute second-factor challenge;
there is no user session or media credential until the challenge succeeds.
Recovery codes are single use. Regenerating them requires the current password
and a valid factor and immediately replaces every old recovery code. Removing
the factor also requires the current password and a valid factor, and revokes
the account's active session. There is no administrator endpoint that silently
removes somebody else's factor.

TOTP uses RFC 6238 with SHA-1, six digits and 30-second steps, including one step
of clock tolerance. Used counters are recorded durably. K5 rejects a reused
counter, including after a host-clock rollback; saved recovery codes remain
available. Authenticator and recorder clocks must agree for normal TOTP login.
The time administration screen addresses recorder time health separately.
Challenges expire by elapsed time, so a wall-clock correction cannot extend
their five-minute lifetime. Failed attempts share a bounded per-user budget
across newly requested challenges.

Authenticator secrets and confidential OIDC client secrets are protected with
current-user Windows DPAPI and site/purpose-specific entropy. The separate
account-security SQLite file is admitted under the private identity profile,
including owner/DACL, file identity, schema and site. Recovery codes are stored
as SHA-256 digests. Non-Windows development uses the strictly private profile as
its secret-storage boundary. Moving the database alone to another Windows user
does not transfer DPAPI authority.

After the first successful initialization, a required-component anchor in the
existing users database prevents loss/deletion of the security database from
silently restoring password-only login. Startup then requires the admitted
security state. Enrollment and challenge records also carry a revocation epoch;
account, camera-grant, role, provider or link revocation invalidates pending
authentication even when no full session has been issued. Completion rechecks
that epoch after session issuance, closing disable/re-enable and role-change
races. OIDC-derived MFA additionally retains the exact provider/link revision.

## OpenID Connect

An administrator can configure **Administration → Single sign-on** with the
provider's exact issuer, display name, registered client ID and optional client
secret. The settings screen displays the callback URI that must be registered
with the identity provider. It follows the actual loopback workspace hostname
and port; register the hostname/port users open. The adapter uses authorization
code flow with PKCE S256. Public native clients use token authentication `none`;
confidential clients use `client_secret_basic`.

The provider must advertise code flow, S256 and RS256 in its discovery document.
Use **Test provider** to check that contract before enabling sign-in. The
discovery issuer must match exactly. Discovery/token/JWKS requests use verified
HTTPS, with numeric-loopback HTTP allowed for an intentionally local provider.
Credentials in endpoint URLs, redirects, duplicate JSON keys and oversized
responses are refused. This release supports RS256 signing keys of 2,048–4,096
bits. It does not negotiate an arbitrary signing algorithm from a token header.
Each HTTP exchange runs in an owned hidden process with a 15-second elapsed
deadline and forced termination/reaping. This bounds slow headers, trickling
bodies and resolver/TLS waits that ordinary socket inactivity timeouts cannot
bound. On Windows, a kill-on-close Job Object is assigned before private
request input is sent. The request child retains the normal verified TLS and
system/session proxy behavior; credentials never appear in command arguments.

Create explicit links from the provider's stable subject (`sub`) to an existing,
enabled, password-activated K5 account. An optional required group must be
present in the configured ID-token group claim. Configure the identity provider
to include that claim when using a group restriction. External display names,
email addresses and role claims do not create users or grant administrator
authority. K5's existing roles and camera permissions remain the authority.
Local password sign-in remains available, including for recovery administrators.

The adapter verifies signature, issuer, audience/authorized party, expiry,
issued-at, nonce and optional token/code hashes. Browser-bound state and PKCE
protect the authorization round trip. Identity/access tokens are transient and
are never placed in browser URLs or persisted. The callback supplies only a
single-use, browser-bound local completion code, valid for 60 elapsed seconds;
the workspace exchanges it for the ordinary K5 login response. Existing local
MFA is still required when enabled. Provider changes and identity unlinking
revoke affected current sessions; subsequent requests also use current K5
account authority. No refresh token is retained.

## Integration contract

The product initializes mandatory `AccountSecurityStore` and
`AccountSecurityService` before installing the login hook. An unreadable store
must fail closed, not silently restore password-only authentication. OIDC uses
`OIDCStore` in that same admitted database and `OIDCService`. The product closes
pending OIDC work before closing account-security state. Existing standalone
control-plane instances that do not install these product services retain their
previous authentication contract.

Self-service endpoints:

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/api/v1/operator/security` | Factor state and remaining recovery count |
| POST | `/api/v1/operator/security/mfa/enroll` | Reauthenticate and prepare a secret |
| POST | `/api/v1/operator/security/mfa/confirm` | Enable the prepared factor and issue recovery codes once |
| POST | `/api/v1/operator/security/mfa/disable` | Reauthenticate with password/factor and disable |
| POST | `/api/v1/operator/security/mfa/recovery-codes` | Replace recovery codes after reauthentication |
| POST | `/api/v1/auth/mfa` | Finish a pre-session challenge |

SSO endpoints use `/api/v1/operator/admin/sso/provider`, `/provider/test` and
`/links` for administration. Public `/api/v1/auth/oidc` advertises availability;
`/start`, `/callback` and `/complete` implement the browser flow. Secret responses
are not cached. Authenticated reauthentication failures are HTTP 403; a missing
or expired session is HTTP 401. Primary login requiring MFA returns HTTP 202
without a session token. Final authentication failures are HTTP 401, and private
state failures are HTTP 503. Validation errors omit submitted credential input.

## Qualification and scope

Focused tests use a real local HTTP discovery/token/JWKS server and real
RSA-signed ID tokens. They exercise the PKCE exchange, signature/claims,
browser binding, replay/expiry, explicit mappings, group restrictions,
disabled-account and provider changes, local MFA, recovery-code concurrency,
password-reset binding, restart persistence and normal password compatibility.
The JWT implementation is maintained PyJWT `2.15.1` (MIT), using cryptography
`50.0.2` (Apache-2.0 OR BSD-3-Clause) for RSA. Windows packaging must admit the
complete pinned wheel closure and retain its upstream notices.

Customer identity-provider registration and configuration require qualification
against that provider. The test fixture does not establish acceptance of a
particular Entra, Keycloak or other tenant. This implements OIDC federation; it
does not directly bind LDAP or implement SAML. MFA enrollment is self-service,
not an organization-wide forced-enrollment policy. Keep recovery codes and
another usable administrator account when deploying second factors.
