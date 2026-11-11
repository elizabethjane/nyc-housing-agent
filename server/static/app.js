// NYC Housing Agent chat page. No framework; talks to /api/chat.
(() => {
  const form = document.getElementById("composer");
  const input = document.getElementById("message");
  const send = document.getElementById("send");
  const counter = document.getElementById("counter");
  const list = document.getElementById("messages");
  const feedbackTemplate = document.getElementById("feedback-template");
  const MAX = 2000;
  let busy = false;

  // --- Bot check (Cloudflare Turnstile), only when the server has a site key.
  // The token goes with the next message; once the server accepts it, the
  // session stays verified, so later messages need no token.
  const botBox = document.getElementById("bot-check");
  let botToken = null;
  let botWidget = null;
  fetch("/api/status").then((r) => r.json()).then((s) => {
    if (!s.turnstile_site_key) return;
    botBox.hidden = false;
    window.onTurnstileLoad = () => {
      botWidget = window.turnstile.render(botBox, {
        sitekey: s.turnstile_site_key,
        callback: (token) => { botToken = token; },
        "expired-callback": () => { botToken = null; },
      });
    };
    const script = document.createElement("script");
    script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?onload=onTurnstileLoad&render=explicit";
    script.async = true;
    document.head.appendChild(script);
  }).catch(() => {});

  // --- Safe rendering of the agent's Markdown: escape everything, then allow
  // links (http/https only), bold, inline code, headings, and lists.
  const escapeHtml = (s) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  function inline(text) {
    let s = escapeHtml(text);
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, label, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+[^\s<).,;:!?])/g,
      (_, pre, url) => `${pre}<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`);
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    return s;
  }

  function renderMarkdown(md) {
    const out = [];
    let listType = null;
    let para = [];
    const flushPara = () => { if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; } };
    const closeList = () => { if (listType) { out.push(`</${listType}>`); listType = null; } };
    for (const raw of md.split("\n")) {
      const line = raw.trimEnd();
      const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
      const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
      const heading = line.match(/^#{1,6}\s+(.*)$/);
      if (bullet || numbered) {
        flushPara();
        const type = bullet ? "ul" : "ol";
        if (listType !== type) { closeList(); out.push(`<${type}>`); listType = type; }
        out.push(`<li>${inline((bullet || numbered)[1])}</li>`);
      } else if (heading) {
        flushPara(); closeList();
        out.push(`<h3>${inline(heading[1])}</h3>`);
      } else if (!line.trim()) {
        flushPara(); closeList();
      } else {
        closeList();
        para.push(line);
      }
    }
    flushPara(); closeList();
    return out.join("");
  }

  // --- Conversation ------------------------------------------------------------
  function scrollToEnd() { window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" }); }

  function addUser(text) {
    const li = document.createElement("li");
    li.className = "msg-user";
    li.textContent = text;
    list.appendChild(li);
  }

  function addTyping() {
    const li = document.createElement("li");
    li.className = "msg-agent typing";
    li.innerHTML = '<span class="dot"></span><span class="dot"></span><span class="dot"></span><span>Looking that up…</span>';
    list.appendChild(li);
    return li;
  }

  function addAgent(reply, turnId, isNotice) {
    const li = document.createElement("li");
    li.className = "msg-agent" + (isNotice ? " notice-reply" : "");
    const body = document.createElement("div");
    body.className = "body";
    body.innerHTML = renderMarkdown(reply);
    li.appendChild(body);
    if (turnId && !isNotice) li.appendChild(feedbackFor(turnId));
    list.appendChild(li);
  }

  function feedbackFor(turnId) {
    const node = feedbackTemplate.content.firstElementChild.cloneNode(true);
    node.querySelectorAll("button").forEach((btn) => {
      btn.addEventListener("click", async () => {
        node.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
        node.querySelector(".feedback-thanks").hidden = false;
        try {
          await fetch("/api/feedback", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ turn_id: turnId, rating: btn.dataset.rating }) });
        } catch (_) { /* feedback is best effort */ }
      });
    });
    return node;
  }

  async function ask(text) {
    text = text.trim();
    if (!text || busy || text.length > MAX) return;
    busy = true;
    document.body.classList.remove("empty");
    addUser(text);
    input.value = "";
    updateComposer();
    const typing = addTyping();
    scrollToEnd();
    try {
      const res = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, turnstile_token: botToken }) });
      const data = await res.json().catch(() => ({}));
      if (data.error === "bot_check") {
        if (botWidget !== null && window.turnstile) window.turnstile.reset(botWidget);
        botToken = null;
      } else if (res.ok && botToken) {
        botBox.hidden = true;  // verified for this conversation
        botToken = null;
      }
      typing.remove();
      addAgent(data.reply || "Sorry, something went wrong. Please try again.", data.turn_id,
        Boolean(data.blocked) || !res.ok);
    } catch (_) {
      typing.remove();
      addAgent("Can't reach the server. Check your connection and try again.", null, true);
    } finally {
      busy = false;
      updateComposer();
      scrollToEnd();
      input.focus();
    }
  }

  // --- Composer --------------------------------------------------------------------
  function updateComposer() {
    const n = input.value.length;
    counter.textContent = `${n.toLocaleString()} / ${MAX.toLocaleString()}`;
    counter.classList.toggle("over", n > MAX);
    send.disabled = busy || !input.value.trim() || n > MAX;
    // Measure from zero height so the box shrinks back after text is deleted.
    input.style.height = "0px";
    input.style.height = Math.min(Math.max(input.scrollHeight + 2, 44), 180) + "px";
  }

  input.addEventListener("input", updateComposer);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(input.value); }
  });
  form.addEventListener("submit", (e) => { e.preventDefault(); ask(input.value); });
  document.querySelectorAll(".starter").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));
  document.getElementById("new-chat").addEventListener("click", async () => {
    try { await fetch("/api/new", { method: "POST" }); } catch (_) { /* start fresh locally anyway */ }
    list.innerHTML = "";
    document.body.classList.add("empty");
    input.value = "";
    updateComposer();
    input.focus();
  });
  window.addEventListener("resize", updateComposer);
  if (document.fonts) document.fonts.ready.then(updateComposer);
  updateComposer();
})();
