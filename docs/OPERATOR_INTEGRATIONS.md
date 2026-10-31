# Lab integration administration and downstream handoff

The Windows workspace owns service connections, camera grants, machine credentials,
metadata delivery and operator event access. The bundled OpenVINO person detector
continues to work independently of connected labs.

## What is implemented

Administration → Integrations creates named **Neural** or **Analytics** connections.
An administrator explicitly selects cameras and enables a connection. A generated
machine token is shown once; subsequent reads show only whether one is configured.
Replacing the token or disabling/deleting the connection immediately revokes its
old machine access. Human bearer sessions and browser cookies cannot substitute for
this separate credential. Current camera membership and current grants are checked
again for every machine read, event and response.

Saving configuration does not claim a provider exists. A connection remains
`awaiting_provider` until an actual authenticated compatible handshake arrives.
The `ready` status means a recent accepted contract handshake, not successful model
inference, prediction accuracy or a production-qualified shared Neural service.
Last observation/response UTC and queue/exhaustion counts show actual handoff
activity. Provider leases use elapsed time, expire after 60 seconds and are not
restored after K5 restarts.

The optional origin is a declared provider identity. K5 does not fetch it, discover
endpoints from it, redirect requests or forward credentials to it. The current
native client is a colocated numeric-loopback HTTP client, matching the desktop
gateway's existing local scope. Remote listener deployment remains separate.

## Actual upstream boundaries

The independent codecs implement these reviewed upstream data contracts:

- Analytics Lab `docs/analytic-event-schema.md`, draft `0.1`, source revision
  `0b6724978c352e5586afdfed67194a8852a370b4`.
- K5 Lab's generic adapter contract `2`, source revision
  `0920b589466e2627dd180e7f83470363cab2cfe6`. It carries opaque references and
  permanently non-authorizing recommendations; transport belongs downstream.

Neither reviewed lab supplies a production shared Neural HTTP service. K5 does not
invent an upstream URL or silently activate a research runtime. No private lab
implementation, protected vocabulary, research weights or reference data is copied
into this product. An admitted provider host must connect to the K5-owned handoff;
that host's deployment, actual inference and accuracy require their own acceptance.

`K5LabBridgeClient` is the reusable native Python bridge for an admitted colocated
host. It performs actual bounded HTTP handshakes, observation reads and correlated
response writes. It neither starts an arbitrary executable nor imports a provider
named by an administrator. Its three-second elapsed watchdog closes the actual
socket even when a peer slowly sends headers or body bytes. Redirects are never
followed, and returned JSON is bounded.

## Administration API

All routes require the current human administrator and the existing origin checks:

| Method | Route | Purpose |
| --- | --- | --- |
| GET / POST | `/api/v1/operator/admin/integrations` | List / create connections |
| PATCH / DELETE | `/api/v1/operator/admin/integrations/{id}` | Configure / revoke |
| POST | `/api/v1/operator/admin/integrations/{id}/test` | Check actual current handshake health |
| POST | `/api/v1/operator/admin/integrations/{id}/retry` | Explicitly renew an exhausted retry budget |

Creation fields are `name`, `kind` (`neural` or `analytics`), optional `origin`,
optional write-only `token`, `enabled`, and `camera_ids`. Enabling without camera
grants is refused. Omitted tokens are generated and returned as `setup_token` once.
A PATCH omitting `token` preserves its current hash. The private site-bound SQLite
store retains only the SHA-256 machine-token identity, never the token itself.
Configuration and journal files use the same profile/DACL admission as camera and
account state. The database is capped at 64 MiB. Successful initialization seals
its required-state anchor in the durable account database; later loss of the
integration database fails closed instead of silently resetting credentials,
queue history or trust. No separate resettable marker is used.

## Machine handoff API

Machines use exactly one `X-Integration-Token` header. Human `Authorization`
headers are refused on these routes. All responses are private and uncached.
Deployment must enforce the 64 KiB machine request limit before JSON parsing.

| Method | Route suffix beneath `/api/v1/operator/integrations/{id}` | Payload |
| --- | --- | --- |
| POST | `/handshake` | `{"supported_versions":["2"]}` for Neural; `["0.1"]` for Analytics |
| GET | `/observations?limit=10` | Neural requests, maximum 20 per pull |
| POST | `/responses` | Exact `request` and correlated `response` objects |
| POST | `/events` | Analytics Lab draft0.1 envelope |

A Neural pull returns a K5 transport wrapper with `request`, `observation` and
`delivery`. The nested request has exactly the upstream five fields:
`contract_version`, `request_id`, `partition`, `record_id`, `timeout_ms`.
The record reference and partition are opaque hashes. The separate observation
contains only committed recorded-person metadata, original frame UTC and a K5
recording reference; no camera URL, password, image or private filesystem path.

Responses must match version, request, partition and record identity exactly and
must carry `authorized:false`. A recommendation requires a bounded operation and
finite confidence. Other statuses forbid operation/confidence. Accepted responses
remain non-executing; there is no device-action or execution-grant path here.

The persisted observation identity and request identity remain unchanged on retry,
restart or explicit retry-budget renewal. Delivery is at least once; providers
must deduplicate on these stable identities. Elapsed backoff increases from two
to 60 seconds, with eight automatic attempts. Exhaustion is visible, does not loop
forever and requires explicit administrator retry. A connection has at most 1,000
pending records; the shared retained response and inbound-event journals each keep
10,000 rows. Retained source history and queue limits bound replay; these are not
claims of an unlimited, lossless archive during prolonged provider outages.

## Analytics event integrity

The draft0.1 adapter validates site, current camera UUID/grant, source schema,
UUID event identity, confidence, status and aware source times. It maps candidate,
confirmed, cleared and abstain without promotion. External recommended actions,
private metadata and arbitrary evidence URLs never acquire production authority.
Duplicate source identities are idempotent; conflicting payloads are refused.

External events are read through
`GET /api/v1/operator/cameras/{camera_id}/integration-events` using the current
human session and assigned-camera authorization. Each event includes its original
observation UTC and `playback_at`, allowing navigation to available camera footage.

Native recorded-person observations preserve their recorded UTC through forward
and backward host changes; delivery order follows append IDs, never wall-time
sorting. The older canonical event1.0 contract forbids ingestion before observation.
An external draft0.1 event ahead of K5's ingestion clock therefore returns explicit
`integration_source_clock_ahead` rather than inventing ingestion UTC. Its connection
shows `source_clock_ahead`, a failed ingress outcome is audited, and no existing
event is overwritten. The producer retains its unacknowledged source event for
retry after time health is corrected.
