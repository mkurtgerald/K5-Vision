/* K5 Vision local operator workspace. Human tokens remain in memory only.
 * Live-player event wiring adapts MediaMTX v1.21.2's HLS page (MIT).
 * Upstream copyright (c) 2019 aler9; see THIRD_PARTY_NOTICES.md.
 */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const DAY = 86400000;
  const RETRY_DELAYS = [1000, 2000, 4000, 8000, 10000];
  const state = {
    token: "", account: null, cameras: [], workerStatus: "ready", selected: null, media: null,
    view: "live", sessionEpoch: 0, selectionEpoch: 0, requests: new Set(),
    hls: null, nativeLive: false, hlsScript: null, retryTimer: null, retryCount: 0,
    pollTimer: null, expiryTimer: null, selectionController: null,
    spans: [], selectedSpan: null, historyEpoch: 0, historyRolling: true, signingOut: false,
    sampleEpoch: 0, sampleTimer: null, sampleExpiryTimer: null, sampleController: null, sampleBusy: false,
    sampleGeneration: null,
    dialogMode: "add", dialogEpoch: 0, dialogController: null,
  };

  class ApiError extends Error {
    constructor(status) { super("Request unavailable"); this.status = status; }
  }
  class SelectionError extends Error {}

  function message(id, value, error = false) {
    const element = $(id);
    element.textContent = value;
    element.classList.toggle("error", error);
  }

  function notice(value, error = false) {
    message("workspace-message", value, error);
    $("workspace-message").hidden = !value;
  }

  function apiMessage(error, fallback) {
    if (error.status === 401) return "Your session has ended. Sign in again.";
    if (error.status === 403) return "Your account does not have permission for this action.";
    if (error.status === 429) return "Too many requests. Wait a moment and try again.";
    if (error.status === 503) return "The camera service is unavailable. Try again shortly.";
    return fallback;
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    const signals = [controller.signal, AbortSignal.timeout(15000)];
    if (options.signal) signals.push(options.signal);
    const signal = AbortSignal.any(signals);
    const token = state.token;
    state.requests.add(controller);
    try {
      const headers = { Accept: options.sample ? "application/octet-stream, application/json" : "application/json" };
      if (token) headers.Authorization = `Bearer ${token}`;
      if (options.body !== undefined) headers["Content-Type"] = "application/json";
      const response = await fetch(path, {
        method: options.method || "GET", headers, signal,
        credentials: "same-origin", cache: "no-store",
        ...(options.body !== undefined ? { body: JSON.stringify(options.body) } : {}),
      });
      if (!response.ok) {
        if (response.status === 401 && token && path !== "/api/v1/auth/login" && !state.signingOut) {
          expireSession();
        }
        throw new ApiError(response.status);
      }
      if (response.status === 204) return null;
      if (options.sample && response.headers.get("content-type") === "application/octet-stream") {
        return await readSampleResponse(response, options.ticket);
      }
      return await response.json();
    } finally {
      state.requests.delete(controller);
    }
  }

  function clearSample(text = "Request one analyzed sample when available.") {
    state.sampleEpoch += 1;
    clearTimeout(state.sampleTimer); clearTimeout(state.sampleExpiryTimer);
    state.sampleTimer = state.sampleExpiryTimer = null;
    state.sampleController?.abort(); state.sampleController = null;
    state.sampleBusy = false; state.sampleGeneration = null;
    const canvas = $("sample-canvas");
    canvas.width = canvas.height = 0; canvas.hidden = true;
    message("sample-message", text);
    $("request-sample").disabled = !state.token || currentCamera()?.analytics_status !== "sample_available";
  }

  async function readSampleResponse(response, ticket) {
    function integer(name, min, max) {
      const text = response.headers.get(name);
      if (typeof text !== "string" || !/^[0-9]{1,8}$/.test(text)) throw new ApiError(503);
      const value = Number(text);
      if (!Number.isSafeInteger(value) || value < min || value > max) throw new ApiError(503);
      return value;
    }
    const width = integer("x-k5-sample-width", 1, 16384);
    const height = integer("x-k5-sample-height", 1, 16384);
    const stride = integer("x-k5-sample-stride", width * 4, 16 * 1024 * 1024);
    const length = integer("content-length", 1, 16 * 1024 * 1024);
    const expires = integer("x-k5-sample-expires", 0, 60);
    const boxes = integer("x-k5-sample-boxes", 0, 128);
    const received = response.headers.get("x-k5-sample-received");
    if (length !== height * stride || response.headers.get("x-k5-sample-ticket") !== ticket ||
        response.headers.get("x-k5-sample-format") !== "BGRx" || typeof received !== "string" ||
        received.length > 40 || !Number.isFinite(Date.parse(received))) throw new ApiError(503);
    const pixels = new Uint8Array(length);
    const reader = response.body.getReader();
    let offset = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (!(value instanceof Uint8Array) || value.length > length - offset) throw new ApiError(503);
        pixels.set(value, offset); offset += value.length;
      }
      if (offset !== length) throw new ApiError(503);
    } catch (error) {
      await reader.cancel().catch(() => null);
      throw error;
    } finally { reader.releaseLock(); }
    return { status: "ready", width, height, stride_bytes: stride, pixels,
      pixel_format: "BGRx", rendered_boxes: boxes, received_at: received, expires_in_seconds: expires };
  }

  function paintSample(data) {
    const { width, height, stride_bytes: stride, pixels: bytes } = data;
    const limit = 16 * 1024 * 1024;
    if (![width, height, stride].every(Number.isSafeInteger) || width < 1 || height < 1 ||
        width > 16384 || height > 16384 || stride < width * 4 || stride * height > limit ||
        data.pixel_format !== "BGRx" || !(bytes instanceof Uint8Array) ||
        bytes.length !== stride * height ||
        !Number.isSafeInteger(data.rendered_boxes) || data.rendered_boxes < 0 || data.rendered_boxes > 128 ||
        typeof data.received_at !== "string" || !Number.isFinite(Date.parse(data.received_at))) throw new ApiError(503);
    const canvas = $("sample-canvas");
    canvas.width = width; canvas.height = height;
    const context = canvas.getContext("2d");
    const image = context.createImageData(width, height);
    for (let y = 0; y < height; y += 1) {
      for (let x = 0; x < width; x += 1) {
        const input = y * stride + x * 4, output = (y * width + x) * 4;
        image.data[output] = bytes[input + 2];
        image.data[output + 1] = bytes[input + 1];
        image.data[output + 2] = bytes[input];
        image.data[output + 3] = 255;
      }
    }
    context.putImageData(image, 0, 0); canvas.hidden = false;
    message("sample-message", `Analyzed sample · ${data.rendered_boxes} boxes · Server received ${data.received_at}. Expires shortly.`);
  }

  async function requestSample() {
    const camera = currentCamera();
    if (state.sampleBusy || !state.token || state.signingOut || camera?.analytics_status !== "sample_available") return;
    clearSample("Analyzing one sample…");
    state.sampleBusy = true; $("request-sample").disabled = true;
    state.sampleGeneration = camera.analytics_generation;
    state.sampleController = new AbortController();
    const epoch = state.sampleEpoch, session = state.sessionEpoch, selection = state.selectionEpoch;
    const signal = state.sampleController.signal;
    const deadline = performance.now() + 60000;
    const current = () => epoch === state.sampleEpoch && session === state.sessionEpoch &&
      selection === state.selectionEpoch && state.token && !state.signingOut;
    const base = `/api/v1/operator/cameras/${encodeURIComponent(camera.device_id)}/analytics-samples`;
    function expiry(seconds) {
      if (!Number.isFinite(seconds) || seconds <= 0 || seconds > 60) throw new ApiError(410);
      const remaining = Math.min(seconds * 1000, deadline - performance.now());
      if (remaining <= 0) throw new ApiError(410);
      clearTimeout(state.sampleExpiryTimer);
      state.sampleExpiryTimer = setTimeout(() => {
        if (current()) clearSample("Analyzed sample expired. Request another sample when ready.");
      }, remaining);
    }
    function failed(error) {
      if (!current()) return;
      const value = error.status === 409 ? "Another analyzed sample is running. No request was queued."
        : error.status === 410 ? "Analyzed sample expired."
          : "Analyzed sample unavailable. Required local analytics and decoder prerequisites may be missing.";
      clearSample(value);
    }
    try {
      expiry(60);
      const admitted = await request(base, { method: "POST", signal });
      if (!current()) return;
      if (admitted.status !== "pending" || !/^[A-Za-z0-9_-]{32}$/.test(admitted.ticket)) throw new ApiError(503);
      expiry(admitted.expires_in_seconds);
      async function poll() {
        try {
          const data = await request(`${base}/${admitted.ticket}`, { signal, sample: true, ticket: admitted.ticket });
          if (!current()) return;
          expiry(data.expires_in_seconds);
          if (data.status === "pending") {
            state.sampleTimer = setTimeout(() => { if (current()) void poll(); }, 1000);
          } else if (data.status === "ready") {
            paintSample(data); state.sampleBusy = false;
            $("request-sample").disabled = currentCamera()?.analytics_status !== "sample_available";
          } else throw new ApiError(503);
        } catch (error) { failed(error); }
      }
      await poll();
    } catch (error) { failed(error); }
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

  function resetVideo(video) {
    video.pause();
    video.removeAttribute("src");
    video.load();
  }

  function stopLive(resetRetries = true) {
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
    resetVideo($("playback-video"));
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
    clearSample();
    state.sessionEpoch += 1;
    state.selectionEpoch += 1;
    state.historyEpoch += 1;
    clearTimeout(state.pollTimer);
    clearTimeout(state.expiryTimer);
    state.pollTimer = null;
    state.expiryTimer = null;
    state.selectionController?.abort();
    for (const controller of state.requests) controller.abort();
    state.requests.clear();
    stopLive();
    stopPlayback();
    state.token = "";
    state.account = null;
    state.cameras = [];
    state.workerStatus = "ready";
    state.selected = null;
    state.media = null;
    state.spans = [];
    state.selectedSpan = null;
    $("workspace").hidden = true;
    $("sign-in").hidden = false;
    $("password").value = "";
    closeAddCamera();
    $("camera-list").replaceChildren();
    $("history-list").replaceChildren();
    $("camera-search").value = "";
    $("sign-in-button").disabled = false;
    $("sign-in-button").textContent = "Sign in →";
    message("sign-in-message", reason, Boolean(reason));
    notice("");
    $("worker-message").hidden = true;
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
    clearSample();
    const token = state.token;
    state.sessionEpoch += 1;
    state.selectionEpoch += 1;
    state.historyEpoch += 1;
    clearTimeout(state.pollTimer);
    clearTimeout(state.expiryTimer);
    state.selectionController?.abort();
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
      details.textContent = `${status.text}${camera.recording ? " · Recording enabled" : ""}`;
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
  }

  function renderCameraHeading() {
    const camera = currentCamera();
    $("camera-title").textContent = camera?.name || "Select a camera";
    $("camera-subtitle").textContent = camera ? String(camera.host || "Enrolled camera") : "Live view and recording history in one place.";
    $("connection-badge").hidden = !camera;
    $("recording-badge").hidden = !camera?.recording;
    $("update-source-button").hidden = !camera || state.account?.role !== "administrator";
    $("recording-badge").textContent = state.workerStatus === "disk_full" ? "Recording paused" : "Recording";
    $("recording-badge").classList.toggle("paused", state.workerStatus === "disk_full");
    if (camera) {
      const status = statusDetails(camera);
      $("connection-badge").textContent = status.text;
      $("connection-badge").className = `status-badge ${status.style}`;
      $("analytics-state").textContent = camera.analytics_status === "sample_available"
        ? "Single analyzed samples available" : camera.analytics_status === "busy"
          ? "An analyzed sample is running" : "Analyzed samples unavailable";
      if (status.style === "offline" && state.view === "live" && state.media) {
        livePlaceholder("Camera disconnected", "Live video will return when the camera reconnects. Recording history is still available.", true);
        message("live-state", "Camera disconnected");
      }
    } else $("analytics-state").textContent = "";
    $("request-sample").disabled = state.sampleBusy || !camera || camera.analytics_status !== "sample_available";
  }

  function renderWorkerStatus() {
    const value = state.workerStatus === "disk_full"
      ? "Recording paused: low disk space or storage limit. Live view and recording history remain available."
      : state.workerStatus === "unavailable" ? "Media service is restarting. Recording history will return when the service is ready." : "";
    $("worker-message").textContent = value;
    $("worker-message").hidden = !value;
  }

  async function refreshCameras(selectFirst = false) {
    if (!state.token) return;
    const epoch = state.sessionEpoch;
    try {
      const data = await request("/api/v1/operator/cameras");
      if (epoch !== state.sessionEpoch) return;
      if (!Array.isArray(data.cameras)) throw new ApiError(503);
      state.workerStatus = ["ready", "unavailable", "disk_full"].includes(data.worker_status) ? data.worker_status : "ready";
      renderWorkerStatus();
      state.cameras = data.cameras.filter((camera) => camera && typeof camera.device_id === "string" && typeof camera.name === "string");
      if (state.sampleGeneration !== null && (currentCamera()?.analytics_generation !== state.sampleGeneration || state.workerStatus === "unavailable")) {
        clearSample("Camera source changed. Request a new sample.");
      }
      renderCameras();
      if (state.selected && !currentCamera()) {
        state.selectionEpoch += 1;
        state.selectionController?.abort();
        clearSample();
        stopLive(); stopPlayback();
        state.selected = null; state.media = null;
        resetHistory(); renderCameras();
        livePlaceholder("Camera unavailable", "Select another camera or refresh the camera list.");
      }
      if (selectFirst && !state.selected && state.cameras.length) await selectCamera(state.cameras[0].device_id);
    } catch (error) {
      if (error.name === "AbortError" || epoch !== state.sessionEpoch) return;
      clearSample("Camera state is unavailable. Request a new sample after it recovers.");
      $("camera-list-message").textContent = apiMessage(error, "Camera list unavailable. Select refresh to try again.");
    }
  }

  function scheduleCameraPoll() {
    clearTimeout(state.pollTimer);
    if (!state.token) return;
    state.pollTimer = setTimeout(async () => {
      await refreshCameras();
      scheduleCameraPoll();
    }, 10000);
  }

  function safeMediaUrl(value, expectedPrefix) {
    if (typeof value !== "string" || !value.startsWith("/")) throw new ApiError(503);
    const url = new URL(value, location.origin);
    if (url.origin !== location.origin || !url.pathname.startsWith(expectedPrefix) || url.username || url.password || url.hash) throw new ApiError(503);
    return url.pathname + url.search;
  }

  async function selectCamera(deviceId) {
    if (!state.token || state.signingOut) return;
    clearSample();
    state.selectionController?.abort();
    state.selectionController = new AbortController();
    const epoch = ++state.selectionEpoch;
    state.historyEpoch += 1;
    state.selected = deviceId;
    state.media = null;
    stopLive(); stopPlayback(); resetHistory();
    renderCameras(); notice("");
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
      else await loadHistory();
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
          if (data.response?.code === 401 || data.response?.code === 403) { expireSession(); return; }
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

  function setView(view) {
    if (state.view === view) return;
    clearSample();
    state.view = view;
    $("live-panel").hidden = view !== "live";
    $("history-panel").hidden = view !== "history";
    for (const value of ["live", "history"]) {
      $(`${value}-tab`).classList.toggle("active", view === value);
      if (view === value) $(`${value}-tab`).setAttribute("aria-current", "page");
      else $(`${value}-tab`).removeAttribute("aria-current");
    }
    if (view === "history") { stopLive(); if (state.selected) void loadHistory(); }
    else { stopPlayback(); if (state.media) void startLive(); }
  }

  function utcInput(date, seconds = false) { return date.toISOString().slice(0, seconds ? 23 : 16); }
  function parseUtc(value) { return new Date(`${value.length === 16 ? value + ":00" : value}Z`); }
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
    const end = new Date(); end.setUTCMilliseconds(0);
    $("history-start").value = utcInput(new Date(end.getTime() - DAY), true);
    $("history-end").value = utcInput(end, true);
    $("history-day").value = "";
    $("history-day").max = new Date().toISOString().slice(0, 10);
    state.historyRolling = true;
  }

  function resetHistory() {
    state.spans = []; state.selectedSpan = null;
    $("history-list").replaceChildren();
    $("history-count").textContent = "0";
    $("load-history").disabled = false;
    $("selected-recording").textContent = "No interval selected";
    $("playback-start").value = "";
    $("playback-duration").value = "60";
    for (const id of ["playback-start", "playback-duration", "play-recording"]) $(id).disabled = true;
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
    if (state.historyRolling) setLastDay();
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
      message("history-message", state.spans.length
        ? `${state.spans.length} available ${state.spans.length === 1 ? "interval" : "intervals"}. Gaps contain no recorded video.`
        : "No recorded video is available in this interval.");
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
    for (const span of state.spans) {
      addGap(cursor, span.start);
      const button = document.createElement("button"); button.type = "button"; button.className = "history-item";
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

  function selectSpan(span, button) {
    stopPlayback();
    state.selectedSpan = span;
    for (const item of $("history-list").querySelectorAll("button")) {
      item.classList.toggle("selected", item === button);
      item.setAttribute("aria-pressed", String(item === button));
    }
    const start = span.start;
    $("playback-start").value = utcInput(new Date(start), true);
    $("playback-start").min = utcInput(new Date(start), true);
    $("playback-start").max = utcInput(new Date(span.end), true);
    $("playback-duration").value = String(Math.max(1, Math.min(60, Math.floor((span.end - start) / 1000))));
    for (const id of ["playback-start", "playback-duration", "play-recording"]) $(id).disabled = false;
    $("selected-recording").textContent = `${utcLabel(span.start)} — ${utcLabel(span.end)} UTC`;
    message("playback-message", "Preview up to 5 minutes. Select a new start time to seek.");
    updateDownload();
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
    } catch (_) { disableDownload(); }
  }

  async function playRecording(event) {
    event.preventDefault();
    try {
      const selection = playbackSelection();
      const video = $("playback-video");
      video.src = selection.url;
      video.load();
      $("playback-placeholder").hidden = true;
      message("playback-message", `Loading ${durationLabel(selection.duration * 1000)} of recorded video…`);
      updateDownload();
      await video.play();
    } catch (error) {
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
      const expires = Date.parse(result.expires_at);
      if (Number.isFinite(expires)) state.expiryTimer = setTimeout(expireSession, Math.max(0, Math.min(expires - Date.now(), DAY)));
      setLastDay(); resetHistory();
      await refreshCameras(true);
      scheduleCameraPoll();
    } catch (error) {
      if (error.name === "AbortError" || epoch !== state.sessionEpoch) return;
      state.token = "";
      message("sign-in-message", error.status === 401
        ? "Username or password is incorrect."
        : apiMessage(error, "Sign-in is unavailable. Try again shortly."), true);
    } finally {
      credentials.password = "";
      if (epoch === state.sessionEpoch) {
        $("sign-in-button").disabled = false;
        $("sign-in-button").textContent = "Sign in →";
      }
    }
  });

  $("sign-out-button").addEventListener("click", () => void signOut());
  $("refresh-cameras").addEventListener("click", () => void refreshCameras(true));
  $("camera-search").addEventListener("input", renderCameras);
  window.addEventListener("pagehide", () => clearSample());
  $("request-sample").addEventListener("click", () => void requestSample());
  $("clear-sample").addEventListener("click", () => clearSample("Sample cleared. Native work may still be finishing."));
  $("live-tab").addEventListener("click", () => setView("live"));
  $("history-tab").addEventListener("click", () => setView("history"));
  $("retry-live").addEventListener("click", () => { if (state.selected) void selectCamera(state.selected); });
  $("play-live").addEventListener("click", () => void $("live-video").play().catch(() => message("live-state", "Unable to play live video. Try reconnecting.")));
  $("live-video").addEventListener("playing", () => {
    if (!state.token || state.view !== "live") return;
    $("live-placeholder").hidden = true; $("live-video-label").hidden = false; $("play-live").hidden = true;
    message("live-state", "Live video connected");
  });
  $("live-video").addEventListener("waiting", () => {
    if (state.token && state.view === "live") {
      $("live-video-label").hidden = true;
      message("live-state", "Buffering live video…");
    }
  });
  $("live-video").addEventListener("error", () => {
    if (state.token && state.media && state.view === "live" && state.nativeLive) scheduleLiveRetry(state.selectionEpoch, "The live stream is unavailable. Waiting for the camera…");
  });
  $("history-form").addEventListener("submit", (event) => { event.preventDefault(); void loadHistory(); });
  $("history-start").addEventListener("input", () => { state.historyRolling = false; });
  $("history-end").addEventListener("input", () => { state.historyRolling = false; });
  $("last-24-hours").addEventListener("click", () => { setLastDay(); void loadHistory(); });
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
  $("playback-video").addEventListener("playing", () => message("playback-message", "Recorded video · Select a new start time to seek to another point."));
  $("playback-video").addEventListener("error", () => { if (state.token && state.selectedSpan) message("playback-message", "Playback is unavailable. Refresh history or select another interval.", true); });

  function resetCameraSetup() {
    state.dialogController?.abort();
    state.dialogController = null;
    $("add-camera-form").reset();
    $("new-camera-name").readOnly = false;
    $("new-camera-host").readOnly = false;
    $("save-camera").disabled = false;
    state.dialogMode = "add";
  }
  function closeAddCamera() { resetCameraSetup(); $("add-camera-dialog").close(); }
  function openCameraSetup(mode) {
    if (state.account?.role !== "administrator") return;
    const camera = mode === "update" ? currentCamera() : null;
    if (mode === "update" && !camera) return;
    resetCameraSetup();
    state.dialogEpoch += 1;
    state.dialogMode = mode;
    message("add-camera-message", "");
    $("new-camera-name").readOnly = mode === "update";
    $("new-camera-host").readOnly = mode === "update";
    $("add-camera-title").textContent = mode === "update" ? "Update camera source" : "Add a camera";
    $("camera-setup-description").textContent = mode === "update"
      ? "Changing the stream address reconnects live video and recording. The current address is kept private."
      : "Connect an RTSP camera to live view and continuous recording.";
    $("save-camera").textContent = mode === "update" ? "Update source" : "Connect camera";
    if (camera) {
      $("new-camera-name").value = camera.name;
      $("new-camera-host").value = camera.host;
    }
    $("add-camera-dialog").showModal();
    $(mode === "update" ? "new-camera-source" : "new-camera-name").focus();
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
    // Native close events are queued and may arrive after a new dialog opens.
    if (!$("add-camera-dialog").open) resetCameraSetup();
  });
  $("add-camera-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    clearSample("Camera configuration is changing. Request a new sample afterward.");
    if (state.account?.role !== "administrator" || !$("add-camera-dialog").open || state.dialogController) return;
    const epoch = state.sessionEpoch;
    const dialogEpoch = state.dialogEpoch;
    const controller = new AbortController();
    state.dialogController = controller;
    const isCurrent = () => epoch === state.sessionEpoch && dialogEpoch === state.dialogEpoch
      && state.dialogController === controller && $("add-camera-dialog").open;
    const updating = state.dialogMode === "update";
    const camera = { name: $("new-camera-name").value.trim(), host: $("new-camera-host").value.trim(), source_uri: $("new-camera-source").value.trim() };
    $("new-camera-source").value = "";
    $("save-camera").disabled = true;
    message("add-camera-message", updating ? "Updating source and reconnecting…" : "Saving camera and connecting…");
    try {
      const created = await request("/api/v1/operator/cameras", { method: "POST", body: camera, signal: controller.signal });
      if (!isCurrent()) return;
      closeAddCamera();
      notice(updating
        ? "Camera source updated. Live video and recording will reconnect."
        : "Camera saved. The live connection and recording status will update as it connects.");
      await refreshCameras();
      if (epoch !== state.sessionEpoch || dialogEpoch !== state.dialogEpoch) return;
      const deviceId = created.device_id || created.camera?.device_id;
      if (deviceId) await selectCamera(deviceId);
    } catch (error) {
      if (error.name === "AbortError" || !isCurrent()) return;
      message("add-camera-message", error.status === 422
        ? "Check the camera IP address and RTSP stream. Enter the stream address again to retry."
        : error.status === 409 ? "This camera is already configured. Refresh the camera list."
          : apiMessage(error, "Camera setup failed. Enter the stream address again and retry."), true);
    } finally {
      camera.source_uri = "";
      if (isCurrent()) {
        state.dialogController = null;
        $("new-camera-source").value = "";
        $("save-camera").disabled = false;
      }
    }
  });

  window.addEventListener("pagehide", () => { stopLive(); stopPlayback(); });
  setLastDay();
  resetHistory();
})();
