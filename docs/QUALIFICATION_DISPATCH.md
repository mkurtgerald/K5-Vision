# Reviewed qualification dispatch

Ordinary pull-request and main quality checks run on hosted workers. Physical
qualification is a separate manual operation; a green hosted job does not replace
any Windows, physical, or human acceptance requirement.

For each retained physical workflow, select the reviewed revision and supply its
complete commit SHA as `reviewed_sha`. The workflow admits only a repository-owner
invocation (including the rerunning actor) whose supplied SHA equals the event's
immutable `github.sha`. Checkout uses that SHA and does not persist its token.
A moved branch therefore requires a new review and matching SHA, not a stale
approval. Required qualification tests and their commands are unchanged.

Before any physical invocation, establish runner admission, host/network
isolation, reviewed workflow/provisioning source, authorized input, and privacy
controls. The YAML condition is an admission constraint, not proof of those
external controls. Changing workflow source in an untrusted branch must never be
assumed safe merely because the normal workflow contains a guard.

Where a workflow has an independent source-free assertion, publication requires
that exact step's successful outcome. Its validated diagnostics may be retained
even when an earlier qualification test failed. A failed or skipped assertion
cannot authorize upload. Workflows without that separate assertion retain only
a successful qualification result. Existing validators and test outcomes remain
intact; no job failure is converted to a passing qualification.

These guards do not certify arbitrary JSON as secret-free. Strict evidence
schemas and independently validated publication snapshots remain part of the
publication boundary's further hardening. Keep detailed review material private.
