/* K5 account administration. Credentials are displayed once and kept out of storage. */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const PREFIX = "/api/v1/operator/admin";
  const PAGE_SIZE = 25;
  const ROLES = new Set(["viewer", "operator", "administrator"]);
  const ERRORS = {
    admin_last_administrator: "Activate another enabled administrator before changing or resetting the last administrator.",
    admin_username_exists: "That username already exists. Choose a different username.",
    admin_capacity: "The account limit has been reached.",
    admin_invalid_request: "Check the account fields and camera selection, then try again.",
    admin_user_not_found: "This account is no longer available. Refresh the user list.",
    admin_unavailable: "Account administration is unavailable. Try refreshing shortly.",
  };

  function create({ request, getSession, getCameras, onSelfChanged }) {
    let visible = false, epoch = 0, controller = null, busy = false;
    let users = [], usersOffset = 0, auditOffset = 0;
    let usersRequest = 0, auditRequest = 0;
    let editTarget = null, resetTarget = null, previousRole = "operator";

    function message(id, text = "", error = false) {
      $(id).textContent = text;
      $(id).classList.toggle("error", error);
    }

    function errorText(error) {
      if (ERRORS[error.code]) return ERRORS[error.code];
      if (error.status === 401) return "Your session has ended. Sign in again.";
      if (error.status === 403) return "Administrator permission is required for this action.";
      if (error.status === 429) return "Too many requests. Wait a moment, then try again.";
      if (error.status === 409) return "The account could not be changed. Keep another activated administrator available and refresh the user list.";
      if (error.status === 422) return "Check the account fields and camera selection, then try again.";
      return "Account administration is unavailable. Try refreshing shortly.";
    }

    function session() {
      const value = getSession();
      return value?.token && value.account?.role === "administrator" ? value : null;
    }

    function current(start, version) {
      const value = session();
      return visible && version === epoch && value && value.token === start.token && value.epoch === start.epoch;
    }

    function validRow(row) {
      return row && typeof row.id === "string" && row.id.length <= 64
        && typeof row.username === "string" && typeof row.display_name === "string"
        && ROLES.has(row.role) && typeof row.enabled === "boolean"
        && ["active", "pending", "expired", "uninitialized"].includes(row.credential_status)
        && row.camera_access && ["all", "selected"].includes(row.camera_access.mode)
        && Array.isArray(row.camera_access.camera_ids) && row.camera_access.camera_ids.length <= 64
        && row.camera_access.camera_ids.every((id) => typeof id === "string");
    }

    function cell(text, className = "") {
      const node = document.createElement("td");
      node.textContent = text;
      if (className) node.className = className;
      return node;
    }

    function accountStatus(row) {
      const credential = { active: "Password active", pending: "Password setup pending", expired: "Temporary code expired", uninitialized: "Password not set" }[row.credential_status];
      return `${row.enabled ? "Enabled" : "Disabled"} · ${credential}`;
    }

    function renderUsers() {
      const fragment = document.createDocumentFragment();
      for (const row of users) {
        const tr = document.createElement("tr"); tr.dataset.userId = row.id;
        const account = cell("");
        const name = document.createElement("strong"); name.textContent = row.display_name;
        const username = document.createElement("span"); username.className = "admin-user-secondary";
        username.textContent = row.username + (row.id === session()?.account.id ? " · You" : "");
        account.append(name, username);
        const scope = row.role === "administrator" || row.camera_access.mode === "all"
          ? "All cameras" : `${row.camera_access.camera_ids.length} selected ${row.camera_access.camera_ids.length === 1 ? "camera" : "cameras"}`;
        const actions = cell("");
        for (const [action, label] of [["edit", "Edit account"], ["reset", "Reset password"]]) {
          const button = document.createElement("button");
          button.type = "button"; button.className = "button secondary";
          button.dataset.userAction = action; button.textContent = label;
          if (action === "reset" && row.id === session()?.account.id) {
            button.disabled = true; button.title = "Ask another administrator to reset your password.";
          }
          button.addEventListener("click", () => action === "edit" ? openEditor(row) : openReset(row));
          actions.append(button);
        }
        tr.append(account, cell(row.role), cell(accountStatus(row)), cell(scope), actions);
        fragment.append(tr);
      }
      $("admin-users-list").replaceChildren(fragment);
      $("admin-users-empty").hidden = users.length !== 0;
    }

    async function loadUsers(offset = usersOffset) {
      const start = session(), version = epoch, sequence = ++usersRequest;
      if (!start || !visible) return;
      $("admin-refresh-users").disabled = true;
      message("admin-message", "Loading accounts…");
      try {
        const data = await request(`${PREFIX}/users?offset=${offset}&limit=${PAGE_SIZE}`, { signal: controller.signal });
        if (!current(start, version) || sequence !== usersRequest) return;
        if (!Array.isArray(data.users) || data.users.length > PAGE_SIZE || !data.users.every(validRow) || typeof data.has_more !== "boolean") throw new Error("Invalid account response");
        users = data.users; usersOffset = offset; renderUsers();
        $("admin-users-previous").disabled = offset === 0;
        $("admin-users-next").disabled = !data.has_more;
        $("admin-users-page").textContent = users.length ? `Accounts ${offset + 1}–${offset + users.length}` : "No accounts on this page";
        message("admin-message");
      } catch (error) {
        if (error.name !== "AbortError" && current(start, version) && sequence === usersRequest) message("admin-message", errorText(error), true);
      } finally {
        if (current(start, version) && sequence === usersRequest) $("admin-refresh-users").disabled = false;
      }
    }

    async function loadAudit(offset = auditOffset) {
      const start = session(), version = epoch, sequence = ++auditRequest;
      if (!start || !visible) return;
      $("admin-refresh-audit").disabled = true;
      message("admin-message", "Loading account activity…");
      try {
        const data = await request(`${PREFIX}/audit?offset=${offset}&limit=${PAGE_SIZE}`, { signal: controller.signal });
        if (!current(start, version) || sequence !== auditRequest) return;
        if (!Array.isArray(data.events) || data.events.length > PAGE_SIZE || typeof data.has_more !== "boolean") throw new Error("Invalid activity response");
        const fragment = document.createDocumentFragment();
        for (const event of data.events) {
          if (!event || typeof event.occurred_at !== "string" || typeof event.actor !== "string" || typeof event.user_id !== "string" || typeof event.action !== "string" || !Array.isArray(event.changed_fields) || !event.changed_fields.every((name) => typeof name === "string")) throw new Error("Invalid activity response");
          const tr = document.createElement("tr"); tr.dataset.eventId = String(event.event_id);
          const known = users.find((user) => user.id === event.user_id);
          const time = new Date(event.occurred_at);
          tr.append(cell(Number.isFinite(time.getTime()) ? time.toISOString().replace("T", " ").replace("Z", " UTC") : "Time unavailable"), cell(event.actor), cell(typeof event.username === "string" ? event.username : known?.username || event.user_id), cell(event.action.replaceAll("_", " ")), cell(event.changed_fields.join(", ")));
          fragment.append(tr);
        }
        $("admin-audit-list").replaceChildren(fragment);
        $("admin-audit-empty").hidden = data.events.length !== 0;
        auditOffset = offset;
        $("admin-audit-previous").disabled = offset === 0;
        $("admin-audit-next").disabled = !data.has_more;
        $("admin-audit-page").textContent = data.events.length ? `Events ${offset + 1}–${offset + data.events.length}` : "No events on this page";
        message("admin-message");
      } catch (error) {
        if (error.name !== "AbortError" && current(start, version) && sequence === auditRequest) message("admin-message", errorText(error), true);
      } finally {
        if (current(start, version) && sequence === auditRequest) $("admin-refresh-audit").disabled = false;
      }
    }

    function setSection(next) {
      for (const name of ["users", "audit"]) {
        $(`admin-${name}-panel`).hidden = name !== next;
        $(`admin-${name}-tab`).classList.toggle("active", name === next);
        if (name === next) $(`admin-${name}-tab`).setAttribute("aria-current", "page");
        else $(`admin-${name}-tab`).removeAttribute("aria-current");
      }
      void (next === "users" ? loadUsers() : loadAudit());
    }

    function renderCameraChoices(ids = []) {
      const chosen = new Set(ids), fragment = document.createDocumentFragment();
      const cameras = getCameras();
      const missing = ids.filter((id) => !cameras.some((camera) => camera.device_id === id));
      for (const camera of [...cameras, ...missing.map((id) => ({ device_id: id, name: `Unavailable camera · ${id}` }))]) {
        const label = document.createElement("label"); label.className = "admin-checkbox";
        const input = document.createElement("input"); input.type = "checkbox"; input.value = camera.device_id;
        input.checked = chosen.has(camera.device_id);
        const name = document.createElement("span"); name.textContent = camera.name;
        label.append(input, name); fragment.append(label);
      }
      $("admin-camera-choices").replaceChildren(fragment);
      renderAccess();
    }

    function renderAccess() {
      const administrator = $("admin-role").value === "administrator";
      if (administrator) $("admin-access-mode").value = "all";
      $("admin-access-mode").disabled = busy || administrator;
      $("admin-camera-choices").hidden = administrator || $("admin-access-mode").value !== "selected";
      $("admin-access-help").textContent = administrator ? "Administrators can access and manage every camera."
        : $("admin-access-mode").value === "all" ? "This account can access current cameras and cameras added later."
          : "Choose the cameras this user may view. Selecting none gives this account no camera access.";
      $("admin-own-change").hidden = editTarget?.id !== session()?.account.id;
    }

    function setBusy(value) {
      busy = value;
      for (const id of ["admin-user-form", "admin-reset-form"]) {
        $(id).setAttribute("aria-busy", String(value));
        for (const input of $(id).querySelectorAll("input,select,button")) input.disabled = value;
      }
      renderAccess();
    }

    function closeEditor(force = false) {
      if (busy && !force) return;
      if ($("admin-user-dialog").open) $("admin-user-dialog").close();
      $("admin-user-form").reset(); editTarget = null;
      $("admin-camera-choices").replaceChildren(); message("admin-user-message");
    }

    function openEditor(row = null) {
      if (!session() || !visible || busy) return;
      closeEditor(); editTarget = row;
      $("admin-user-title").textContent = row ? "Edit account" : "Create user";
      $("admin-username").value = row?.username || ""; $("admin-username").readOnly = Boolean(row);
      $("admin-display-name").value = row?.display_name || "";
      $("admin-role").value = row?.role || "operator";
      previousRole = $("admin-role").value;
      $("admin-enabled").checked = row?.enabled !== false;
      $("admin-access-mode").value = row?.camera_access.mode || "selected";
      $("admin-save-user").textContent = row ? "Save account" : "Create user";
      setBusy(false); renderCameraChoices(row?.camera_access.camera_ids || []);
      $("admin-user-dialog").showModal();
      $(row ? "admin-display-name" : "admin-username").focus();
    }

    function closeCredential() {
      $("admin-temporary-credential").value = "";
      $("admin-credential-username").textContent = "";
      $("admin-credential-expiry").textContent = "";
      if ($("admin-credential-dialog").open) $("admin-credential-dialog").close();
    }

    function showCredential(issue) {
      if (!validRow(issue?.account) || typeof issue.temporary_credential !== "string" || !/^[A-Za-z0-9_-]{32,128}$/.test(issue.temporary_credential) || typeof issue.expires_at !== "string" || !Number.isFinite(Date.parse(issue.expires_at))) throw new Error("Invalid temporary credential response");
      closeCredential();
      $("admin-credential-username").textContent = issue.account.username;
      $("admin-temporary-credential").value = issue.temporary_credential;
      issue.temporary_credential = "";
      $("admin-credential-expiry").textContent = `Expires ${new Date(issue.expires_at).toISOString().replace("T", " ").replace("Z", " UTC")}. The code can be used once.${issue.account.enabled ? "" : " Enable this account before password setup."}`;
      $("admin-credential-dialog").showModal();
    }

    function closeReset(force = false) {
      if (busy && !force) return;
      if ($("admin-reset-dialog").open) $("admin-reset-dialog").close();
      resetTarget = null; $("admin-reset-username").textContent = ""; message("admin-reset-message");
    }

    function openReset(row) {
      if (!session() || !visible || busy || row.id === session().account.id) return;
      resetTarget = row; $("admin-reset-username").textContent = row.username;
      message("admin-reset-message"); $("admin-reset-dialog").showModal();
    }

    $("admin-user-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const start = session(), version = epoch, target = editTarget;
      if (!start || busy) return;
      const body = {
        display_name: $("admin-display-name").value.trim(), role: $("admin-role").value,
        enabled: $("admin-enabled").checked,
        camera_access: { mode: $("admin-role").value === "administrator" ? "all" : $("admin-access-mode").value, camera_ids: [] },
      };
      if (body.camera_access.mode === "selected") body.camera_access.camera_ids = Array.from($("admin-camera-choices").querySelectorAll("input:checked"), (input) => input.value);
      if (!target) body.username = $("admin-username").value.trim();
      setBusy(true); message("admin-user-message", "Saving account…");
      try {
        const data = await request(`${PREFIX}/users${target ? "/" + encodeURIComponent(target.id) : ""}`, { method: target ? "PATCH" : "POST", body, signal: controller.signal });
        if (!current(start, version)) return;
        if (target && !validRow(data)) throw new Error("Invalid account response");
        setBusy(false); closeEditor();
        if (!target) showCredential(data);
        else if (target.id === start.account.id) {
          const scope = (value) => JSON.stringify({ mode: value.mode, camera_ids: [...value.camera_ids].sort() });
          const permissionsChanged = target.role !== data.role || target.enabled !== data.enabled
            || scope(target.camera_access) !== scope(data.camera_access);
          onSelfChanged(data, permissionsChanged);
          if (permissionsChanged) return;
        }
        await loadUsers(target ? usersOffset : 0);
      } catch (error) {
        if (error.name !== "AbortError" && current(start, version)) {
          message($("admin-user-dialog").open ? "admin-user-message" : "admin-message", errorText(error), true);
        }
      } finally {
        if (current(start, version)) setBusy(false);
      }
    });

    $("admin-reset-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const start = session(), version = epoch, target = resetTarget;
      if (!start || !target || busy) return;
      setBusy(true); message("admin-reset-message", "Resetting password…");
      try {
        const data = await request(`${PREFIX}/users/${encodeURIComponent(target.id)}/reset-password`, { method: "POST", signal: controller.signal });
        if (!current(start, version)) return;
        setBusy(false); closeReset(); showCredential(data); await loadUsers();
      } catch (error) {
        if (error.name !== "AbortError" && current(start, version)) message("admin-reset-message", errorText(error), true);
      } finally {
        if (current(start, version)) setBusy(false);
      }
    });

    for (const id of ["admin-close-user", "admin-cancel-user"]) $(id).addEventListener("click", () => closeEditor());
    for (const id of ["admin-close-reset", "admin-cancel-reset"]) $(id).addEventListener("click", () => closeReset());
    for (const id of ["admin-close-credential", "admin-credential-done"]) $(id).addEventListener("click", closeCredential);
    $("admin-credential-dialog").addEventListener("close", closeCredential);
    $("admin-user-dialog").addEventListener("cancel", (event) => { if (busy) event.preventDefault(); });
    $("admin-reset-dialog").addEventListener("cancel", (event) => { if (busy) event.preventDefault(); });
    $("admin-user-dialog").addEventListener("close", () => { if (!busy) closeEditor(); });
    $("admin-reset-dialog").addEventListener("close", () => { if (!busy) closeReset(); });
    $("admin-create-user").addEventListener("click", () => openEditor());
    $("admin-role").addEventListener("change", () => {
      if (previousRole === "administrator" && $("admin-role").value !== "administrator") $("admin-access-mode").value = "selected";
      previousRole = $("admin-role").value;
      renderAccess();
    });
    $("admin-access-mode").addEventListener("change", renderAccess);
    $("admin-users-tab").addEventListener("click", () => setSection("users"));
    $("admin-audit-tab").addEventListener("click", () => setSection("audit"));
    $("admin-refresh-users").addEventListener("click", () => void loadUsers());
    $("admin-refresh-audit").addEventListener("click", () => void loadAudit());
    $("admin-users-previous").addEventListener("click", () => void loadUsers(Math.max(0, usersOffset - PAGE_SIZE)));
    $("admin-users-next").addEventListener("click", () => void loadUsers(usersOffset + PAGE_SIZE));
    $("admin-audit-previous").addEventListener("click", () => void loadAudit(Math.max(0, auditOffset - PAGE_SIZE)));
    $("admin-audit-next").addEventListener("click", () => void loadAudit(auditOffset + PAGE_SIZE));

    function hide() {
      visible = false; epoch += 1; controller?.abort(); controller = null;
      setBusy(false); closeEditor(true); closeReset(true); closeCredential();
      users = []; usersOffset = 0; auditOffset = 0;
      $("admin-users-list").replaceChildren(); $("admin-audit-list").replaceChildren();
      message("admin-message");
    }

    return {
      show() {
        hide();
        if (!session()) return;
        visible = true; controller = new AbortController();
        setSection("users");
      },
      hide,
      refreshCameras() {
        if ($("admin-user-dialog").open && !busy) {
          const ids = Array.from($("admin-camera-choices").querySelectorAll("input:checked"), (input) => input.value);
          renderCameraChoices(ids);
        }
      },
    };
  }

  window.K5Admin = Object.freeze({ create });
})();
