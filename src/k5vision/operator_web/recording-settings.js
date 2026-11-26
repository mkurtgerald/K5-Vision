/* Recorder-owned motion policy. Preview pixels are authorized server captures, never trigger evidence. */
"use strict";

(() => {
  const PREFIX = "/api/v1/operator/cameras/";
  const NS = "http://www.w3.org/2000/svg";
  const REVISION = /^[0-9a-f]{64}$/;
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  const OPAQUE = /^[A-Za-z0-9_-]{16,128}$/;
  const STATES = {
    continuous: ["Continuous recording", "Recording follows the camera's master Pause / Resume control."],
    armed: ["Armed · Waiting for motion", "The motion detector is running. New footage starts when enabled regions move."],
    recording: ["Motion recording", "Movement is keeping this camera's recording gate open."],
    post_capture: ["Finishing motion recording", "Recording continues for the saved hold time after movement stops."],
    paused: ["Recording paused", "Resume recording to enable this camera's saved recording mode."],
    storage_blocked: ["Storage blocked", "Recording is blocked until storage recovers. Motion settings remain saved."],
    unavailable: ["Motion unavailable", "Motion recording is not confirmed. Check camera and recorder health."],
    zones_need_review: ["Zones need review", "The camera view changed. Refresh the frame and save reviewed zones before motion recording can resume."],
  };
  const ERRORS = {
    motion_invalid_request: "Check the recording values and finish or cancel the zone being drawn.",
    motion_policy_conflict: "These settings changed elsewhere. Refresh before saving again.",
    motion_source_changed: "The camera connection or view changed. Refresh settings and review the zones.",
    motion_preview_expired: "The frozen frame expired. Capture a new frame before saving zone changes.",
    motion_preview_unavailable: "A current camera frame is unavailable. Check live video, then capture again.",
    motion_zones_empty: "The zones leave no area for motion detection. Remove or reduce an exclusion.",
    motion_policy_unavailable: "Recording settings are unavailable. Check recorder health and refresh.",
  };
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const finite = (value, low, high) => typeof value === "number" && Number.isFinite(value) && value >= low && value <= high;
  const emptyZones = () => ({ reference: null, include: [], exclude: [] });
  const text = (tag, value = "", className = "") => {
    const node = document.createElement(tag);
    node.textContent = value;
    if (className) node.className = className;
    return node;
  };
  function button(value, id, className = "button secondary") {
    const node = text("button", value, className); node.type = "button"; node.id = id; return node;
  }
  function polygonValid(points) {
    if (!Array.isArray(points) || points.length < 3 || points.length > 16
      || !points.every((point) => Array.isArray(point) && point.length === 2 && point.every((n) => finite(n, 0, 1)))
      || new Set(points.map((point) => point.join(","))).size !== points.length) return false;
    let area = 0;
    const cross = (a, b, c) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
    const between = (a, b, p) => Math.abs(cross(a, b, p)) < 1e-10
      && p[0] >= Math.min(a[0], b[0]) - 1e-10 && p[0] <= Math.max(a[0], b[0]) + 1e-10
      && p[1] >= Math.min(a[1], b[1]) - 1e-10 && p[1] <= Math.max(a[1], b[1]) + 1e-10;
    for (let i = 0; i < points.length; i++) {
      const a = points[i], b = points[(i + 1) % points.length];
      area += a[0] * b[1] - b[0] * a[1];
      for (let j = i + 1; j < points.length; j++) {
        if (j === i + 1 || i === 0 && j === points.length - 1) continue;
        const c = points[j], d = points[(j + 1) % points.length];
        if (cross(a, b, c) * cross(a, b, d) < 0 && cross(c, d, a) * cross(c, d, b) < 0
          || between(a, b, c) || between(a, b, d) || between(c, d, a) || between(c, d, b)) return false;
      }
    }
    return Math.abs(area) / 2 > 1e-6;
  }
  function zonesValid(value) {
    if (!value || !Array.isArray(value.include) || !Array.isArray(value.exclude) || value.include.length + value.exclude.length > 8) return false;
    const all = [...value.include, ...value.exclude];
    if (!all.every((zone) => zone && UUID.test(zone.id) && typeof zone.name === "string" && zone.name.trim().length > 0
      && zone.name.length <= 64 && !/[\x00-\x1f\x7f]/.test(zone.name) && polygonValid(zone.points))
      || new Set(all.map((zone) => zone.id)).size !== all.length) return false;
    const r = value.reference;
    return r === null ? all.length === 0 : r && Number.isInteger(r.width) && Number.isInteger(r.height)
      && r.width > 0 && r.height > 0 && r.width <= 8192 && r.height <= 8192 && r.width * r.height <= 33554432
      && r.rotation === 0 && REVISION.test(r.source_revision);
  }
  function policyValid(value, id) {
    return value && value.camera_id === id && Number.isSafeInteger(value.revision) && value.revision >= 0
      && REVISION.test(value.source_revision) && REVISION.test(value.policy_source_revision)
      && ["continuous", "motion"].includes(value.mode) && Number.isInteger(value.post_capture_seconds) && finite(value.post_capture_seconds, 1, 120)
      && Number.isInteger(value.sensitivity) && finite(value.sensitivity, 1, 100)
      && finite(value.minimum_area_percent, 0.1, 100) && value.pre_capture_seconds === 0
      && Object.hasOwn(STATES, value.status) && zonesValid(value.zones);
  }

  function create({ dialog, request, getSession, onChanged = () => {} }) {
    if (!dialog || typeof request !== "function" || typeof getSession !== "function") throw new Error("Recording settings could not be initialized");
    let camera = null, policy = null, zones = emptyZones(), preview = null, objectUrl = null, controller = null;
    let epoch = 0, busy = false, draft = null, cursor = [0.5, 0.5], previewDeadline = 0, previewTimer = null;
    let savedSignature = "", invalidated = false, owner = null, previewRequest = false;
    const heading = text("div", "", "dialog-heading"), titles = text("div");
    titles.append(text("p", "CAMERA SETTINGS", "eyebrow"));
    const title = text("h2", "Recording and motion zones"); title.id = "recording-settings-title";
    titles.append(title);
    const closeButton = button("×", "recording-settings-close", "icon-button"); closeButton.setAttribute("aria-label", "Close recording settings");
    heading.append(titles, closeButton);
    const target = text("p", "", "muted"); target.id = "recording-settings-target";
    const status = text("p", "", "motion-policy-status"); status.id = "recording-settings-status";
    const help = text("p", "", "field-help"); help.id = "recording-settings-state-help";
    const message = text("p", "", "form-message"); message.id = "recording-settings-message"; message.setAttribute("role", "status"); message.setAttribute("aria-live", "polite");
    const form = text("form"); form.id = "recording-settings-form"; form.autocomplete = "off";
    const fields = text("fieldset", "", "recording-policy-fields");
    fields.append(text("legend", "Recording behavior"));
    const grid = text("div", "", "recording-policy-grid");
    function field(label, input) { const group = text("div"), node = text("label", label); node.htmlFor = input.id; group.append(node, input); grid.append(group); }
    const mode = document.createElement("select"); mode.id = "recording-mode";
    for (const [value, label] of [["continuous", "Continuous"], ["motion", "Motion-triggered"]]) { const option = text("option", label); option.value = value; mode.append(option); }
    field("Recording mode", mode);
    function number(label, id, min, max, step) { const input = document.createElement("input"); input.type = "number"; input.id = id; input.min = String(min); input.max = String(max); input.step = String(step); input.required = true; field(label, input); return input; }
    const sensitivity = number("Motion sensitivity", "motion-sensitivity", 1, 100, 1);
    const area = number("Minimum changed area (%)", "motion-minimum-area", 0.1, 100, 0.1);
    const hold = number("Record after movement stops (seconds)", "motion-post-capture", 1, 120, 1);
    fields.append(grid,
      text("p", "Motion-triggered mode detects general scene movement. Higher sensitivity detects smaller brightness changes. The changed area is measured inside enabled regions as a percentage of the full frame.", "field-help"),
      text("p", "Master Pause / Resume remains separate. No pre-event footage is buffered.", "field-help"));
    const zoneSection = text("section", "", "motion-zones"); zoneSection.setAttribute("aria-labelledby", "motion-zones-title");
    const zoneTitle = text("h3", "Motion inclusion and exclusion zones"); zoneTitle.id = "motion-zones-title";
    const zoneHelp = text("p", "No inclusion zones means the whole frame. Exclusions subtract last. These zones control recording triggers; they do not hide pixels in live video, recordings or exports.", "field-help");
    const capture = button("Capture current frame", "motion-preview-capture");
    const previewHelp = text("p", "Capture an authorized camera frame to draw or change zones.", "field-help"); previewHelp.id = "motion-preview-help";
    const frame = text("div", "", "motion-preview-frame"); frame.id = "motion-preview-frame"; frame.hidden = true;
    const svg = document.createElementNS(NS, "svg"); svg.id = "motion-zone-canvas"; svg.setAttribute("tabindex", "0"); svg.setAttribute("role", "application");
    svg.setAttribute("aria-label", "Motion zone editor"); svg.setAttribute("aria-describedby", "motion-zone-instructions");
    const image = document.createElementNS(NS, "image"); image.setAttribute("preserveAspectRatio", "none");
    const overlay = document.createElementNS(NS, "g"); svg.append(image, overlay); frame.append(svg);
    const instructions = text("p", "Draw an inclusion or exclusion, then select points on the frame and Finish zone. Keyboard: focus the frame, use arrow keys to move by 1% (Shift: 0.1%), Enter to add a point, Backspace to undo and Escape to cancel drawing.", "field-help");
    instructions.id = "motion-zone-instructions";
    const tools = text("div", "", "motion-zone-tools");
    const include = button("Draw inclusion", "motion-zone-include"), exclude = button("Draw exclusion", "motion-zone-exclude");
    const finish = button("Finish zone", "motion-zone-finish"), undo = button("Undo point", "motion-zone-undo");
    const cancelDraft = button("Cancel drawing", "motion-zone-cancel"), clear = button("Clear zones", "motion-zone-clear");
    tools.append(include, exclude, finish, undo, cancelDraft, clear);
    const list = text("ol", "", "motion-zone-list"); list.id = "motion-zone-list"; list.setAttribute("aria-label", "Saved and draft motion zones");
    zoneSection.append(zoneTitle, zoneHelp, capture, previewHelp, frame, instructions, tools, list);
    const actions = text("div", "", "dialog-actions"), cancel = button("Cancel", "recording-settings-cancel");
    const refresh = button("Refresh", "recording-settings-refresh"), save = button("Save recording settings", "recording-settings-save", "button primary"); save.type = "submit";
    actions.append(cancel, refresh, save); form.append(fields, zoneSection, actions);
    dialog.replaceChildren(heading, target, status, help, message, form); dialog.setAttribute("aria-labelledby", title.id);

    function session() {
      const value = getSession(), permissions = value?.account?.effective_permissions;
      return value?.token && value.account?.role === "administrator" && Array.isArray(permissions)
        && permissions.includes("recording.manage") ? value : null;
    }
    function canView() { return Boolean(session()?.account.effective_permissions.includes("cameras.view_live")); }
    function ownedSession() { const value = session(); return value && owner && value.token === owner.token && value.epoch === owner.epoch; }
    function current(version) { return dialog.open && camera && ownedSession() && version === epoch; }
    function tell(value = "", error = false) { message.textContent = value; message.classList.toggle("error", error); }
    function endpoint() { return PREFIX + encodeURIComponent(camera.device_id); }
    function signature() { return JSON.stringify({ mode: mode.value, post_capture_seconds: Number(hold.value), sensitivity: Number(sensitivity.value), minimum_area_percent: Number(area.value), zones }); }
    function changedZones() { return policy && JSON.stringify(zones) !== JSON.stringify(policy.zones); }
    function usablePreview() { return preview && objectUrl && performance.now() < previewDeadline && canView(); }
    function referenceMatches() {
      const r = zones.reference;
      return r === null || preview && r.width === preview.width && r.height === preview.height
        && r.rotation === preview.rotation && r.source_revision === preview.source_revision;
    }
    function dropPreview() {
      clearTimeout(previewTimer); previewTimer = null; previewDeadline = 0; preview = null;
      image.removeAttribute("href"); frame.hidden = true; overlay.replaceChildren();
      previewHelp.textContent = "Capture an authorized camera frame to draw or change zones.";
      if (objectUrl) URL.revokeObjectURL(objectUrl); objectUrl = null;
    }
    function stop() { epoch++; controller?.abort(); controller = null; busy = false; previewRequest = false; }
    function close() { stop(); dropPreview(); policy = null; camera = null; draft = null; zones = emptyZones(); invalidated = false; owner = null; if (dialog.open) dialog.close(); }
    function invalidate(value) { stop(); dropPreview(); draft = null; policy = null; invalidated = true; status.textContent = "Settings need refresh"; help.textContent = ""; tell(value, true); renderZones(); controls(); }
    function controls() {
      const active = dialog.open && Boolean(ownedSession()) && Boolean(camera), ready = active && Boolean(policy) && !busy;
      const drawing = Boolean(draft), editable = ready && usablePreview();
      form.setAttribute("aria-busy", String(busy)); fields.disabled = !ready;
      for (const input of [sensitivity, area, hold]) input.disabled = !ready || mode.value !== "motion";
      refresh.disabled = !active || busy; capture.disabled = !ready || !canView();
      include.disabled = !editable || drawing || !referenceMatches() && zones.include.length + zones.exclude.length > 0 || zones.include.length + zones.exclude.length >= 8;
      exclude.disabled = include.disabled; finish.disabled = !editable || !drawing || !polygonValid(draft.points);
      undo.disabled = !editable || !drawing || !draft.points.length; cancelDraft.disabled = !drawing || busy;
      clear.disabled = !editable || drawing;
      clear.textContent = zones.include.length + zones.exclude.length ? "Clear zones" : "Use whole frame";
      const numbers = Number.isInteger(Number(hold.value)) && finite(Number(hold.value), 1, 120)
        && Number.isInteger(Number(sensitivity.value)) && finite(Number(sensitivity.value), 1, 100) && finite(Number(area.value), 0.1, 100);
      save.disabled = !ready || drawing || !numbers || !zonesValid(zones) || signature() === savedSignature
        || changedZones() && (!usablePreview() || !referenceMatches());
      save.textContent = busy ? "Working…" : "Save recording settings";
      svg.setAttribute("aria-disabled", String(!editable)); svg.classList.toggle("drawing", drawing);
      if (!canView()) previewHelp.textContent = "Live-view permission is required to capture frames and edit zones. Recording-mode settings remain available.";
    }
    function renderZones() {
      list.replaceChildren(); overlay.replaceChildren();
      const width = preview?.width || zones.reference?.width || 1, height = preview?.height || zones.reference?.height || 1;
      for (const kind of ["include", "exclude"]) for (const [index, zone] of zones[kind].entries()) {
        const item = text("li"), label = text("span", (kind === "include" ? "Include: " : "Exclude: ") + zone.name + " · " + zone.points.length + " points");
        const remove = button("Delete " + zone.name, "motion-zone-delete-" + zone.id, "button quiet");
        remove.disabled = !usablePreview() || busy || Boolean(draft);
        remove.addEventListener("click", () => { if (!usablePreview() || busy || draft) return; zones[kind].splice(index, 1); renderZones(); controls(); });
        item.append(label, remove); list.append(item);
        const polygon = document.createElementNS(NS, "polygon");
        polygon.setAttribute("points", zone.points.map((point) => point[0] * width + "," + point[1] * height).join(" "));
        polygon.setAttribute("class", "motion-zone-polygon " + kind); overlay.append(polygon);
      }
      if (!list.children.length) list.append(text("li", "Whole-frame motion detection; no zones saved.", "field-help"));
      if (draft) {
        const line = document.createElementNS(NS, "polyline");
        line.setAttribute("points", draft.points.map((point) => point[0] * width + "," + point[1] * height).join(" "));
        line.setAttribute("class", "motion-zone-draft " + draft.kind); overlay.append(line);
        for (const point of [...draft.points, cursor]) {
          const marker = document.createElementNS(NS, "circle"); marker.setAttribute("cx", String(point[0] * width)); marker.setAttribute("cy", String(point[1] * height));
          marker.setAttribute("r", String(Math.max(width, height) * 0.006)); marker.setAttribute("class", "motion-zone-point"); overlay.append(marker);
        }
        previewHelp.textContent = (draft.kind === "include" ? "Drawing inclusion" : "Drawing exclusion") + " · " + draft.points.length + " of 16 points. Cursor " + Math.round(cursor[0] * 1000) / 10 + "% across, " + Math.round(cursor[1] * 1000) / 10 + "% down.";
        if (draft.points.length >= 3 && !polygonValid(draft.points)) previewHelp.textContent += " This outline crosses itself or has no area. Undo points before finishing.";
      } else if (usablePreview()) {
        previewHelp.textContent = referenceMatches()
          ? "Frozen current view. Inclusions are blue; exclusions are orange. Capture a new frame after its authorization expires."
          : "The camera view changed. Clear the old zones and redraw them on this frame before saving.";
      }
    }
    function accept(value) {
      if (!policyValid(value, camera.device_id)) throw { status: 502 };
      policy = clone(value); zones = clone(value.zones); draft = null;
      mode.value = value.mode; sensitivity.value = String(value.sensitivity); area.value = String(value.minimum_area_percent); hold.value = String(value.post_capture_seconds);
      status.textContent = STATES[value.status][0]; help.textContent = STATES[value.status][1];
      savedSignature = signature(); invalidated = false; renderZones(); controls();
    }
    function errorText(error, fallback) {
      if (error?.status === 401) return "Your session ended. Sign in again.";
      if (error?.status === 403) return "Your current account cannot change these recording settings or view this frame.";
      if (error?.status === 404) return "This camera is no longer available.";
      return ERRORS[error?.code] || fallback;
    }
    async function read() {
      if (!dialog.open || !session() || !camera || busy) return;
      stop(); dropPreview(); policy = null; draft = null; zones = emptyZones(); busy = true; const version = epoch;
      controller = new AbortController(); tell(""); status.textContent = "Reading recording settings…"; help.textContent = ""; renderZones(); controls();
      try { const value = await request(endpoint() + "/recording-policy", { signal: controller.signal }); if (current(version)) accept(value); }
      catch (error) { if (current(version) && error?.name !== "AbortError") tell(errorText(error, "Recording settings could not be loaded. Select Refresh to try again."), true); }
      finally { if (current(version)) { busy = false; controls(); } }
    }
    async function captureFrame() {
      if (!policy || busy || !canView() || !camera) return;
      if (draft) { tell("Finish or cancel the zone being drawn before replacing the frozen frame.", true); return; }
      stop(); dropPreview(); busy = true; previewRequest = true; const version = epoch; controller = new AbortController(); tell(""); controls();
      try {
        const value = await request(endpoint() + "/motion-preview", { method: "POST", signal: controller.signal });
        if (!current(version) || !canView()) return;
        const expected = endpoint() + "/motion-preview/" + encodeURIComponent(value?.preview_id || "");
        if (!value || !OPAQUE.test(value.preview_id) || value.preview_url !== expected || value.source_revision !== policy.source_revision
          || !Number.isInteger(value.width) || !Number.isInteger(value.height) || !finite(value.width, 1, 8192) || !finite(value.height, 1, 8192)
          || value.width * value.height > 33554432
          || value.rotation !== 0 || !Number.isInteger(value.expires_in_seconds) || !finite(value.expires_in_seconds, 1, 120)) throw { code: "motion_source_changed" };
        const received = performance.now(), blob = await request(expected, { signal: controller.signal, responseType: "blob" });
        if (!current(version) || !canView()) return;
        if (!(blob instanceof Blob) || blob.type !== "image/jpeg" || blob.size < 4 || blob.size > 2 * 1024 * 1024) throw { code: "motion_preview_unavailable" };
        const decoded = await createImageBitmap(blob);
        const scale = Math.min(1, 640 / Math.max(value.width, value.height));
        const expectedWidth = Math.max(1, Math.round(value.width * scale)), expectedHeight = Math.max(1, Math.round(value.height * scale));
        if (Math.abs(decoded.width - expectedWidth) > 1 || Math.abs(decoded.height - expectedHeight) > 1) { decoded.close(); throw { code: "motion_preview_unavailable" }; }
        decoded.close(); if (!current(version) || !canView()) return;
        preview = value; previewDeadline = received + value.expires_in_seconds * 1000;
        objectUrl = URL.createObjectURL(blob); image.setAttribute("href", objectUrl); image.setAttribute("width", String(value.width)); image.setAttribute("height", String(value.height));
        svg.setAttribute("viewBox", "0 0 " + value.width + " " + value.height); svg.setAttribute("width", String(value.width)); svg.setAttribute("height", String(value.height));
        frame.hidden = false; previewHelp.textContent = referenceMatches()
          ? "Frozen current view. Inclusions are blue; exclusions are orange. Capture a new frame after its authorization expires."
          : "The camera view changed. Clear the old zones and redraw them on this frame before saving.";
        previewTimer = setTimeout(() => { if (!dialog.open || preview !== value) return; dropPreview(); renderZones(); tell("The frozen frame expired. Capture a new frame before saving zone changes.", true); controls(); }, Math.max(0, previewDeadline - performance.now()));
        renderZones();
      } catch (error) { if (current(version) && error?.name !== "AbortError") { dropPreview(); tell(errorText(error, "The current camera frame could not be loaded."), true); } }
      finally { if (current(version)) { busy = false; previewRequest = false; controls(); } }
    }
    async function savePolicy(event) {
      event.preventDefault(); if (save.disabled || !policy || !session()) return;
      const body = { expected_revision: policy.revision, mode: mode.value, post_capture_seconds: Number(hold.value), sensitivity: Number(sensitivity.value), minimum_area_percent: Number(area.value), zones: clone(zones) };
      if (changedZones()) body.preview_id = preview.preview_id;
      stop(); busy = true; const version = epoch; controller = new AbortController(); tell(""); controls();
      try {
        const value = await request(endpoint() + "/recording-policy", { method: "PUT", body, signal: controller.signal });
        if (!current(version)) return;
        dropPreview(); accept(value); tell("Recording settings saved. This camera uses the saved mode even when this dialog or browser closes."); onChanged();
      } catch (error) {
        if (!current(version) || error?.name === "AbortError") return;
        if (error?.status === 422) tell(errorText(error, "Check the recording values and zones."), true);
        else invalidate(errorText(error, "The save outcome could not be confirmed. Refresh before making another change."));
      } finally { if (current(version)) { busy = false; controls(); } }
    }
    function startDraw(kind) {
      if (!usablePreview() || busy || draft || zones.include.length + zones.exclude.length >= 8) return;
      const priorReference = clone(zones.reference);
      zones.reference = { width: preview.width, height: preview.height, rotation: 0, source_revision: preview.source_revision };
      draft = { kind, points: [], priorReference }; cursor = [0.5, 0.5]; renderZones(); controls(); svg.focus();
    }
    function addPoint(point) {
      if (!draft || !usablePreview() || busy) return;
      if (draft.points.length >= 16) { tell("A zone supports up to 16 points. Finish it or undo a point.", true); return; }
      const chosen = point.map((value) => Math.round(Math.min(1, Math.max(0, value)) * 1000000) / 1000000);
      if (draft.points.some((old) => old[0] === chosen[0] && old[1] === chosen[1])) { tell("Choose a different point, then use Finish zone to close the outline.", true); return; }
      draft.points.push(chosen); cursor = chosen; tell(""); renderZones(); controls();
    }
    include.addEventListener("click", () => startDraw("include")); exclude.addEventListener("click", () => startDraw("exclude"));
    finish.addEventListener("click", () => {
      if (!draft || !usablePreview() || !polygonValid(draft.points)) return;
      const name = (draft.kind === "include" ? "Inclusion " : "Exclusion ") + (zones[draft.kind].length + 1);
      zones[draft.kind].push({ id: crypto.randomUUID(), name, points: draft.points }); draft = null; tell(""); renderZones(); controls();
    });
    undo.addEventListener("click", () => { if (draft) { draft.points.pop(); renderZones(); controls(); } });
    function discardDraft() { if (draft) zones.reference = draft.priorReference; draft = null; renderZones(); controls(); }
    cancelDraft.addEventListener("click", discardDraft);
    clear.addEventListener("click", () => { if (usablePreview() && !draft && !busy) { zones = { reference: { width: preview.width, height: preview.height, rotation: 0, source_revision: preview.source_revision }, include: [], exclude: [] }; renderZones(); controls(); } });
    svg.addEventListener("click", (event) => {
      if (!draft || event.button !== 0) return;
      const box = svg.getBoundingClientRect(); if (box.width <= 0 || box.height <= 0) return;
      addPoint([(event.clientX - box.left) / box.width, (event.clientY - box.top) / box.height]);
    });
    svg.addEventListener("keydown", (event) => {
      if (!draft || !usablePreview()) return;
      const movement = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key];
      if (movement) { event.preventDefault(); const step = event.shiftKey ? 0.001 : 0.01; cursor = cursor.map((n, i) => Math.min(1, Math.max(0, n + movement[i] * step))); renderZones(); }
      else if (event.key === "Enter" || event.key === " ") { event.preventDefault(); addPoint(cursor); }
      else if (event.key === "Backspace") { event.preventDefault(); draft.points.pop(); renderZones(); controls(); }
      else if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); discardDraft(); }
    });
    for (const input of [mode, sensitivity, area, hold]) input.addEventListener("input", controls);
    closeButton.addEventListener("click", close); cancel.addEventListener("click", close);
    refresh.addEventListener("click", () => void read()); capture.addEventListener("click", () => void captureFrame()); form.addEventListener("submit", savePolicy);
    dialog.addEventListener("cancel", (event) => { event.preventDefault(); close(); });
    dialog.addEventListener("close", () => { if (!dialog.open && camera) close(); });
    return {
      open(value) {
        close(); if (!session() || !value || !UUID.test(value.device_id)) return;
        owner = { token: session().token, epoch: session().epoch };
        camera = { ...value }; target.textContent = value.name || "Enrolled camera"; dialog.showModal(); void read();
      },
      close,
      selection(id) { if (camera && id !== camera.device_id) close(); },
      authority() {
        if (!dialog.open) return;
        if (!ownedSession()) { close(); return; }
        if (!canView() && (previewRequest || preview || objectUrl || draft)) {
          const wasSaving = busy && !previewRequest;
          stop(); dropPreview(); draft = null; if (policy) zones = clone(policy.zones); renderZones();
          if (wasSaving) { invalidate("Live-view permission changed during a save. Refresh before another change."); return; }
          tell("Live-view access ended. The frozen frame and unsaved zone edits were cleared.", true);
        }
        controls();
      },
      inventory(values) {
        if (!camera || !dialog.open) return;
        const next = values.find((value) => value.device_id === camera.device_id);
        if (!next || !session()) { close(); return; }
        if (!invalidated && camera.source_revision && next.source_revision && camera.source_revision !== next.source_revision) invalidate("The camera connection changed. Refresh recording settings and review the zones.");
        if (!invalidated && Object.hasOwn(STATES, next.motion_policy_state)) { status.textContent = STATES[next.motion_policy_state][0]; help.textContent = STATES[next.motion_policy_state][1]; }
        camera = { ...next }; controls();
      },
    };
  }
  window.K5RecordingSettings = Object.freeze({ create, errorCodes: Object.freeze(Object.keys(ERRORS)) });
})();
