/* Deterministic unit tests of the served script with a small DOM/fetch double.
 * Deliberately finish responses after abort to exercise dialog ownership guards.
 * This is not browser, native Windows, camera, or media acceptance evidence.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const source = fs.readFileSync(path.join(root, "src/k5vision/operator_web/app.js"), "utf8");
const html = fs.readFileSync(path.join(root, "src/k5vision/operator_web/index.html"), "utf8");
const turn = () => new Promise((resolve) => setImmediate(resolve));
const reply = (body, status = 200) => ({ ok: status < 400, status, json: async () => body });

function harness() {
  const elements = new Map();
  const closeEvents = [];
  const posts = [];
  const requests = [];
  const timers = new Map();
  let nextTimer = 0;
  let cameras = [];
  let pendingList = null;
  class Element {
    constructor(id) {
      this.id = id;
      this.value = "";
      this.textContent = "";
      this.disabled = false;
      this.readOnly = false;
      this.open = false;
      this.hidden = false;
      this.listeners = new Map();
      this.attributes = new Map();
      this.classList = { toggle() {}, add() {}, remove() {} };
    }
    addEventListener(name, handler) {
      if (!this.listeners.has(name)) this.listeners.set(name, []);
      this.listeners.get(name).push(handler);
    }
    async dispatch(name) {
      const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
      for (const handler of this.listeners.get(name) || []) await handler(event);
      if (name === "cancel" && !event.defaultPrevented) this.close();
    }
    reset() {
      if (this.id === "add-camera-form") {
        for (const id of ["new-camera-name", "new-camera-host", "new-camera-source"]) get(id).value = "";
      }
    }
    close() {
      if (!this.open) return;
      this.open = false;
      closeEvents.push(() => this.dispatch("close"));
    }
    showModal() { this.open = true; }
    focus() {}
    pause() {}
    load() {}
    play() { return Promise.resolve(); }
    canPlayType() { return ""; }
    append() {}
    replaceChildren() {}
    setAttribute(name, value) { this.attributes.set(name, value); }
    removeAttribute(name) { this.attributes.delete(name); }
  }
  for (const match of html.matchAll(/\bid="([^"]+)"/g)) elements.set(match[1], new Element(match[1]));
  function get(id) { assert.ok(elements.has(id), `Unknown element ${id}`); return elements.get(id); }
  const context = {
    document: {
      getElementById: get,
      createElement: () => new Element(""),
      createDocumentFragment: () => new Element(""),
    },
    window: { addEventListener() {} },
    location: { origin: "http://127.0.0.1:8080" },
    URL, AbortController, AbortSignal,
    setTimeout(handler) { const id = ++nextTimer; timers.set(id, handler); return id; },
    clearTimeout(id) { timers.delete(id); },
    async fetch(url, options) {
      requests.push({ url, options });
      if (url === "/api/v1/auth/login") {
        return reply({ session_token: "test-session", account: {}, expires_at: "2099-01-01T00:00:00Z" });
      }
      if (url === "/api/v1/auth/me") return reply({ role: "administrator", username: "test-admin" });
      if (url === "/api/v1/auth/logout" || options.method === "DELETE") return reply(null, 204);
      if (url === "/api/v1/operator/cameras" && options.method === "POST") {
        return new Promise((resolve, reject) => posts.push({ resolve, reject, options }));
      }
      if (url === "/api/v1/operator/cameras") {
        if (pendingList) return pendingList;
        return reply({ cameras, worker_status: "ready" });
      }
      if (/\/media-session$/.test(url)) return reply({}, 503);
      throw new Error(`Unexpected fetch: ${url}`);
    },
  };
  vm.runInNewContext(source, context, { filename: "operator_web/app.js" });
  return {
    get, posts, requests,
    async login() {
      get("username").value = "test-admin"; get("password").value = "test-only";
      await get("sign-in-form").dispatch("submit");
      assert.equal(get("workspace").hidden, false);
    },
    async logout() {
      await get("sign-out-button").dispatch("click");
      await turn();
      assert.equal(get("workspace").hidden, true);
    },
    async open(mode = "add") {
      await get(mode === "add" ? "add-camera-button" : "update-source-button").dispatch("click");
      assert.equal(get("add-camera-dialog").open, true);
    },
    fill(label = "new") {
      get("new-camera-name").value = label;
      get("new-camera-host").value = "192.0.2.1";
      get("new-camera-source").value = `rtsp://192.0.2.1/${label}`;
    },
    submit() { return get("add-camera-form").dispatch("submit"); },
    async dismiss(kind = "cancel-add-camera") {
      await get(kind === "escape" ? "add-camera-dialog" : kind).dispatch(kind === "escape" ? "cancel" : "click");
    },
    async flushClose() { while (closeEvents.length) await closeEvents.shift()(); },
    setCameras(value) { cameras = value; },
    deferList() { return new Promise((resolve) => { pendingList = new Promise((finish) => resolve(finish)); }); },
  };
}

for (const dismissal of ["close-add-camera", "cancel-add-camera", "escape"]) {
  for (const outcome of ["success", "failure"]) {
    test(`${dismissal}: stale ${outcome} preserves reopened input and state`, async () => {
      const h = harness(); await h.login(); await h.open(); h.fill("first");
      const first = h.submit();
      await h.dismiss(dismissal); await h.open(); h.fill("second");
      await h.flushClose(); // A delayed native close event must not reset B.
      assert.equal(h.get("new-camera-name").value, "second");
      assert.equal(h.get("save-camera").disabled, false);
      assert.equal(h.posts[0].options.signal.aborted, true);
      h.posts[0].resolve(reply({}, outcome === "success" ? 201 : 422));
      await first;
      assert.equal(h.get("add-camera-dialog").open, true);
      assert.equal(h.get("new-camera-source").value, "rtsp://192.0.2.1/second");
      assert.equal(h.get("add-camera-message").textContent, "");
      assert.equal(h.get("workspace-message").textContent, "");
      assert.equal(h.get("save-camera").disabled, false);
    });
  }
}

test("stale failure cannot enable a newer pending submit or replace its message", async () => {
  const h = harness(); await h.login(); await h.open(); h.fill("first");
  const first = h.submit(); await h.dismiss(); await h.open(); h.fill("second");
  const second = h.submit();
  h.posts[0].reject(new Error("old transport error")); await first;
  assert.equal(h.get("save-camera").disabled, true);
  assert.equal(h.get("add-camera-message").textContent, "Saving camera and connecting…");
  h.posts[1].resolve(reply({}, 201)); await second;
  assert.equal(h.get("add-camera-dialog").open, false);
});

test("repeated submit produces only one request and preserves pending input", async () => {
  const h = harness(); await h.login(); await h.open(); h.fill("first");
  const first = h.submit(); h.get("new-camera-source").value = "new input";
  const repeated = h.submit();
  assert.equal(h.posts.length, 1);
  await repeated;
  assert.equal(h.get("new-camera-source").value, "new input");
  h.posts[0].resolve(reply({}, 201)); await first;
  assert.equal(h.get("add-camera-dialog").open, false);
  assert.equal(h.get("new-camera-source").value, "");
});

for (const outcome of ["success", "failure"]) {
  test(`old-session ${outcome} leaves the next session's pending dialog alone`, async () => {
    const h = harness(); await h.login(); await h.open(); h.fill("first");
    const first = h.submit(); await h.logout(); await h.login(); await h.open(); h.fill("second");
    const second = h.submit(); h.get("new-camera-source").value = "newer input";
    await h.flushClose();
    h.posts[0].resolve(reply({}, outcome === "success" ? 201 : 503)); await first;
    assert.equal(h.get("add-camera-dialog").open, true);
    assert.equal(h.get("new-camera-source").value, "newer input");
    assert.equal(h.get("save-camera").disabled, true);
    h.posts[1].resolve(reply({}, 422)); await second;
    assert.equal(h.get("save-camera").disabled, false);
  });
}

test("ordinary failure clears the submitted source and allows a successful retry", async () => {
  const h = harness(); await h.login(); await h.open(); h.fill();
  const first = h.submit();
  assert.equal(h.get("new-camera-source").value, "");
  h.posts[0].resolve(reply({}, 422)); await first;
  assert.equal(h.get("add-camera-dialog").open, true);
  assert.equal(h.get("save-camera").disabled, false);
  assert.match(h.get("add-camera-message").textContent, /Check the camera IP/);
  h.fill("retry"); const second = h.submit(); h.posts[1].resolve(reply({}, 201)); await second;
  assert.equal(h.get("add-camera-dialog").open, false);
  assert.match(h.get("workspace-message").textContent, /Camera saved/);
});

test("update source uses the same ownership guard and preserves read-only fields", async () => {
  const h = harness();
  h.setCameras([{ device_id: "camera-1", name: "Front", host: "192.0.2.1" }]);
  await h.login(); await h.open("update"); h.get("new-camera-source").value = "rtsp://192.0.2.1/first";
  const first = h.submit(); await h.dismiss(); await h.open("update");
  h.get("new-camera-source").value = "rtsp://192.0.2.1/second"; await h.flushClose();
  h.posts[0].resolve(reply({}, 201)); await first;
  assert.equal(h.get("add-camera-dialog").open, true);
  assert.equal(h.get("new-camera-name").value, "Front");
  assert.equal(h.get("new-camera-name").readOnly, true);
  assert.equal(h.get("new-camera-host").readOnly, true);
  assert.equal(h.get("new-camera-source").value, "rtsp://192.0.2.1/second");
  const second = h.submit(); h.posts[1].resolve(reply({}, 201)); await second;
  assert.match(h.get("workspace-message").textContent, /Camera source updated/);
});

test("a successful old save cannot select its camera after another dialog opens during refresh", async () => {
  const h = harness(); await h.login(); await h.open(); h.fill();
  const finishList = await h.deferList();
  const first = h.submit(); h.posts[0].resolve(reply({ device_id: "old-camera" }, 201));
  await turn(); await h.open(); h.fill("second"); await h.flushClose();
  finishList(reply({ cameras: [], worker_status: "ready" })); await first;
  assert.equal(h.get("new-camera-source").value, "rtsp://192.0.2.1/second");
  assert.equal(h.requests.some((request) => request.url.includes("old-camera/media-session")), false);
});
