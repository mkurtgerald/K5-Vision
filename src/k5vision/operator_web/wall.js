/* K5 Vision camera wall. Live-player lifecycle adapts MediaMTX v1.21.2's
 * HLS page (MIT), copyright (c) 2019 aler9; see THIRD_PARTY_NOTICES.md.
 * Camera credentials and source addresses never enter this component.
 */
"use strict";

(() => {
  const RETRIES = [1000, 2000, 4000, 8000, 10000];
  const LAYOUTS = [1, 4, 9];

  function element(document, tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = text;
    return result;
  }

  function create(options) {
    const { container, request, ensureHls, safeMediaUrl, getSession, onOpenCamera } = options;
    if (!container?.ownerDocument || [request, ensureHls, safeMediaUrl, getSession, onOpenCamera]
      .some(value => typeof value !== "function")) throw new TypeError("Camera wall configuration is invalid");
    const document = container.ownerDocument;
    const tiles = new Map();
    let cameras = [], layout = 4, page = 0, visible = false, destroyed = false;
    let owner = null;
    const root = element(document, "section", "camera-wall");
    root.setAttribute("aria-label", "Live camera wall");
    const toolbar = element(document, "div", "camera-wall-toolbar");
    const heading = element(document, "div", "camera-wall-heading");
    heading.append(element(document, "h2", "", "All cameras"));
    const summary = element(document, "p", "camera-wall-summary");
    heading.append(summary);
    const controls = element(document, "div", "camera-wall-controls");
    const layoutLabel = element(document, "label", "camera-wall-layout-label", "Layout");
    const layoutSelect = element(document, "select", "camera-wall-layout");
    layoutSelect.id = "wall-layout";
    layoutSelect.setAttribute("aria-label", "Camera wall layout");
    for (const count of LAYOUTS) {
      const option = element(document, "option", "", count === 1 ? "1 camera" : `${Math.sqrt(count)} × ${Math.sqrt(count)}`);
      option.value = String(count);
      layoutSelect.append(option);
    }
    layoutSelect.value = "4";
    layoutLabel.append(layoutSelect);
    const previous = element(document, "button", "button secondary", "Previous");
    const next = element(document, "button", "button secondary", "Next");
    previous.id = "wall-page-previous";
    next.id = "wall-page-next";
    previous.type = next.type = "button";
    const pages = element(document, "span", "camera-wall-pages");
    pages.setAttribute("aria-live", "polite");
    controls.append(layoutLabel, previous, pages, next);
    toolbar.append(heading, controls);
    const grid = element(document, "div", "camera-wall-grid");
    grid.setAttribute("role", "list");
    const empty = element(document, "p", "camera-wall-empty", "No cameras connected yet. Add a camera to start live viewing and recording.");
    root.append(toolbar, grid, empty);
    container.append(root);

    function session() {
      const value = getSession();
      return value && typeof value.token === "string" && value.token ? { token: value.token, epoch: value.epoch } : null;
    }

    function sameSession(value) {
      const current = session();
      return Boolean(value && current && value.token === current.token && value.epoch === current.epoch);
    }

    function list(values) {
      const seen = new Set();
      return (Array.isArray(values) ? values : []).slice(0, 64).filter(camera => {
        const id = camera?.device_id;
        if (typeof id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(id) || seen.has(id)) return false;
        seen.add(id);
        return true;
      });
    }

    class Tile {
      constructor(camera) {
        this.camera = camera;
        this.id = camera.device_id;
        this.generation = 0;
        this.operation = 0;
        this.retryCount = 0;
        this.hls = null;
        this.media = null;
        this.controller = null;
        this.timer = null;
        this.authRefreshUsed = false;
        this.statusReadmitUsed = false;
        this.failure = "";
        this.native = false;
        this.state = "connecting";
        this.node = element(document, "article", "camera-wall-tile");
        this.node.dataset.cameraId = this.id;
        this.node.setAttribute("role", "listitem");
        const header = element(document, "div", "camera-wall-tile-heading");
        this.name = element(document, "h3", "camera-wall-name");
        this.status = element(document, "span", "camera-wall-status");
        header.append(this.name, this.status);
        this.surface = element(document, "div", "camera-wall-surface");
        this.video = element(document, "video", "camera-wall-video");
        this.video.autoplay = true;
        this.video.muted = true;
        this.video.playsInline = true;
        this.video.preload = "none";
        this.overlay = element(document, "div", "camera-wall-overlay");
        this.description = element(document, "p", "camera-wall-description", "Opening live video…");
        this.retry = element(document, "button", "button secondary camera-wall-retry", "Retry live video");
        this.retry.type = "button";
        this.retry.hidden = true;
        this.overlay.append(this.description, this.retry);
        this.recording = element(document, "span", "camera-wall-recording");
        this.surface.append(this.video, this.overlay, this.recording);
        const footer = element(document, "div", "camera-wall-tile-actions");
        const watch = element(document, "button", "button secondary", "Watch");
        const history = element(document, "button", "button quiet", "Recordings");
        const fullscreen = element(document, "button", "button quiet", "Fullscreen");
        watch.dataset.wallAction = "watch";
        history.dataset.wallAction = "recordings";
        fullscreen.dataset.wallAction = "fullscreen";
        for (const button of [watch, history, fullscreen]) button.type = "button";
        footer.append(watch, history, fullscreen);
        this.node.append(header, this.surface, footer);
        this.video.addEventListener("playing", () => {
          if (!this.current(this.generation)) return;
          this.state = "live";
          this.retryCount = 0;
          this.overlay.hidden = true;
          this.labels();
        });
        this.video.addEventListener("waiting", () => {
          if (!this.current(this.generation) || this.state !== "live") return;
          this.state = "buffering";
          this.labels();
        });
        this.video.addEventListener("error", () => {
          if (this.native && this.current(this.generation)) this.schedule(this.generation);
        });
        this.retry.addEventListener("click", () => {
          if (this.state === "play") this.play(this.generation, this.operation);
          else this.begin();
        });
        watch.addEventListener("click", () => { if (this.current(this.generation)) onOpenCamera(this.id, "live"); });
        history.addEventListener("click", () => { if (this.current(this.generation)) onOpenCamera(this.id, "history"); });
        fullscreen.addEventListener("click", () => {
          if (!this.current(this.generation)) return;
          const operation = document.fullscreenElement === this.node
            ? document.exitFullscreen?.() : this.node.requestFullscreen?.();
          Promise.resolve(operation).catch(() => { fullscreen.textContent = "Fullscreen unavailable"; });
        });
        this.labels();
      }

      current(generation, operation = this.operation) {
        return visible && !destroyed && tiles.get(this.id) === this && generation === this.generation
          && operation === this.operation && sameSession(this.owner) && !this.controller?.signal.aborted;
      }

      labels() {
        this.name.textContent = typeof this.camera.name === "string" ? this.camera.name.slice(0, 128) : "Camera";
        this.video.setAttribute("aria-label", `${this.name.textContent} live video`);
        const status = this.camera.status;
        const label = status === "disconnected" ? "Camera disconnected" : status === "unavailable" ? "Service unavailable"
          : status === "connecting" ? "Camera connecting" : this.state === "live" ? "Live"
            : this.state === "buffering" ? "Buffering" : this.state === "error" ? "Live unavailable" : "Connecting";
        this.status.textContent = label;
        this.status.dataset.state = label === "Live" ? "live" : label === "Buffering" || label === "Connecting" || label === "Camera connecting" ? "connecting" : "unavailable";
        const recording = this.camera.recording_state;
        const enabled = this.camera.recording_enabled === true || this.camera.recording === true;
        this.recording.textContent = recording === "recording" ? "Recording"
          : recording === "paused" ? "Recording paused" : recording === "waiting" ? "Waiting for recording"
            : recording === "stalled" ? "Recording stalled" : recording === "error" ? "Recording unavailable"
              : enabled ? "Recording enabled" : "Not recording";
        this.recording.dataset.state = recording === "recording" ? "recording" : recording === "waiting" || enabled && !["paused", "stalled", "error"].includes(recording) ? "enabled" : "paused";
      }

      message(text, retry = false) {
        this.overlay.hidden = false;
        this.description.textContent = text;
        this.retry.textContent = this.state === "play" ? "Play live video" : "Retry live video";
        this.retry.hidden = !retry;
        this.labels();
      }

      stop() {
        clearTimeout(this.timer);
        this.timer = null;
        this.native = false;
        const hls = this.hls;
        this.hls = null;
        if (hls) hls.destroy();
        this.video.onplay = null;
        this.video.pause();
        this.video.removeAttribute("src");
        this.video.load();
      }

      begin() {
        if (!visible || destroyed || !session()) return;
        this.generation += 1;
        this.controller?.abort();
        this.controller = new AbortController();
        this.owner = session();
        this.authRefreshUsed = false;
        this.statusReadmitUsed = false;
        this.failure = "";
        this.retryCount = 0;
        this.media = null;
        this.stop();
        void this.admit(this.generation);
      }

      async admit(generation) {
        const operation = ++this.operation;
        const previousStatus = this.camera.status;
        this.failure = "";
        this.state = "connecting";
        this.message(this.authRefreshUsed ? "Restoring camera access…" : "Opening live video…");
        try {
          const media = await request(`/api/v1/operator/cameras/${encodeURIComponent(this.id)}/media-session`, {
            method: "POST", signal: this.controller.signal,
          });
          if (!this.current(generation, operation)) return;
          this.media = { live: safeMediaUrl(media.live_url, "/media/live/") };
          await this.launch(generation);
        } catch (error) {
          if (!this.current(generation, operation) || error?.name === "AbortError") return;
          this.state = "error";
          this.failure = error?.status === 401 || error?.status === 403 ? "auth"
            : !error?.status || [429, 500, 502, 503, 504].includes(error.status) ? "transport" : "unsupported";
          this.message(error?.status === 401 ? "Your session has ended. Sign in again."
            : error?.status === 403 ? "This account cannot open this camera."
              : "Camera access is unavailable. Retry or open Recordings.", true);
          // A status poll can observe recovery while this failed request is
          // still in flight. Preserve that edge without starting another loop.
          if (previousStatus !== "live" && this.camera.status === "live") this.recoverTransport();
        }
      }

      async launch(generation) {
        if (!this.current(generation) || !this.media) return;
        const operation = ++this.operation;
        this.stop();
        this.failure = "";
        this.state = "connecting";
        this.message("Connecting live video…");
        let timer;
        try {
          await Promise.race([ensureHls(), new Promise((_, reject) => {
            timer = setTimeout(() => reject(new Error("Player unavailable")), 10000);
          })]);
          if (!this.current(generation, operation)) return;
          if (window.Hls?.isSupported()) {
            const hls = new window.Hls({ maxLiveSyncPlaybackRate: 1.5, manifestLoadingMaxRetry: 1, levelLoadingMaxRetry: 1, fragLoadingMaxRetry: 1 });
            this.hls = hls;
            const current = () => this.current(generation, operation) && this.hls === hls;
            hls.on(window.Hls.Events.ERROR, (_event, data) => {
              if (!current() || !data.fatal) return;
              if (data.response?.code === 401 || data.response?.code === 403) {
                this.stop();
                if (this.authRefreshUsed) {
                  this.state = "error";
                  this.failure = "auth";
                  this.message("Media access could not be restored. Retry live video.", true);
                } else {
                  this.authRefreshUsed = true;
                  void this.admit(generation);
                }
              } else if (data.details === "manifestIncompatibleCodecsError") {
                this.stop();
                this.state = "error";
                this.failure = "unsupported";
                this.message("This stream needs H.264 video. Reconnect the camera with an H.264 profile.", true);
              } else this.schedule(generation);
            });
            hls.on(window.Hls.Events.MEDIA_ATTACHED, () => { if (current()) hls.loadSource(this.media.live); });
            hls.on(window.Hls.Events.MANIFEST_PARSED, () => { if (current()) this.play(generation, operation); });
            this.video.onplay = () => {
              if (current() && Number.isFinite(hls.liveSyncPosition)) this.video.currentTime = hls.liveSyncPosition;
            };
            hls.attachMedia(this.video);
          } else if (this.video.canPlayType("application/vnd.apple.mpegurl")) {
            this.native = true;
            this.video.src = this.media.live;
            this.play(generation, operation);
          } else {
            this.state = "error";
            this.failure = "unsupported";
            this.message("Use a current version of Microsoft Edge or Chrome to play this camera.", true);
          }
        } catch (_) {
          if (this.current(generation, operation)) this.schedule(generation);
        } finally {
          clearTimeout(timer);
        }
      }

      play(generation, operation) {
        const promise = this.video.play();
        if (promise) promise.catch(() => {
          if (!this.current(generation, operation)) return;
          this.state = "play";
          this.message("Select play to start this camera.", true);
        });
      }

      schedule(generation) {
        if (!this.current(generation)) return;
        this.stop();
        this.failure = "transport";
        if (this.retryCount >= RETRIES.length) {
          this.state = "error";
          this.message("Camera video is disconnected. Check its network or retry live video. Recordings remain available.", true);
          return;
        }
        const delay = RETRIES[this.retryCount++];
        this.state = "connecting";
        this.message(`Reconnecting live video · attempt ${this.retryCount} of ${RETRIES.length}…`);
        this.timer = setTimeout(() => { if (this.current(generation)) void this.launch(generation); }, delay);
      }

      update(camera) {
        const previous = this.camera.status;
        this.camera = camera;
        this.labels();
        if (camera.status === "live" && previous !== "live") this.recoverTransport();
      }

      recoverTransport() {
        if (!this.current(this.generation) || this.state !== "error" || this.failure !== "transport") return;
        if (this.media) {
          this.retryCount = 0;
          void this.launch(this.generation);
        } else if (!this.statusReadmitUsed) {
          // One admission retry per tile generation; status flapping cannot
          // issue unbounded token-bearing POSTs or reset media-auth recovery.
          this.statusReadmitUsed = true;
          void this.admit(this.generation);
        }
      }

      dispose() {
        this.generation += 1;
        this.operation += 1;
        this.controller?.abort();
        this.stop();
        this.media = null;
        this.node.remove();
      }
    }

    function clear() {
      for (const tile of tiles.values()) tile.dispose();
      tiles.clear();
    }

    function render() {
      if (!visible || destroyed) return;
      if (!sameSession(owner)) {
        clear();
        owner = session();
      }
      if (!owner) { hide(); return; }
      const count = Math.max(1, Math.ceil(cameras.length / layout));
      page = Math.min(page, count - 1);
      const shown = cameras.slice(page * layout, (page + 1) * layout);
      const ids = new Set(shown.map(camera => camera.device_id));
      for (const [id, tile] of tiles) {
        if (!ids.has(id)) { tile.dispose(); tiles.delete(id); }
      }
      grid.dataset.layout = String(layout);
      for (const [index, camera] of shown.entries()) {
        let tile = tiles.get(camera.device_id);
        if (tile) tile.update(camera);
        else {
          tile = new Tile(camera);
          tiles.set(camera.device_id, tile);
          tile.begin();
        }
        // Moving an already mounted fullscreen tile exits fullscreen in the
        // browser. Polls that only update metadata must leave its node in place.
        if (grid.children[index] !== tile.node) grid.insertBefore(tile.node, grid.children[index] || null);
      }
      summary.textContent = cameras.length ? `${cameras.length} ${cameras.length === 1 ? "camera" : "cameras"} · Live views only on this page. Recording continues when you leave the wall.` : "Connect your first camera to begin.";
      pages.textContent = `Page ${page + 1} of ${count}`;
      previous.disabled = page === 0;
      next.disabled = page + 1 === count;
      empty.hidden = cameras.length > 0;
      grid.hidden = cameras.length === 0;
    }

    function hide() {
      visible = false;
      clear();
      owner = null;
      container.hidden = true;
    }

    layoutSelect.addEventListener("change", () => {
      const value = Number(layoutSelect.value);
      if (!LAYOUTS.includes(value)) return;
      layout = value;
      page = 0;
      render();
    });
    previous.addEventListener("click", () => { if (page > 0) { page -= 1; render(); } });
    next.addEventListener("click", () => { if ((page + 1) * layout < cameras.length) { page += 1; render(); } });

    return {
      show(values) { if (destroyed) return; cameras = list(values); visible = true; container.hidden = false; render(); },
      update(values) { if (destroyed) return; cameras = list(values); render(); },
      hide,
      destroy() { hide(); destroyed = true; root.remove(); cameras = []; },
    };
  }

  window.K5CameraWall = Object.freeze({ create });
})();
