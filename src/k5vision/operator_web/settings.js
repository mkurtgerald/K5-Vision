/* Recorder administration and account security. Secret values stay in this view only. */
"use strict";
(() => {
  const PREFIX = "/api/v1/operator/admin";
  const SECURITY = "/api/v1/operator/security";
  const GIB = 1024 ** 3, DAY = 86400;
  const SECTIONS = [
    ["storage", "Storage", "storage.manage"], ["time", "Time", "time.manage"],
    ["activity", "Audit trail", "audit.read"], ["integrations", "Integrations", "integrations.manage"],
    ["roles", "Custom roles", "roles.manage"], ["sso", "Organization sign-in", "users.manage"], ["security", "Account security", null],
  ];
  const ERRORS = Object.freeze({
    storage_invalid_request: "Check the storage values and try again.", storage_conflict: "Storage settings changed. Refresh before saving again.",
    storage_directory_refused: "That recording directory cannot be used safely. Choose a private local directory.", storage_unavailable: "Recording storage settings are unavailable.",
    time_unavailable: "Time administration is unavailable.", time_unsupported: "This time action is not supported on this recorder.", time_busy: "Another time operation is running. Wait, then refresh.",
    time_ntp_unreachable: "The configured time reference could not be reached. Synchronization has not been verified.",
    time_elevation_cancelled: "The Windows administrator prompt was cancelled. No successful change is claimed.", time_operation_failed: "Windows could not complete this time operation. Refresh its status before retrying.",
    camera_time_unsupported: "This camera does not support the requested ONVIF time action.", camera_time_invalid_response: "The camera did not return a valid time response.",
    camera_time_unreachable: "The camera time service could not be reached.", camera_time_auth_failed: "The camera account was rejected. Reconnect the camera to update its account.",
    audit_invalid_request: "Check the audit filters and try again.", audit_unavailable: "The audit trail is unavailable.",
    integration_invalid_request: "Check the connection name, service kind and HTTPS address.", integration_unavailable: "Integration settings are unavailable.",
    integration_not_found: "This connection no longer exists. Refresh the list.", integration_capacity: "The integration connection limit has been reached.",
    integration_unreachable: "The service could not be reached. No compatible connection has been verified.", integration_incompatible: "The service does not support the required K5 contract.",
    role_invalid_request: "Check the role name, base role and permissions.", role_conflict: "This role changed or is still assigned. Refresh before trying again.",
    role_not_found: "This custom role no longer exists.", roles_unavailable: "Custom-role settings are unavailable.",
    roles_invalid_request: "Check the role name, base role and permissions.", roles_not_found: "This custom role no longer exists.",
    roles_revision_conflict: "Role settings changed. Refresh before saving again.", roles_in_use: "This role is assigned to an account. Remove its assignments before deleting it.",
    roles_last_administrator: "Keep another activated administrator with full permissions before making this change.", roles_profile_bound: "Change this account's custom-role profile before changing its base role.",
    roles_base_immutable: "A custom role's base role cannot be changed after creation. Create another profile.", roles_name_exists: "That role name already exists.", roles_capacity: "The custom-role limit has been reached.",
    sso_invalid_request: "Check the organization sign-in settings.", sso_unavailable: "Organization sign-in is unavailable.", sso_incompatible: "The identity provider did not pass required OpenID Connect verification.",
    account_security_not_accepted: "Your current password or authenticator/recovery code was not accepted. Enter a current unused code and try again.",
    account_security_unavailable: "Account security is unavailable. Try again shortly.", account_security_invalid_request: "Check the account security fields and try again.", account_security_rate_limit: "Too many security attempts. Wait before trying again.",
    sso_configuration_refused: "The identity-provider configuration was refused. Check the HTTPS issuer and client settings.", sso_provider_test_failed: "The identity provider did not pass verification. Check its issuer, discovery and signing settings.",
    mfa_invalid_code: "That authenticator or recovery code was not accepted. Try a current unused code.",
    mfa_invalid_password: "Your current password was not accepted.", mfa_unavailable: "Account security is unavailable.", mfa_invalid_request: "Check the security fields and try again.",
    mfa_enrollment_expired: "This enrollment expired. Start a new enrollment.", mfa_already_enabled: "An authenticator is already enabled. Refresh account security.",
    mfa_not_enabled: "An authenticator has not been enabled for this account.",
  });
  const ALL_PERMISSIONS = ["cameras.view_live", "cameras.playback", "cameras.export", "cameras.manage", "recording.manage", "analytics.manage", "users.manage", "roles.manage", "storage.manage", "time.manage", "integrations.manage", "audit.read"];
  const $ = (id) => document.getElementById(id);
  function element(tag, text = "", attributes = {}) {
    const node = document.createElement(tag); if (text) node.textContent = text;
    for (const [key, value] of Object.entries(attributes)) { if (key === "className") node.className = value; else node.setAttribute(key, value); }
    return node;
  }
  function button(id, text, action, primary = false) {
    const node = element("button", text, { id, type: "button", className: `button ${primary ? "primary" : "secondary"}` });
    node.addEventListener("click", action); return node;
  }
  function field(parent, id, label, { type = "text", value = "", help = "", ...attributes } = {}) {
    const holder = element("div"); holder.append(element("label", label, { for: id }));
    const input = element(type === "textarea" ? "textarea" : type === "select" ? "select" : "input", "", { id, ...attributes });
    if (!["textarea", "select"].includes(type)) input.type = type;
    input.value = value; holder.append(input); if (help) holder.append(element("p", help, { className: "field-help" })); parent.append(holder); return input;
  }
  function options(input, values) { input.replaceChildren(...values.map(([value, text]) => element("option", text, { value }))); }
  function bytes(value) { return Number.isFinite(value) && value >= 0 ? `${(value / GIB).toFixed(2)} GiB` : "Unavailable"; }
  function timestamp(value) { const ms = typeof value === "string" ? Date.parse(value) : NaN; return Number.isFinite(ms) ? new Date(ms).toISOString().replace("T", " ").replace("Z", " UTC") : "Unavailable"; }
  function servers(value) { return value.split(/[\s,;]+/).map((v) => v.trim()).filter(Boolean); }

  function create({ request, getSession, getCameras, onReauthenticate, onSecurityTransition = () => {} }) {
    let visible = false, epoch = 0, controller = null, currentSection = "storage", sequence = 0;
    let storage = null, host = null, camera = null, connections = [], editingConnection = null, cameraSelectionLost = false;
    let auditBefore = null, auditStack = [], auditNext = null, roleData = null, editingRole = null;
    let enrollmentToken = "", mustReauthenticate = false, objectUrl = null, objectTimer = null, authoritySuspended = false;
    const tabs = new Map(), panels = new Map(), messages = new Map(), busy = new Map();
    const permitted = (permission) => {
      const account = getSession()?.account;
      return account?.authority_resolved === true && (!permission
        || Array.isArray(account.effective_permissions) && account.effective_permissions.includes(permission));
    };
    const activeSession = () => { const s = getSession(); return s?.token && s.account?.enabled !== false && s.account?.authority_resolved === true ? s : null; };
    function errorText(error) {
      if (ERRORS[error.code]) return ERRORS[error.code];
      if (error.status === 401) return "Your session ended. Sign in again.";
      if (error.status === 403) return "Your current account does not have permission for this action.";
      if (error.status === 429) return "Too many attempts. Wait before trying again.";
      if (error.status === 422) return "Check the entered values and try again.";
      if (error.status === 404) return "This feature is unavailable in this running build. Refresh after updating K5.";
      return "This operation could not be completed. Refresh its status before retrying.";
    }
    function message(name, text = "", error = false) { const n = messages.get(name); n.textContent = text; n.classList.toggle("error", error); }
    function current(start, version, name) { const s = activeSession(); return visible && epoch === version && currentSection === name && s?.token === start.token && s?.epoch === start.epoch; }
    async function run(name, operation, success = null) {
      const start = activeSession(), version = epoch, seq = ++sequence;
      if (!start || !visible || currentSection !== name || busy.has(name)) return;
      busy.set(name, seq); panels.get(name).setAttribute("aria-busy", "true");
      for (const n of panels.get(name).querySelectorAll("button,input,select,textarea")) n.disabled = true;
      message(name, "Working…");
      try {
        const result = await operation(controller.signal);
        if (!current(start, version, name)) return;
        if (success) success(result); else message(name);
      } catch (error) { if (error.name !== "AbortError" && current(start, version, name)) message(name, errorText(error), true); }
      finally {
        if (busy.get(name) === seq) busy.delete(name);
        if (current(start, version, name) && !busy.has(name)) {
          panels.get(name).setAttribute("aria-busy", "false");
          for (const n of panels.get(name).querySelectorAll("button,input,select,textarea")) n.disabled = false;
          updateDisabled(name);
        }
      }
      return seq;
    }
    function section(name, title, help) {
      const node = element("section", "", { id: `settings-${name}-panel`, className: "settings-section", "aria-label": title, hidden: "" });
      node.append(element("h2", title), element("p", help, { className: "field-help" }));
      const status = element("p", "", { id: `settings-${name}-message`, className: "form-message settings-status", role: "status", "aria-live": "polite" });
      node.append(status); $("settings-content").append(node); panels.set(name, node); messages.set(name, status); return node;
    }
    function actions(parent, ...nodes) { const row = element("div", "", { className: "settings-actions" }); row.append(...nodes); parent.append(row); }
    function health(parent, id) { const node = element("div", "", { id, className: "settings-health", role: "status" }); parent.append(node); return node; }
    function updateDisabled(name) {
      if (name === "time") {
        $("time-save-host").disabled = host?.capabilities?.configure_ntp !== true;
        $("time-sync-host").disabled = host?.capabilities?.resynchronize !== true;
        $("time-save-camera").disabled = camera?.capabilities?.configure_ntp !== true;
        $("time-sync-camera").disabled = camera?.capabilities?.synchronize_to_host !== true || !$("time-confirm-camera").checked;
      }
      if (name === "activity") { $("activity-older").disabled = auditNext === null; $("activity-newer").disabled = !auditStack.length; }
      if (name === "roles") { $("roles-delete").disabled = !editingRole; $("roles-base").disabled = Boolean(editingRole); }
      if (name === "integrations") $("integration-kind").disabled = Boolean(editingConnection);
      if (name === "sso") {
        const full = canConfigureSso();
        for (const node of ssoForm.querySelectorAll("input,button[type=submit]")) node.disabled = !full;
        $("sso-authority").hidden = full;
        for (const option of linkUser.options) if (option.value) option.disabled = !canLinkSsoUser(ssoUsers.find((u) => u.id === option.value));
        if (linkUser.selectedOptions[0]?.disabled) linkUser.value = "";
        for (const node of $("sso-links").querySelectorAll("button")) node.disabled = !canLinkSsoUser(ssoUsers.find((u) => u.id === ssoLinks.find((link) => `sso-unlink-${link.id}` === node.id)?.user_id));
      }
      if (name === "security" && mustReauthenticate) for (const node of securityPanel.querySelectorAll("button,input")) node.disabled = node.id !== "security-recovery-done";
    }

    // Storage settings are revision checked; directory changes are never shown as immediately active.
    const storagePanel = section("storage", "Recording storage", "Set recording capacity and retention. Lowering the quota or retention can remove older closed recordings. Retention uses elapsed stable time so clock changes cannot age footage unexpectedly.");
    health(storagePanel, "storage-health");
    const storageForm = element("form", "", { id: "storage-form" }), storageGrid = element("div", "", { className: "settings-grid" });
    field(storageGrid, "storage-quota", "Recording quota · GiB", { type: "number", min: "1", max: "4096", step: "1", required: "" });
    field(storageGrid, "storage-free", "Keep free disk space · GiB", { type: "number", min: "0", max: "4096", step: "0.25", required: "" });
    field(storageGrid, "storage-retention", "Closed footage retention · days", { type: "number", min: "0", max: "30", step: "0.25", required: "", help: "0 disables age-based pruning. Capacity and free-space safeguards still apply." });
    field(storageGrid, "storage-directory", "Recording directory", { maxlength: "1024", autocomplete: "off", spellcheck: "false", help: "A directory change takes effect after K5 restarts. Retained footage is copied and verified first; the original files are preserved." });
    storageForm.append(storageGrid); actions(storageForm, button("storage-refresh", "Refresh storage", () => loadStorage()), element("button", "Save storage settings", { id: "storage-save", type: "submit", className: "button primary" })); storagePanel.append(storageForm);
    function renderStorage(data) {
      if (!Number.isInteger(data?.revision) || !Number.isInteger(data.quota_bytes) || !Number.isInteger(data.retention_seconds) || !Number.isInteger(data.minimum_free_bytes) || typeof data.recording_directory !== "string") throw new Error("Invalid storage response");
      storage = data; $("storage-quota").value = data.quota_bytes / GIB; $("storage-free").value = data.minimum_free_bytes / GIB; $("storage-retention").value = data.retention_seconds / DAY;
      $("storage-directory").value = data.pending_recording_directory || data.recording_directory;
      let text = `Recorded video: ${bytes(data.recording_bytes)} · Free space: ${bytes(data.free_bytes)}\nActive directory: ${data.recording_directory}`;
      if (data.location_status === "restart_required") text += `\nPending directory: ${data.pending_recording_directory}. Restart K5 to copy and verify retained footage before activation.`;
      if (data.location_status === "migration_required") text += `\nThe new directory has not been activated. Migration requires attention; the original directory and footage remain active.`;
      if (data.recording_allowed === false) text += "\nStorage currently prevents recording. Check free space and the quota.";
      $("storage-health").textContent = text;
    }
    function loadStorage() { return run("storage", (signal) => request(`${PREFIX}/storage`, { signal }), (d) => { renderStorage(d); message("storage"); }); }
    storageForm.addEventListener("submit", (event) => {
      event.preventDefault(); if (!storage) return;
      const body = { expected_revision: storage.revision, quota_bytes: Math.round(Number($("storage-quota").value) * GIB), minimum_free_bytes: Math.round(Number($("storage-free").value) * GIB), retention_seconds: Math.round(Number($("storage-retention").value) * DAY) };
      const directory = $("storage-directory").value.trim(); if (directory !== (storage.pending_recording_directory || storage.recording_directory)) body.recording_directory = directory;
      void run("storage", (signal) => request(`${PREFIX}/storage`, { method: "PATCH", body, signal }), (d) => { renderStorage(d); message("storage", d.location_status === "active" ? "Storage policy saved and active." : "Storage policy saved. The directory change remains pending until a successful K5 restart migration."); });
    });

    const timePanel = section("time", "Time administration", "Manage Windows Time and camera ONVIF time settings. Reference measurements, configured settings and successful synchronization are reported separately.");
    health(timePanel, "time-host-health");
    const hostForm = element("form", "", { id: "time-host-form" }), hostGrid = element("div", "", { className: "settings-grid" });
    const timeMode = field(hostGrid, "time-host-mode", "Windows Time source", { type: "select" }); options(timeMode, [["manual", "Configured NTP servers"], ["domain", "Windows domain hierarchy"]]);
    field(hostGrid, "time-host-servers", "NTP servers", { maxlength: "1024", placeholder: "time.example.org", help: "Separate server names with commas. Windows may request administrator approval." });
    hostForm.append(hostGrid); actions(hostForm, element("button", "Save Windows Time configuration", { id: "time-save-host", type: "submit", className: "button secondary", disabled: "" }), button("time-sync-host", "Synchronize Windows Time", () => syncHost(), true), button("time-refresh", "Refresh time health", () => loadTime())); timePanel.append(hostForm);
    const cameraPart = element("div", "", { className: "settings-divider" }); cameraPart.append(element("h3", "Camera time · ONVIF"));
    const cameraSelect = field(cameraPart, "time-camera", "Camera", { type: "select" }); cameraSelect.addEventListener("change", () => { cameraSelectionLost = false; camera = null; $("time-confirm-camera").checked = false; void loadCameraTime(); });
    health(cameraPart, "time-camera-health");
    const managementChoice = element("label", "", { className: "settings-check" }); managementChoice.append(element("input", "", { id: "time-camera-use-account", type: "checkbox" }), element("span", "Use a separate camera management account for the next action")); cameraPart.append(managementChoice);
    const managementFields = element("div", "", { className: "settings-grid" }); field(managementFields, "time-camera-username", "Camera management username", { maxlength: "128", autocomplete: "off" }); field(managementFields, "time-camera-password", "Camera management password", { type: "password", maxlength: "256", autocomplete: "new-password" }); cameraPart.append(managementFields, element("p", "Leave the option unchecked to use the camera's saved connection account. Separate management credentials are cleared after every action and when leaving this panel.", { className: "field-help" })); actions(cameraPart, button("time-refresh-camera", "Refresh camera time", () => loadCameraTime()));
    function clearCameraCredentials() { $("time-camera-username").value = ""; $("time-camera-password").value = ""; $("time-camera-use-account").checked = false; }
    function takeCameraCredentials() { const values = $("time-camera-use-account").checked ? { username: $("time-camera-username").value, password: $("time-camera-password").value } : {}; clearCameraCredentials(); return values; }

    const cameraForm = element("form", "", { id: "time-camera-form" }), cameraGrid = element("div", "", { className: "settings-grid" });
    const dhcp = field(cameraGrid, "time-camera-dhcp", "Camera NTP servers", { type: "select" }); options(dhcp, [["true", "Obtain from DHCP"], ["false", "Use configured NTP servers"]]);
    field(cameraGrid, "time-camera-servers", "Camera NTP server names or addresses", { maxlength: "1024", help: "Saving NTP settings requests the camera's NTP mode; it does not establish absolute UTC accuracy." }); cameraForm.append(cameraGrid);
    actions(cameraForm, element("button", "Save camera NTP settings", { id: "time-save-camera", type: "submit", className: "button secondary", disabled: "" })); cameraPart.append(cameraForm);
    const confirm = element("label", "", { className: "settings-check" }); const checkbox = element("input", "", { id: "time-confirm-camera", type: "checkbox" }); checkbox.addEventListener("change", () => updateDisabled("time")); confirm.append(checkbox, element("span", "Set this camera's UTC directly from the recorder. This changes camera time and verifies relative offset only; the recorder must have an accurate clock.")); cameraPart.append(confirm); actions(cameraPart, button("time-sync-camera", "Set camera UTC to recorder UTC", () => syncCamera())); timePanel.append(cameraPart);
    function renderHost(data) {
      if (!data?.host || !data.capabilities) throw new Error("Invalid time response"); host = data;
      const h = data.host, ref = h.reference || {}, offset = Number.isFinite(ref.offset_seconds) ? `${ref.offset_seconds.toFixed(3)} seconds` : "unavailable";
      $("time-host-mode").value = ["manual", "domain"].includes(h.mode) ? h.mode : "manual"; $("time-host-servers").value = Array.isArray(h.ntp_servers) ? h.ntp_servers.join(", ") : "";
      $("time-host-health").textContent = `Windows Time service: ${h.service_running === true ? "running" : h.service_running === false ? "stopped" : "unavailable"}\nReference status: ${ref.status || "unavailable"} · Measured offset: ${offset}\nReference measurement: ${ref.sync_verified === true ? "within the verification limit" : "not verified"}${ref.measured_at ? ` · ${timestamp(ref.measured_at)}` : ""}`;
      updateDisabled("time");
    }
    function renderCameraTime(data) {
      if (!data?.capabilities || !data.clock || data.camera_id !== cameraSelect.value) throw new Error("Invalid camera time response"); camera = data;
      $("time-camera-dhcp").value = String(data.ntp?.from_dhcp !== false); $("time-camera-servers").value = Array.isArray(data.ntp?.servers) ? data.ntp.servers.join(", ") : "";
      const offset = data.clock.offset_seconds ?? data.clock.skew_seconds;
      $("time-camera-health").textContent = `Camera time: ${data.clock.status || "observed"}\nOffset from recorder: ${Number.isFinite(offset) ? `${offset.toFixed(3)} seconds` : "unavailable"}\nNTP mode: ${data.ntp?.mode || "unavailable"}\nAbsolute synchronization is not established by copying recorder UTC.`;
      updateDisabled("time");
    }
    function cameraTimePath() { return `/api/v1/operator/cameras/${encodeURIComponent($("time-camera").value)}/time`; }
    function renderCameraOptions() {
      const previous = cameraSelect.value, choices = getCameras();
      options(cameraSelect, [["", "Choose a camera"], ...choices.map((c) => [c.device_id, c.name])]);
      if (choices.some((c) => c.device_id === previous)) cameraSelect.value = previous;
      else if (previous) { cameraSelectionLost = true; camera = null; clearCameraCredentials(); $("time-confirm-camera").checked = false; $("time-camera-health").textContent = "The selected camera is no longer available. Choose a camera and refresh its time settings."; }
      else if (!cameraSelectionLost && choices.length) cameraSelect.value = choices[0].device_id;
      updateDisabled("time");
    }
    function loadTime() { renderCameraOptions(); return run("time", async (signal) => { const d = await request(`${PREFIX}/time`, { signal }); let c = null; if (cameraSelect.value) { try { c = await request(cameraTimePath(), { signal }); } catch (e) { if (e.name === "AbortError") throw e; } } return { d, c }; }, ({ d, c }) => { renderHost(d); if (c) renderCameraTime(c); else { camera = null; $("time-camera-health").textContent = cameraSelect.value ? "Camera time is unavailable. This camera may not support ONVIF time administration." : "Enroll an ONVIF camera to manage its time."; } message("time"); }); }
    function loadCameraTime() { if (!cameraSelect.value) return; const body = takeCameraCredentials(); return run("time", async (signal) => { try { return await request(Object.hasOwn(body, "username") ? `${cameraTimePath()}/status` : cameraTimePath(), { ...(Object.hasOwn(body, "username") ? { method: "POST", body } : {}), signal }); } finally { body.username = ""; body.password = ""; } }, (d) => { renderCameraTime(d); message("time"); }); }
    function operationMessage(data, fallback) { return data?.operation?.verified === true ? `${fallback} Verification completed.` : data?.operation?.status === "configured" ? "Configuration saved. Synchronization has not yet been verified." : `${fallback} Refresh time health to inspect the result.`; }
    hostForm.addEventListener("submit", (event) => { event.preventDefault(); void run("time", (signal) => request(`${PREFIX}/time`, { method: "PATCH", body: { mode: timeMode.value, ntp_servers: servers($("time-host-servers").value) }, signal, timeout: 120000 }), (d) => { if (d.host) renderHost(d); message("time", operationMessage(d, "Windows Time configuration completed.")); }); });
    function syncHost() { void run("time", (signal) => request(`${PREFIX}/time/synchronize`, { method: "POST", body: {}, signal, timeout: 120000 }), (d) => { if (d.host) renderHost(d); message("time", operationMessage(d, "Windows Time resynchronization completed.")); }); }
    cameraForm.addEventListener("submit", (event) => { event.preventDefault(); const body = { from_dhcp: dhcp.value === "true", ntp_servers: servers($("time-camera-servers").value), ...takeCameraCredentials() }; void run("time", async (signal) => { try { return await request(cameraTimePath(), { method: "PATCH", body, signal }); } finally { body.username = ""; body.password = ""; } }, (d) => { if (d.clock) renderCameraTime(d); message("time", operationMessage(d, "Camera NTP configuration completed.")); }); });
    function syncCamera() { if (!$("time-confirm-camera").checked) return; const body = { mode: "host", ...takeCameraCredentials() }; void run("time", async (signal) => { try { return await request(`${cameraTimePath()}/synchronize`, { method: "POST", body, signal }); } finally { body.username = ""; body.password = ""; } }, (d) => { if (d.clock) renderCameraTime(d); $("time-confirm-camera").checked = false; message("time", d.operation?.verified === true ? "Camera UTC was set and its relative offset verified. Absolute UTC accuracy still depends on the recorder's time reference." : "Camera time action completed without verified relative synchronization. Refresh camera time."); }); }

    const activityPanel = section("activity", "Audit trail", "Recorder operations, authentication and administrative changes are listed in append order. Started/incomplete actions remain visible even if an operation is interrupted.");
    const activityForm = element("form", "", { id: "activity-form" }), activityGrid = element("div", "", { className: "settings-grid" });
    field(activityGrid, "activity-action", "Action", { placeholder: "All actions", maxlength: "80", list: "activity-actions" });
    const auditActions = element("datalist", "", { id: "activity-actions" }); activityPanel.append(auditActions); const outcome = field(activityGrid, "activity-outcome", "Outcome", { type: "select" }); options(outcome, [["", "All outcomes"], ["started", "Started / incomplete"], ["success", "Success"], ["denied", "Denied"], ["failure", "Failure"], ["interrupted", "Interrupted"]]);
    field(activityGrid, "activity-start", "From · UTC", { type: "datetime-local", step: "1" }); field(activityGrid, "activity-end", "To · UTC", { type: "datetime-local", step: "1" }); activityForm.append(activityGrid);
    actions(activityForm, element("button", "Apply filters", { id: "activity-filter", type: "submit", className: "button primary" }), button("activity-export", "Download audit JSON", () => exportAudit())); activityPanel.append(activityForm);
    const auditTable = element("table", "", { className: "admin-table" }), thead = element("thead"), heading = element("tr"); for (const label of ["When · UTC", "Actor", "Action", "Outcome", "Target", "Changes"]) heading.append(element("th", label, { scope: "col" })); thead.append(heading); auditTable.append(thead, element("tbody", "", { id: "activity-list" })); const wrap = element("div", "", { className: "admin-table-wrap" }); wrap.append(auditTable); activityPanel.append(wrap);
    health(activityPanel, "activity-summary"); actions(activityPanel, button("activity-newer", "Newer events", () => { const previous = auditStack.pop(); auditBefore = previous ?? null; void loadAudit(); }), button("activity-older", "Older events", () => { if (auditNext !== null) { auditStack.push(auditBefore); auditBefore = auditNext; void loadAudit(); } }));
    function auditQuery(exporting = false) {
      const p = new URLSearchParams({ limit: exporting ? "5000" : "50" });
      for (const [key, id] of [["action", "activity-action"], ["outcome", "activity-outcome"]]) if ($(id).value.trim()) p.set(key, $(id).value.trim());
      for (const [key, id] of [["start", "activity-start"], ["end", "activity-end"]]) { const value = $(id).value; if (value) p.set(key, new Date(`${value}Z`).toISOString()); }
      if (!exporting && auditBefore !== null) p.set("before", String(auditBefore)); return p.toString();
    }
    function renderAudit(data) {
      if (!Array.isArray(data?.events) || data.events.length > 100 || typeof data.has_more !== "boolean") throw new Error("Invalid audit response");
      if (Array.isArray(data.actions)) auditActions.replaceChildren(...data.actions.filter((v) => typeof v === "string" && v.length <= 80).map((v) => element("option", v.replaceAll(".", " · "), { value: v })));
      const rows = data.events.map((e) => { const tr = element("tr"); for (const value of [timestamp(e.occurred_at), e.actor_id || "System / unauthenticated", String(e.action || "").replaceAll("_", " "), e.outcome === "started" ? "Started / incomplete" : e.outcome || "Unavailable", [e.target_kind, e.target_id].filter(Boolean).join(" · ") || "—", Array.isArray(e.changes) ? e.changes.join(", ") : ""]) tr.append(element("td", String(value))); return tr; });
      $("activity-list").replaceChildren(...rows); auditNext = data.has_more && Number.isInteger(data.next_before) ? data.next_before : null;
      $("activity-summary").textContent = `${data.logging_available === false ? "Audit logging is unavailable; audited changes are blocked. " : ""}${data.events.length} events on this page · ${Number.isInteger(data.total_rows) ? data.total_rows : "Unknown"} retained audit rows. Append order is preserved when UTC changes.`; updateDisabled("activity");
    }
    function loadAudit() { return run("activity", (signal) => request(`${PREFIX}/activity?${auditQuery()}`, { signal }), (d) => { renderAudit(d); message("activity"); }); }
    activityForm.addEventListener("submit", (event) => { event.preventDefault(); auditBefore = null; auditStack = []; void loadAudit(); });
    function clearDownload() { clearTimeout(objectTimer); if (objectUrl) URL.revokeObjectURL(objectUrl); objectUrl = null; }
    function exportAudit() { void run("activity", (signal) => request(`${PREFIX}/activity/export?${auditQuery(true)}`, { signal, responseType: "blob" }), (blob) => { if (!(blob instanceof Blob) || blob.size > 16 * 1024 * 1024) throw new Error("Invalid audit export"); clearDownload(); objectUrl = URL.createObjectURL(blob); const a = element("a", "", { href: objectUrl, download: "k5-activity.json" }); document.body.append(a); a.click(); a.remove(); objectTimer = setTimeout(clearDownload, 60000); message("activity", "Audit download prepared. The JSON includes pagination fields if more than 5,000 matching rows exist."); }); }

    const integrationsPanel = section("integrations", "Neural and analytics connections", "Configure a provider for K5's authenticated handoff contract, then have the provider complete its handshake. This screen reports actual handshake and delivery status. Neural providers receive observations and return non-authorizing analysis; device-control authority is not granted.");
    const connectionForm = element("form", "", { id: "integration-form", autocomplete: "off" }), connectionGrid = element("div", "", { className: "settings-grid" });
    field(connectionGrid, "integration-name", "Connection name", { required: "", maxlength: "64" }); const kind = field(connectionGrid, "integration-kind", "Service kind", { type: "select" }); options(kind, [["analytics", "Analytics"], ["neural", "Neural"]]);
    field(connectionGrid, "integration-origin", "Provider origin · optional", { type: "url", placeholder: "https://analytics.example.org", maxlength: "2048", help: "Optional provider identity metadata. Saving an origin does not contact or connect a remote inference service." });
    field(connectionGrid, "integration-token", "Provider access token", { type: "password", maxlength: "2048", autocomplete: "new-password", help: "Write-only. Leave blank when adding a connection to generate a token displayed once. When editing, leave blank to keep the current token." });
    const connectionCameras = element("div", "", { id: "integration-camera-choices", className: "settings-grid" });
    connectionForm.append(connectionGrid, element("h3", "Cameras shared with this provider"), element("p", "Choose explicit camera grants. A provider cannot be enabled without a camera assignment.", { className: "field-help" }), connectionCameras);
    actions(connectionForm, element("button", "Add connection", { id: "integration-save", type: "submit", className: "button primary" }), button("integration-cancel", "Clear editor", () => clearConnection()), button("integration-refresh", "Refresh connections", () => loadConnections())); integrationsPanel.append(connectionForm, element("div", "", { id: "integration-list", className: "settings-cards" }));
    const providerSecret = element("div", "", { id: "integration-setup-token", className: "settings-secret settings-divider", hidden: "" });
    providerSecret.append(element("p", "Save this provider token now and configure it in the provider. It will not be displayed again. The provider must connect to K5's documented handoff endpoints and complete a compatible handshake."), element("code", "", { id: "integration-setup-token-value" }));
    actions(providerSecret, button("integration-setup-token-done", "I saved the provider token", () => { $("integration-setup-token-value").textContent = ""; providerSecret.hidden = true; })); integrationsPanel.append(providerSecret);
    function renderConnectionCameras(selected = []) { const set = new Set(selected); connectionCameras.replaceChildren(...getCameras().map((c) => { const label = element("label", "", { className: "settings-check" }); const box = element("input", "", { type: "checkbox", value: c.device_id }); box.checked = set.has(c.device_id); label.append(box, element("span", c.name)); return label; })); }
    function showProviderToken(value) { if (value === undefined) return; if (typeof value !== "string" || value.length > 2048) throw new Error("Invalid provider token"); $("integration-setup-token-value").textContent = value; providerSecret.hidden = false; }
    function clearConnection() { editingConnection = null; connectionForm.reset(); kind.disabled = false; $("integration-token").value = ""; $("integration-save").textContent = "Add connection"; renderConnectionCameras(); }
    function renderConnections(data) {
      if (!Array.isArray(data?.connections) || data.connections.length > 64) throw new Error("Invalid integrations response"); connections = data.connections;
      const cards = connections.map((c) => {
        if (typeof c.id !== "string" || typeof c.name !== "string" || typeof c.origin !== "string") throw new Error("Invalid connection");
        const card = element("article", "", { className: "settings-card", "data-connection-id": c.id }); card.append(element("h3", c.name), element("p", `${c.kind} · ${c.origin}`, { className: "muted" }));
        card.append(element("p", `Status: ${c.status || "unverified"} · ${c.enabled ? "Enabled" : "Disabled"} · Token: ${c.credential_configured ? "configured" : "not configured"}\nLast capability test: ${timestamp(c.last_test_utc)}\nQueued events: ${Number.isInteger(c.queued_events) ? c.queued_events : 0} · Delivered: ${Number.isInteger(c.delivered_events) ? c.delivered_events : 0} · Exhausted retries: ${Number.isInteger(c.exhausted_events) ? c.exhausted_events : 0}`, { className: "settings-status" }));
        const id = encodeURIComponent(c.id);
        card.append(element("p", `Provider handoff: ${window.location.origin}/api/v1/operator/integrations/${id}`, { className: "field-help" }));
        actions(card, button(`integration-test-${c.id}`, "Check provider handshake", () => connectionAction(c, "test")), button(`integration-toggle-${c.id}`, c.enabled ? "Disable delivery" : "Enable delivery", () => connectionAction(c, "toggle")), button(`integration-edit-${c.id}`, "Edit", () => { editingConnection = c.id; $("integration-name").value = c.name; kind.value = c.kind; kind.disabled = true; $("integration-origin").value = c.origin; $("integration-token").value = ""; $("integration-save").textContent = "Save connection"; renderConnectionCameras(c.camera_ids || []); $("integration-name").focus(); }), button(`integration-delete-${c.id}`, "Remove connection", () => connectionAction(c, "delete")));
        if (Number.isInteger(c.exhausted_events) && c.exhausted_events > 0) actions(card, button(`integration-retry-${c.id}`, "Retry exhausted deliveries", () => connectionAction(c, "retry")));
        return card;
      }); $("integration-list").replaceChildren(...(cards.length ? cards : [element("p", "No external service connections configured. Built-in person detection remains available in camera History.", { className: "settings-empty" })]));
    }
    function loadConnections() { renderConnectionCameras(editingConnection ? connections.find((c) => c.id === editingConnection)?.camera_ids || [] : []); return run("integrations", (signal) => request(`${PREFIX}/integrations`, { signal }), (d) => { renderConnections(d); message("integrations"); }); }
    connectionForm.addEventListener("submit", (event) => {
      event.preventDefault(); const body = { name: $("integration-name").value.trim(), origin: $("integration-origin").value.trim(), camera_ids: Array.from(connectionCameras.querySelectorAll("input:checked"), (n) => n.value) }; if (!editingConnection) { body.kind = kind.value; body.enabled = false; } if ($("integration-token").value) body.token = $("integration-token").value;
      const path = `${PREFIX}/integrations${editingConnection ? `/${encodeURIComponent(editingConnection)}` : ""}`; const method = editingConnection ? "PATCH" : "POST"; $("integration-token").value = "";
      void run("integrations", async (signal) => { try { const result = await request(path, { method, body, signal }); return { result, list: await request(`${PREFIX}/integrations`, { signal }) }; } finally { body.token = ""; } }, ({ result, list }) => { clearConnection(); renderConnections(list); showProviderToken(result?.setup_token); message("integrations", "Connection configured. Waiting for a compatible provider handshake before delivery is ready."); });
    });
    function connectionAction(c, action) { void run("integrations", async (signal) => { const p = `${PREFIX}/integrations/${encodeURIComponent(c.id)}`; const result = await request(["test", "retry"].includes(action) ? `${p}/${action}` : p, { method: ["test", "retry"].includes(action) ? "POST" : action === "delete" ? "DELETE" : "PATCH", ...(action === "delete" ? {} : { body: ["test", "retry"].includes(action) ? {} : { enabled: !c.enabled } }), signal }); return { result, list: await request(`${PREFIX}/integrations`, { signal }) }; }, ({ result, list }) => { renderConnections(list); message("integrations", action === "test" ? result?.compatible === true ? "The provider has a verified compatible K5 handshake." : "Waiting for a compatible provider handshake. A remote inference connection has not been established." : action === "retry" ? "Delivery retry budget renewed. Original observation identifiers are preserved; monitor provider delivery status." : action === "delete" ? "Connection removed." : c.enabled ? "Event delivery disabled." : "Provider enabled. Delivery remains dependent on its compatible handshake and explicit camera grants.", action === "test" && result?.compatible !== true); }); }

    const rolesPanel = section("roles", "Custom roles", "Start from a built-in role and narrow its permissions. Camera assignments continue to apply independently. Creating a profile never grants more authority than its base role.");
    const rolesForm = element("form", "", { id: "roles-form" }), rolesGrid = element("div", "", { className: "settings-grid" });
    field(rolesGrid, "roles-name", "Role name", { required: "", maxlength: "64" }); const baseRole = field(rolesGrid, "roles-base", "Base role", { type: "select" }); options(baseRole, [["viewer", "Viewer"], ["operator", "Operator"], ["administrator", "Administrator"]]);
    field(rolesGrid, "roles-description", "Description", { maxlength: "256" }); rolesForm.append(rolesGrid, element("div", "", { id: "roles-permissions", className: "settings-grid" }));
    actions(rolesForm, element("button", "Create role", { id: "roles-save", type: "submit", className: "button primary" }), button("roles-reset", "New role", () => clearRole()), button("roles-delete", "Delete role", () => deleteRole()), button("roles-refresh", "Refresh roles", () => loadRoles())); rolesPanel.append(rolesForm, element("div", "", { id: "roles-list", className: "settings-cards" }));
    function permissionChoices(selected = null) {
      const choices = roleData?.base_permissions?.[baseRole.value] || [];
      const chosen = new Set(selected ?? choices); const nodes = choices.map((permission) => { const label = element("label", "", { className: "settings-check" }); const box = element("input", "", { type: "checkbox", value: permission }); box.checked = chosen.has(permission); label.append(box, element("span", permission.replaceAll(".", " · ").replaceAll("_", " "))); return label; }); $("roles-permissions").replaceChildren(...nodes);
    }
    baseRole.addEventListener("change", () => permissionChoices());
    function clearRole() { editingRole = null; rolesForm.reset(); baseRole.disabled = false; $("roles-save").textContent = "Create role"; permissionChoices(); updateDisabled("roles"); }
    function renderRoles(d) {
      if (!Array.isArray(d?.roles) || !d.base_permissions) throw new Error("Invalid role response"); roleData = d;
      $("roles-list").replaceChildren(...d.roles.map((r) => { const card = element("article", "", { className: "settings-card", "data-role-id": r.id }); card.append(element("h3", r.name), element("p", r.description || "", { className: "muted" }), element("p", `${r.base_role} · ${r.permissions?.length || 0} permissions`)); actions(card, button(`roles-edit-${r.id}`, "Edit role", () => { editingRole = r.id; $("roles-name").value = r.name; baseRole.value = r.base_role; $("roles-description").value = r.description || ""; permissionChoices(r.permissions); $("roles-save").textContent = "Save role"; baseRole.disabled = true; updateDisabled("roles"); })); return card; })); permissionChoices(editingRole ? d.roles.find((r) => r.id === editingRole)?.permissions : null);
    }
    function loadRoles() { return run("roles", (signal) => request(`${PREFIX}/roles`, { signal }), (d) => { renderRoles(d); message("roles"); }); }
    rolesForm.addEventListener("submit", (event) => { event.preventDefault(); const body = { ...(editingRole ? { expected_revision: roleData.revision } : {}), name: $("roles-name").value.trim(), description: $("roles-description").value.trim(), base_role: baseRole.value, permissions: Array.from($("roles-permissions").querySelectorAll("input:checked"), (n) => n.value) }; const path = `${PREFIX}/roles${editingRole ? `/${encodeURIComponent(editingRole)}` : ""}`; void run("roles", async (signal) => { await request(path, { method: editingRole ? "PATCH" : "POST", body, signal }); return request(`${PREFIX}/roles`, { signal }); }, (d) => { editingRole = null; rolesForm.reset(); baseRole.disabled = false; $("roles-save").textContent = "Create role"; renderRoles(d); message("roles", "Custom role saved. Assign it from the account editor."); }); });
    function deleteRole() { if (!editingRole) return; const id = editingRole; void run("roles", async (signal) => { await request(`${PREFIX}/roles/${encodeURIComponent(id)}?expected_revision=${roleData.revision}`, { method: "DELETE", signal }); return request(`${PREFIX}/roles`, { signal }); }, (d) => { editingRole = null; rolesForm.reset(); baseRole.disabled = false; $("roles-save").textContent = "Create role"; renderRoles(d); message("roles", "Custom role removed."); }); }

    const ssoPanel = section("sso", "Organization sign-in", "Use an OpenID Connect identity provider with PKCE and signed ID tokens. Each provider subject must be explicitly linked to an existing K5 account; local sign-in remains available.");
    health(ssoPanel, "sso-health");
    ssoPanel.append(element("p", "Changing the identity provider requires an administrator with every recorder permission. You can link only accounts whose permissions are within your own.", { id: "sso-authority", className: "field-help", hidden: "" }));
    field(ssoPanel, "sso-callback-uri", "Redirect URI to register with your identity provider", { readonly: "", help: "Register this exact local callback URI in the identity-provider application." });
    const ssoForm = element("form", "", { id: "sso-provider-form", autocomplete: "off" }), ssoGrid = element("div", "", { className: "settings-grid" });
    field(ssoGrid, "sso-display-name", "Organization sign-in name", { required: "", maxlength: "80", placeholder: "Your organization" });
    field(ssoGrid, "sso-issuer", "OpenID Connect issuer", { type: "url", required: "", maxlength: "2048", placeholder: "https://identity.example.org" });
    field(ssoGrid, "sso-client-id", "Client ID", { required: "", maxlength: "256" });
    field(ssoGrid, "sso-client-secret", "Client secret · optional", { type: "password", maxlength: "2048", autocomplete: "new-password", help: "Write-only. Leave blank to preserve a configured secret." });
    field(ssoGrid, "sso-groups-claim", "Groups claim", { value: "groups", maxlength: "64", required: "" });
    const ssoEnabled = element("label", "", { className: "settings-check" }); ssoEnabled.append(element("input", "", { id: "sso-enabled", type: "checkbox" }), element("span", "Enable organization sign-in"));
    const clearSsoSecret = element("label", "", { className: "settings-check" }); clearSsoSecret.append(element("input", "", { id: "sso-clear-secret", type: "checkbox" }), element("span", "Remove the saved client secret"));
    ssoForm.append(ssoGrid, ssoEnabled, clearSsoSecret); actions(ssoForm, element("button", "Save identity provider", { id: "sso-save", type: "submit", className: "button primary" }), button("sso-test", "Test identity provider", () => testSso()), button("sso-refresh", "Refresh organization sign-in", () => loadSso())); ssoPanel.append(ssoForm);
    ssoPanel.append(element("h3", "Link an existing account"));
    const ssoLinkForm = element("form", "", { id: "sso-link-form" }), linkGrid = element("div", "", { className: "settings-grid" });
    field(linkGrid, "sso-link-subject", "Provider subject (sub)", { required: "", maxlength: "256", help: "Use the stable subject issued by this identity provider, not an unverified email address." });
    const linkUser = field(linkGrid, "sso-link-user", "Existing K5 account", { type: "select", required: "" });
    field(linkGrid, "sso-link-group", "Required group · optional", { maxlength: "128", help: "When set, the verified identity must also contain this group." });
    ssoLinkForm.append(linkGrid); actions(ssoLinkForm, element("button", "Link account", { id: "sso-link-save", type: "submit", className: "button primary" }), button("sso-more-users", "Load more accounts", () => moreSsoUsers()));
    ssoPanel.append(ssoLinkForm, element("div", "", { id: "sso-links", className: "settings-cards" }));
    let ssoProvider = null, ssoUsers = [], ssoLinks = [], ssoUsersOffset = 0, ssoMoreUsers = false;
    function canConfigureSso() { return ALL_PERMISSIONS.every(permitted); }
    function canLinkSsoUser(user) {
      return Array.isArray(user?.effective_permissions) && user.effective_permissions.length <= ALL_PERMISSIONS.length
        && user.effective_permissions.every((p) => ALL_PERMISSIONS.includes(p) && permitted(p));
    }
    function renderSsoProvider(d) {
      if (typeof d?.configured !== "boolean" || typeof d.enabled !== "boolean") throw new Error("Invalid SSO provider response");
      const redirect = d.redirect_uri || ssoProvider?.redirect_uri || ""; ssoProvider = { ...d, redirect_uri: redirect }; $("sso-callback-uri").value = redirect; $("sso-display-name").value = d.display_name || ""; $("sso-issuer").value = d.issuer || ""; $("sso-client-id").value = d.client_id || ""; $("sso-groups-claim").value = d.groups_claim || "groups"; $("sso-enabled").checked = d.enabled; $("sso-client-secret").value = ""; $("sso-clear-secret").checked = false;
      $("sso-health").textContent = `${d.configured ? "Identity provider configured" : "No identity provider configured"} · ${d.enabled ? "Enabled" : "Disabled"}\nClient secret: ${d.has_client_secret ? "configured" : "not configured"}. A saved configuration is not a completed provider verification.`;
    }
    function renderSsoUsers(d, append = false) {
      if (!Array.isArray(d?.users) || typeof d.has_more !== "boolean") throw new Error("Invalid account response");
      const previous = linkUser.value; ssoUsers = append ? [...ssoUsers, ...d.users] : d.users;
      ssoUsersOffset = append ? ssoUsersOffset + d.users.length : d.users.length; ssoMoreUsers = d.has_more;
      options(linkUser, [["", "Choose an existing account"], ...ssoUsers.map((u) => [u.id, `${u.display_name || u.username} · ${u.username} · ${u.role}${u.enabled ? "" : " · disabled"}`])]);
      if (ssoUsers.some((u) => u.id === previous)) linkUser.value = previous; $("sso-more-users").hidden = !ssoMoreUsers;
    }
    function renderSsoLinks(d) {
      const links = Array.isArray(d) ? d : d?.links; if (!Array.isArray(links) || links.length > 10000) throw new Error("Invalid SSO links"); ssoLinks = links;
      $("sso-links").replaceChildren(...(links.length ? links.map((link) => { const card = element("article", "", { className: "settings-card" }); const known = ssoUsers.find((u) => u.id === link.user_id); card.append(element("h3", known?.username || link.user_id), element("p", `Issuer: ${link.issuer}\nSubject: ${link.subject}\nRequired group: ${link.required_group || "none"}`, { className: "settings-status" })); actions(card, button(`sso-unlink-${link.id}`, "Remove identity link", () => unlinkSso(link.id))); return card; }) : [element("p", "No organization identities linked. Identity-provider users cannot sign in until an administrator links their subject to a K5 account.", { className: "settings-empty" })]));
    }
    function loadSso() { return run("sso", async (signal) => { const provider = await request(`${PREFIX}/sso/provider`, { signal }); const users = await request(`${PREFIX}/users?offset=0&limit=100`, { signal }); const links = await request(`${PREFIX}/sso/links`, { signal }); return { provider, users, links }; }, (d) => { renderSsoProvider(d.provider); renderSsoUsers(d.users); renderSsoLinks(d.links); message("sso"); }); }
    function moreSsoUsers() { if (!ssoMoreUsers) return; void run("sso", (signal) => request(`${PREFIX}/users?offset=${ssoUsersOffset}&limit=100`, { signal }), (d) => { renderSsoUsers(d, true); message("sso"); }); }
    ssoForm.addEventListener("submit", (event) => { event.preventDefault(); if (!canConfigureSso()) { $("sso-client-secret").value = ""; message("sso", "Changing the identity provider requires every recorder permission.", true); return; } const body = { enabled: $("sso-enabled").checked, display_name: $("sso-display-name").value.trim(), issuer: $("sso-issuer").value.trim(), client_id: $("sso-client-id").value.trim(), groups_claim: $("sso-groups-claim").value.trim() }; if ($("sso-clear-secret").checked) body.client_secret = ""; else if ($("sso-client-secret").value) body.client_secret = $("sso-client-secret").value; $("sso-client-secret").value = ""; void run("sso", async (signal) => { try { return await request(`${PREFIX}/sso/provider`, { method: "PUT", body, signal }); } finally { body.client_secret = ""; } }, (d) => { renderSsoProvider(d); message("sso", "Identity-provider configuration saved. Test the provider and link accounts before relying on organization sign-in."); }); });
    function testSso() { void run("sso", (signal) => request(`${PREFIX}/sso/provider/test`, { method: "POST", body: {}, signal, timeout: 30000 }), (d) => { const verified = d?.reachable === true && d.compatible === true && d.issuer_verified === true && d.pkce_s256 === true && d.signing_algorithm === "RS256"; message("sso", verified ? "The identity provider passed issuer, PKCE and signing-capability verification. A linked user's actual sign-in must still complete normally." : "The identity provider did not establish all required OpenID Connect capabilities.", !verified); }); }
    ssoLinkForm.addEventListener("submit", (event) => { event.preventDefault(); if (!canLinkSsoUser(ssoUsers.find((u) => u.id === linkUser.value))) { message("sso", "Choose an account whose permissions are within your own.", true); return; } if (!ssoProvider?.issuer) { message("sso", "Configure and save an identity provider first.", true); return; } const body = { subject: $("sso-link-subject").value.trim(), user_id: linkUser.value }; if ($("sso-link-group").value.trim()) body.required_group = $("sso-link-group").value.trim(); void run("sso", async (signal) => { await request(`${PREFIX}/sso/links`, { method: "POST", body, signal }); return request(`${PREFIX}/sso/links`, { signal }); }, (d) => { renderSsoLinks(d); ssoLinkForm.reset(); message("sso", "Organization identity linked to the existing account. Its current role, camera access and MFA still apply."); }); });
    function unlinkSso(id) { if (!canLinkSsoUser(ssoUsers.find((u) => u.id === ssoLinks.find((link) => link.id === id)?.user_id))) { message("sso", "This account's permissions exceed your current authority.", true); return; } void run("sso", async (signal) => { await request(`${PREFIX}/sso/links/${encodeURIComponent(id)}`, { method: "DELETE", signal }); return request(`${PREFIX}/sso/links`, { signal }); }, (d) => { renderSsoLinks(d); message("sso", "Organization identity link removed."); }); }

    const securityPanel = section("security", "Account security", "Protect your own account with an authenticator. Recovery codes are shown only when generated; keep them privately outside this recorder.");
    health(securityPanel, "security-health");
    const password = field(securityPanel, "security-password", "Current password", { type: "password", autocomplete: "current-password", maxlength: "256" });
    const code = field(securityPanel, "security-code", "Authenticator or recovery code", { autocomplete: "one-time-code", maxlength: "128" });
    actions(securityPanel, button("security-enroll", "Set up authenticator", () => enrollMfa(), true), button("security-disable", "Disable authenticator", () => securityMutation("disable")), button("security-regenerate", "Generate replacement recovery codes", () => securityMutation("recovery-codes")), button("security-refresh", "Refresh account security", () => loadSecurity()));
    const enrollment = element("div", "", { id: "security-enrollment", className: "settings-secret settings-divider", hidden: "" }); enrollment.append(element("p", "Add this secret manually to your authenticator, then enter its current six-digit code below. This setup expires after five minutes."), element("code", "", { id: "security-secret" }));
    field(enrollment, "security-confirm-code", "Current six-digit authenticator code", { autocomplete: "one-time-code", inputmode: "numeric", pattern: "[0-9]{6}", maxlength: "6" }); actions(enrollment, button("security-confirm", "Confirm authenticator", () => confirmMfa(), true), button("security-enrollment-cancel", "Cancel setup", () => clearEnrollment())); securityPanel.append(enrollment);
    const recovery = element("div", "", { id: "security-recovery", className: "settings-secret settings-divider", hidden: "" }); recovery.append(element("p", "Save these recovery codes now. Each code works once; they will not be displayed again."), element("div", "", { id: "security-recovery-codes" })); actions(recovery, button("security-recovery-done", "I saved my recovery codes", () => { clearRecovery(); if (mustReauthenticate) { mustReauthenticate = false; onReauthenticate(); } })); securityPanel.append(recovery);
    function clearEnrollment() { enrollmentToken = ""; $("security-secret").textContent = ""; $("security-confirm-code").value = ""; enrollment.hidden = true; }
    function clearRecovery() { $("security-recovery-codes").replaceChildren(); recovery.hidden = true; }
    function showRecovery(values) { if (!Array.isArray(values) || !values.length || values.length > 32 || !values.every((v) => typeof v === "string" && v.length <= 128)) throw new Error("Invalid recovery codes"); clearRecovery(); $("security-recovery-codes").append(...values.map((v) => element("code", v))); recovery.hidden = false; }
    function loadSecurity() { return run("security", (signal) => request(SECURITY, { signal }), (d) => { if (typeof d?.mfa_enabled !== "boolean") throw new Error("Invalid security status"); $("security-health").textContent = `Authenticator: ${d.mfa_enabled ? "enabled" : "not enabled"}\nUnused recovery codes: ${Number.isInteger(d.recovery_codes_remaining) ? d.recovery_codes_remaining : "Unavailable"}`; $("security-enroll").hidden = d.mfa_enabled; $("security-disable").hidden = !d.mfa_enabled; $("security-regenerate").hidden = !d.mfa_enabled; message("security"); }); }
    function enrollMfa() { const body = { current_password: password.value }; password.value = ""; clearEnrollment(); clearRecovery(); void run("security", async (signal) => { try { return await request(`${SECURITY}/mfa/enroll`, { method: "POST", body, signal }); } finally { body.current_password = ""; } }, (d) => { if (typeof d?.enrollment_token !== "string" || typeof d.secret !== "string" || d.secret.length > 128) throw new Error("Invalid enrollment"); enrollmentToken = d.enrollment_token; $("security-secret").textContent = d.secret; enrollment.hidden = false; $("security-confirm-code").focus(); message("security", "Authenticator setup is prepared. It becomes active only after confirmation."); }); }
    function confirmMfa() { const body = { enrollment_token: enrollmentToken, code: $("security-confirm-code").value.trim() }; $("security-confirm-code").value = ""; if (!body.enrollment_token) return; onSecurityTransition(true); void run("security", async (signal) => { try { return await request(`${SECURITY}/mfa/confirm`, { method: "POST", body, signal }); } catch (error) { onSecurityTransition(false); throw error; } finally { body.code = ""; body.enrollment_token = ""; } }, (d) => { clearEnrollment(); showRecovery(d.recovery_codes); mustReauthenticate = d.reauthentication_required === true; message("security", "Authenticator enabled. Save the recovery codes, then sign in again."); }); }
    function securityMutation(action) { const body = { current_password: password.value, code: code.value.trim() }; password.value = ""; code.value = ""; clearEnrollment(); clearRecovery(); void run("security", async (signal) => { try { return await request(`${SECURITY}/mfa/${action}`, { method: "POST", body, signal }); } finally { body.current_password = ""; body.code = ""; } }, (d) => { if (action === "recovery-codes") { showRecovery(d.recovery_codes); message("security", "Replacement recovery codes generated. Earlier recovery codes no longer work."); } else { message("security", "Authenticator disabled. Sign in again to continue."); if (d.reauthentication_required === true) onReauthenticate(); } }); }

    const loaders = { storage: loadStorage, time: loadTime, activity: loadAudit, integrations: loadConnections, roles: loadRoles, sso: loadSso, security: loadSecurity };
    for (const [name, label, permission] of SECTIONS) { const tab = button(`settings-${name}-tab`, label, () => select(name)); tab.className = "tab"; $("settings-tabs").append(tab); tabs.set(name, tab); }
    function select(name) {
      if (mustReauthenticate) { message("security", "Save your recovery codes, then select I saved my recovery codes to sign in again."); return; }
      const entry = SECTIONS.find((s) => s[0] === name); if (!entry || !permitted(entry[2]) || !activeSession()) return;
      controller?.abort(); controller = new AbortController(); epoch += 1; currentSection = name; busy.clear();
      if (name !== "security") { clearEnrollment(); clearRecovery(); password.value = ""; code.value = ""; }
      if (name !== "integrations") { $("integration-setup-token-value").textContent = ""; providerSecret.hidden = true; $("integration-token").value = ""; }
      if (name !== "sso") $("sso-client-secret").value = "";
      if (name !== "time") clearCameraCredentials();
      for (const [key, tab] of tabs) { const chosen = key === name; tab.classList.toggle("active", chosen); if (chosen) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current"); panels.get(key).hidden = !chosen; }
      $("settings-title").textContent = name === "security" ? "Account security" : "Recorder settings";
      void loaders[name]();
    }
    function hide() { mustReauthenticate = false; authoritySuspended = false; visible = false; epoch += 1; controller?.abort(); controller = null; busy.clear(); clearEnrollment(); clearRecovery(); clearConnection(); clearCameraCredentials(); $("integration-setup-token-value").textContent = ""; providerSecret.hidden = true; $("sso-client-secret").value = ""; password.value = ""; code.value = ""; clearDownload(); for (const n of panels.values()) { n.hidden = true; n.setAttribute("aria-busy", "false"); } for (const name of messages.keys()) message(name); }
    return {
      show(preferred = null) { hide(); if (!activeSession()) return; visible = true; for (const [name, , permission] of SECTIONS) tabs.get(name).hidden = !permitted(permission); const chosen = SECTIONS.find((s) => s[0] === preferred && permitted(s[2])) || SECTIONS.find((s) => permitted(s[2])); select(chosen[0]); },
      hide, refreshCameras() { if (visible && currentSection === "time") renderCameraOptions(); },
      refreshAuthority() {
        if (!visible) return;
        if (!activeSession()) {
          if (!authoritySuspended) { authoritySuspended = true; epoch += 1; controller?.abort(); controller = null; busy.clear(); }
          for (const tab of tabs.values()) tab.disabled = true;
          for (const panel of panels.values()) for (const node of panel.querySelectorAll("button,input,select,textarea")) node.disabled = true;
          message(currentSection, "Current account permissions could not be verified. Changes are paused until a successful refresh.", true);
          return;
        }
        for (const [name, , permission] of SECTIONS) { tabs.get(name).hidden = !permitted(permission); tabs.get(name).disabled = false; }
        if (authoritySuspended) {
          authoritySuspended = false; controller = new AbortController();
          for (const node of panels.get(currentSection).querySelectorAll("button,input,select,textarea")) node.disabled = false;
          message(currentSection); updateDisabled(currentSection);
        }
        if (!busy.has(currentSection)) updateDisabled(currentSection);
        const active = SECTIONS.find((s) => s[0] === currentSection);
        if (!permitted(active[2])) select(SECTIONS.find((s) => permitted(s[2]))[0]);
      },
    };
  }
  window.K5Settings = Object.freeze({ create, errorCodes: Object.freeze(Object.keys(ERRORS)) });
})();
