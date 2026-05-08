/* AI News Agent dashboard JS — Routines edition.
 *
 * Phase R6 rewrite, trimmed for the Vercel deploy. v1's SSE streaming
 * and inline-await flows are gone. Daily generation runs on a routine
 * cron, not the dashboard. Follow-up Q&A and custom briefings queue
 * request files on the data branch and poll for the response.
 *
 * What this script does:
 *   - On a briefing page: adds an ASK button to each item, opens a
 *     follow-up panel, queues a question to the data branch, polls
 *     /follow-up/{id}/status until ready, renders the answer.
 *   - On the custom page: wires the modal + form, queues a custom
 *     briefing request, polls /custom/status/{id} until ready.
 *
 * Removed for the Vercel deploy: profile editor, saved-items toggle,
 * read-flag toggle. Vercel's filesystem is ephemeral per-invocation;
 * those features need a different store and will land in a follow-up.
 *
 * Vanilla JS, no build step. Single-user, single-tab. The browser
 * forwards basic-auth credentials to fetch() automatically.
 *
 * Security: never use innerHTML with response bodies. Markdown answers
 * render as plain text inside <pre>; users click through to the data-
 * branch file for hyperlinked views.
 */

(() => {
  const $ = (id) => document.getElementById(id);

  // ---------------------------------------------------------------
  // Briefing-page item buttons (ASK only, in the Vercel deploy)
  // ---------------------------------------------------------------
  // Briefings emit `<!-- item:slug -->` HTML comments before each <h3>.
  // Walk those, attach an ASK button + follow-up panel.
  const ITEM_COMMENT_RE = /^\s*item:([a-z0-9][a-z0-9-]*)/;

  function findItems(article) {
    const items = [];
    const walker = document.createTreeWalker(article, NodeFilter.SHOW_COMMENT, null);
    let n;
    while ((n = walker.nextNode())) {
      const m = (n.nodeValue || "").match(ITEM_COMMENT_RE);
      if (!m) continue;
      let el = n.nextSibling;
      while (el && el.nodeType !== 1) el = el.nextSibling;
      if (!el || el.tagName !== "H3") continue;
      items.push({ id: m[1], heading: el });
    }
    return items;
  }

  function makeAskButton(item) {
    item.heading.id = item.id;
    const tb = document.createElement("span");
    tb.className = "item-toolbar";
    const askBtn = document.createElement("button");
    askBtn.type = "button";
    askBtn.textContent = "ASK";
    tb.appendChild(askBtn);
    return { toolbar: tb, askBtn };
  }

  function buildPanel(item) {
    const panel = document.createElement("div");
    panel.className = "follow-up-panel";
    panel.hidden = true;

    const status = document.createElement("p");
    status.className = "muted follow-up-status";
    status.textContent = "";

    const answer = document.createElement("pre");
    answer.className = "follow-up-answer";
    answer.hidden = true;

    const form = document.createElement("form");
    form.className = "follow-up-form";
    const textarea = document.createElement("textarea");
    textarea.rows = 3;
    textarea.placeholder = "Ask a follow-up about this item...";
    textarea.required = true;
    const submit = document.createElement("button");
    submit.type = "submit";
    submit.className = "btn";
    submit.textContent = "QUEUE QUESTION";
    form.appendChild(textarea);
    form.appendChild(submit);

    panel.appendChild(form);
    panel.appendChild(status);
    panel.appendChild(answer);
    item.heading.parentElement.insertBefore(panel, item.heading.nextSibling);

    return { panel, status, answer, form, textarea, submit };
  }

  function pollForAnswer(requestId, panelRefs) {
    const { status, answer, submit, textarea } = panelRefs;
    status.textContent =
      "queued (id " + requestId + "). Drained at next 12:00 UTC daily run; checking every 5 min...";
    let attempts = 0;
    const maxAttempts = 24 * 12; // 24 hours at 5-min intervals

    const tick = async () => {
      attempts += 1;
      try {
        const res = await fetch("/follow-up/" + encodeURIComponent(requestId) + "/status");
        if (!res.ok) {
          status.textContent = "status check failed (" + res.status + "); will retry";
          return;
        }
        const data = await res.json();
        if (data.status === "ready") {
          status.textContent = "answered.";
          answer.textContent = data.answer_md || "";
          answer.hidden = false;
          submit.disabled = false;
          textarea.disabled = false;
          textarea.value = "";
          clearInterval(handle);
          return;
        }
        status.textContent =
          "queued (id " + requestId + "). " + attempts + " checks; next 12:00 UTC daily run drains the queue.";
        if (attempts >= maxAttempts) {
          status.textContent =
            "timeout after 24hr. Check the daily routine session URL for errors. Request id: " + requestId;
          clearInterval(handle);
        }
      } catch (e) {
        status.textContent = "network error during status check; will retry";
      }
    };
    tick();
    const handle = setInterval(tick, 300000);  // 5 min
  }

  function wirePanel(date, item, panelRefs) {
    const { panel, form, textarea, submit, status } = panelRefs;
    item.askBtn.addEventListener("click", () => {
      panel.hidden = !panel.hidden;
      if (!panel.hidden) textarea.focus();
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const question = (textarea.value || "").trim();
      if (!question) {
        status.textContent = "enter a question first";
        return;
      }
      submit.disabled = true;
      textarea.disabled = true;
      status.textContent = "queueing...";
      try {
        const res = await fetch(
          "/briefing/" + encodeURIComponent(date) +
          "/item/" + encodeURIComponent(item.id) + "/ask",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              question,
              item_headline: (item.heading.textContent || "").trim(),
            }),
          }
        );
        if (!res.ok) {
          let msg = "queue failed (" + res.status + ")";
          try {
            const data = await res.json();
            if (data && data.detail) msg = data.detail;
          } catch (_) { /* keep default */ }
          status.textContent = "error: " + msg;
          submit.disabled = false;
          textarea.disabled = false;
          return;
        }
        const data = await res.json();
        pollForAnswer(data.request_id, panelRefs);
      } catch (err) {
        status.textContent = "network error";
        submit.disabled = false;
        textarea.disabled = false;
      }
    });
  }

  function wireBriefingItems() {
    const article = document.querySelector("article.briefing");
    if (!article) return;
    const date = article.getAttribute("data-briefing-date");
    if (!date) return;

    const items = findItems(article);
    if (items.length === 0) return;

    items.forEach((item) => {
      const tb = makeAskButton(item);
      item.heading.appendChild(tb.toolbar);
      item.askBtn = tb.askBtn;
      const panelRefs = buildPanel(item);
      wirePanel(date, item, panelRefs);
    });
  }

  // ---------------------------------------------------------------
  // Custom briefing — async via processor routine
  // ---------------------------------------------------------------
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

  function setQueueMsgReady(filename, url) {
    const queueMsg = $("queue-msg");
    if (!queueMsg) return;
    while (queueMsg.firstChild) queueMsg.removeChild(queueMsg.firstChild);
    queueMsg.appendChild(document.createTextNode("Ready! "));
    const a = document.createElement("a");
    a.href = url;
    a.textContent = filename;
    queueMsg.appendChild(a);
  }

  function pollForCustom(requestId) {
    const queueSection = $("queue-section");
    const queueMsg = $("queue-msg");
    const queueId = $("queue-id");
    if (queueSection) queueSection.hidden = false;
    if (queueId) queueId.textContent = requestId;

    let attempts = 0;
    const maxAttempts = 24 * 12; // 24hr at 5-min intervals

    const tick = async () => {
      attempts += 1;
      try {
        const res = await fetch("/custom/status/" + encodeURIComponent(requestId));
        if (!res.ok) {
          if (queueMsg) queueMsg.textContent = "status check failed (" + res.status + "); will retry";
          return;
        }
        const data = await res.json();
        if (data.status === "ready") {
          setQueueMsgReady(data.filename, data.url);
          clearInterval(handle);
          setTimeout(() => window.location.reload(), 2000);
          return;
        }
        if (queueMsg) {
          queueMsg.textContent =
            "queued. " + attempts + " checks; drained at next 12:00 UTC daily run.";
        }
        if (attempts >= maxAttempts) {
          if (queueMsg) {
            queueMsg.textContent =
              "timeout after 24hr. Check the daily routine session URL. Request id: " + requestId;
          }
          clearInterval(handle);
        }
      } catch (e) {
        if (queueMsg) queueMsg.textContent = "network error during status check; will retry";
      }
    };
    tick();
    const handle = setInterval(tick, 300000);  // 5 min
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
    if (statusEl) statusEl.textContent = "queueing...";
    try {
      const res = await fetch("/trigger/custom", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ focus }),
      });
      if (!res.ok) {
        let msg = "queue failed (" + res.status + ")";
        try {
          const data = await res.json();
          if (data && data.detail) msg = data.detail;
        } catch (_) { /* keep default */ }
        if (statusEl) statusEl.textContent = msg;
        return;
      }
      const data = await res.json();
      closeCustomModal();
      pollForCustom(data.request_id);
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
    wireBriefingItems();
    wireCustomPage();
  });
})();
