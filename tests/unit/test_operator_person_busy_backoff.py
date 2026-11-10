"""Prevent repeated expensive frame uploads while installed native person view warms.

The normal active-view cadence and exact-frame, no-retention contract remain.
"""

from pathlib import Path


def test_busy_person_preparation_does_not_flood_native_worker():
    app = (Path(__file__).resolve().parents[2] / "src/k5vision/operator_web/app.js").read_text(
        encoding="utf-8"
    )
    sampled = app.split("async function samplePerson(epoch)", 1)[1].split(
        "async function startLive(", 1
    )[0]

    assert "let busyRetryDelayMs = null;" in sampled
    busy = sampled.split('if (result.status === "busy") {', 1)[1].split("} else {", 1)[0]
    assert "busyRetryDelayMs = 1500;" in busy
    refused = sampled.split("if (error.status === 429) {", 1)[1].split("} else if", 1)[0]
    assert "busyRetryDelayMs = 1500;" in refused
    assert "busyRetryDelayMs ?? Math.max(0, 500 - (performance.now() - began))" in sampled
    assert "pixels?.fill(0);" in sampled
    assert "snapshot?.data.fill(0);" in sampled
    assert "state.personController?.abort();" in app


def test_busy_retry_respects_existing_stalled_video_guard_without_upload():
    """HTTP 429 followed by transient HLS waiting must not abort before guard expiry."""
    app = (Path(__file__).resolve().parents[2] / "src/k5vision/operator_web/app.js").read_text(
        encoding="utf-8"
    )
    retry = app.split("async function samplePerson(epoch)", 1)[1].split(
        "const began = performance.now();", 1
    )[0]
    assert "if (!canSamplePerson()) {" in retry
    assert "state.personBufferTimer !== null" in retry
    assert "state.token && !state.signingOut" in retry
    assert 'state.view === "live"' in retry
    assert 'camera?.analytics_status === "configured"' in retry
    assert 'statusDetails(camera).style === "online"' in retry
    assert 'state.workerStatus !== "unavailable"' in retry
    assert "state.personTimer = setTimeout(() => void samplePerson(epoch), 250);" in retry
    assert "return;" in retry
    assert "stopPersonView(" in retry
    assert "const PERSON_BUFFER_GRACE_MS = 5000;" in app
    # The timer still terminates a genuinely stalled player after its guard.
    guard = app.split("function checkPersonBuffer(epoch, video, frames)", 1)[1].split(
        '$("live-video").addEventListener("waiting"', 1
    )[0]
    assert "stopPersonView(" in guard
    assert "current <= frames" in guard


def test_inflight_native_sample_survives_only_bounded_same_camera_hls_reconnect():
    """Transient HLS failures cannot silently abort an authenticated frozen POST.

    The existing 24-second request deadline, camera/session/authentication and
    actual rendered same-frame positive boxes remain separate acceptance gates.
    """
    app = (Path(__file__).resolve().parents[2] / "src/k5vision/operator_web/app.js").read_text(
        encoding="utf-8"
    )
    teardown = app.split("function stopLive(", 1)[1].split("function stopPlayback(", 1)[0]
    assert "preserveInFlightPerson = false" in teardown
    assert "preserveInFlightPerson && state.personEnabled && state.personController" in teardown
    for guard in (
        "state.token",
        "!state.signingOut",
        "!state.securityTransition",
        "state.capabilitiesResolved",
        'allowed("cameras.view_live")',
        'state.view === "live"',
        'camera?.analytics_status === "configured"',
        'statusDetails(camera).style === "online"',
        'state.workerStatus !== "unavailable"',
    ):
        assert guard in teardown
    assert teardown.index("stopPersonView();") < teardown.index('resetVideo($("live-video"));')
    retry = app.split("function scheduleLiveRetry(", 1)[1].split(
        "async function recoverMediaAccess(", 1
    )[0]
    assert "if (state.retryCount >= RETRY_DELAYS.length) {" in retry
    assert retry.index("stopLive(false);") < retry.index("stopLive(false, true);")
    assert "startLive(epoch, true)" in retry
    launch = app.split("async function startLive(", 1)[1].split("function setView(", 1)[0]
    assert "preserveInFlightPerson = false" in launch
    assert "stopLive(false, preserveInFlightPerson);" in launch
    assert "scheduleLiveRetry(epoch," in launch
    # Security/media revocation must never opt in to preserving the request.
    recover = app.split("async function recoverMediaAccess(", 1)[1].split(
        "function canSamplePerson(", 1
    )[0]
    assert "stopLive(false);" in recover
    assert "timeout: 24000" in app
    assert "state.personController?.abort();" in app


def test_native_person_sample_isolates_bounded_background_inventory_work():
    """A sampled POST must not be torn down by a preexisting slow UI poll.

    Authority and membership are still checked on the server both before and
    after native inference. This does not permit stale authorization bypass.
    """
    app = (Path(__file__).resolve().parents[2] / "src/k5vision/operator_web/app.js").read_text(
        encoding="utf-8"
    )
    poll = app.split("function scheduleCameraPoll()", 1)[1].split(
        "function safeMediaUrl(", 1
    )[0]
    assert "if (state.personController) {" in poll
    assert "scheduleCameraPoll();" in poll
    assert "const controller = new AbortController();" in poll
    assert "state.cameraPollController = controller;" in poll
    assert "refreshCameras(false, controller.signal)" in poll
    assert "refreshDiagnostics(controller.signal)" in poll
    assert "state.cameraPollController = null;" in poll

    cameras = app.split("async function refreshCameras(", 1)[1].split(
        "function scheduleCameraPoll()", 1
    )[0]
    assert 'request("/api/v1/operator/cameras", { signal })' in cameras
    assert 'request("/api/v1/operator/capabilities", { timeout: 4000, signal })' in cameras
    assert "suspendCapabilities();" in cameras
    diagnostics = app.split("async function refreshDiagnostics(", 1)[1].split(
        "function clearCameraSelection(", 1
    )[0]
    assert 'request("/api/v1/operator/status", { signal })' in diagnostics

    start_sample = app.split(
        '$("toggle-person-view").addEventListener("click", () => {', 1
    )[1].split('$("live-video").addEventListener("error"', 1)[0]
    assert "state.cameraPollController?.abort();" in start_sample
    assert start_sample.index("state.cameraPollController?.abort();") < (
        start_sample.index("void samplePerson(epoch);")
    )
    assert "timeout: 24000" in app
    assert "state.personController?.abort();" in app
