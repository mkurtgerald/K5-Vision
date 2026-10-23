/* K5 Vision local operator workspace. Human tokens remain in memory only.
 * Live-player event wiring adapts MediaMTX v1.21.2's HLS page (MIT).
 * Upstream copyright (c) 2019 aler9; see THIRD_PARTY_NOTICES.md.
 */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const DAY = 86400000;
  const TIME_SAMPLE_MAX_AGE = 30000;
  const RETRY_DELAYS = [1000, 2000, 4000, 8000, 10000];
  const CAMERA_ERROR_CODES = new Set([
    "discovery_unavailable", "camera_auth_failed", "camera_unreachable",
    "camera_unsupported", "camera_invalid_response", "camera_busy",
    "camera_probe_expired", "camera_profile_unsupported",
    "stream_invalid", "stream_auth_failed", "stream_unsupported", "stream_unreachable", "stream_invalid_response",
  ]);
  const state = {
    token: "", account: null, cameras: [], workerStatus: "ready", selected: null, media: null,
    view: "wall", sessionEpoch: 0, selectionEpoch: 0, requests: new Set(),
    hls: null, nativeLive: false, hlsScript: null, retryTimer: null, retryCount: 0,
    mediaAuthRefreshUsed: false,
    pollTimer: null, expiryTimer: null, selectionController: null,
    spans: [], selectedSpan: null, historyEpoch: 0, historyRolling: true, signingOut: false,
    dialogMode: "add", cameraSetupEpoch: 0, diagnostics: null, statusUnavailable: false,
    cameraSetupBusy: false, cameraSetupController: null, cameraProbe: null, cameraProbeTimer: null,
    cameraSetupTarget: null,
    cameraConnectionNotice: null,
    recorderTime: null, sessionExpiresAt: null, sessionExpirySteady: null,
    removeTarget: null, removing: false,
    personEnabled: false, personEpoch: 0, personTimer: null, personController: null,
    personFrameId: 0,
    recordingBusy: false, cameraListEpoch: 0, navigationEpoch: 0, historyRangeMs: DAY, historyFetchedSteady: 0,
    playbackStart: null, playbackEpoch: 0, selectedStatus: null,
  };
  let wall = null, timeline = null;
  let wallLayoutPending = false;

  function fitCameraWall() {
    if (wallLayoutPending) return;
    wallLayoutPending = true;
    requestAnimationFrame(() => {
      wallLayoutPending = false;
      const grid = $("wall-panel").querySelector(".camera-wall-grid");
      if (grid && state.view === "wall" && !$("workspace").hidden) {
        const top = grid.getBoundingClientRect().top + window.scrollY;
        grid.style.setProperty("--wall-height", `${Math.max(280, window.innerHeight - top - 24)}px`);
      }
    });
  }

  function cameraWall() {
    if (!wall) wall = window.K5CameraWall.create({
      container: $("wall-panel"), request, ensureHls, safeMediaUrl,
      getSession: () => state.signingOut ? null : ({ token: state.token, epoch: state.sessionEpoch }),
      onOpenCamera: (deviceId, view = "live") => {
        setView(view, false);
        void selectCamera(deviceId);
      },
    });
    return wall;
  }

  function recordingTimeline() {
    if (!timeline) timeline = window.K5RecordingTimeline.create({
      container: $("recording-timeline"), onSeek: seekRecording,
    });
    return timeline;
  }

  class ApiError extends Error {
    constructor(status, code = "", clock = null) { super("Request unavailable"); this.status = status; this.code = code; this.clock = clock; }
  }
  class SelectionError extends Error {}

  function message(id, value, error = false) {
    const element = $(id);
    element.textContent = value;
    element.classList.toggle("error", error);
  }

  function notice(value, error = false, owner = "") {
    if (owner !== "camera_connection") state.cameraConnectionNotice = null;
    message("workspace-message", value, error);
    $("workspace-message").hidden = !value;
  }

  function cameraConnectionStatus(camera, workerStatus) {
    if (workerStatus === "unavailable" || camera?.status === "unavailable") return {
      text: "Camera saved. The media service is unavailable. Live video and recording are not confirmed.",
      error: true,
    };
    if (camera?.status === "live") {
      if (workerStatus === "disk_full") return {
        text: "Camera stream is reaching K5. Recording is paused because storage needs attention.",
        error: true,
      };
      return { text: `Camera video is connected. ${recordingDetails(camera).detail}`, error: camera.recording_state === "stalled" || camera.recording_state === "error" };
    }
    if (camera?.status === "disconnected") return {
      text: "Camera saved, but its video stream is disconnected. Check its power, network and video permissions, then use Reconnect camera.",
      error: true,
    };
    return {
      text: "Camera saved. Waiting for its video stream. Live video and recording are not confirmed yet.",
      error: false,
    };
  }

  function renderCameraConnectionNotice(camera) {
    if (!camera || state.cameraConnectionNotice !== camera.device_id) return;
    const result = cameraConnectionStatus(camera, state.workerStatus);
    notice(result.text, result.error, "camera_connection");
  }

  function apiMessage(error, fallback) {
    if (error.status === 401) return "Your session has ended. Sign in again.";
    if (error.status === 403) return "Your account does not have permission for this action.";
    if (error.status === 429) return "Too many requests. Wait a moment and try again.";
    if (error.status === 503) return "The camera service is unavailable. Try again shortly.";
    return fallback;
  }

  async function request(path, options = {}) {
    if (state.signingOut) throw new DOMException("Sign out in progress", "AbortError");
    const controller = new AbortController();
    const signals = [controller.signal, AbortSignal.timeout(15000)];
    if (options.signal) signals.push(options.signal);
    const signal = AbortSignal.any(signals);
    const token = state.token;
    state.requests.add(controller);
    try {
      const headers = { Accept: "application/json" };
      if (token) headers.Authorization = `Bearer ${token}`;
      if (options.body !== undefined) headers["Content-Type"] = "application/json";
      if (options.rawBody !== undefined) headers["Content-Type"] = "application/octet-stream";
      const response = await fetch(path, {
        method: options.method || "GET", headers, signal,
        credentials: "same-origin", cache: "no-store",
        ...(options.body !== undefined ? { body: JSON.stringify(options.body) } : {}),
        ...(options.rawBody !== undefined ? { body: options.rawBody } : {}),
      });
      if (!response.ok) {
        if (response.status === 401 && token && token === state.token && path !== "/api/v1/auth/login" && !state.signingOut) {
          expireSession();
        }
        let code = "", clock = null;
        if (path.startsWith("/api/v1/operator/camera")) {
          try {
            const detail = (await response.json()).detail;
            const candidate = typeof detail === "string" ? detail : detail?.code;
            if (CAMERA_ERROR_CODES.has(candidate)) {
              code = candidate;
              clock = cameraClockReport(detail?.clock);
            }
          } catch (_) { /* Unknown response text never becomes a user-facing error. */ }
        }
        throw new ApiError(response.status, code, clock);
      }
      if (response.status === 204) return null;
      return await response.json();
    } finally {
      state.requests.delete(controller);
    }
  }

  function currentCamera() {
    return state.cameras.find((camera) => camera.device_id === state.selected) || null;
  }

  function statusDetails(camera) {
    const status = String(camera?.status || "unavailable").toLowerCase();
    if (["online", "live", "ready", "connected"].includes(status)) return { text: "Online", style: "online" };
    if (["connecting", "starting", "pending"].includes(status)) return { text: "Connecting", style: "connecting" };
    if (["offline", "disconnected"].includes(status)) return { text: "Disconnected", style: "offline" };
    return { text: "Unavailable", style: "offline" };
  }

  function recordingDetails(camera) {
    if (!camera) return { text: "No camera selected", detail: "Select a camera to view its recording state.", paused: false };
    const reason = camera.recording_status?.reason;
    if (camera.recording_enabled === false || reason === "operator_paused") return {
      text: "Recording paused", detail: "Recording is paused for this camera. Live video and retained recordings remain available.", paused: true,
    };
    if (state.workerStatus === "disk_full" || reason === "storage_paused") return {
      text: "Storage paused", detail: "Recording is paused because storage needs attention. Check the storage status above.", paused: true,
    };
    if (camera.recording_state === "recording") return {
      text: "Recording", detail: "New recording data has been observed on disk.", paused: false,
    };
    if (camera.recording_state === "stalled" || camera.recording_state === "error") return {
      text: "Recording needs attention", detail: reason === "camera_unavailable"
        ? "No new footage is confirmed while the camera is disconnected. Check the camera connection."
        : "New recording data is not being confirmed. Check the camera and storage status, then reconnect the camera if needed.", paused: true,
    };
    if (camera.status !== "live") return {
      text: "Waiting for video", detail: "Recording will be checked after the camera starts sending video.", paused: false,
    };
    return { text: "Checking recording", detail: "Video is connected. Waiting to confirm new recording data on disk.", paused: false };
  }

  function renderOverview() {
    const cameras = state.cameras;
    $("summary-online").textContent = String(cameras.filter((camera) => camera.status === "live").length);
    $("summary-camera-total").textContent = `${cameras.length} ${cameras.length === 1 ? "camera" : "cameras"} configured`;
    $("summary-recording").textContent = String(cameras.filter((camera) => camera.recording_state === "recording").length);
    const paused = cameras.filter((camera) => camera.recording_enabled === false || camera.recording_state === "paused").length;
    const attention = cameras.filter((camera) => ["stalled", "error"].includes(camera.recording_state)).length;
    $("summary-recording-detail").textContent = attention ? `${attention} need attention${paused ? ` · ${paused} paused` : ""}`
      : paused ? `${paused} paused · Confirmed from disk activity` : "Confirmed from observed disk activity";
    $("summary-storage").textContent = state.statusUnavailable ? "Unavailable" : byteLabel(state.diagnostics?.storage?.recording_bytes) || "—";
    $("summary-storage-detail").textContent = byteLabel(state.diagnostics?.max_recording_bytes)
      ? `${byteLabel(state.diagnostics.max_recording_bytes)} recording limit` : "Checking recording storage";
    $("summary-free").textContent = state.statusUnavailable ? "Unavailable" : byteLabel(state.diagnostics?.storage?.free_bytes) || "—";
    fitCameraWall();
  }

  function resetVideo(video) {
    video.pause();
    video.removeAttribute("src");
    video.load();
  }

  function stopLive(resetRetries = true) {
    stopPersonView();
    clearTimeout(state.retryTimer);
    state.retryTimer = null;
    state.nativeLive = false;
    if (state.hls) { state.hls.destroy(); state.hls = null; }
    $("live-video").onplay = null;
    resetVideo($("live-video"));
    $("live-video-label").hidden = true;
    $("play-live").hidden = true;
    if (resetRetries) state.retryCount = 0;
  }

  function stopPlayback() {
    state.playbackEpoch += 1;
    resetVideo($("playback-video"));
    state.playbackStart = null;
    $("playback-placeholder").hidden = false;
  }

  function livePlaceholder(title, description, retry = false) {
    $("live-placeholder-title").textContent = title;
    $("live-placeholder-description").textContent = description;
    $("live-placeholder").hidden = false;
    $("retry-live").hidden = !retry;
    $("live-video-label").hidden = true;
  }

  function clearWorkspace(reason = "") {
    state.sessionEpoch += 1;
    state.selectionEpoch += 1;
    state.historyEpoch += 1;
    clearTimeout(state.pollTimer);
    clearTimeout(state.expiryTimer);
    state.pollTimer = null;
    state.expiryTimer = null;
    state.selectionController?.abort();
    wall?.hide();
    timeline?.clear();
    for (const controller of state.requests) controller.abort();
    state.requests.clear();
    stopLive();
    stopPlayback();
    state.token = "";
    state.account = null;
    state.cameras = [];
    state.workerStatus = "ready";
    state.diagnostics = null;
    state.statusUnavailable = false;
    state.cameraConnectionNotice = null;
    state.recorderTime = null;
    state.sessionExpiresAt = null;
    state.sessionExpirySteady = null;
    state.removeTarget = null;
    state.removing = false;
    state.selected = null;
    state.media = null;
    state.mediaAuthRefreshUsed = false;
    state.recordingBusy = false;
    state.selectedStatus = null;
    state.view = "wall";
    state.historyRangeMs = DAY;
    state.spans = [];
    state.selectedSpan = null;
    $("workspace").hidden = true;
    $("sign-in").hidden = false;
    $("password").value = "";
    resetCameraSetup();
    if ($("add-camera-dialog").open) $("add-camera-dialog").close();
    for (const id of ["remove-camera-dialog", "connection-help-dialog"]) {
      if ($(id).open) $(id).close();
    }
    setRemovalBusy(false);
    $("camera-list").replaceChildren();
    $("history-list").replaceChildren();
    $("camera-search").value = "";
    $("sign-in-button").disabled = false;
    $("sign-in-button").textContent = "Sign in →";
    message("sign-in-message", reason, Boolean(reason));
    notice("");
    $("worker-message").hidden = true;
    $("recorder-time-warning").hidden = true;
    $("recorder-time-status").textContent = "Checking recorder time…";
  }

  function clearMediaCookie(token) {
    return fetch("/api/v1/operator/media-session", {
      method: "DELETE", credentials: "same-origin", cache: "no-store",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      signal: AbortSignal.timeout(5000),
    }).catch(() => null);
  }

  function expireSession() {
    const token = state.token;
    if (!token) return;
    clearWorkspace("Your session has ended. Sign in again.");
    void clearMediaCookie(token);
  }

  async function signOut() {
    if (state.signingOut) return;
    state.signingOut = true;
    closeAddCamera();
    const token = state.token;
    state.sessionEpoch += 1;
    state.selectionEpoch += 1;
    state.historyEpoch += 1;
    clearTimeout(state.pollTimer);
    clearTimeout(state.expiryTimer);
    state.selectionController?.abort();
    wall?.hide();
    for (const controller of state.requests) controller.abort();
    state.requests.clear();
    stopLive();
    stopPlayback();
    $("sign-out-button").disabled = true;
    try {
      await fetch("/api/v1/auth/logout", {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { Authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(5000),
      });
    } catch (_) { /* Local state still closes when the service is unavailable. */ }
    await clearMediaCookie(token);
    clearWorkspace();
    state.signingOut = false;
    $("sign-out-button").disabled = false;
    $("username").focus();
  }

  function renderCameras() {
    const query = $("camera-search").value.trim().toLowerCase();
    const visible = state.cameras.filter((camera) => String(camera.name).toLowerCase().includes(query));
    const fragment = document.createDocumentFragment();
    for (const camera of visible) {
      const status = statusDetails(camera);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "camera-item";
      button.classList.toggle("selected", camera.device_id === state.selected);
      button.setAttribute("aria-pressed", String(camera.device_id === state.selected));
      const heading = document.createElement("span");
      heading.className = "camera-item-heading";
      const dot = document.createElement("span");
      dot.className = `small-dot ${status.style}`;
      dot.setAttribute("aria-hidden", "true");
      const name = document.createElement("span");
      name.className = "camera-item-name";
      name.textContent = camera.name;
      heading.append(dot, name);
      const details = document.createElement("span");
      details.className = "camera-item-details";
      details.textContent = `${status.text} · ${recordingDetails(camera).text}`;
      button.append(heading, details);
      button.addEventListener("click", () => void selectCamera(camera.device_id));
      fragment.append(button);
    }
    $("camera-list").replaceChildren(fragment);
    $("camera-count").textContent = String(state.cameras.length);
    if (!state.cameras.length) {
      $("camera-list-message").textContent = state.account?.role === "administrator"
        ? "Add your first camera to begin." : "No cameras are configured. Ask your administrator to add one.";
    } else {
      $("camera-list-message").textContent = visible.length ? "" : "No cameras match your search.";
    }
    renderCameraHeading();
    renderOverview();
    if (state.token && state.view === "wall") cameraWall().update(state.cameras);
  }

  function renderCameraHeading() {
    const camera = currentCamera();
    const focused = Boolean(camera && state.view !== "wall");
    const recording = recordingDetails(camera);
    $("camera-title").textContent = state.view === "wall" ? "All cameras" : camera?.name || "Select a camera";
    $("camera-subtitle").textContent = state.view === "wall" ? "Live cameras, recording activity and one-click access to recordings."
      : camera ? String(camera.host || "Enrolled camera") : "Select a camera from the wall or camera list.";
    $("connection-badge").hidden = !focused;
    $("recording-badge").hidden = !focused;
    $("update-source-button").hidden = !focused || state.account?.role !== "administrator";
    $("remove-camera-button").hidden = !focused || state.account?.role !== "administrator";
    $("toggle-recording").hidden = !focused || state.account?.role !== "administrator";
    $("toggle-recording").disabled = state.recordingBusy || state.workerStatus === "unavailable";
    const enabled = camera?.recording_enabled !== false;
    $("toggle-recording").textContent = state.recordingBusy ? "Applying…" : enabled ? "Pause recording" : "Resume recording";
    $("toggle-recording").setAttribute("aria-pressed", String(enabled));
    $("toggle-recording").dataset.enabled = String(enabled);
    $("recent-recordings").hidden = !focused;
    $("live-tab").disabled = !camera;
    $("history-tab").disabled = !camera;
    $("recording-state-detail").hidden = !focused;
    $("recording-state-detail").textContent = recording.detail;
    $("person-view-panel").hidden = !focused || camera.analytics_status !== "configured";
    $("recording-badge").textContent = recording.text;
    $("recording-badge").classList.toggle("paused", recording.paused);
    $("recording-badge").classList.toggle("waiting", !recording.paused && camera?.recording_state !== "recording");
    if (camera) {
      const status = statusDetails(camera);
      $("connection-badge").textContent = status.text;
      $("connection-badge").className = `status-badge ${status.style}`;
      $("analytics-state").textContent = camera.analytics_status === "configured"
        ? "Person detection ready · Sampled view" : camera.analytics_status === "unavailable"
          ? "Person detection unavailable" : "Person detection not configured";
      if (status.style !== "online" || state.workerStatus === "unavailable") {
        stopPersonView("Sampled view stopped while the camera is unavailable. Start it again after live video returns.");
      }
      if (status.style === "offline" && state.view === "live" && state.media) {
        livePlaceholder("Camera disconnected", "Check the camera’s power and network, or use Reconnect camera to test its account and video stream. Recording history is still available.", true);
        message("live-state", "Camera disconnected");
      }
      if (["disconnected", "connecting", "unavailable"].includes(state.selectedStatus) && camera.status === "live" && state.view === "live" && state.media) {
        state.retryCount = 0;
        void startLive();
      }
      state.selectedStatus = camera.status;
      renderCameraConnectionNotice(camera);
    } else $("analytics-state").textContent = "";
    renderPersonControls();
  }

  function renderWorkerStatus() {
    const reason = state.diagnostics?.storage?.reason;
    const value = state.workerStatus === "disk_full"
      ? reason === "inspection"
        ? "Recording paused: storage could not be checked safely. Check the K5 startup message. Live view and retained recording history remain available."
        : reason === "free_space"
          ? "Recording paused: disk space is low. Free space on the recording drive. K5 checks storage automatically; live view and retained history remain available."
          : "Recording paused: storage limit reached. K5 retires eligible older recordings automatically. Live view and retained recording history remain available."
      : state.workerStatus === "unavailable" ? "Media service is restarting. Recording history will return when the service is ready." : "";
    $("worker-message").textContent = value;
    $("worker-message").hidden = !value;
  }

  function byteLabel(value) {
    if (!Number.isSafeInteger(value) || value < 0) return null;
    if (value < 1024) return `${value} B`;
    const units = ["KiB", "MiB", "GiB", "TiB"];
    let size = value / 1024;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
    return `${size.toFixed(size >= 10 ? 0 : 1)} ${units[unit]}`;
  }

  function renderDiagnostics() {
    const diagnostics = state.diagnostics;
    const service = state.statusUnavailable ? "Service status unavailable · Select refresh to retry"
      : state.workerStatus === "ready" ? "Media service ready"
        : state.workerStatus === "disk_full" ? "Recording paused · Storage needs attention"
          : "Media service restarting";
    $("service-status").textContent = service;
    $("service-status").closest(".system-status").classList.toggle("warning", state.statusUnavailable || state.workerStatus !== "ready");
    const storage = diagnostics?.storage;
    const used = byteLabel(storage?.recording_bytes);
    const free = byteLabel(storage?.free_bytes);
    const limit = byteLabel(diagnostics?.max_recording_bytes);
    $("storage-status").textContent = state.statusUnavailable ? "Recording storage status could not be checked."
      : storage?.reason === "inspection" ? `Storage could not be checked safely${limit ? ` · ${limit} recording limit` : ""}`
        : used && free && limit ? `${used} recorded · ${limit} recording limit · ${free} disk free`
          : limit ? `Recording limit ${limit} · Waiting for storage status`
            : "Checking recording storage…";
    const clock = diagnostics?.clock;
    const recorderUtc = recorderNow();
    $("recorder-time-status").textContent = recorderUtc === null
      ? "Recorder time could not be checked. Time synchronization is not verified."
      : `Recorder time: ${utcLabel(recorderUtc)} UTC · Time synchronization is not verified`;
    const changed = clock?.status === "changed";
    const unavailable = recorderUtc === null || clock?.status === "unavailable";
    const step = typeof clock?.last_step_seconds === "number" && Number.isFinite(clock.last_step_seconds)
      ? Math.abs(clock.last_step_seconds).toFixed(1) : null;
    const direction = clock?.last_step_seconds < 0 ? "backward" : "forward";
    $("recorder-time-warning").textContent = changed
      ? `The recorder clock changed${step ? ` by ${step} seconds (${direction})` : ""}. Check Windows Settings → Time & language → Date & time and the camera's NTP settings, then retest the camera connection. Earlier footage may use the previous clock. Age retention uses observed elapsed time to protect footage from clock corrections.`
      : unavailable ? "Recorder time is unavailable. Check Windows time synchronization before relying on recording timestamps." : "";
    $("recorder-time-warning").hidden = !changed && !unavailable;
    $("connection-help-status").textContent = state.workerStatus === "disk_full"
      ? diagnostics?.storage?.reason === "inspection"
        ? "Recording is paused because storage could not be checked safely. Check the K5 startup message for the recording-storage problem. Live view and retained history remain available."
        : "Recording is paused. Free disk space or increase the configured recording limit, then K5 will resume automatically when storage can be admitted. Live view and retained history remain available."
      : state.workerStatus === "unavailable" || state.statusUnavailable
        ? "K5 is checking or restarting its media service. If this persists, confirm K5 is running and check its startup message."
        : "The media service is ready. If a camera stays disconnected or connecting, check the camera settings below.";
    renderOverview();
  }

  async function refreshDiagnostics() {
    if (!state.token) return;
    const epoch = state.sessionEpoch;
    try {
      const diagnostics = await request("/api/v1/operator/status");
      if (epoch !== state.sessionEpoch) return;
      if (!diagnostics || !["ready", "unavailable", "disk_full"].includes(diagnostics.worker_status)) throw new ApiError(503);
      state.diagnostics = diagnostics;
      const serverUtc = utcMilliseconds(diagnostics.server_utc);
      state.recorderTime = Number.isFinite(serverUtc) ? { utc: serverUtc, steady: performance.now() } : null;
      state.statusUnavailable = false;
      scheduleSessionExpiry();
      state.workerStatus = diagnostics.worker_status;
      renderWorkerStatus(); renderCameraHeading(); renderDiagnostics();
    } catch (error) {
      if (error.name === "AbortError" || epoch !== state.sessionEpoch) return;
      state.statusUnavailable = true;
      state.recorderTime = null;
      renderDiagnostics();
    }
  }

  function clearCameraSelection(title = "Choose a camera to begin", description = "Your live feed will appear here.") {
    state.selectionEpoch += 1;
    state.selectionController?.abort();
    stopLive(); stopPlayback();
    state.selected = null; state.media = null;
    state.cameraConnectionNotice = null;
    notice("");
    resetHistory(); renderCameras();
    livePlaceholder(title, description);
    message("live-state", "No camera selected");
    if (state.token) setView("wall", false);
  }

  async function refreshCameras(selectFirst = false) {
    if (!state.token) return;
    const epoch = state.sessionEpoch;
    const cameraListEpoch = ++state.cameraListEpoch;
    try {
      const data = await request("/api/v1/operator/cameras");
      if (epoch !== state.sessionEpoch || cameraListEpoch !== state.cameraListEpoch) return;
      if (!Array.isArray(data.cameras)) throw new ApiError(503);
      state.workerStatus = ["ready", "unavailable", "disk_full"].includes(data.worker_status) ? data.worker_status : "ready";
      renderWorkerStatus();
      state.cameras = data.cameras.filter((camera) => camera && typeof camera.device_id === "string" && typeof camera.name === "string");
      renderCameras();
      if (state.selected && !currentCamera()) {
        clearCameraSelection("Camera unavailable", "Select another camera or refresh the camera list.");
      }
      if (selectFirst && !state.selected && state.cameras.length && state.view !== "wall") await selectCamera(state.cameras[0].device_id);
      return true;
    } catch (error) {
      if (error.name === "AbortError" || epoch !== state.sessionEpoch || cameraListEpoch !== state.cameraListEpoch) return;
      $("camera-list-message").textContent = apiMessage(error, "Camera list unavailable. Select refresh to try again.");
      return false;
    }
  }

  function scheduleCameraPoll() {
    clearTimeout(state.pollTimer);
    if (!state.token) return;
    state.pollTimer = setTimeout(async () => {
      await Promise.all([refreshCameras(), refreshDiagnostics()]);
      scheduleCameraPoll();
    }, 3000);
  }

  function safeMediaUrl(value, expectedPrefix) {
    if (typeof value !== "string" || !value.startsWith("/")) throw new ApiError(503);
    const url = new URL(value, location.origin);
    if (url.origin !== location.origin || !url.pathname.startsWith(expectedPrefix) || url.username || url.password || url.hash) throw new ApiError(503);
    return url.pathname + url.search;
  }

  async function selectCamera(deviceId) {
    if (!state.token || state.signingOut) return;
    state.navigationEpoch += 1;
    if (state.view === "wall") setView("live", false);
    state.selectionController?.abort();
    state.selectionController = new AbortController();
    const epoch = ++state.selectionEpoch;
    state.historyEpoch += 1;
    state.selected = deviceId;
    state.media = null;
    state.mediaAuthRefreshUsed = false;
    state.selectedStatus = null;
    stopLive(); stopPlayback(); resetHistory();
    if (state.cameraConnectionNotice !== deviceId) {
      state.cameraConnectionNotice = null;
      notice("");
    }
    renderCameras();
    livePlaceholder("Connecting to camera", "Opening your live view…");
    message("live-state", "Connecting…");
    try {
      const media = await request(`/api/v1/operator/cameras/${encodeURIComponent(deviceId)}/media-session`, {
        method: "POST", signal: state.selectionController.signal,
      });
      if (epoch !== state.selectionEpoch) return;
      state.media = {
        live_url: safeMediaUrl(media.live_url, "/media/live/"),
        playback_url: safeMediaUrl(media.playback_url, "/media/playback"),
      };
      if (state.view === "live") await startLive(epoch);
      else if (state.view === "history") await loadHistory();
    } catch (error) {
      if (error.name === "AbortError" || epoch !== state.selectionEpoch || !state.token) return;
      const value = apiMessage(error, "Camera media is unavailable. Try reconnecting.");
      livePlaceholder("Camera unavailable", value, true);
      message("live-state", "Live view unavailable");
      if (state.view === "history") message("history-message", value, true);
    }
  }

  function ensureHls() {
    if (window.Hls) return Promise.resolve();
    if (state.hlsScript) return state.hlsScript;
    state.hlsScript = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/media/hls.js";
      script.onload = () => {
        if (window.Hls) resolve();
        else { state.hlsScript = null; script.remove(); reject(new ApiError(503)); }
      };
      script.onerror = () => { state.hlsScript = null; script.remove(); reject(new ApiError(503)); };
      document.head.append(script);
    });
    return state.hlsScript;
  }

  function scheduleLiveRetry(epoch, reason) {
    if (epoch !== state.selectionEpoch || !state.token || state.view !== "live") return;
    stopLive(false);
    if (state.retryCount >= RETRY_DELAYS.length) {
      livePlaceholder("Live view disconnected", "The connection could not be restored. Recording history may still be available.", true);
      message("live-state", "Disconnected · Select reconnect to try again");
      return;
    }
    const delay = RETRY_DELAYS[state.retryCount++];
    livePlaceholder("Reconnecting", reason);
    message("live-state", `Reconnecting · Attempt ${state.retryCount} of ${RETRY_DELAYS.length}`);
    state.retryTimer = setTimeout(() => void startLive(epoch), delay);
  }

  async function recoverMediaAccess(epoch) {
    const session = state.sessionEpoch, token = state.token, deviceId = state.selected;
    if (!token || !deviceId || epoch !== state.selectionEpoch || state.view !== "live" || state.signingOut) return;
    const current = () => session === state.sessionEpoch && token === state.token
      && epoch === state.selectionEpoch && deviceId === state.selected
      && state.view === "live" && !state.signingOut;
    stopLive(false);
    if (state.mediaAuthRefreshUsed) {
      livePlaceholder("Media access unavailable", "Media access could not be restored. Select reconnect to try again.", true);
      message("live-state", "Live view unavailable · Select reconnect to try again");
      return;
    }
    state.mediaAuthRefreshUsed = true;
    livePlaceholder("Restoring media access", "Checking your existing camera access…");
    message("live-state", "Restoring media access…");
    try {
      const media = await request(`/api/v1/operator/cameras/${encodeURIComponent(deviceId)}/media-session`, {
        method: "POST", signal: state.selectionController?.signal,
      });
      if (!current()) return;
      state.media = {
        live_url: safeMediaUrl(media.live_url, "/media/live/"),
        playback_url: safeMediaUrl(media.playback_url, "/media/playback"),
      };
      await startLive(epoch);
    } catch (error) {
      if (error.name === "AbortError" || !current()) return;
      livePlaceholder("Media access unavailable", apiMessage(error, "Media access could not be restored. Select reconnect to try again."), true);
      message("live-state", "Live view unavailable · Select reconnect to try again");
    }
  }

  function canSamplePerson() {
    const video = $("live-video");
    const camera = currentCamera();
    return Boolean(state.token && !state.signingOut && state.view === "live"
      && camera?.analytics_status === "configured" && statusDetails(camera).style === "online"
      && state.workerStatus !== "unavailable" && video.readyState >= 2 && !video.paused
      && video.videoWidth > 0 && video.videoHeight > 0);
  }

  function renderPersonControls() {
    $("toggle-person-view").textContent = state.personEnabled ? "Stop sampled view" : "Start sampled view";
    $("toggle-person-view").setAttribute("aria-pressed", String(state.personEnabled));
    $("toggle-person-view").disabled = !state.personEnabled && !canSamplePerson();
  }

  function stopPersonView(reason = "Select Start sampled view while live video is playing. Samples are processed locally and are not saved.") {
    state.personEnabled = false;
    state.personEpoch += 1;
    clearTimeout(state.personTimer);
    state.personTimer = null;
    state.personController?.abort();
    state.personController = null;
    const canvas = $("person-view-canvas");
    canvas.width = 0; canvas.height = 0; canvas.hidden = true;
    message("person-view-message", reason);
    renderPersonControls();
  }

  function validPersonBox(box) {
    return box && box.category === "person"
      && [box.confidence, box.x_min, box.y_min, box.x_max, box.y_max].every((value) => Number.isFinite(value) && value >= 0 && value <= 1)
      && box.x_min < box.x_max && box.y_min < box.y_max;
  }

  async function samplePerson(epoch) {
    if (!state.personEnabled || epoch !== state.personEpoch) return;
    if (!canSamplePerson()) {
      stopPersonView("Sampled view stopped while live video is unavailable. Start it again when video returns.");
      return;
    }
    const began = performance.now();
    const cameraId = state.selected;
    const video = $("live-video");
    const scale = Math.min(1, 544 / video.videoWidth, 320 / video.videoHeight);
    const width = Math.max(1, Math.floor(video.videoWidth * scale));
    const height = Math.max(1, Math.floor(video.videoHeight * scale));
    const capture = document.createElement("canvas");
    capture.width = width; capture.height = height;
    let snapshot = null;
    let pixels = null;
    const controller = new AbortController();
    state.personController = controller;
    try {
      const context = capture.getContext("2d", { willReadFrequently: true });
      context.drawImage(video, 0, 0, width, height);
      snapshot = context.getImageData(0, 0, width, height);
      capture.width = 0; capture.height = 0;
      pixels = new Uint8Array(snapshot.data.length);
      for (let offset = 0; offset < pixels.length; offset += 4) {
        pixels[offset] = snapshot.data[offset + 2];
        pixels[offset + 1] = snapshot.data[offset + 1];
        pixels[offset + 2] = snapshot.data[offset];
        pixels[offset + 3] = 255;
      }
      const frameId = state.personFrameId = (state.personFrameId + 1) % 2147483648;
      const query = new URLSearchParams({ width: String(width), height: String(height), frame_id: String(frameId) });
      const result = await request(`/api/v1/operator/cameras/${encodeURIComponent(cameraId)}/person-view?${query}`, {
        method: "POST", rawBody: pixels, signal: controller.signal,
      });
      if (epoch !== state.personEpoch || !state.personEnabled || cameraId !== state.selected || !canSamplePerson()) return;
      if (!result || result.frame_id !== frameId || !["active", "busy", "unavailable"].includes(result.status)
        || !Array.isArray(result.boxes) || result.boxes.length > 32 || !result.boxes.every(validPersonBox)) throw new ApiError(503);
      if (result.status === "unavailable") {
        stopPersonView("Person detection is unavailable. Live video and recording continue. Start sampled view to retry.");
        return;
      }
      if (result.status === "busy") message("person-view-message", "Person detection is busy. Waiting for the next sample…");
      else {
        // The response belongs to this exact frozen sample, never the moving
        // live player. Publish frame and boxes together in one canvas update.
        const canvas = $("person-view-canvas");
        canvas.width = width; canvas.height = height;
        const output = canvas.getContext("2d");
        output.putImageData(snapshot, 0, 0);
        output.strokeStyle = "#68d2a4"; output.fillStyle = "#68d2a4";
        output.lineWidth = 2; output.font = "12px Segoe UI, sans-serif";
        for (const box of result.boxes) {
          const x = box.x_min * width; const y = box.y_min * height;
          output.strokeRect(x, y, (box.x_max - box.x_min) * width, (box.y_max - box.y_min) * height);
          output.fillText(`Person ${Math.round(box.confidence * 100)}%`, x + 3, Math.max(14, y - 4));
        }
        canvas.hidden = false;
        message("person-view-message", `${result.boxes.length} ${result.boxes.length === 1 ? "person" : "people"} in this sample · Samples are not saved or recorded as events.`);
      }
    } catch (error) {
      if (error.name === "AbortError" || epoch !== state.personEpoch || !state.token) return;
      if (error.status === 429) message("person-view-message", "Person detection is busy. Waiting for the next sample…");
      else stopPersonView(apiMessage(error, "Person detection is unavailable. Live video and recording continue. Start sampled view to retry."));
    } finally {
      pixels?.fill(0);
      snapshot?.data.fill(0);
      capture.width = 0; capture.height = 0;
      if (state.personController === controller) state.personController = null;
      if (state.personEnabled && epoch === state.personEpoch) {
        state.personTimer = setTimeout(() => void samplePerson(epoch), Math.max(0, 500 - (performance.now() - began)));
      }
    }
  }

  async function startLive(epoch = state.selectionEpoch) {
    if (!state.media || state.view !== "live" || !state.token) return;
    const url = state.media.live_url;
    try {
      await ensureHls();
      if (epoch !== state.selectionEpoch || state.view !== "live" || !state.token || state.signingOut) return;
      stopLive(false);
      livePlaceholder("Opening live view", "Waiting for camera video…");
      const video = $("live-video");
      video.muted = true;
      if (window.Hls.isSupported()) {
        const hls = new window.Hls({ maxLiveSyncPlaybackRate: 1.5, manifestLoadingMaxRetry: 1, levelLoadingMaxRetry: 1, fragLoadingMaxRetry: 1 });
        state.hls = hls;
        hls.on(window.Hls.Events.ERROR, (_event, data) => {
          if (!data.fatal || hls !== state.hls || epoch !== state.selectionEpoch) return;
          if (data.response?.code === 401 || data.response?.code === 403) { void recoverMediaAccess(epoch); return; }
          if (data.details === "manifestIncompatibleCodecsError") {
            stopLive();
            livePlaceholder("Video format unavailable", "Select an H.264 stream in your camera settings, then reconnect.", true);
            message("live-state", "Unsupported video format");
            return;
          }
          scheduleLiveRetry(epoch, "The camera may be offline. Waiting for the connection to return…");
        });
        hls.on(window.Hls.Events.MEDIA_ATTACHED, () => hls.loadSource(url));
        hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
          if (hls !== state.hls || epoch !== state.selectionEpoch) return;
          void video.play().catch(() => {
            $("live-placeholder").hidden = true;
            $("play-live").hidden = false;
            message("live-state", "Ready · Select play to begin");
          });
        });
        video.onplay = () => {
          if (Number.isFinite(hls.liveSyncPosition)) video.currentTime = hls.liveSyncPosition;
        };
        hls.attachMedia(video);
      } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
        state.nativeLive = true;
        video.src = url;
        await video.play().catch(() => { $("live-placeholder").hidden = true; $("play-live").hidden = false; });
      } else {
        livePlaceholder("Video playback unavailable", "Use a current version of Microsoft Edge or Chrome.");
        message("live-state", "Browser playback unavailable");
      }
    } catch (error) {
      if (epoch !== state.selectionEpoch || !state.token || state.view !== "live") return;
      scheduleLiveRetry(epoch, "The live player is unavailable. Trying again…");
    }
  }

  function setView(view, load = true) {
    if (state.signingOut) return;
    if (!["wall", "live", "history"].includes(view)) return;
    state.navigationEpoch += 1;
    state.view = view;
    $("recorder-overview").open = view !== "wall";
    $("wall-panel").hidden = view !== "wall";
    $("live-panel").hidden = view !== "live";
    $("history-panel").hidden = view !== "history";
    for (const value of ["wall", "live", "history"]) {
      $(`${value}-tab`).classList.toggle("active", view === value);
      if (view === value) $(`${value}-tab`).setAttribute("aria-current", "page");
      else $(`${value}-tab`).removeAttribute("aria-current");
    }
    if (view === "wall") {
      if (state.cameraConnectionNotice) notice("");
      state.selectionEpoch += 1;
      state.selectionController?.abort();
      stopLive(); stopPlayback();
      if (state.token) cameraWall().show(state.cameras);
      fitCameraWall();
    } else {
      wall?.hide();
      if (view === "history") {
        stopLive();
        if (load && state.selected) {
          if (state.media && !state.selectionController?.signal.aborted) void loadHistory();
          else void selectCamera(state.selected);
        }
      } else {
        stopPlayback();
        if (load && state.selected) {
          if (state.media && !state.selectionController?.signal.aborted) void startLive();
          else void selectCamera(state.selected);
        }
      }
    }
    renderCameraHeading();
  }

  function utcInput(date, seconds = false) { return date.toISOString().slice(0, seconds ? 23 : 16); }
  function parseUtc(value) { return new Date(`${value.length === 16 ? value + ":00" : value}Z`); }
  function utcMilliseconds(value) {
    return typeof value === "string" && value.length <= 32 &&
      /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/.test(value) ? Date.parse(value) : NaN;
  }
  function spanStartMilliseconds(value) {
    const milliseconds = Date.parse(value);
    const fraction = typeof value === "string" ? value.match(/\.([0-9]+)(?:Z|[+-]\d{2}:\d{2})$/)?.[1] : null;
    // JavaScript truncates finer timestamps. Move forward, never before the recording.
    return milliseconds + (fraction && /[1-9]/.test(fraction.slice(3)) ? 1 : 0);
  }
  function utcLabel(value, includeDate = true) {
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "Unavailable";
    return date.toISOString().replace("T", " ").slice(includeDate ? 0 : 11, 19);
  }
  function durationLabel(milliseconds) {
    if (milliseconds < 1000) return "Less than 1 sec";
    const seconds = Math.max(1, Math.round(milliseconds / 1000));
    if (seconds < 60) return `${seconds} sec`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ${seconds % 60 ? `${seconds % 60} sec` : ""}`.trim();
    return `${Math.floor(seconds / 3600)} hr ${Math.floor((seconds % 3600) / 60)} min`;
  }

  function setLastDay() {
    const current = recorderNow();
    if (current === null) return false;
    const end = new Date(current); end.setUTCMilliseconds(0);
    $("history-start").value = utcInput(new Date(end.getTime() - state.historyRangeMs), true);
    $("history-end").value = utcInput(end, true);
    $("history-day").value = "";
    $("history-day").max = end.toISOString().slice(0, 10);
    state.historyRolling = true;
    return true;
  }

  function recorderNow() {
    const sample = state.recorderTime;
    if (!sample || state.statusUnavailable) return null;
    const elapsed = performance.now() - sample.steady;
    return Number.isFinite(elapsed) && elapsed >= 0 && elapsed <= TIME_SAMPLE_MAX_AGE ? sample.utc + elapsed : null;
  }

  function scheduleSessionExpiry() {
    clearTimeout(state.expiryTimer);
    state.expiryTimer = null;
    const current = recorderNow();
    if (state.sessionExpirySteady === null && current !== null && Number.isFinite(state.sessionExpiresAt)) {
      state.sessionExpirySteady = performance.now() + Math.max(0, Math.min(state.sessionExpiresAt - current, DAY));
    }
    if (Number.isFinite(state.sessionExpirySteady)) {
      state.expiryTimer = setTimeout(expireSession, Math.max(0, Math.min(state.sessionExpirySteady - performance.now(), DAY)));
    }
  }

  function resetHistory() {
    state.spans = []; state.selectedSpan = null;
    timeline?.clear();
    $("history-list").replaceChildren();
    $("history-count").textContent = "0";
    $("load-history").disabled = false;
    $("selected-recording").textContent = "No interval selected";
    $("playback-start").value = "";
    $("playback-duration").value = "60";
    for (const id of ["playback-start", "playback-duration", "play-recording"]) $(id).disabled = true;
    $("clip-mark-in").disabled = true;
    $("clip-mark-out").disabled = true;
    $("clip-selection-label").textContent = "Select up to 5 minutes from an available interval.";
    disableDownload();
    message("history-message", state.selected ? "Select a date or interval to see recorded video." : "Select a camera to view its history.");
    message("playback-message", "Preview up to 5 minutes. Select a new start time to seek.");
  }

  function disableDownload() {
    const link = $("download-recording");
    link.removeAttribute("href"); link.removeAttribute("download");
    link.classList.add("disabled"); link.setAttribute("aria-disabled", "true"); link.tabIndex = -1;
  }

  async function loadHistory() {
    if (!state.selected || !state.token) { message("history-message", "Select a camera to view its history."); return; }
    if (state.historyRolling) {
      const before = state.selected, session = state.sessionEpoch;
      await refreshDiagnostics();
      if (before !== state.selected || session !== state.sessionEpoch || !state.token) return;
      if (!state.historyRolling) return;
      if (!setLastDay()) {
        message("history-message", "Recorder time is unavailable. Refresh service status or choose an explicit UTC interval.", true);
        return;
      }
    }
    const start = parseUtc($("history-start").value);
    const end = parseUtc($("history-end").value);
    if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime()) || end <= start || end - start > DAY) {
      message("history-message", "Choose a valid interval of 24 hours or less.", true); return;
    }
    const deviceId = state.selected;
    const epoch = ++state.historyEpoch;
    stopPlayback(); resetHistory();
    $("load-history").disabled = true;
    message("history-message", "Loading recording history…");
    try {
      const query = new URLSearchParams({ start: start.toISOString(), end: end.toISOString() });
      const data = await request(`/api/v1/operator/cameras/${encodeURIComponent(deviceId)}/history?${query}`, { signal: state.selectionController?.signal });
      if (epoch !== state.historyEpoch || deviceId !== state.selected) return;
      if (!Array.isArray(data.spans)) throw new ApiError(503);
      state.spans = data.spans.map((span) => {
        const originalStart = spanStartMilliseconds(span.started_at);
        const originalEnd = Date.parse(span.started_at) + span.duration_seconds * 1000;
        return { ...span, start: Math.max(originalStart, start.getTime()), end: Math.min(originalEnd, end.getTime()) };
      }).filter((span) => Number.isFinite(span.start) && Number.isFinite(span.end) && span.end > span.start).sort((a, b) => a.start - b.start);
      renderHistory(start.getTime(), end.getTime());
      recordingTimeline().setData({ start: start.getTime(), end: end.getTime(), spans: state.spans });
      state.historyFetchedSteady = performance.now();
      message("history-message", state.spans.length
        ? `${state.spans.length} available ${state.spans.length === 1 ? "interval" : "intervals"}. Gaps contain no recorded video.`
        : "No recorded video is available in this interval.");
      return true;
    } catch (error) {
      if (error.name === "AbortError" || epoch !== state.historyEpoch || !state.token) return;
      message("history-message", apiMessage(error, "Recording history is unavailable. Try again."), true);
    } finally {
      if (epoch === state.historyEpoch) $("load-history").disabled = false;
    }
  }

  function renderHistory(start, end) {
    const fragment = document.createDocumentFragment();
    let cursor = start;
    function addGap(from, to) {
      if (to - from < 1000) return;
      const gap = document.createElement("div"); gap.className = "history-gap";
      const title = document.createElement("strong"); title.textContent = "No recording";
      const time = document.createElement("span"); time.textContent = `${utcLabel(from)} → ${utcLabel(to)}`;
      gap.append(title, time); fragment.append(gap);
    }
    for (const [index, span] of state.spans.entries()) {
      addGap(cursor, span.start);
      const button = document.createElement("button"); button.type = "button"; button.className = "history-item";
      button.dataset.recordingIndex = String(index);
      button.setAttribute("aria-pressed", "false");
      const time = document.createElement("span"); time.className = "history-item-time";
      time.textContent = `${utcLabel(span.start)} → ${utcLabel(span.end, false)}`;
      const duration = document.createElement("span"); duration.className = "history-item-duration";
      duration.textContent = `${durationLabel(span.end - span.start)} · Available`;
      button.append(time, duration);
      button.addEventListener("click", () => selectSpan(span, button));
      fragment.append(button);
      cursor = Math.max(cursor, span.end);
    }
    addGap(cursor, end);
    $("history-list").replaceChildren(fragment);
    $("history-count").textContent = String(state.spans.length);
  }

  function selectSpan(span, button, start = span.start, autoPlay = true) {
    stopPlayback();
    state.selectedSpan = span;
    for (const item of $("history-list").querySelectorAll("button")) {
      item.classList.toggle("selected", item === button);
      item.setAttribute("aria-pressed", String(item === button));
    }
    $("playback-start").value = utcInput(new Date(start), true);
    $("playback-start").min = utcInput(new Date(span.start), true);
    $("playback-start").max = utcInput(new Date(span.end), true);
    $("playback-duration").value = String(Math.max(1, Math.min(60, Math.floor((span.end - start) / 1000))));
    for (const id of ["playback-start", "playback-duration", "play-recording"]) $(id).disabled = false;
    $("clip-mark-in").disabled = false;
    $("clip-mark-out").disabled = false;
    $("selected-recording").textContent = `${utcLabel(span.start)} — ${utcLabel(span.end)} UTC`;
    message("playback-message", "Preview up to 5 minutes. Select a new start time to seek.");
    updateDownload();
    recordingTimeline().setPlayhead(start);
    if (autoPlay && span.end - start >= 1000) void playRecording({ preventDefault() {} });
  }

  function seekRecording(milliseconds) {
    if (!Number.isFinite(milliseconds) || state.view !== "history" || !state.token) return;
    const index = state.spans.findIndex((span) => span.start <= milliseconds && span.end - milliseconds >= 1000);
    if (index < 0) {
      stopPlayback();
      state.selectedSpan = null;
      for (const item of $("history-list").querySelectorAll("button")) {
        item.classList.remove("selected");
        item.setAttribute("aria-pressed", "false");
      }
      $("selected-recording").textContent = "No interval selected";
      $("playback-start").value = "";
      $("playback-duration").value = "60";
      for (const id of ["playback-start", "playback-duration", "play-recording", "clip-mark-in", "clip-mark-out"]) $(id).disabled = true;
      $("clip-selection-label").textContent = "Select up to 5 minutes from an available interval.";
      disableDownload();
      message("playback-message", `No playable recording at ${utcLabel(milliseconds)} UTC. Choose a recorded interval on the timeline.`, true);
      return;
    }
    const button = $("history-list").querySelector(`[data-recording-index="${index}"]`);
    selectSpan(state.spans[index], button, milliseconds);
  }

  function playLatestSpan() {
    const span = [...state.spans].reverse().find((item) => item.end - item.start >= 1000);
    if (!span) {
      message("playback-message", "No playable recording is available yet. Check the recording state and refresh history after video has been recorded.", true);
      return;
    }
    const index = state.spans.indexOf(span);
    const start = Math.max(span.start, span.end - 60000);
    selectSpan(span, $("history-list").querySelector(`[data-recording-index="${index}"]`), start);
  }

  async function showLatestRecording() {
    if (!state.selected || !state.token) return;
    const deviceId = state.selected, session = state.sessionEpoch;
    setView("history", false);
    if (state.media && !state.selectionController?.signal.aborted) await loadHistory();
    else await selectCamera(deviceId);
    if (deviceId === state.selected && session === state.sessionEpoch && state.view === "history") playLatestSpan();
  }

  async function toggleRecording() {
    const camera = currentCamera();
    if (!camera || !canManageCameras() || state.recordingBusy || state.signingOut) return;
    const deviceId = camera.device_id, session = state.sessionEpoch;
    const enabled = camera.recording_enabled === false;
    state.recordingBusy = true;
    state.cameraListEpoch += 1;
    renderCameraHeading();
    try {
      await request(`/api/v1/operator/cameras/${encodeURIComponent(deviceId)}/recording`, { method: "PUT", body: { enabled } });
      if (session !== state.sessionEpoch) return;
      await refreshCameras();
      if (deviceId === state.selected) notice(enabled
        ? "Recording resumed. K5 will confirm new recording data on disk."
        : "Recording paused for this camera. Live video and retained recordings remain available.");
    } catch (error) {
      if (error.name !== "AbortError" && session === state.sessionEpoch && deviceId === state.selected) {
        notice(apiMessage(error, "The recording change could not be confirmed. Refresh and check its state before trying again."), true);
      }
    } finally {
      if (session === state.sessionEpoch) { state.recordingBusy = false; renderCameraHeading(); }
    }
  }

  function rollingHistory(milliseconds) {
    state.historyRangeMs = milliseconds;
    state.historyRolling = true;
    void loadHistory();
  }

  function playbackUtc() {
    const elapsed = $("playback-video").currentTime;
    return Number.isFinite(state.playbackStart) && Number.isFinite(elapsed)
      ? state.playbackStart + elapsed * 1000 : parseUtc($("playback-start").value).getTime();
  }

  function playbackSelection() {
    if (!state.selectedSpan || !state.media) throw new SelectionError("Choose an available recording interval first.");
    const start = parseUtc($("playback-start").value).getTime();
    const seconds = Number($("playback-duration").value);
    const span = state.selectedSpan;
    if (!Number.isFinite(start) || start < span.start || start >= span.end) throw new SelectionError("Choose a start time inside the selected interval.");
    if (!Number.isFinite(seconds) || seconds < 1 || seconds > 300 || !Number.isInteger(seconds)) throw new SelectionError("Choose a preview length from 1 to 300 seconds.");
    const duration = Math.min(seconds, (span.end - start) / 1000);
    if (duration < 1) throw new SelectionError("Choose a start time at least one second before the interval ends.");
    const url = new URL(state.media.playback_url, location.origin);
    url.searchParams.set("start", new Date(start).toISOString());
    url.searchParams.set("duration", String(duration));
    url.searchParams.set("format", "mp4");
    return { url: url.pathname + url.search, start, duration };
  }

  function updateDownload() {
    try {
      const selection = playbackSelection();
      const link = $("download-recording");
      link.href = selection.url;
      link.download = `K5-recording-${new Date(selection.start).toISOString().replace(/[:.]/g, "-")}.mp4`;
      link.classList.remove("disabled"); link.setAttribute("aria-disabled", "false"); link.tabIndex = 0;
      $("clip-selection-label").textContent = `${utcLabel(selection.start)} → ${utcLabel(selection.start + selection.duration * 1000, false)} UTC · ${durationLabel(selection.duration * 1000)}`;
    } catch (_) { disableDownload(); }
  }

  async function playRecording(event) {
    event.preventDefault();
    const epoch = ++state.playbackEpoch;
    try {
      const selection = playbackSelection();
      const video = $("playback-video");
      state.playbackStart = selection.start;
      video.src = selection.url;
      video.load();
      $("playback-placeholder").hidden = true;
      message("playback-message", `Loading ${durationLabel(selection.duration * 1000)} of recorded video…`);
      updateDownload();
      await video.play();
    } catch (error) {
      if (epoch !== state.playbackEpoch || error.name === "AbortError") return;
      message("playback-message", error.name === "NotAllowedError"
        ? "Select play in the video controls to begin."
        : error instanceof SelectionError ? error.message
          : "Playback is unavailable. Refresh history or select another interval.", true);
    }
  }

  $("sign-in-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const epoch = ++state.sessionEpoch;
    $("sign-in-button").disabled = true;
    $("sign-in-button").textContent = "Signing in…";
    message("sign-in-message", "");
    const credentials = { username: $("username").value.trim(), password: $("password").value };
    $("password").value = "";
    try {
      const result = await request("/api/v1/auth/login", { method: "POST", body: credentials });
      credentials.password = "";
      if (epoch !== state.sessionEpoch) return;
      if (typeof result.session_token !== "string" || !result.account) throw new ApiError(503);
      state.token = result.session_token;
      const account = await request("/api/v1/auth/me");
      if (epoch !== state.sessionEpoch) return;
      state.account = account;
      $("account-name").textContent = account.display_name || account.username;
      $("account-role").textContent = account.role;
      $("add-camera-button").hidden = account.role !== "administrator";
      $("sign-in").hidden = true;
      $("workspace").hidden = false;
      state.sessionExpiresAt = Date.parse(result.expires_at);
      state.sessionExpirySteady = Number.isInteger(result.expires_in_seconds) && result.expires_in_seconds > 0 && result.expires_in_seconds <= 86400
        ? performance.now() + result.expires_in_seconds * 1000 : null;
      scheduleSessionExpiry();
      await Promise.all([refreshCameras(true), refreshDiagnostics()]);
      if (epoch !== state.sessionEpoch) return;
      setLastDay(); resetHistory();
      setView("wall", false);
      scheduleCameraPoll();
    } catch (error) {
      if (error.name === "AbortError" || epoch !== state.sessionEpoch) return;
      state.token = "";
      message("sign-in-message", error.status === 401
        ? "Username or password is incorrect."
        : apiMessage(error, "Sign-in is unavailable. Confirm K5 is running on this PC and check its startup message."), true);
    } finally {
      credentials.password = "";
      if (epoch === state.sessionEpoch) {
        $("sign-in-button").disabled = false;
        $("sign-in-button").textContent = "Sign in →";
      }
    }
  });

  $("sign-out-button").addEventListener("click", () => void signOut());
  $("refresh-cameras").addEventListener("click", () => void Promise.all([refreshCameras(true), refreshDiagnostics()]));
  $("camera-search").addEventListener("input", renderCameras);
  $("wall-tab").addEventListener("click", () => setView("wall"));
  $("live-tab").addEventListener("click", () => setView("live"));
  $("history-tab").addEventListener("click", () => setView("history"));
  $("toggle-recording").addEventListener("click", () => void toggleRecording());
  $("recent-recordings").addEventListener("click", () => void showLatestRecording());
  $("play-latest-recording").addEventListener("click", async () => {
    const session = state.sessionEpoch, deviceId = state.selected;
    if (await loadHistory() && session === state.sessionEpoch && deviceId === state.selected && state.view === "history") playLatestSpan();
  });
  $("retry-live").addEventListener("click", () => { if (state.selected) void selectCamera(state.selected); });
  $("play-live").addEventListener("click", () => void $("live-video").play().catch(() => message("live-state", "Unable to play live video. Try reconnecting.")));
  $("live-video").addEventListener("playing", () => {
    if (!state.token || state.view !== "live") return;
    $("live-placeholder").hidden = true; $("live-video-label").hidden = false; $("play-live").hidden = true;
    message("live-state", "Live video connected");
    renderPersonControls();
  });
  $("live-video").addEventListener("pause", () => { if (state.personEnabled) stopPersonView("Sampled view stopped while live video is paused."); });
  $("live-video").addEventListener("waiting", () => {
    if (state.personEnabled) stopPersonView("Sampled view stopped while live video is buffering. Start it again when video returns.");
    if (state.token && state.view === "live") {
      $("live-video-label").hidden = true;
      message("live-state", "Buffering live video…");
    }
  });
  $("toggle-person-view").addEventListener("click", () => {
    if (state.personEnabled) { stopPersonView(); return; }
    if (!canSamplePerson()) return;
    state.personEnabled = true;
    const epoch = ++state.personEpoch;
    renderPersonControls();
    message("person-view-message", "Processing a sampled frame…");
    void samplePerson(epoch);
  });
  $("live-video").addEventListener("error", () => {
    if (state.token && state.media && state.view === "live" && state.nativeLive) scheduleLiveRetry(state.selectionEpoch, "The live stream is unavailable. Waiting for the camera…");
  });
  $("history-form").addEventListener("submit", (event) => { event.preventDefault(); void loadHistory(); });
  $("history-start").addEventListener("input", () => { state.historyRolling = false; });
  $("history-end").addEventListener("input", () => { state.historyRolling = false; });
  $("last-24-hours").addEventListener("click", () => rollingHistory(DAY));
  $("history-last-hour").addEventListener("click", () => rollingHistory(3600000));
  $("history-15-minutes").addEventListener("click", () => rollingHistory(900000));
  $("history-day").addEventListener("change", () => {
    const date = new Date(`${$("history-day").value}T00:00:00Z`);
    if (!Number.isFinite(date.getTime())) return;
    $("history-start").value = utcInput(date);
    $("history-end").value = utcInput(new Date(date.getTime() + DAY));
    state.historyRolling = false;
    void loadHistory();
  });
  $("playback-form").addEventListener("submit", (event) => void playRecording(event));
  $("playback-start").addEventListener("input", updateDownload);
  $("playback-duration").addEventListener("input", updateDownload);
  $("playback-video").addEventListener("timeupdate", () => {
    if (Number.isFinite(state.playbackStart)) timeline?.setPlayhead(playbackUtc());
  });
  $("clip-mark-in").addEventListener("click", () => {
    const at = playbackUtc(), span = state.selectedSpan;
    if (!span || !Number.isFinite(at) || at < span.start || span.end - at < 1000) return;
    $("playback-start").value = utcInput(new Date(at), true);
    $("playback-duration").value = String(Math.max(1, Math.min(Number($("playback-duration").value) || 60, Math.floor((span.end - at) / 1000))));
    updateDownload();
  });
  $("clip-mark-out").addEventListener("click", () => {
    const at = playbackUtc(), start = parseUtc($("playback-start").value).getTime();
    const duration = Math.floor((at - start) / 1000);
    if (!state.selectedSpan || !Number.isFinite(duration) || duration < 1 || duration > 300 || at > state.selectedSpan.end) {
      message("playback-message", "The clip end must be after its start, within the same recorded interval, and at most 5 minutes later.", true);
      return;
    }
    $("playback-duration").value = String(duration);
    updateDownload();
  });
  $("playback-video").addEventListener("playing", () => message("playback-message", "Recorded video · Select a new start time to seek to another point."));
  $("playback-video").addEventListener("error", () => { if (state.token && state.selectedSpan) message("playback-message", "Playback is unavailable. Refresh history or select another interval.", true); });

  function canManageCameras() {
    return state.account?.role === "administrator" && Boolean(state.token) && !state.signingOut;
  }

  function cameraSetupType() {
    return $("camera-connection-type").value === "rtsp" ? "rtsp" : "onvif";
  }

  function renderCameraSetupType() {
    const direct = cameraSetupType() === "rtsp", updating = state.dialogMode === "update";
    $("camera-onvif-settings").hidden = direct;
    $("camera-stream-settings").hidden = !direct;
    $("camera-discovery-section").hidden = direct || updating;
    $("camera-setup-description").textContent = direct
      ? updating ? "Enter the replacement RTSP stream address for this camera. Reconnecting keeps its identity and recording history."
        : "Connect a camera or video source directly using its RTSP stream address, with an optional camera account."
      : updating ? "Sign in to this camera and test its available video streams. Reconnecting keeps its identity and recording history."
        : "Find an ONVIF camera on your network, then sign in to the camera. K5 finds its available video streams for you.";
    $("camera-account-help").textContent = direct
      ? "Leave the account blank for a stream that does not require authentication. The stream address, username and password are cleared after testing."
      : "Use the account configured on this camera. Its password is cleared after testing.";
    $("camera-profile-help").textContent = direct
      ? "This stream reports H.264 video for live viewing and continuous recording. K5 does not change the source settings."
      : "Choose an H.264 stream for live video and continuous recording. K5 does not change camera settings.";
  }

  function clearCameraProbe() {
    clearTimeout(state.cameraProbeTimer);
    state.cameraProbeTimer = null;
    state.cameraProbe = null;
    $("camera-profile").replaceChildren();
    $("camera-profile-section").hidden = true;
    $("save-camera").disabled = true;
    $("camera-clock-status").hidden = true;
    $("camera-clock-status").textContent = "";
  }

  function cameraClockReport(value) {
    if (!value || value.sync_verified !== false || !["measured", "unavailable"].includes(value.status)) return null;
    if (value.status === "unavailable") return { status: "unavailable" };
    if (typeof value.offset_seconds !== "number" || !Number.isFinite(value.offset_seconds) ||
        Math.abs(value.offset_seconds) > 315576000000 || typeof value.measured_at !== "string" ||
        !Number.isFinite(utcMilliseconds(value.measured_at)) ||
        typeof value.uncertainty_seconds !== "number" || !Number.isFinite(value.uncertainty_seconds) ||
        value.uncertainty_seconds < 0 || value.uncertainty_seconds > 15 ||
        !["ntp", "manual", "unknown"].includes(value.reported_mode)) return null;
    return value;
  }

  function renderCameraClock(value) {
    const clock = cameraClockReport(value);
    let text = "Camera time could not be checked. Time synchronization is not verified. Check Windows time synchronization and the camera's NTP settings.";
    let warning = true;
    if (clock?.status === "measured") {
      const offset = Math.abs(clock.offset_seconds);
      const mode = clock.reported_mode === "ntp" ? "Camera reports NTP mode." : clock.reported_mode === "manual" ? "Camera reports manual time." : "Camera time mode is unknown.";
      const alignment = offset > 5
        ? `Camera is ${offset.toFixed(1)} seconds ${clock.offset_seconds > 0 ? "ahead of" : "behind"} the recorder. Authentication uses camera time.`
        : `Camera and recorder differ by ${offset.toFixed(1)} seconds.`;
      text = `${alignment} Checked ${utcLabel(clock.measured_at)} UTC, with measurement uncertainty ±${clock.uncertainty_seconds.toFixed(1)} seconds. ${mode} Time synchronization is not verified. Check Windows time synchronization and camera NTP settings.`;
      warning = offset > 5;
    }
    $("camera-clock-status").textContent = text;
    $("camera-clock-status").classList.toggle("error", warning);
    $("camera-clock-status").hidden = false;
  }

  function renderStreamClock() {
    message("camera-clock-status", "RTSP does not provide a camera clock check. Recording history uses recorder UTC. Verify Windows time synchronization and the camera’s NTP settings.", true);
    $("camera-clock-status").hidden = false;
  }

  function setCameraSetupBusy(busy, action = "") {
    state.cameraSetupBusy = busy;
    const direct = cameraSetupType() === "rtsp";
    $("add-camera-form").setAttribute("aria-busy", String(busy));
    for (const id of ["new-camera-name", "new-camera-username", "new-camera-password"]) $(id).disabled = busy;
    for (const id of ["new-camera-host", "new-camera-port", "new-camera-secure"]) $(id).disabled = busy || direct;
    $("new-camera-stream-uri").disabled = busy || !direct;
    $("camera-connection-type").disabled = busy;
    $("find-cameras").disabled = busy || direct || !canManageCameras() || state.dialogMode === "update";
    $("test-camera-connection").disabled = busy || !canManageCameras();
    $("camera-profile").disabled = busy || !state.cameraProbe;
    const selected = state.cameraProbe?.profiles.find((profile) => profile.id === $("camera-profile").value);
    $("save-camera").disabled = busy || !canManageCameras() || !selected?.supported || state.cameraProbe?.kind !== cameraSetupType();
    $("find-cameras").textContent = action === "discovery" ? "Finding cameras…" : "Find cameras on this network";
    $("test-camera-connection").textContent = action === "profiles" ? "Testing camera…" : "Test connection";
    $("save-camera").textContent = action === "connect" ? "Connecting camera…" : state.dialogMode === "update" ? "Reconnect camera" : "Connect camera";
    for (const button of $("discovered-cameras").querySelectorAll("button")) button.disabled = busy;
  }

  function resetCameraSetup() {
    state.cameraSetupEpoch += 1;
    state.cameraSetupController?.abort();
    state.cameraSetupController = null;
    clearCameraProbe();
    $("add-camera-form").reset();
    state.cameraSetupTarget = null;
    $("new-camera-name").readOnly = false;
    $("new-camera-host").readOnly = false;
    $("discovered-cameras").replaceChildren();
    message("camera-discovery-message", "If Windows Firewall blocks discovery, enter the camera’s IP address below.");
    message("add-camera-message", "");
    state.dialogMode = "add";
    renderCameraSetupType();
    setCameraSetupBusy(false);
  }

  function closeAddCamera() {
    resetCameraSetup();
    if ($("add-camera-dialog").open) $("add-camera-dialog").close();
  }

  function cameraSetupChanged() {
    const previouslyTested = Boolean(state.cameraProbe) || state.cameraSetupBusy;
    state.cameraSetupEpoch += 1;
    state.cameraSetupController?.abort();
    state.cameraSetupController = null;
    clearCameraProbe();
    setCameraSetupBusy(false);
    if (previouslyTested) message("add-camera-message", "The camera details changed. Test the connection again.");
  }

  function cameraSetupError(error) {
    const messages = {
      discovery_unavailable: "Camera discovery is unavailable. Enter the camera’s IP address instead.",
      camera_auth_failed: "Camera authentication failed. Check the camera account and its time/NTP settings. Some cameras use a separate ONVIF account.",
      camera_unreachable: "The camera could not be reached. Check its power, IP address and camera settings port, and confirm ONVIF is enabled.",
      camera_unsupported: "This camera has no supported H.264 stream. Enable ONVIF and H.264 in its settings, then test again.",
      camera_invalid_response: "The camera did not return usable video settings. Check that ONVIF is enabled and try again.",
      camera_busy: "The camera is busy. Wait a moment, then test again.",
      camera_probe_expired: "The connection test expired. Enter the camera password and test again.",
      camera_profile_unsupported: "Choose an H.264 stream. Other video formats are not supported yet.",
    };
    if (cameraSetupType() === "rtsp") {
      Object.assign(messages, {
        camera_auth_failed: "Stream authentication failed. Check the camera username and password, then test again.",
        camera_unreachable: "The stream could not be reached. Check the camera address, stream port and path, and confirm RTSP is enabled.",
        camera_unsupported: "The stream does not report supported H.264 video. Choose an H.264 stream and test again.",
        camera_invalid_response: "The source did not return a usable RTSP stream. Check its address and stream settings.",
        camera_probe_expired: "The connection test expired. Enter the stream address and account, then test again.",
      });
      messages.stream_invalid = "Enter an rtsp:// or rtsps:// stream address with the camera’s numeric IP address and stream path. Use either the URL account or the separate account fields.";
      for (const name of ["auth_failed", "unsupported", "unreachable", "invalid_response"]) {
        messages[`stream_${name}`] = messages[`camera_${name}`];
      }
    }
    return messages[error.code] || (error.status === 410
      ? messages.camera_probe_expired
      : error.status === 409 ? "This camera is already configured. Select it in the camera list to reconnect."
        : error.status === 422 ? cameraSetupType() === "rtsp"
          ? "Check the RTSP address, numeric camera IP, stream port and account, then test again. Reconnecting uses the same camera IP address."
          : "Check the camera’s IP address, camera settings port and account, then test again."
          : apiMessage(error, "The camera connection did not complete. Check the camera settings and try again."));
  }

  function cameraConnection() {
    if (cameraSetupType() === "rtsp") {
      const source_uri = $("new-camera-stream-uri").value.trim();
      try {
        const parsed = new URL(source_uri);
        if (!source_uri || source_uri.length > 4096 || /[\s\\]/.test(source_uri) ||
          !["rtsp:", "rtsps:"].includes(parsed.protocol) || !parsed.hostname || parsed.hash) throw new SelectionError();
      } catch {
        message("add-camera-message", "Enter an rtsp:// or rtsps:// stream address with the camera’s numeric IP address and stream path.", true);
        $("new-camera-stream-uri").focus();
        return null;
      }
      return { source_uri, username: $("new-camera-username").value, password: $("new-camera-password").value };
    }
    const host = $("new-camera-host").value.trim();
    if (!host || /[\s/?#@\\]/.test(host) || host.includes("://")) {
      message("add-camera-message", "Enter the camera’s numeric IP address, without a web or video address.", true);
      $("new-camera-host").focus();
      return null;
    }
    if (!$("new-camera-port").reportValidity()) return null;
    const port = Number($("new-camera-port").value);
    if (!Number.isInteger(port) || port < 1 || port > 65535) return null;
    return {
      host, port, secure: $("new-camera-secure").checked,
      username: $("new-camera-username").value, password: $("new-camera-password").value,
    };
  }

  function openCameraSetup(mode) {
    if (!canManageCameras()) return;
    const camera = mode === "update" ? currentCamera() : null;
    if (mode === "update" && !camera) return;
    if (mode === "update") stopPersonView();
    resetCameraSetup();
    state.dialogMode = mode;
    state.cameraSetupTarget = camera?.device_id || null;
    $("camera-connection-type").value = camera?.connection_type === "rtsp" ? "rtsp" : "onvif";
    $("new-camera-name").readOnly = mode === "update";
    $("new-camera-host").readOnly = mode === "update";
    $("add-camera-title").textContent = mode === "update" ? "Reconnect camera" : "Add a camera";
    renderCameraSetupType();
    if (camera) {
      $("new-camera-name").value = camera.name;
      $("new-camera-host").value = camera.host;
      $("new-camera-port").value = camera.management_port || 80;
      $("new-camera-secure").checked = camera.management_secure === true;
    }
    setCameraSetupBusy(false);
    $("add-camera-dialog").showModal();
    $(cameraSetupType() === "rtsp" ? "new-camera-stream-uri" : mode === "update" ? "new-camera-username" : "find-cameras").focus();
  }
  $("add-camera-button").addEventListener("click", () => openCameraSetup("add"));
  $("update-source-button").addEventListener("click", () => openCameraSetup("update"));
  $("close-add-camera").addEventListener("click", closeAddCamera);
  $("cancel-add-camera").addEventListener("click", closeAddCamera);
  $("add-camera-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeAddCamera();
  });
  $("add-camera-dialog").addEventListener("close", () => {
    if ($("add-camera-dialog").open) return;
    $("new-camera-password").value = "";
    $("new-camera-username").value = "";
    $("new-camera-stream-uri").value = "";
  });
  for (const id of [
    "new-camera-name", "new-camera-host", "new-camera-port", "new-camera-username", "new-camera-password", "new-camera-stream-uri",
  ]) $(id).addEventListener("input", cameraSetupChanged);
  $("camera-connection-type").addEventListener("change", () => {
    cameraSetupChanged();
    $("new-camera-stream-uri").value = "";
    $("new-camera-username").value = "";
    $("new-camera-password").value = "";
    renderCameraSetupType();
    setCameraSetupBusy(false);
  });
  $("new-camera-secure").addEventListener("change", () => {
    if ($("new-camera-secure").checked && $("new-camera-port").value === "80") $("new-camera-port").value = "443";
    else if (!$("new-camera-secure").checked && $("new-camera-port").value === "443") $("new-camera-port").value = "80";
    cameraSetupChanged();
  });
  $("camera-profile").addEventListener("change", () => setCameraSetupBusy(false));

  $("find-cameras").addEventListener("click", async () => {
    if (!canManageCameras() || state.cameraSetupBusy) return;
    cameraSetupChanged();
    const epoch = state.sessionEpoch, setupEpoch = state.cameraSetupEpoch;
    const controller = new AbortController();
    state.cameraSetupController = controller;
    $("discovered-cameras").replaceChildren();
    setCameraSetupBusy(true, "discovery");
    message("camera-discovery-message", "Looking for ONVIF cameras on this network…");
    try {
      const found = await request("/api/v1/operator/camera-discovery", {
        method: "POST", body: {}, signal: controller.signal,
      });
      if (epoch !== state.sessionEpoch || setupEpoch !== state.cameraSetupEpoch) return;
      if (!Array.isArray(found.cameras)) throw new SelectionError();
      for (const camera of found.cameras.slice(0, 64)) {
        if (typeof camera.host !== "string" || camera.host.length > 45 || /[\s/?#@\\]/.test(camera.host) ||
          !Number.isInteger(camera.port) || camera.port < 1 || camera.port > 65535 || typeof camera.secure !== "boolean") continue;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "button secondary discovered-camera";
        const name = typeof camera.name === "string" && !camera.name.includes("://")
          ? camera.name.slice(0, 128) : "Network camera";
        button.textContent = (name || "Network camera") + " · " + camera.host;
        button.addEventListener("click", () => {
          cameraSetupChanged();
          $("new-camera-host").value = camera.host;
          $("new-camera-port").value = camera.port;
          $("new-camera-secure").checked = camera.secure;
          $("new-camera-username").value = "";
          $("new-camera-password").value = "";
          if (!$("new-camera-name").value.trim()) $("new-camera-name").value = name || "Network camera";
          message("add-camera-message", "Camera selected. Enter the account configured on this camera, then test the connection.");
          $("new-camera-username").focus();
        });
        $("discovered-cameras").append(button);
      }
      message("camera-discovery-message", $("discovered-cameras").children.length
        ? "Choose your camera below, or enter a different IP address."
        : "No ONVIF cameras were found. Enter the camera’s IP address below; discovery may be blocked by the network or Windows Firewall.");
    } catch (error) {
      if (controller.signal.aborted || epoch !== state.sessionEpoch || setupEpoch !== state.cameraSetupEpoch) return;
      message("camera-discovery-message", cameraSetupError(error), true);
    } finally {
      if (epoch === state.sessionEpoch && setupEpoch === state.cameraSetupEpoch) {
        state.cameraSetupController = null;
        setCameraSetupBusy(false);
      }
    }
  });

  $("test-camera-connection").addEventListener("click", async () => {
    if (!canManageCameras() || state.cameraSetupBusy) return;
    const credentials = cameraConnection();
    if (!credentials) return;
    const kind = cameraSetupType(), direct = kind === "rtsp";
    clearCameraProbe();
    const epoch = state.sessionEpoch, setupEpoch = state.cameraSetupEpoch;
    const controller = new AbortController();
    state.cameraSetupController = controller;
    setCameraSetupBusy(true, "profiles");
    message("add-camera-message", direct ? "Checking the RTSP stream and its video format…" : "Signing in to the camera and reading its video streams…");
    try {
      const tested = await request(direct ? "/api/v1/operator/camera-stream-probe" : "/api/v1/operator/camera-profiles", {
        method: "POST", body: credentials, signal: controller.signal,
      });
      if (epoch !== state.sessionEpoch || setupEpoch !== state.cameraSetupEpoch) return;
      if (typeof tested.probe_id !== "string" || !tested.probe_id || tested.probe_id.length > 256 || tested.probe_id.includes("://") ||
        !Array.isArray(tested.profiles) || tested.profiles.length > 64) throw new SelectionError();
      if (direct) renderStreamClock();
      else renderCameraClock(tested.clock);
      const profiles = tested.profiles.filter((profile) =>
        typeof profile.id === "string" && profile.id && profile.id.length <= 256 && !profile.id.includes("://") &&
        ["H264", "H265", "MJPEG", "UNKNOWN"].includes(profile.codec)
      ).map((profile) => ({
        id: profile.id, name: profile.name, codec: profile.codec, width: profile.width, height: profile.height,
        supported: profile.supported === true && profile.codec === "H264",
      }));
      const first = profiles.find((profile) => profile.supported);
      for (const profile of profiles) {
        const option = document.createElement("option");
        option.value = profile.id;
        const name = typeof profile.name === "string" && !profile.name.includes("://")
          ? profile.name.slice(0, 128) : "Camera stream";
        const dimensions = Number.isInteger(profile.width) && Number.isInteger(profile.height) &&
          profile.width > 0 && profile.height > 0 && profile.width <= 16384 && profile.height <= 16384
          ? " · " + profile.width + " × " + profile.height : "";
        option.textContent = (name || "Camera stream") + " · " + profile.codec + dimensions + (profile.supported ? "" : " · H.264 required");
        option.disabled = !profile.supported;
        $("camera-profile").append(option);
      }
      $("camera-profile-section").hidden = false;
      if (!first) {
        message("add-camera-message", direct
          ? "The stream does not report supported H.264 video. Choose an H.264 stream and test again."
          : "The camera account was accepted, but no H.264 stream is available. Enable H.264 in the camera settings, then test again.", true);
        return;
      }
      state.cameraProbe = { id: tested.probe_id, profiles, kind };
      $("camera-profile").value = first.id;
      const lifetime = Number.isInteger(tested.expires_in_seconds) && tested.expires_in_seconds > 0
        ? Math.min(tested.expires_in_seconds, 120) : 120;
      state.cameraProbeTimer = setTimeout(() => {
        if (epoch !== state.sessionEpoch || setupEpoch !== state.cameraSetupEpoch || !$("add-camera-dialog").open) return;
        state.cameraSetupEpoch += 1;
        state.cameraSetupController?.abort();
        state.cameraSetupController = null;
        clearCameraProbe();
        message("add-camera-message", direct
          ? "The connection test expired. Enter the stream address and account, then test again."
          : "The connection test expired. Enter the camera password and test again.", true);
        setCameraSetupBusy(false);
      }, lifetime * 1000);
      message("add-camera-message", direct
        ? "RTSP stream reports H.264 video. Connect to start live viewing and recording; video is confirmed after connecting."
        : "Camera account accepted. Choose an H.264 stream; video is checked after connecting.");
    } catch (error) {
      if (controller.signal.aborted || epoch !== state.sessionEpoch || setupEpoch !== state.cameraSetupEpoch) return;
      if (direct) renderStreamClock();
      else renderCameraClock(error.clock);
      message("add-camera-message", cameraSetupError(error), true);
    } finally {
      credentials.password = "";
      if (direct) {
        credentials.source_uri = "";
        credentials.username = "";
      }
      if (epoch === state.sessionEpoch && setupEpoch === state.cameraSetupEpoch) {
        $("new-camera-password").value = "";
        if (direct) {
          $("new-camera-stream-uri").value = "";
          $("new-camera-username").value = "";
        }
        state.cameraSetupController = null;
        setCameraSetupBusy(false);
        if (state.cameraProbe) $("camera-profile").focus();
      }
    }
  });

  $("add-camera-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!canManageCameras() || state.cameraSetupBusy || $("save-camera").disabled || !state.cameraProbe) return;
    const epoch = state.sessionEpoch;
    const setupEpoch = state.cameraSetupEpoch;
    const updating = state.dialogMode === "update";
    const direct = state.cameraProbe.kind === "rtsp";
    const profile = state.cameraProbe.profiles.find((item) => item.id === $("camera-profile").value && item.supported);
    if (!profile) return;
    const camera = {
      name: $("new-camera-name").value.trim(), probe_id: state.cameraProbe.id, profile_id: profile.id,
      ...(direct && updating ? { device_id: state.cameraSetupTarget } : {}),
    };
    const controller = new AbortController();
    state.cameraSetupController = controller;
    setCameraSetupBusy(true, "connect");
    message("add-camera-message", updating ? "Reconnecting live video and recording…" : "Connecting your camera…");
    try {
      const created = await request(direct ? "/api/v1/operator/cameras/stream-connect" : "/api/v1/operator/cameras/connect", {
        method: "POST", body: camera, signal: controller.signal,
      });
      if (epoch !== state.sessionEpoch || setupEpoch !== state.cameraSetupEpoch) return;
      closeAddCamera();
      const completedEpoch = state.cameraSetupEpoch;
      const navigationEpoch = state.navigationEpoch;
      const deviceId = created.device_id || created.camera?.device_id;
      const cameraStatus = created.camera || created;
      state.cameraConnectionNotice = deviceId || null;
      const connection = cameraConnectionStatus(cameraStatus, state.workerStatus);
      notice(connection.text, connection.error, "camera_connection");
      await refreshCameras();
      if (deviceId && epoch === state.sessionEpoch && completedEpoch === state.cameraSetupEpoch &&
        navigationEpoch === state.navigationEpoch) await selectCamera(deviceId);
    } catch (error) {
      if (controller.signal.aborted || epoch !== state.sessionEpoch || setupEpoch !== state.cameraSetupEpoch) return;
      if (error.status === 410 || error.code === "camera_probe_expired") clearCameraProbe();
      message("add-camera-message", cameraSetupError(error), true);
    } finally {
      if (epoch === state.sessionEpoch && setupEpoch === state.cameraSetupEpoch) {
        state.cameraSetupController = null;
        setCameraSetupBusy(false);
      }
    }
  });

  function setRemovalBusy(busy) {
    state.removing = busy;
    for (const id of ["confirm-remove-camera", "cancel-remove-camera", "close-remove-camera"]) $(id).disabled = busy;
    $("confirm-remove-camera").textContent = busy ? "Removing…" : "Remove camera";
  }

  function closeRemoveCamera() {
    if (!state.removing) $("remove-camera-dialog").close();
  }

  $("remove-camera-button").addEventListener("click", () => {
    const camera = currentCamera();
    if (!camera || state.account?.role !== "administrator") return;
    state.removeTarget = { device_id: camera.device_id, name: camera.name, host: camera.host };
    $("remove-camera-name").textContent = `${camera.name} · ${camera.host}`;
    message("remove-camera-message", "");
    setRemovalBusy(false);
    $("remove-camera-dialog").showModal();
    $("cancel-remove-camera").focus();
  });
  $("cancel-remove-camera").addEventListener("click", closeRemoveCamera);
  $("close-remove-camera").addEventListener("click", closeRemoveCamera);
  $("remove-camera-dialog").addEventListener("cancel", (event) => { if (state.removing) event.preventDefault(); });
  $("remove-camera-dialog").addEventListener("close", () => { state.removeTarget = null; });
  $("remove-camera-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.removing || !state.removeTarget || state.account?.role !== "administrator") return;
    const target = state.removeTarget;
    const epoch = state.sessionEpoch;
    setRemovalBusy(true);
    message("remove-camera-message", "Removing camera and stopping new recording…");
    let delayed = false;
    try {
      try {
        await request(`/api/v1/operator/cameras/${encodeURIComponent(target.device_id)}`, { method: "DELETE" });
      } catch (error) {
        if (error.name === "AbortError" || epoch !== state.sessionEpoch) return;
        if (error.status === 503) {
          const refreshed = await refreshCameras();
          if (!refreshed || state.cameras.some((camera) => camera.device_id === target.device_id)) throw error;
          delayed = true;
        } else if (error.status !== 404) throw error;
      }
      if (epoch !== state.sessionEpoch) return;
      state.cameras = state.cameras.filter((camera) => camera.device_id !== target.device_id);
      if (state.selected === target.device_id) clearCameraSelection();
      else renderCameras();
      $("remove-camera-dialog").close();
      await Promise.all([refreshCameras(true), refreshDiagnostics()]);
      if (epoch !== state.sessionEpoch) return;
      notice(delayed
        ? "Camera removed from the workspace. The media service is restarting to finish applying the change. Existing recording files remain subject to retention."
        : "Camera removed. Existing recording files remain subject to retention.");
    } catch (error) {
      if (error.name === "AbortError" || epoch !== state.sessionEpoch) return;
      message("remove-camera-message", apiMessage(error, "Camera removal could not be confirmed. Refresh the camera list before trying again."), true);
    } finally {
      if (epoch === state.sessionEpoch) setRemovalBusy(false);
    }
  });

  $("connection-help-button").addEventListener("click", () => { renderDiagnostics(); $("connection-help-dialog").showModal(); });
  for (const id of ["close-connection-help", "done-connection-help"]) {
    $(id).addEventListener("click", () => $("connection-help-dialog").close());
  }

  window.addEventListener("pagehide", () => { wall?.hide(); stopLive(); stopPlayback(); });
  window.addEventListener("resize", fitCameraWall);
  $("recorder-overview").addEventListener("toggle", fitCameraWall);
  setLastDay();
  resetHistory();
})();
