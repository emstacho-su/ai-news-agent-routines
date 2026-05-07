/* AI News Agent dashboard JS.
 *
 * - Refreshes the budget cell on page load and after a run
 * - Handles the RUN DAILY BRIEFING button: kicks off a job, opens an
 *   SSE stream for live activity, refreshes the briefings list on
 *   completion.
 *
 * Vanilla JS, no build step. Single-user, single-tab assumption. The
 * browser forwards basic-auth credentials to fetch and EventSource
 * automatically.
 */

(() => {
  const $ = (id) => document.getElementById(id);

  async function refreshBudget() {
    const cell = $("budget-cell");
    if (!cell) return;
    try {
      const res = await fetch("/api/budget");
      if (!res.ok) {
        cell.textContent = "$? / $?";
        return;
      }
      const b = await res.json();
      const ratioPct = (b.ratio * 100).toFixed(0);
      cell.textContent =
        "$" + b.month_usd.toFixed(2) +
        " / $" + b.cap_usd.toFixed(0) +
        " (" + ratioPct + "%)";
      if (b.ratio >= 0.8) cell.style.color = "var(--warn)";
    } catch (e) {
      cell.textContent = "budget unavailable";
    }
  }

  function setRunStatus(text) {
    const cell = $("run-status");
    if (cell) cell.textContent = text;
  }

  // -------------------------------------------------------------------
  // Live progress indicator. Independent of SSE events so the UI never
  // looks frozen while a server-side tool (web_search, code_execution)
  // is in flight inside Anthropic. Ticks once per second.
  // -------------------------------------------------------------------
  function formatDuration(ms) {
    if (ms < 0) ms = 0;
    const s = Math.floor(ms / 1000);
    if (s < 60) return s + "s";
    const m = Math.floor(s / 60);
    const rem = s % 60;
    return m + "m " + rem + "s";
  }

  function startProgress() {
    const wrap = $("run-progress");
    const txt = $("run-progress-text");
    const hint = $("run-progress-hint");
    if (!wrap || !txt) return { update: () => {}, stop: () => {} };
    wrap.hidden = false;
    const startedAt = Date.now();
    let lastEventAt = Date.now();

    const tick = () => {
      const now = Date.now();
      const elapsed = now - startedAt;
      const idle = now - lastEventAt;
      txt.textContent =
        "running " + formatDuration(elapsed) +
        " · last event " + formatDuration(idle) + " ago";
      // Server-side tools (web_search, code_execution) block the loop
      // for 30-90s with zero events. Surface that fact once we cross
      // 20s of silence so the user knows it's not frozen.
      if (hint) {
        if (idle > 20000) {
          hint.textContent =
            "server tool in flight (web_search runs inside Anthropic; 30-90s typical)";
        } else {
          hint.textContent = "";
        }
      }
    };
    tick();
    const handle = setInterval(tick, 1000);

    return {
      update: () => { lastEventAt = Date.now(); },
      stop: () => {
        clearInterval(handle);
        if (wrap) wrap.hidden = true;
      },
    };
  }

  function appendActivity(event) {
    const log = $("activity-log");
    if (!log) return;
    const ts = (event.ts || "").slice(11, 19); // HH:MM:SS
    const msg = event.message || "(no message)";
    const args = event.args ? " " + event.args : "";
    const line = document.createElement("div");
    const tsSpan = document.createElement("span");
    tsSpan.className = "ts";
    tsSpan.textContent = "[" + ts + "] ";
    const msgSpan = document.createElement("span");
    msgSpan.className = "msg";
    msgSpan.textContent = msg;
    const argsSpan = document.createElement("span");
    argsSpan.className = "args";
    argsSpan.textContent = args;
    line.appendChild(tsSpan);
    line.appendChild(msgSpan);
    line.appendChild(argsSpan);
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
  }

  function showActivity() {
    const sec = $("activity-section");
    const log = $("activity-log");
    if (sec) sec.hidden = false;
    if (log) log.textContent = "";
  }

  function streamJob(jobId) {
    return new Promise((resolve) => {
      const progress = startProgress();
      const es = new EventSource("/status/" + encodeURIComponent(jobId) + "/stream");
      es.addEventListener("message", (e) => {
        let event;
        try {
          event = JSON.parse(e.data);
        } catch (err) {
          return; // ignore malformed
        }
        if (event.type === "done") {
          es.close();
          progress.stop();
          if (event.status === "complete") {
            const path = event.result && event.result.briefing_path;
            setRunStatus("done" + (path ? ": " + path : ""));
          } else if (event.status === "error") {
            setRunStatus("error: " + (event.error || "unknown"));
          } else {
            setRunStatus("status: " + event.status);
          }
          resolve(event);
          return;
        }
        progress.update();
        appendActivity(event);
      });
      es.addEventListener("error", () => {
        // EventSource auto-reconnects on transient errors. If the
        // server intentionally closed (after sending `done`), readyState
        // will be CLOSED and we've already resolved -- this branch
        // catches genuine connection failures.
        if (es.readyState === EventSource.CLOSED) {
          progress.stop();
          resolve({ status: "error", error: "stream closed" });
        }
      });
    });
  }

  async function triggerDaily() {
    const btn = $("trigger-daily");
    if (!btn) return;
    btn.disabled = true;
    setRunStatus("status: starting");
    showActivity();
    try {
      const res = await fetch("/trigger/daily", { method: "POST" });
      if (!res.ok) {
        const txt = await res.text();
        setRunStatus("trigger failed: " + res.status);
        alert("Trigger failed (" + res.status + "): " + txt);
        return;
      }
      const data = await res.json();
      setRunStatus("status: queued (" + data.job_id + ")");
      const final = await streamJob(data.job_id);
      await refreshBudget();
      if (final.status === "complete") {
        setTimeout(() => window.location.reload(), 800);
      }
    } catch (e) {
      setRunStatus("trigger error");
    } finally {
      btn.disabled = false;
    }
  }

  // -----------------------------------------------------------------
  // Phase 7: profile editor
  // -----------------------------------------------------------------
  function wireProfileForm() {
    const form = $("profile-form");
    if (!form) return;
    const textarea = $("profile-content");
    const statusEl = $("profile-status");

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!textarea) return;
      statusEl.textContent = "saving...";
      try {
        const res = await fetch("/profile", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: textarea.value }),
        });
        if (!res.ok) {
          const txt = await res.text();
          statusEl.textContent = "error: " + res.status;
          alert("Save failed (" + res.status + "): " + txt);
          return;
        }
        const data = await res.json();
        statusEl.textContent = "saved (" + data.bytes + " bytes)";
      } catch (err) {
        statusEl.textContent = "network error";
      }
    });
  }

  // -----------------------------------------------------------------
  // Phase 7: per-item Save / Read toggles on briefing pages
  // -----------------------------------------------------------------
  // Briefings emit `<!-- item:slug-N priority:N status:S -->` HTML
  // comments before each <h3>. We walk those, build a toolbar, and
  // sync state with the backend.
  const ITEM_COMMENT_RE = /^\s*item:([a-z0-9][a-z0-9-]*)\s+priority:(\d)\s+status:([a-z]+)/;

  function findItems(article) {
    const items = [];
    const walker = document.createTreeWalker(
      article,
      NodeFilter.SHOW_COMMENT,
      null
    );
    let n;
    while ((n = walker.nextNode())) {
      const m = (n.nodeValue || "").match(ITEM_COMMENT_RE);
      if (!m) continue;
      let el = n.nextSibling;
      while (el && el.nodeType !== 1) el = el.nextSibling;
      if (!el || el.tagName !== "H3") continue;
      items.push({
        id: m[1],
        priority: parseInt(m[2], 10),
        status: m[3],
        heading: el,
      });
    }
    return items;
  }

  async function fetchItemState(date) {
    try {
      const res = await fetch("/api/state/" + encodeURIComponent(date));
      if (!res.ok) return { saved: [], read: [] };
      return await res.json();
    } catch (e) {
      return { saved: [], read: [] };
    }
  }

  async function toggleItem(kind, date, itemId) {
    const url = "/briefing/" + encodeURIComponent(date) +
                "/item/" + encodeURIComponent(itemId) + "/" + kind;
    const res = await fetch(url, { method: "POST" });
    if (!res.ok) return null;
    return res.json();
  }

  function makeToolbar(item, date, initialState) {
    const tb = document.createElement("span");
    tb.className = "item-toolbar";
    item.heading.id = item.id;

    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.textContent = initialState.saved ? "SAVED" : "SAVE";
    if (initialState.saved) saveBtn.classList.add("active");

    const readBtn = document.createElement("button");
    readBtn.type = "button";
    readBtn.textContent = initialState.read ? "READ" : "MARK READ";
    if (initialState.read) {
      readBtn.classList.add("active");
      item.heading.classList.add("item-read");
    }

    saveBtn.addEventListener("click", async () => {
      saveBtn.disabled = true;
      const r = await toggleItem("save", date, item.id);
      saveBtn.disabled = false;
      if (!r) return;
      saveBtn.textContent = r.saved ? "SAVED" : "SAVE";
      saveBtn.classList.toggle("active", !!r.saved);
    });

    readBtn.addEventListener("click", async () => {
      readBtn.disabled = true;
      const r = await toggleItem("read", date, item.id);
      readBtn.disabled = false;
      if (!r) return;
      readBtn.textContent = r.read ? "READ" : "MARK READ";
      readBtn.classList.toggle("active", !!r.read);
      item.heading.classList.toggle("item-read", !!r.read);
    });

    const askBtn = document.createElement("button");
    askBtn.type = "button";
    askBtn.textContent = "ASK";
    askBtn.title = "Ask a follow-up question about this item";

    tb.appendChild(saveBtn);
    tb.appendChild(readBtn);
    tb.appendChild(askBtn);
    return { toolbar: tb, askBtn };
  }

  // -----------------------------------------------------------------
  // Phase 9: follow-up Q&A panel
  // -----------------------------------------------------------------
  // Each item gets a hidden <section class="follow-up"> appended after
  // the next sibling block-of-text. Toggling ASK loads the conversation
  // (lazy) and then the thread + textarea are interactive. The agent
  // runs synchronously on the server side -- we just wait for the POST.
  function buildPanel(date, item) {
    const panel = document.createElement("section");
    panel.className = "follow-up";
    panel.dataset.itemId = item.id;
    panel.hidden = true;

    const thread = document.createElement("div");
    thread.className = "follow-up-thread";
    thread.dataset.empty = "true";

    const form = document.createElement("form");
    form.className = "follow-up-form";

    const textarea = document.createElement("textarea");
    textarea.className = "follow-up-input";
    textarea.placeholder = "Ask a follow-up...";
    textarea.rows = 3;
    textarea.maxLength = 4000;

    const submit = document.createElement("button");
    submit.type = "submit";
    submit.className = "btn follow-up-submit";
    submit.textContent = "ASK";

    const statusEl = document.createElement("span");
    statusEl.className = "follow-up-status muted";

    form.appendChild(textarea);
    const row = document.createElement("div");
    row.className = "follow-up-actions";
    row.appendChild(submit);
    row.appendChild(statusEl);
    form.appendChild(row);

    panel.appendChild(thread);
    panel.appendChild(form);

    // Insert after the heading's next sibling block (the rendered item
    // body). We append at the end of all the item's siblings, just
    // before the next item comment or section header.
    let cursor = item.heading.nextSibling;
    let lastSibling = item.heading;
    while (cursor) {
      if (cursor.nodeType === 8) {
        // HTML comment -- if it's another item:..., stop.
        if ((cursor.nodeValue || "").match(ITEM_COMMENT_RE)) break;
      }
      if (cursor.nodeType === 1 && (cursor.tagName === "H2" || cursor.tagName === "H3")) {
        break;
      }
      lastSibling = cursor;
      cursor = cursor.nextSibling;
    }
    if (lastSibling.parentNode) {
      lastSibling.parentNode.insertBefore(panel, lastSibling.nextSibling);
    }

    return { panel, thread, form, textarea, submit, statusEl };
  }

  function renderThread(threadEl, messages) {
    threadEl.textContent = "";
    if (!messages || messages.length === 0) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No follow-up questions yet.";
      threadEl.appendChild(empty);
      threadEl.dataset.empty = "true";
      return;
    }
    threadEl.dataset.empty = "false";
    messages.forEach((msg) => {
      const block = document.createElement("div");
      block.className = "follow-up-message role-" + (msg.role || "user");
      const label = document.createElement("span");
      label.className = "follow-up-role";
      label.textContent = msg.role === "assistant" ? "AGENT" : "STACK";
      const body = document.createElement("pre");
      body.className = "follow-up-body";
      body.textContent = msg.content || "";
      block.appendChild(label);
      block.appendChild(body);
      threadEl.appendChild(block);
    });
    threadEl.scrollTop = threadEl.scrollHeight;
  }

  async function loadConversation(date, itemId) {
    const url =
      "/briefing/" + encodeURIComponent(date) +
      "/item/" + encodeURIComponent(itemId) + "/conversation";
    const res = await fetch(url);
    if (!res.ok) return { messages: [] };
    return res.json();
  }

  async function askQuestion(date, itemId, question) {
    const url =
      "/briefing/" + encodeURIComponent(date) +
      "/item/" + encodeURIComponent(itemId) + "/ask";
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    if (!res.ok) {
      let msg = "request failed (" + res.status + ")";
      try {
        const data = await res.json();
        if (data && data.detail) msg = data.detail;
      } catch (e) { /* keep default */ }
      throw new Error(msg);
    }
    return res.json();
  }

  function wirePanel(date, item, panelRefs) {
    const { panel, thread, form, textarea, submit, statusEl } = panelRefs;
    let loaded = false;

    const open = async () => {
      panel.hidden = false;
      if (!loaded) {
        statusEl.textContent = "loading...";
        try {
          const record = await loadConversation(date, item.id);
          renderThread(thread, record.messages || []);
          statusEl.textContent = "";
        } catch (e) {
          statusEl.textContent = "load failed";
        }
        loaded = true;
      }
    };
    const close = () => { panel.hidden = true; };

    item.askBtn.addEventListener("click", () => {
      if (panel.hidden) open();
      else close();
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const question = (textarea.value || "").trim();
      if (!question) {
        statusEl.textContent = "enter a question first";
        return;
      }
      submit.disabled = true;
      textarea.disabled = true;
      statusEl.textContent = "thinking...";
      try {
        const record = await askQuestion(date, item.id, question);
        renderThread(thread, record.messages || []);
        textarea.value = "";
        statusEl.textContent = "";
      } catch (err) {
        statusEl.textContent = "error: " + (err.message || "unknown");
      } finally {
        submit.disabled = false;
        textarea.disabled = false;
        textarea.focus();
      }
    });
  }

  async function wireBriefingItems() {
    const article = document.querySelector("article.briefing");
    if (!article) return;
    const date = article.getAttribute("data-briefing-date");
    if (!date) return;

    const items = findItems(article);
    if (items.length === 0) return;

    const state = await fetchItemState(date);
    const savedSet = new Set(state.saved || []);
    const readSet = new Set(state.read || []);

    items.forEach((item) => {
      const initial = {
        saved: savedSet.has(item.id),
        read: readSet.has(item.id),
      };
      const tb = makeToolbar(item, date, initial);
      item.heading.appendChild(tb.toolbar);
      item.askBtn = tb.askBtn;
      const panelRefs = buildPanel(date, item);
      wirePanel(date, item, panelRefs);
    });

    // Engagement: log clicks on outbound source links
    article.querySelectorAll("a[href^='http']").forEach((a) => {
      a.addEventListener("click", () => {
        let h = a.previousElementSibling || a.parentElement;
        while (h && !(h.tagName === "H3" && h.id)) h = h.previousElementSibling;
        if (!h) return;
        fetch(
          "/briefing/" + encodeURIComponent(date) +
          "/item/" + encodeURIComponent(h.id) + "/click",
          { method: "POST", keepalive: true }
        ).catch(() => {});
      });
    });
  }

  // -----------------------------------------------------------------
  // Phase 10: custom briefing modal + trigger
  // -----------------------------------------------------------------
  function openCustomModal() {
    const modal = $("custom-modal");
    if (!modal) return;
    modal.hidden = false;
    const ta = $("custom-focus");
    if (ta) {
      ta.value = "";
      setTimeout(() => ta.focus(), 0);
    }
    const statusEl = $("custom-status");
    if (statusEl) statusEl.textContent = "";
  }
  function closeCustomModal() {
    const modal = $("custom-modal");
    if (modal) modal.hidden = true;
  }

  async function submitCustom(e) {
    e.preventDefault();
    const ta = $("custom-focus");
    const statusEl = $("custom-status");
    const focus = (ta && ta.value || "").trim();
    if (!focus) {
      if (statusEl) statusEl.textContent = "enter a focus area";
      return;
    }
    if (statusEl) statusEl.textContent = "starting...";
    try {
      const res = await fetch("/trigger/custom", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ focus }),
      });
      if (!res.ok) {
        let msg = "trigger failed (" + res.status + ")";
        try {
          const data = await res.json();
          if (data && data.detail) msg = data.detail;
        } catch (e) { /* keep default */ }
        if (statusEl) statusEl.textContent = msg;
        return;
      }
      const data = await res.json();
      closeCustomModal();
      setRunStatus("status: queued (" + data.job_id + ")");
      showActivity();
      const final = await streamJob(data.job_id);
      await refreshBudget();
      if (final.status === "complete") {
        setTimeout(() => window.location.reload(), 800);
      }
    } catch (err) {
      if (statusEl) statusEl.textContent = "network error";
    }
  }

  function wireCustomPage() {
    const btn = $("trigger-custom");
    if (btn) btn.addEventListener("click", openCustomModal);

    const form = $("custom-form");
    if (form) form.addEventListener("submit", submitCustom);

    const modal = $("custom-modal");
    if (!modal) return;
    modal.querySelectorAll("[data-modal-close]").forEach((el) => {
      el.addEventListener("click", closeCustomModal);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !modal.hidden) closeCustomModal();
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    refreshBudget();
    const btn = $("trigger-daily");
    if (btn) btn.addEventListener("click", triggerDaily);
    wireProfileForm();
    wireBriefingItems();
    wireCustomPage();
  });
})();
