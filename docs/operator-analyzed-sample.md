# One analyzed operator sample

This optional local source feature adds a separate **Analyzed sample** canvas to
the durable operator dashboard. An authenticated operator explicitly requests one
sample. The existing HLS live player remains independent. This is not a live
video overlay, continuous inference, event detector, recording annotation, or
claim that a current installation is ready for analytics.

## Prerequisites and truthful availability

The existing `K5_ANALYTICS_CONFIG` admission must validate the already-installed
pinned Analytics-lab package, runtime versions and local model artifacts. The
existing Windows direct GStreamer decoder also requires its configured local
runtime. Missing or invalid prerequisites return `unavailable` and refuse sample
admission. Read-only prerequisite checks do not prove successful native startup.
The current pilot launcher provisions neither those analytics extras/models nor
GStreamer. This feature adds no installer, dependencies, package/donor pins,
downloads, credentials, ports or new listeners. Native qualification is separate.

## Product boundary

- POST `/api/v1/operator/cameras/{device_id}/analytics-samples` accepts no body or
  query parameters. Current bearer-session authentication, enabled account, read
  role, origin checks and enrolled-camera validation are required. It returns
  HTTP 202 promptly with an opaque ticket and at most 60 seconds of result life
- Authenticated GET of that path plus `/{ticket}` polls pending/failed/ready state
  without creating providers. Tickets bind the initiating account, session
  digest, camera and current service generation. The coordinator belongs to one
  site. Unknown or wrong-owner tickets return 404; expired tickets return 410
- The service alone resolves numeric-loopback RTSP from its current MediaMTX
  port, worker credentials and exact applied path binding. The browser supplies
  no source address, pixels or analytics configuration. Sample responses never
  expose source addresses, worker credentials or track IDs. Existing private
  source validation is retained
- The unchanged `GStreamerDirectFrameDelivery(frame_goal=1)` captures one immutable
  frame, limited to 16 MiB. Its decoder is closed before inference starts. The
  existing `AnalyticsProviderFactory`, normalized detection adapter and
  `BoundedDetectionOverlayRenderer` then analyze and render that identical frame
  at the 0.5 confidence threshold. Empty detections are a successful sample
- Successful polls atomically consume the one ready result and return raw binary
  BGRx (`application/octet-stream`), with bounded integer geometry, box count,
  expiry, ticket and server-receipt metadata in `X-K5-Sample-*` headers. Subsequent
  polls report `consumed`; no replay, recapture or retry is automatic. Unauthorized
  or stale polls cannot consume it. An interrupted transfer is not retried
- The browser checks Content-Length and metadata before reading a bounded stream,
  then validates the exact byte count. The browser respects row stride and converts
  BGRx to opaque RGBA on a separate canvas. Server receipt is not camera capture
  time, event time, recording offset, or HLS position. No synthetic timestamps
  are introduced

## Lifetime, cancellation and memory

There is exactly one product-wide operation, no queue and no automatic sample
retry. Browser pending-state polling never submits another sample. A repeated
request while native work is active returns 409. The server owns the task, rather
than an HTTP waiter. Navigation, HTTP aborts and Clear sample invalidate the
browser's pending request and displayed pixels only; they do not send a server
cancellation. The owned operation can finish and retain its one RAM result until
its original expiry. Logout, source changes and server expiry invalidate the
server result without cancelling native constructor/pull/close calls or inference.
Shutdown shields and drains the owned task before media shutdown, even through
repeated cancellation of the shutdown waiter. Cancellation is propagated only
after that owned task has completed.
Synchronous native initialization and teardown do not have a hard cancellation
bound; an unresponsive native call can keep admission busy indefinitely.

A factory-start failure, decoder failure with ambiguous cleanup, or provider
cleanup failure latches admission unavailable. Recovery requires termination and
restart of the product process, not an in-process coordinator reset. A completed
or cancelled waiter is never treated as proof that native resources were freed.
The existing provider itself retains native capacity until its underlying future
ends. No new sample is admitted through an unresolved cleanup state.

If analytics cleanup or native media stop remains unresolved (including an
interrupted stop waiter), shutdown still attempts safe store cleanup but retains
the profile's exclusive lease for the rest of the process
lifetime. A second product cannot reopen that profile. A bounded strong ownership
registry reserves before acquisition, rolls back failed acquisition, and removes
a lease only after clean analytics drain, completed media stop and profile close. Its 64-entry cap is
strictly internal lease-bookkeeping admission, not native capacity or a claim of
64 supported products. Application garbage collection or repeated close attempts
never silently unlock an unresolved lease. Normal clean shutdown releases it.

Only one rendered result is retained in RAM and a timer clears it after 60 seconds
from admission, even when nobody polls. One successful poll transfers it out of
the coordinator atomically without JSON/base64 expansion or repeated serialization.
Acquisition/inference/rendering use bounded transient copies; the 16 MiB limit is the raw-frame bound, not a total process-RSS
claim. Native work still running after expiry may retain its single bounded input
until it finishes.
No sample frames, detections, credentials or private source exceptions are written
to recordings, files, logs or diagnostic artifacts by this feature.

Source mutation, reconciliation, worker restart and product shutdown change the
service generation and invalidate results. The account, session and current
camera binding are revalidated before processing and result storage, and on each
poll. Logout clears the sample; revoked/disabled accounts cannot retrieve it.
The browser clears pixels, timers and stale request ownership on logout, session
expiry, selection/view/source changes, dismissal, errors or sample expiry. Its
monotonic deadline starts before POST and never extends beyond 60 seconds from
the click; delayed admission or binary transfer cannot extend displayed lifetime.

## Verification boundary

Local tests use ASGI transports, VM/DOM doubles and mocked native resources only.
They cover exact frame/result pairing, empty detections, frame bounds, secret-free
responses, account/session/camera/generation mismatches, rejected browser inputs,
revocation, removal, cancellation in each native phase, occupied capacity,
fail-closed cleanup, expiry, canvas stride/alpha and stale-response suppression.
PR338 camera-dialog and PR339 constructor-cleanup tests remain intact.

Not yet proven: installed Windows GStreamer admission and teardown, real current
MediaMTX loopback authentication/path behavior, pinned inference/model execution,
real camera sample/box contrast or screenshot, browser/native performance, CI,
merge eligibility or installed delivery readiness. Those require separate
admission and independent review of the exact source candidate.
