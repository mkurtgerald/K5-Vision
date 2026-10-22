/* UTC recording availability and seek controls. Playback admission stays with K5. */
"use strict";

(() => {
  let nextId = 0;
  const MAX_DATE = 8640000000000000;
  const validTime = (value) => Number.isFinite(value) && Math.abs(value) <= MAX_DATE;
  const utc = (value, milliseconds = false) => new Date(value).toISOString()
    .replace("T", " ").replace(milliseconds ? /Z$/ : /\.\d{3}Z$/, " UTC");

  function create({ container, onSeek }) {
    if (!(container instanceof HTMLElement) || typeof onSeek !== "function") {
      throw new TypeError("A timeline container and seek callback are required");
    }
    const element = (tag, className, text = "") => {
      const node = document.createElement(tag);
      node.className = className;
      node.textContent = text;
      return node;
    };
    const root = element("section", "k5-recording-timeline is-empty");
    root.setAttribute("aria-label", "Recording timeline");
    const heading = element("div", "k5-timeline-heading");
    const title = element("h3", "k5-timeline-title", "Recording timeline");
    const label = element("span", "k5-timeline-time");
    heading.append(title, label);
    const track = element("div", "k5-timeline-track");
    track.setAttribute("role", "slider");
    track.setAttribute("aria-label", "Recording time in UTC");
    track.setAttribute("aria-orientation", "horizontal");
    const ranges = element("div", "k5-timeline-ranges");
    ranges.setAttribute("aria-hidden", "true");
    const hover = element("div", "k5-timeline-hover");
    const playhead = element("div", "k5-timeline-playhead");
    hover.setAttribute("aria-hidden", "true");
    playhead.setAttribute("aria-hidden", "true");
    const empty = element("span", "k5-timeline-empty", "Load history to see availability");
    track.append(ranges, hover, playhead, empty);
    const ticks = element("div", "k5-timeline-ticks");
    ticks.setAttribute("aria-hidden", "true");
    const footer = element("div", "k5-timeline-footer");
    const legend = element("div", "k5-timeline-legend");
    legend.append(
      element("span", "k5-timeline-recorded-key", "Recording"),
      element("span", "k5-timeline-gap-key", "No recording"),
    );
    const summary = element("span", "k5-timeline-summary");
    footer.append(legend, summary);
    const help = element("p", "k5-timeline-help",
      "Choose a time or drag, then release to seek. Arrow keys move 1 second; Shift moves 10 seconds. Home and End reach the window edges.");
    help.id = `k5-timeline-help-${++nextId}`;
    track.setAttribute("aria-describedby", help.id);
    root.append(heading, track, ticks, footer, help);
    container.append(root);

    let windowStart = null, windowEnd = null, intervals = [];
    let current = null, hoverTime = null, drag = null, destroyed = false;
    const enabled = () => !destroyed && windowStart !== null;
    const clamp = (value) => Math.max(windowStart, Math.min(windowEnd, value));
    const position = (value) => 100 * (value - windowStart) / (windowEnd - windowStart);
    const recorded = (value) => intervals.some((span) => span.start <= value && value < span.end);
    const describe = (value) => `${utc(value, true)} · ${recorded(value) ? "Recording available" : "No recording"}`;

    function renderPosition() {
      const selected = drag ? drag.time : current;
      playhead.hidden = !enabled() || selected === null;
      hover.hidden = !enabled() || hoverTime === null || drag !== null;
      if (!enabled()) return;
      if (selected !== null) {
        playhead.style.left = `${position(selected)}%`;
        track.setAttribute("aria-valuenow", String(selected));
        track.setAttribute("aria-valuetext", describe(selected));
      }
      if (!hover.hidden) hover.style.left = `${position(hoverTime)}%`;
      const shown = drag ? drag.time : hoverTime !== null ? hoverTime : current;
      label.textContent = shown === null ? "Choose a UTC time" : describe(shown);
      root.classList.toggle("is-scrubbing", drag !== null);
    }

    function renderTicks() {
      ticks.replaceChildren();
      if (!enabled()) return;
      const count = Math.max(2, Math.min(7, Math.floor(track.clientWidth / 160) + 1));
      for (let index = 0; index < count; index += 1) {
        const value = windowStart + (windowEnd - windowStart) * index / (count - 1);
        const tick = element("time", "k5-timeline-tick");
        tick.dateTime = new Date(value).toISOString();
        tick.style.left = `${100 * index / (count - 1)}%`;
        const [date, time] = tick.dateTime.split("T");
        tick.append(element("span", "", date), element("span", "", time.slice(0, 8) + " UTC"));
        if (index === 0) tick.classList.add("first");
        if (index === count - 1) tick.classList.add("last");
        ticks.append(tick);
      }
    }

    function addRange(from, to, available) {
      if (to <= from) return;
      const range = element("span", available ? "k5-timeline-recording" : "k5-timeline-gap");
      range.style.left = `${position(from)}%`;
      range.style.width = `${position(to) - position(from)}%`;
      range.title = `${available ? "Recording" : "No recording"}: ${utc(from, true)} → ${utc(to, true)}`;
      range.dataset.start = String(from);
      range.dataset.end = String(to);
      ranges.append(range);
    }

    function releaseDrag() {
      if (!drag) return;
      const pointerId = drag.pointerId;
      drag = null;
      if (track.hasPointerCapture(pointerId)) track.releasePointerCapture(pointerId);
    }

    function clear() {
      if (destroyed) return;
      releaseDrag();
      windowStart = null; windowEnd = null; intervals = [];
      current = null; hoverTime = null;
      ranges.replaceChildren(); ticks.replaceChildren();
      track.tabIndex = -1;
      track.setAttribute("aria-disabled", "true");
      for (const name of ["aria-valuemin", "aria-valuemax", "aria-valuenow", "aria-valuetext"]) track.removeAttribute(name);
      root.classList.add("is-empty");
      root.classList.remove("is-scrubbing");
      delete container.dataset.windowStart;
      delete container.dataset.windowEnd;
      empty.hidden = false; hover.hidden = true; playhead.hidden = true;
      label.textContent = "Choose a camera and load history";
      summary.textContent = "Availability has not been loaded";
    }

    function setData(data = {}) {
      if (destroyed) return;
      const { start, end, spans } = data || {};
      if (!validTime(start) || !validTime(end) || end <= start || !Number.isFinite(end - start)) {
        clear();
        return;
      }
      releaseDrag();
      windowStart = start; windowEnd = end;
      container.dataset.windowStart = String(start);
      container.dataset.windowEnd = String(end);
      intervals = (Array.isArray(spans) ? spans : [])
        .filter((span) => span && validTime(span.start) && validTime(span.end))
        .map((span) => ({ start: Math.max(start, span.start), end: Math.min(end, span.end) }))
        .filter((span) => span.end > span.start)
        .sort((left, right) => left.start - right.start || left.end - right.end);
      current = validTime(current) && current >= start && current <= end ? current : start;
      hoverTime = null;
      ranges.replaceChildren();
      // Keep every supplied recording interval separate. Only the gap calculation
      // uses overlapping coverage, so a real discontinuity is never filled in.
      let cursor = start;
      for (const span of intervals) {
        addRange(cursor, span.start, false);
        cursor = Math.max(cursor, span.end);
      }
      addRange(cursor, end, false);
      for (const span of intervals) addRange(span.start, span.end, true);
      root.classList.remove("is-empty");
      empty.hidden = true;
      track.tabIndex = 0;
      track.setAttribute("aria-disabled", "false");
      track.setAttribute("aria-valuemin", String(start));
      track.setAttribute("aria-valuemax", String(end));
      summary.textContent = intervals.length
        ? `${intervals.length} recording ${intervals.length === 1 ? "interval" : "intervals"} · Gaps contain no video`
        : "No recorded video in this window";
      renderTicks(); renderPosition();
    }

    function setPlayhead(value) {
      if (!enabled() || !validTime(value) || value < windowStart || value > windowEnd) return;
      current = value;
      renderPosition();
    }

    function pointerTime(event) {
      const bounds = track.getBoundingClientRect();
      if (!enabled() || !Number.isFinite(event.clientX) || bounds.width <= 0) return null;
      return clamp(Math.round(windowStart + (event.clientX - bounds.left) / bounds.width * (windowEnd - windowStart)));
    }

    function seek(value) {
      if (!enabled() || !validTime(value) || value < windowStart || value > windowEnd) return;
      current = value;
      renderPosition();
      onSeek(value);
    }

    function pointerDown(event) {
      if (event.button !== 0 || drag) return;
      const value = pointerTime(event);
      if (value === null) return;
      event.preventDefault();
      track.focus({ preventScroll: true });
      drag = { pointerId: event.pointerId, time: value };
      hoverTime = null;
      track.setPointerCapture(event.pointerId);
      renderPosition();
    }

    function pointerMove(event) {
      if (drag && drag.pointerId !== event.pointerId) return;
      const value = pointerTime(event);
      if (value === null) return;
      if (drag) drag.time = value;
      else hoverTime = value;
      renderPosition();
    }

    function pointerUp(event) {
      if (!drag || drag.pointerId !== event.pointerId) return;
      const value = pointerTime(event);
      releaseDrag(); hoverTime = null;
      if (value !== null) seek(value);
      else renderPosition();
    }

    function pointerCancel(event) {
      if (!drag || drag.pointerId !== event.pointerId) return;
      releaseDrag(); hoverTime = null; renderPosition();
    }

    function pointerLeave() {
      if (!drag) { hoverTime = null; renderPosition(); }
    }

    function keyDown(event) {
      if (!enabled() || drag) return;
      const step = event.shiftKey ? 10000 : 1000;
      const from = current === null ? windowStart : current;
      let value;
      if (event.key === "Home") value = windowStart;
      else if (event.key === "End") value = windowEnd;
      else if (event.key === "ArrowLeft" || event.key === "ArrowDown") value = clamp(from - step);
      else if (event.key === "ArrowRight" || event.key === "ArrowUp") value = clamp(from + step);
      else if (event.key === "PageUp") value = clamp(from + (windowEnd - windowStart) / 10);
      else if (event.key === "PageDown") value = clamp(from - (windowEnd - windowStart) / 10);
      else return;
      event.preventDefault();
      hoverTime = null;
      seek(value);
    }

    const listeners = [
      ["pointerdown", pointerDown], ["pointermove", pointerMove], ["pointerup", pointerUp],
      ["pointercancel", pointerCancel], ["lostpointercapture", pointerCancel],
      ["pointerleave", pointerLeave], ["keydown", keyDown],
    ];
    for (const [name, listener] of listeners) track.addEventListener(name, listener);
    const resize = typeof ResizeObserver === "function" ? new ResizeObserver(renderTicks) : null;
    resize?.observe(track);
    clear();

    function destroy() {
      if (destroyed) return;
      clear();
      destroyed = true;
      resize?.disconnect();
      for (const [name, listener] of listeners) track.removeEventListener(name, listener);
      root.remove();
    }

    return Object.freeze({ setData, setPlayhead, clear, destroy });
  }

  window.K5RecordingTimeline = Object.freeze({ create });
})();
