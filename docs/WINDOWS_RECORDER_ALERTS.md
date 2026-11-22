# Recorder alerts

The operator workspace displays saved recorder alerts and recent recovery
history. This feature is part of the candidate source; it still requires the
matching installed-Windows qualification before release.

K5 reports three conditions:

- **Recorder engine unavailable:** the recorder's existing worker-health checks
  remain unhealthy for at least ten seconds while recording is enabled.
- **Storage is blocking recording:** the existing storage manager cannot admit
  recording. Check storage capacity, the free-space reserve and recorder status.
- **No new recording confirmed:** a camera expected to record has no fresh
  confirmed writing after the 45-second grace period. Check its connection and
  recording settings. Intentional pause and quiet, armed motion recording do not
  create this alert.

Alerts are collected in the background while the operator workspace is closed.
Active alerts survive a recorder restart and clear only after K5 observes the
relevant recovery, or removes the affected camera. The workspace includes a
bounded recent history with UTC timestamps. Clock corrections do not advance
the elapsed-time thresholds or reorder the saved transitions.

Operators see alerts for cameras they are permitted to use. Administrators can
see the site-wide state. If saved history or monitoring cannot be verified,
the workspace says that monitoring is unavailable; it does not report a healthy
recorder from stale data.

These are local product alerts. They do not send email, SMS or push messages.
A stopped computer or stopped recorder cannot report its own outage in the
background. An open workspace reports a failed status connection separately.
Alerts use the existing maintenance observations; they do not increase the
qualified camera capacity or remove storage-inventory limits. See
[commercial readiness](COMMERCIAL_READINESS.md).
