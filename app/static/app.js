/* Stream Monitor — UI interactions */
(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  /* ------------------------------------------------------------------ toast */
  function toast(message, type = "info", timeout = 4500) {
    const stack = $("#toast-stack");
    if (!stack) return;
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = message;
    stack.appendChild(el);
    window.setTimeout(() => {
      el.classList.add("hide");
      window.setTimeout(() => el.remove(), 220);
    }, timeout);
  }

  /* -------------------------------------------------------------------- api */
  async function api(url, { method = "GET", body = null } = {}) {
    const options = { method, headers: { Accept: "application/json" } };
    if (body !== null) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(url, options);
    } catch (error) {
      throw new Error("Network error: could not reach the server.");
    }
    const text = await response.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (error) {
        data = { error: text.slice(0, 300) };
      }
    }
    // Endpoints such as /api/tools/test report failures as HTTP 200 with
    // ok=false; only transport/HTTP errors should throw here.
    if (!response.ok) {
      const message =
        (data && (data.error || data.detail)) || `Request failed (${response.status})`;
      throw new Error(message);
    }
    return data || {};
  }

  function setBusy(button, busy) {
    if (!button) return;
    button.disabled = busy;
    button.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function reloadSoon(delay = 500) {
    window.setTimeout(() => window.location.reload(), delay);
  }

  /* ----------------------------------------------------------- relative time */
  function relative(date) {
    const seconds = (date.getTime() - Date.now()) / 1000;
    const abs = Math.abs(seconds);
    if (abs < 10) return "just now";
    if (abs < 60) return seconds > 0 ? `in ${Math.round(abs)}s` : `${Math.round(abs)}s ago`;
    if (abs < 3600) {
      const minutes = Math.round(abs / 60);
      return seconds > 0 ? `in ${minutes}m` : `${minutes}m ago`;
    }
    if (abs < 86400) {
      const hours = Math.round(abs / 3600);
      return seconds > 0 ? `in ${hours}h` : `${hours}h ago`;
    }
    if (abs < 604800) {
      const days = Math.round(abs / 86400);
      return seconds > 0 ? `in ${days}d` : `${days}d ago`;
    }
    return date.toLocaleDateString();
  }

  function updateTimes() {
    $$("time[data-ts]").forEach((el) => {
      const raw = el.dataset.ts;
      if (!raw) return;
      const date = new Date(raw);
      if (Number.isNaN(date.getTime())) return;
      if (!el.title) el.title = date.toLocaleString();
      el.textContent = relative(date);
    });
  }

  /* --------------------------------------------------------------- versions */
  async function loadVersions(force = false) {
    const rows = $$("[data-version-key]");
    if (!rows.length) return;
    try {
      const data = await api(`/api/tools/versions${force ? "?refresh=1" : ""}`);
      const byKey = new Map((data.tools || []).map((tool) => [tool.key, tool]));
      rows.forEach((row) => {
        const tool = byKey.get(row.dataset.versionKey);
        const valueEl = $(".tool-value", row);
        if (!valueEl) return;
        row.classList.remove("ok", "err");
        if (!tool) {
          valueEl.textContent = "unknown";
          return;
        }
        if (tool.ok) {
          row.classList.add("ok");
          valueEl.textContent = tool.version || "installed";
          valueEl.title = tool.path || "";
        } else {
          row.classList.add("err");
          valueEl.textContent = tool.error || "unavailable";
          valueEl.title = tool.path || "";
        }
      });
    } catch (error) {
      rows.forEach((row) => {
        row.classList.remove("ok");
        row.classList.add("err");
        const valueEl = $(".tool-value", row);
        if (valueEl) valueEl.textContent = "unavailable";
      });
    }
  }

  /* ----------------------------------------------------------------- channel */
  function setupPlatformHint() {
    const select = $("#platform");
    const input = $("#identifier");
    const hint = $("#channel-hint");
    if (!select || !input || !hint) return;
    const update = () => {
      const option = select.selectedOptions[0];
      if (!option) return;
      input.placeholder = option.dataset.placeholder || "";
      hint.textContent = option.dataset.hint || "";
    };
    if (select.dataset.hintBound === "1") {
      update();
      return;
    }
    select.dataset.hintBound = "1";
    select.addEventListener("change", update);
    update();
  }

  function applyChannelFilters() {
    const search = ($("#channel-search")?.value || "").trim().toLowerCase();
    const rows = $$("#channels-table tbody tr");
    let visible = 0;
    rows.forEach((row) => {
      const matchesPlatform =
        activePlatformFilter === "all" || row.dataset.platform === activePlatformFilter;
      const matchesSearch = !search || (row.dataset.search || "").includes(search);
      const show = matchesPlatform && matchesSearch;
      row.classList.toggle("hidden", !show);
      if (show) visible += 1;
    });
    const count = $("#channel-visible-count");
    if (count) count.textContent = `${visible} channel${visible === 1 ? "" : "s"}`;
    const empty = $("#channel-no-results");
    if (empty) empty.classList.toggle("hidden", visible !== 0 || rows.length === 0);
  }

  let activePlatformFilter = "all";

  function setupChannelFilters() {
    $$("[data-filter-platform]").forEach((button) => {
      button.addEventListener("click", () => {
        activePlatformFilter = button.dataset.filterPlatform || "all";
        $$("[data-filter-platform]").forEach((other) =>
          other.classList.toggle("active", other === button)
        );
        applyChannelFilters();
      });
    });
    const search = $("#channel-search");
    if (search) search.addEventListener("input", applyChannelFilters);
  }

  /* --------------------------------------------------------------- recordings */
  function setupRecordingSearch() {
    const input = $("#recording-search");
    if (!input) return;
    input.addEventListener("input", () => {
      const search = input.value.trim().toLowerCase();
      const rows = $$("#recordings-table tbody tr");
      let visible = 0;
      rows.forEach((row) => {
        const show = !search || (row.dataset.search || "").includes(search);
        row.classList.toggle("hidden", !show);
        if (show) visible += 1;
      });
      const count = $("#recording-visible-count");
      if (count) count.textContent = `${visible} recording${visible === 1 ? "" : "s"}`;
      const empty = $("#recording-no-results");
      if (empty) empty.classList.toggle("hidden", visible !== 0 || rows.length === 0);
    });
  }

  /* -------------------------------------------------------------------- logs */
  function setupEventFilter() {
    const buttons = $$("[data-filter-level]");
    if (!buttons.length) return;
    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        const level = button.dataset.filterLevel || "all";
        buttons.forEach((other) => other.classList.toggle("active", other === button));
        let visible = 0;
        $$("#event-list .event").forEach((event) => {
          const show = level === "all" || event.dataset.level === level;
          event.classList.toggle("hidden", !show);
          if (show) visible += 1;
        });
        const empty = $("#event-no-results");
        if (empty) empty.classList.toggle("hidden", visible !== 0);
      });
    });
  }

  /* ---------------------------------------------------------------- auto refresh */
  function setupAutoRefresh() {
    const boxes = $$("[data-auto-refresh]");
    if (!boxes.length) return;
    const key = `sm:auto-refresh:${window.location.pathname}`;
    const defaultOn = !window.location.pathname.startsWith("/logs");
    const stored = window.localStorage.getItem(key);
    let enabled = stored === null ? defaultOn : stored === "1";
    boxes.forEach((box) => {
      box.checked = enabled;
      box.addEventListener("change", () => {
        enabled = boxes.some((candidate) => candidate.checked);
        boxes.forEach((candidate) => {
          candidate.checked = enabled;
        });
        window.localStorage.setItem(key, enabled ? "1" : "0");
      });
    });
    window.setInterval(() => {
      if (!boxes.some((box) => box.checked)) return;
      if (document.hidden) return;
      if ($("dialog[open]")) return;
      const active = document.activeElement;
      if (active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName)) return;
      // Never discard an in-progress add form or an active search.
      const dirty = $$(
        "#add-channel-form input, #bulk-add-form textarea, #channel-search, #recording-search"
      ).some((field) => field.value.trim() !== "");
      if (dirty) return;
      window.location.reload();
    }, 20000);
  }

  /* ------------------------------------------------------------------ actions */
  async function saveSettings(button) {
    const form = $("#settings-form");
    if (!form) return;
    if (typeof form.reportValidity === "function" && !form.reportValidity()) return;
    const values = {};
    form.querySelectorAll("input[name], select[name], textarea[name]").forEach((field) => {
      values[field.name] = field.type === "checkbox" ? field.checked : field.value;
    });
    setBusy(button, true);
    try {
      const data = await api("/api/settings", { method: "POST", body: { values } });
      toast(`Settings saved (${(data.saved || []).length} values).`, "success");
      await loadVersions(true);
      reloadSoon(900);
    } finally {
      setBusy(button, false);
    }
  }

  async function openRecordingLog(recordingId) {
    const dialog = $("#log-dialog");
    const content = $("#log-dialog-content");
    const title = $("#log-dialog-title");
    if (!dialog || !content) return;
    content.textContent = "Loading…";
    if (title) title.textContent = `Recording #${recordingId}`;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    try {
      const data = await api(`/api/recordings/${recordingId}/log`);
      content.textContent = data.log || "(the log is empty)";
      if (title && data.recording) {
        title.textContent = `${data.recording.channel_name || "Recording"} — ${data.recording.title || ""}`;
      }
    } catch (error) {
      content.textContent = error.message;
    }
  }

  const actions = {
    "monitor-toggle": async (button) => {
      setBusy(button, true);
      try {
        const data = await api("/api/monitor/toggle", { method: "POST" });
        toast(`Monitoring ${data.monitor.enabled ? "resumed" : "paused"}.`, "success");
        reloadSoon(300);
      } finally {
        setBusy(button, false);
      }
    },

    "check-all": async (button) => {
      setBusy(button, true);
      try {
        const data = await api("/api/monitor/check-all", { method: "POST" });
        toast(data.message || "Checks queued.", "success");
        reloadSoon(9000);
      } finally {
        setBusy(button, false);
      }
    },

    "check-channel": async (button) => {
      const id = button.dataset.id;
      setBusy(button, true);
      try {
        const data = await api(`/api/channels/${id}/check`, { method: "POST" });
        if (data.status === "live") {
          toast(`Live now: ${data.title || "stream detected"}.`, "success");
          reloadSoon(1200);
        } else if (data.status === "recording") {
          toast(data.message || "Already recording.", "info");
        } else if (data.status === "offline") {
          toast(data.message || "Channel is offline.", "info");
          reloadSoon(1200);
        } else {
          toast(data.message || "Check failed.", "error", 8000);
          reloadSoon(1800);
        }
      } finally {
        setBusy(button, false);
      }
    },

    "stop-channel": async (button) => {
      const id = button.dataset.id;
      setBusy(button, true);
      try {
        const data = await api(`/api/channels/${id}/stop`, { method: "POST" });
        toast((data && data.message) || "Stopping recording…", "success");
        reloadSoon(900);
      } finally {
        setBusy(button, false);
      }
    },

    "stop-recording": async (button) => {
      const id = button.dataset.id;
      setBusy(button, true);
      try {
        const data = await api(`/api/recordings/${id}/stop`, { method: "POST" });
        toast((data && data.message) || "Stopping recording…", "success");
        reloadSoon(900);
      } finally {
        setBusy(button, false);
      }
    },

    "toggle-channel": async (button) => {
      const id = button.dataset.id;
      setBusy(button, true);
      try {
        const data = await api(`/api/channels/${id}/toggle`, { method: "POST" });
        toast(`Channel ${data.enabled ? "enabled" : "disabled"}.`, "success");
        reloadSoon(400);
      } finally {
        setBusy(button, false);
      }
    },

    "delete-channel": async (button) => {
      const id = button.dataset.id;
      const name = button.dataset.name || "this channel";
      const question =
        `Remove ${name} from monitoring?\n\nExisting recordings are kept. ` +
        "A recording in progress is stopped and finishes post-processing in the background.";
      if (!window.confirm(question)) {
        return;
      }
      setBusy(button, true);
      try {
        const data = await api(`/api/channels/${id}`, { method: "DELETE" });
        toast((data && data.message) || "Channel removed.", "success");
        reloadSoon(1200);
      } finally {
        setBusy(button, false);
      }
    },

    "delete-recording": async (button) => {
      const id = button.dataset.id;
      const deleteFile = button.dataset.file === "1";
      const name = button.dataset.name || "this recording";
      const question = deleteFile
        ? `Delete "${name}" and its video file?\n\nThis cannot be undone.`
        : "Remove this entry from history?\n\nThe video file will be kept on disk.";
      if (!window.confirm(question)) return;
      setBusy(button, true);
      try {
        const suffix = deleteFile ? "?delete_file=1" : "";
        const data = await api(`/api/recordings/${id}${suffix}`, { method: "DELETE" });
        toast(data.message || "Recording deleted.", data.file_error ? "warning" : "success");
        reloadSoon(500);
      } finally {
        setBusy(button, false);
      }
    },

    "recording-log": async (button) => {
      setBusy(button, true);
      try {
        await openRecordingLog(button.dataset.id);
      } finally {
        setBusy(button, false);
      }
    },

    "refresh-versions": async (button) => {
      setBusy(button, true);
      try {
        await loadVersions(true);
        toast("Versions refreshed.", "success");
      } finally {
        setBusy(button, false);
      }
    },

    "test-tool": async (button) => {
      const tool = button.dataset.tool;
      const input = document.getElementById(button.dataset.input || "");
      setBusy(button, true);
      try {
        const data = await api("/api/tools/test", {
          method: "POST",
          body: { tool, path: input ? input.value : "" },
        });
        const result = data.result || {};
        const row = document.querySelector(`[data-version-key="${tool}"]`);
        if (row) {
          row.classList.remove("ok", "err");
          row.classList.add(result.ok ? "ok" : "err");
          const valueEl = $(".tool-value", row);
          if (valueEl) valueEl.textContent = result.ok ? result.version || "installed" : result.error || "failed";
        }
        if (result.ok) {
          toast(`${result.label || tool} ${result.version || "is available"}.`, "success");
        } else {
          toast(result.error || "Tool check failed.", "error", 8000);
        }
      } finally {
        setBusy(button, false);
      }
    },

    "impersonate-targets": async (button) => {
      setBusy(button, true);
      const output = $("#impersonate-output");
      try {
        const data = await api("/api/tools/impersonate-targets");
        if (output) {
          output.classList.remove("hidden");
          output.textContent = data.output || data.error || "No impersonation targets reported.";
        }
        if (data.ok) toast("Impersonation targets loaded.", "success");
        else toast(data.error || "Could not read impersonation targets.", "error", 8000);
      } finally {
        setBusy(button, false);
      }
    },

    "save-settings": async (button) => {
      await saveSettings(button);
    },

    "close-dialog": () => {
      const dialog = $("#log-dialog");
      if (dialog && typeof dialog.close === "function") dialog.close();
      else if (dialog) dialog.removeAttribute("open");
    },
  };

  document.addEventListener("click", async (event) => {
    const target = event.target.closest("[data-action]");
    if (!target) return;
    const handler = actions[target.dataset.action];
    if (!handler) return;
    event.preventDefault();
    try {
      await handler(target);
    } catch (error) {
      toast(error.message || "Something went wrong.", "error", 8000);
    }
  });

  /* ------------------------------------------------------------------- forms */
  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (form.id === "add-channel-form") {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      if (typeof form.reportValidity === "function" && !form.reportValidity()) return;
      const field = (name) => form.elements.namedItem(name);
      const payload = {
        platform: field("platform").value,
        identifier: field("identifier").value.trim(),
        name: (field("name")?.value || "").trim(),
      };
      setBusy(button, true);
      try {
        await api("/api/channels", { method: "POST", body: payload });
        toast("Channel added — live check queued.", "success");
        form.reset();
        setupPlatformHint();
        reloadSoon(700);
      } catch (error) {
        toast(error.message, "error", 8000);
      } finally {
        setBusy(button, false);
      }
      return;
    }

    if (form.id === "bulk-add-form") {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      if (typeof form.reportValidity === "function" && !form.reportValidity()) return;
      const payload = {
        platform: form.elements.namedItem("platform").value,
        lines: form.elements.namedItem("lines").value,
      };
      if (!payload.lines.trim()) {
        toast("Paste at least one channel.", "error");
        return;
      }
      setBusy(button, true);
      try {
        const data = await api("/api/channels/bulk", { method: "POST", body: payload });
        toast(data.message || "Channels imported.", data.errors && data.errors.length ? "warning" : "success", 7000);
        if (data.errors && data.errors.length) {
          const first = data.errors[0];
          toast(`${first.line}: ${first.error}`, "error", 9000);
        }
        form.lines.value = "";
        reloadSoon(1200);
      } catch (error) {
        toast(error.message, "error", 8000);
      } finally {
        setBusy(button, false);
      }
      return;
    }

    if (form.id === "settings-form") {
      event.preventDefault();
      await saveSettings(null);
    }
  });

  /* -------------------------------------------------------------------- init */
  function init() {
    setupPlatformHint();
    setupChannelFilters();
    setupRecordingSearch();
    setupEventFilter();
    setupAutoRefresh();
    updateTimes();
    window.setInterval(updateTimes, 30000);
    loadVersions(false);
    const dialog = $("#log-dialog");
    if (dialog) {
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog) actions["close-dialog"]();
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
