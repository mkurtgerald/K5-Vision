/* Local person-detection controls and recorded events. Session authority stays in the app. */
"use strict";

(() => {
  const PREFIX = "/api/v1/operator/cameras";
  const PAGE_SIZE = 50;
  const MAX_EVENTS = 250;
  const POLL_SECONDS = 5;
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const STATUSES = new Set(["disabled", "starting", "active", "waiting", "unavailable"]);

  function element(tag, className = "", text = "") {
    const value = document.createElement(tag);
    if (className) value.className = className;
    if (text) value.textContent = text;
    return value;
  }

  function timestamp(value) {
    return typeof value === "string" && value.length <= 40
      && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));
  }

  function utcLabel(value) {
    return new Date(value).toISOString().replace("T", " ").replace(".000Z", " UTC").replace("Z", " UTC");
  }

  function validRange(value) {
    if (!value) return null;
    const start = typeof value.start === "number" ? value.start : timestamp(value.start) ? Date.parse(value.start) : NaN;
    const end = typeof value.end === "number" ? value.end : timestamp(value.end) ? Date.parse(value.end) : NaN;
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start || end - start > 86400000) return null;
    try { return { start: new Date(start).toISOString(), end: new Date(end).toISOString() }; }
    catch (_) { return null; }
  }

  function validSettings(value) {
    return value && typeof value.enabled === "boolean" && STATUSES.has(value.status)
      && Number.isFinite(value.confidence_threshold) && value.confidence_threshold >= 0.5 && value.confidence_threshold <= 0.99
      && (value.last_sample_at === null || timestamp(value.last_sample_at))
      && (value.last_event_at === null || timestamp(value.last_event_at))
      && Number.isSafeInteger(value.processed_frames) && value.processed_frames >= 0
      && Number.isSafeInteger(value.event_count) && value.event_count >= 0
      && Number.isFinite(value.sample_interval_seconds) && value.sample_interval_seconds > 0
      && Number.isSafeInteger(value.max_parallel_cameras) && value.max_parallel_cameras > 0;
  }

  function validEvent(value, cameraId) {
    return value && Number.isSafeInteger(value.id) && value.id > 0 && value.camera_id === cameraId
      && timestamp(value.occurred_at) && timestamp(value.playback_at)
      && Number.isFinite(value.confidence) && value.confidence >= 0.5 && value.confidence <= 1
      && Number.isSafeInteger(value.person_count) && value.person_count > 0 && value.person_count <= 32;
  }

  function errorText(error, action) {
    if (error?.status === 401) return "Your session has ended. Sign in again.";
    if (error?.status === 403) return "Administrator permission is required to change detection settings.";
    if (error?.status === 404) return "This camera is no longer available to your account.";
    if (error?.status === 429) return "Too many requests. Wait a moment, then refresh.";
    if (error?.status === 422) return "Choose a valid time range or confidence between 50% and 99%.";
    return action === "save" ? "Detection settings could not be saved. Try again."
      : action === "events" ? "Person events could not be loaded. Select Refresh to try again."
        : "Detection status is unavailable. Recording continues independently.";
  }

  function create({ container, request, getSession, onSeek }) {
    if (!container || typeof request !== "function" || typeof getSession !== "function" || typeof onSeek !== "function") throw new Error("Person events could not be initialized");
    let camera = null, range = null, settings = null, events = [], before = null;
    let visible = false, destroyed = false, epoch = 0, controller = null, timer = null;
    let loading = false, saving = false, dirty = false, loadedOlder = false;
    let requestNumber = 0, eventRequestNumber = 0;

    container.classList.add("analytics-panel");
    container.setAttribute("aria-labelledby", "analytics-title");
    container.hidden = true;
    const heading = element("div", "analytics-heading");
    const titleGroup = element("div");
    const title = element("h3", "", "Person detection"); title.id = "analytics-title";
    const cameraName = element("p", "field-help"); cameraName.id = "analytics-camera-name";
    titleGroup.append(title, cameraName);
    const refreshButton = element("button", "button secondary", "Refresh"); refreshButton.type = "button"; refreshButton.id = "analytics-refresh";
    heading.append(titleGroup, refreshButton);

    const health = element("div", "analytics-health");
    const status = element("span", "analytics-status", "Select a camera"); status.id = "analytics-status";
    const lastSample = element("span", "muted"); lastSample.id = "analytics-last-sample";
    health.append(status, lastSample);
    const statusMessage = element("p", "form-message"); statusMessage.id = "analytics-message"; statusMessage.setAttribute("role", "status"); statusMessage.setAttribute("aria-live", "polite");

    const form = element("form", "analytics-settings"); form.id = "analytics-settings-form"; form.hidden = true;
    const enableLabel = element("label", "analytics-checkbox");
    const enabled = element("input"); enabled.type = "checkbox"; enabled.id = "analytics-enabled";
    enableLabel.append(enabled, element("span", "", "Detect people on this camera"));
    const confidenceLabel = element("label", "analytics-confidence"); confidenceLabel.htmlFor = "analytics-confidence";
    confidenceLabel.append(element("span", "", "Minimum confidence"));
    const confidenceGroup = element("span", "analytics-confidence-input");
    const confidence = element("input"); confidence.type = "number"; confidence.min = "50"; confidence.max = "99"; confidence.step = "1"; confidence.value = "60"; confidence.required = true; confidence.id = "analytics-confidence";
    confidenceGroup.append(confidence, element("span", "", "%")); confidenceLabel.append(confidenceGroup);
    const saveButton = element("button", "button primary", "Save detection settings"); saveButton.type = "submit"; saveButton.id = "analytics-save";
    const help = element("p", "field-help", "Person detection analyzes recorded camera frames in the background. Keep recording enabled. Higher confidence reduces uncertain detections.");
    help.id = "analytics-settings-help"; enabled.setAttribute("aria-describedby", help.id); confidence.setAttribute("aria-describedby", help.id);
    form.append(enableLabel, confidenceLabel, saveButton, help);
    const limits = element("p", "field-help"); limits.id = "analytics-limits";

    const eventHeading = element("div", "analytics-events-heading");
    eventHeading.append(element("h4", "", "Recorded person events"));
    const eventCount = element("span", "count", "0"); eventCount.id = "analytics-event-count"; eventHeading.append(eventCount);
    const eventRange = element("p", "field-help"); eventRange.id = "analytics-event-range";
    const eventMessage = element("p", "form-message"); eventMessage.id = "analytics-events-message"; eventMessage.setAttribute("role", "status"); eventMessage.setAttribute("aria-live", "polite");
    const eventList = element("ol", "analytics-events"); eventList.id = "analytics-events"; eventList.setAttribute("aria-label", "Recorded person events");
    const olderButton = element("button", "button secondary", "Load older events"); olderButton.type = "button"; olderButton.id = "analytics-older"; olderButton.hidden = true;
    container.replaceChildren(heading, health, statusMessage, form, limits, eventHeading, eventRange, eventMessage, eventList, olderButton);

    function message(node, text = "", error = false) {
      node.textContent = text;
      node.classList.toggle("error", error);
    }

    function session() {
      const value = getSession();
      return value?.token && ["administrator", "operator", "viewer"].includes(value.account?.role)
        ? { token: value.token, epoch: value.epoch, account: { role: value.account.role } } : null;
    }

    function current(start, version, cameraId) {
      const value = session();
      return !destroyed && visible && version === epoch && camera?.device_id === cameraId
        && value && value.token === start.token && value.epoch === start.epoch && value.account.role === start.account.role;
    }

    function stopRequests() {
      epoch++; controller?.abort(); controller = null;
      if (timer !== null) clearTimeout(timer);
      timer = null; loading = false; saving = false;
    }

    function controls() {
      const active = !destroyed && visible && Boolean(camera) && Boolean(session());
      const administrator = session()?.account.role === "administrator";
      form.hidden = !administrator;
      form.setAttribute("aria-busy", String(saving));
      enabled.disabled = !active || !settings || saving;
      confidence.disabled = !active || !settings || saving;
      saveButton.disabled = !active || !settings || saving || !dirty;
      refreshButton.disabled = !active || loading || saving;
      olderButton.disabled = !active || loading || saving || !before;
      saveButton.textContent = saving ? "Saving…" : "Save detection settings";
    }

    function renderSettings() {
      if (!settings) {
        status.textContent = camera ? "Checking detection…" : "Select a camera";
        status.dataset.state = "unavailable"; lastSample.textContent = ""; limits.textContent = "";
      } else {
        const labels = { disabled: "Detection off", starting: "Starting detection", active: "Detecting people", waiting: "Waiting for recorded video", unavailable: "Detector unavailable" };
        status.textContent = labels[settings.status]; status.dataset.state = settings.status;
        lastSample.textContent = settings.last_sample_at ? `Last recorded frame: ${utcLabel(settings.last_sample_at)}` : "No frames processed yet";
        limits.textContent = `Processes up to ${settings.max_parallel_cameras} ${settings.max_parallel_cameras === 1 ? "camera" : "cameras"} at a time · Background check target: ${settings.sample_interval_seconds} seconds. Per-camera frequency depends on enabled cameras. ${settings.processed_frames.toLocaleString()} frames processed.`;
        if (!dirty && !saving) { enabled.checked = settings.enabled; confidence.value = String(Math.round(settings.confidence_threshold * 100)); }
      }
      controls();
    }

    function renderEvents() {
      const fragment = document.createDocumentFragment();
      for (const event of events) {
        const row = element("li", "analytics-event"); row.dataset.eventId = String(event.id);
        const button = element("button", "analytics-event-button"); button.type = "button"; button.dataset.eventId = String(event.id);
        const time = element("time", "analytics-event-time", utcLabel(event.occurred_at)); time.dateTime = event.occurred_at;
        const details = element("span", "analytics-event-details");
        details.append(element("strong", "", event.person_count === 1 ? "Person detected" : `${event.person_count} people detected`), element("span", "muted", `${Math.round(event.confidence * 100)}% confidence`));
        const action = element("span", "analytics-event-action", "Play recording");
        button.append(time, details, action);
        button.addEventListener("click", () => {
          const start = session(), version = epoch;
          if (!visible || !start || !camera || event.camera_id !== camera.device_id) return;
          Promise.resolve().then(() => {
            if (current(start, version, event.camera_id)) return onSeek({ cameraId: event.camera_id, timestamp: event.playback_at });
          }).catch(() => {
            if (current(start, version, event.camera_id)) message(eventMessage, "This recording could not be opened. Refresh the recording history and try again.", true);
          });
        });
        row.append(button); fragment.append(row);
      }
      eventList.replaceChildren(fragment); eventCount.textContent = String(events.length);
      olderButton.hidden = !before || events.length >= MAX_EVENTS;
      controls();
    }

    async function loadSettings(start, version, cameraId, number) {
      try {
        const value = await request(`${PREFIX}/${cameraId}/analytics`, { signal: controller.signal });
        if (!current(start, version, cameraId) || number !== requestNumber) return;
        if (!validSettings(value)) throw new Error("Invalid detection response");
        settings = value; renderSettings();
        message(statusMessage, value.status === "unavailable" ? "Person detection is unavailable. Recording continues independently."
          : value.status === "waiting" ? "Detection will resume when recorded camera video is available."
            : "", value.status === "unavailable");
      } catch (error) {
        if (error.name !== "AbortError" && current(start, version, cameraId) && number === requestNumber) {
          settings = null; status.textContent = "Status unavailable"; status.dataset.state = "unavailable";
          lastSample.textContent = ""; limits.textContent = ""; controls(); message(statusMessage, errorText(error, "status"), true);
        }
      }
    }

    async function loadEvents(start, version, cameraId, append = false) {
      if (!range) { events = []; before = null; renderEvents(); message(eventMessage, "Choose a recording time range to see person events."); return; }
      const number = ++eventRequestNumber;
      const query = new URLSearchParams({ start: range.start, end: range.end, limit: String(PAGE_SIZE) });
      if (append && before) query.set("before", String(before));
      message(eventMessage, append ? "Loading older events…" : "Loading person events…");
      try {
        const value = await request(`${PREFIX}/${cameraId}/events?${query}`, { signal: controller.signal });
        if (!current(start, version, cameraId) || number !== eventRequestNumber) return;
        if (!Array.isArray(value.events) || value.events.length > PAGE_SIZE || !value.events.every((item) => validEvent(item, cameraId))
          || typeof value.has_more !== "boolean" || !(value.next_before === null || Number.isSafeInteger(value.next_before) && value.next_before > 0)
          || value.has_more && value.next_before === null) throw new Error("Invalid event response");
        const min = Date.parse(range.start), max = Date.parse(range.end);
        if (value.events.some((item) => Date.parse(item.occurred_at) < min || Date.parse(item.occurred_at) > max)) throw new Error("Invalid event range");
        const merged = append ? [...events, ...value.events] : value.events;
        events = [...new Map(merged.map((item) => [item.id, item])).values()].slice(0, MAX_EVENTS);
        before = value.has_more ? value.next_before : null; loadedOlder = append || loadedOlder;
        renderEvents();
        message(eventMessage, !events.length ? "No person events in this time range."
          : events.length >= MAX_EVENTS && value.has_more ? "Showing 250 events. Narrow the recording time range to see more."
            : loadedOlder ? "Select an event to play its recording. Refresh to load the latest events." : "Select an event to play its recording.");
      } catch (error) {
        if (error.name !== "AbortError" && current(start, version, cameraId) && number === eventRequestNumber) {
          if (!append) { events = []; before = null; renderEvents(); }
          message(eventMessage, errorText(error, "events"), true);
        }
      }
    }

    function schedule() {
      if (timer !== null) clearTimeout(timer);
      timer = null;
      if (!destroyed && visible && camera && session()) timer = setTimeout(() => { timer = null; void refresh(false); }, POLL_SECONDS * 1000);
    }

    async function refresh(manual = true) {
      const start = session(), cameraId = camera?.device_id;
      if (destroyed || !visible || !start || !cameraId || saving || loading) return;
      const version = epoch, number = ++requestNumber;
      controller ??= new AbortController(); loading = true;
      if (manual) { loadedOlder = false; before = null; }
      controls();
      try {
        await Promise.allSettled([loadSettings(start, version, cameraId, number), ...(!loadedOlder || manual ? [loadEvents(start, version, cameraId)] : [])]);
      } finally {
        if (current(start, version, cameraId) && number === requestNumber) { loading = false; controls(); schedule(); }
      }
    }

    async function save(event) {
      event.preventDefault();
      const start = session(), cameraId = camera?.device_id;
      if (destroyed || !visible || !cameraId || !start || start.account.role !== "administrator" || saving || !settings) return;
      const percent = Number(confidence.value);
      if (!Number.isInteger(percent) || percent < 50 || percent > 99) { message(statusMessage, "Choose a confidence between 50% and 99%.", true); return; }
      stopRequests(); controller = new AbortController(); const version = epoch;
      saving = true; controls(); message(statusMessage, "Saving detection settings…");
      try {
        const value = await request(`${PREFIX}/${cameraId}/analytics`, { method: "PATCH", body: { enabled: enabled.checked, confidence_threshold: percent / 100 }, signal: controller.signal });
        if (!current(start, version, cameraId)) return;
        if (!validSettings(value)) throw new Error("Invalid detection response");
        settings = value; dirty = false; saving = false; renderSettings();
        message(statusMessage, value.status === "unavailable" ? "Settings saved. The detector is unavailable; recording continues independently."
          : value.enabled ? "Detection settings saved. Background person detection is enabled." : "Detection settings saved. Person detection is off.", value.status === "unavailable");
      } catch (error) {
        if (error.name !== "AbortError" && current(start, version, cameraId)) message(statusMessage, errorText(error, "save"), true);
      } finally {
        if (current(start, version, cameraId)) { saving = false; controls(); schedule(); }
      }
    }

    function resetDisplay() {
      settings = null; events = []; before = null; dirty = false; loadedOlder = false;
      enabled.checked = false; confidence.value = "60"; renderSettings(); renderEvents();
      message(statusMessage); message(eventMessage);
    }

    function selectCamera(value) {
      if (destroyed) return;
      const next = value && UUID.test(value.device_id) ? { device_id: value.device_id, name: typeof value.name === "string" ? value.name.slice(0, 128) : "Selected camera" } : null;
      if (next?.device_id === camera?.device_id) { camera = next; cameraName.textContent = next?.name || ""; controls(); return; }
      stopRequests(); camera = next; resetDisplay();
      cameraName.textContent = camera?.name || "Select a camera to view detection settings and events.";
      container.hidden = !visible || !camera;
      if (camera && visible) void refresh();
    }

    function setRange(value) {
      if (destroyed) return;
      const next = validRange(value);
      if (next?.start === range?.start && next?.end === range?.end) return;
      stopRequests(); range = next; events = []; before = null; loadedOlder = false;
      eventRange.textContent = range ? `${utcLabel(range.start)} → ${utcLabel(range.end)}` : "Events follow the recording history time range. All times UTC.";
      renderEvents(); message(eventMessage, "Choose a recording time range to see person events.");
      if (visible && camera) void refresh();
    }

    function setVisible(value) {
      if (destroyed || visible === Boolean(value)) return;
      stopRequests(); visible = Boolean(value); container.hidden = !visible || !camera;
      controls();
      if (visible && camera) void refresh();
    }

    function clear() {
      if (destroyed) return;
      stopRequests(); camera = null; range = null; visible = false; container.hidden = true;
      cameraName.textContent = ""; eventRange.textContent = ""; resetDisplay();
    }

    function destroy() {
      if (destroyed) return;
      clear(); destroyed = true; container.replaceChildren();
      container.classList.remove("analytics-panel"); container.removeAttribute("aria-labelledby");
    }

    refreshButton.addEventListener("click", () => void refresh());
    olderButton.addEventListener("click", async () => {
      const start = session(), cameraId = camera?.device_id;
      if (!start || !cameraId || !visible || !before || loading || saving) return;
      const version = epoch; loading = true; controls(); controller ??= new AbortController();
      try { await loadEvents(start, version, cameraId, true); }
      finally { if (current(start, version, cameraId)) { loading = false; controls(); schedule(); } }
    });
    for (const input of [enabled, confidence]) input.addEventListener("input", () => { dirty = true; controls(); });
    form.addEventListener("submit", save);
    renderSettings(); renderEvents();
    return { selectCamera, setRange, setVisible, refresh, clear, destroy };
  }

  window.K5Analytics = Object.freeze({ create });
})();
