// TAG Assistant front end. Plain DOM, no framework. All server text goes in
// through textContent (or the tiny escaped markdown renderer), never raw HTML.

const $ = (sel) => document.querySelector(sel);
const thread = $("#thread");
const scroller = $("#view-chat");
const sheet = document.querySelector(".sheet");
const input = $("#input");
const sendBtn = $("#send");

const STORE_KEY = "ea.conversation";
const ACTIVE_KEY = "ea.lastActive";
// After a gap this long, opening the app starts a fresh conversation. Dave
// never has to manage chats; old ones stay under Recent.
const FRESH_AFTER_MS = 2 * 60 * 60 * 1000;
let busy = false;
// A conversation started from a brief carries that meeting until its first message is sent.
let pendingTopic = null; // { eventId, subject }

function readStored(key = STORE_KEY) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function writeStored(value, key = STORE_KEY) {
  try { value ? localStorage.setItem(key, value) : localStorage.removeItem(key); } catch { /* private mode */ }
}

let conversationId = (() => {
  const lastActive = Number(readStored(ACTIVE_KEY) || 0);
  return Date.now() - lastActive < FRESH_AFTER_MS ? readStored() : null;
})();

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...options,
  });
  if (res.status === 401) { location.href = "/auth/login"; throw new Error("Signed out"); }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    throw new Error(detail);
  }
  return res.json();
}

function scrollToEnd() {
  requestAnimationFrame(() => { scroller.scrollTop = scroller.scrollHeight; });
}

// ── minimal markdown: escaped first, then bold/italic/lists/headings ───────────

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function inline(text) {
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*(?!\s)(.+?)\*(?!\*)/g, "$1<em>$2</em>");
}
function renderMarkdown(source) {
  const out = [];
  let list = null;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of (source || "").split("\n")) {
    const line = raw.trim();
    if (!line || /^-{3,}$/.test(line) || /^\|?\s*:?-{3,}/.test(line)) { closeList(); continue; }
    let m;
    if ((m = line.match(/^#{1,6}\s+(.*)/))) { closeList(); out.push(`<h4>${inline(m[1])}</h4>`); continue; }
    if ((m = line.match(/^[-*•]\s+(.*)/))) {
      if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
      out.push(`<li>${inline(m[1])}</li>`); continue;
    }
    if ((m = line.match(/^\d+[.)]\s+(.*)/))) {
      if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
      out.push(`<li>${inline(m[1])}</li>`); continue;
    }
    closeList();
    if (line.startsWith("|")) {
      const cells = line.split("|").map((c) => c.trim()).filter(Boolean);
      out.push(`<p>${inline(cells.join(" · "))}</p>`);
      continue;
    }
    out.push(`<p>${inline(line)}</p>`);
  }
  closeList();
  return out.join("");
}

// ── tabs ───────────────────────────────────────────────────────────────────────

function showTab(name) {
  sheet.dataset.tab = name;
  $("#view-today").hidden = name !== "today";
  $("#view-chat").hidden = name !== "chat";
  document.querySelectorAll(".tab").forEach((t) =>
    t.dataset.tab === name ? t.setAttribute("aria-current", "page") : t.removeAttribute("aria-current"));
  if (name === "chat") scrollToEnd();
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));

function setTopic(subject) {
  const topic = $("#topic");
  topic.hidden = !subject;
  topic.textContent = subject ? `About: ${subject}` : "";
}

function updateEmptyState() {
  $("#thread-empty").hidden = thread.children.length > 0;
}

// ── today ──────────────────────────────────────────────────────────────────────

async function loadToday() {
  try {
    const day = await api("/api/today");
    $("#date").textContent = day.date;
    $("#greeting").textContent = day.name ? `${day.greeting}, ${day.name}` : day.greeting;
    const isToday = day.agenda_title === "Today";
    $("#today-title").textContent = isToday ? "Today" : `${day.agenda_title} · ${day.agenda_date}`;
    const done = $("#done-today");
    done.hidden = !day.done_today;
    done.textContent = day.done_today === 1 ? "Today's meeting is done." : `Today's ${day.done_today} meetings are done.`;
    renderItinerary(day.events, isToday ? "today" : day.agenda_title.toLowerCase());
    renderSignoff(day.pending.filter((a) => !["reply_email", "book_meeting"].includes(a.kind)));
    renderRequests(day.requests || []);
    renderJunk(day.junk_moved || []);
    // While the worker is preparing briefs, check back so the "Brief" buttons appear.
    clearTimeout(loadToday.timer);
    if (day.events.some((e) => e.brief === "preparing")) loadToday.timer = setTimeout(loadToday, 5000);
  } catch (err) {
    $("#itinerary").replaceChildren(el("li", { class: "itinerary__empty", text: `Couldn't load your calendar: ${err.message}` }));
  }
}

function renderItinerary(events, when) {
  const list = $("#itinerary");
  if (!events.length) {
    list.replaceChildren(el("li", { class: "itinerary__empty", text: `Nothing on the calendar ${when === "today" || when === "tomorrow" ? when : `on ${when}`}.` }));
    return;
  }
  list.replaceChildren(...events.map((e) => {
    const classes = ["stop"];
    if (e.past) classes.push("stop--past");
    if (e.now) classes.push("stop--now");
    if (e.show_as === "tentative") classes.push("stop--tentative");
    const meta = [e.location, e.show_as === "tentative" ? "tentative" : ""].filter(Boolean).join(" · ");
    const pill = e.brief === "ready" ? el("span", { class: "pill pill--brief", text: "Brief" })
      : e.brief === "preparing" ? el("span", { class: "pill", text: "Preparing…" }) : null;
    const join = e.join_url && !e.past
      ? el("a", { class: "join", href: e.join_url, target: "_blank", rel: "noopener", text: "Join",
                  onclick: (ev) => ev.stopPropagation() })
      : null;
    return el("li", { class: classes.join(" ") },
      el("button", { class: "stop__open", type: "button", "aria-label": `${e.subject}, ${e.start}. Open brief`,
                     onclick: () => openBrief(e) },
        el("span", { class: "stop__time", text: e.start }),
        el("span", { class: "stop__what" },
          el("span", { class: "stop__subject", text: e.subject }),
          meta || pill ? el("span", { class: "stop__meta" }, pill, meta) : null)),
      join || el("span"),
    );
  }));
}

// ── meeting briefs ─────────────────────────────────────────────────────────────

let openEvent = null;

// Sources of the open brief, by label ("Email 2", "Web 1", "Invite").
let briefSources = new Map();

function sourceIndex(material) {
  const index = new Map();
  const m = material || {};
  if (m.invite) index.set("Invite", { kind: "Calendar invite", title: "Invite", excerpt: m.invite.excerpt || "No description in the invite." });
  for (const e of m.emails || []) {
    index.set(e.label, { kind: e.label, title: e.subject, meta: `${e.from} \u00b7 ${e.received}`, excerpt: e.excerpt,
                         link: e.web_link, linkText: "Open in Outlook" });
  }
  for (const a of m.attachments || []) {
    index.set(a.label, { kind: a.label, title: a.name, meta: `Attached to ${a.from_email}`, excerpt: a.excerpt,
                         link: a.web_link, linkText: "Open the email in Outlook" });
  }
  for (const w of m.web || []) {
    let host = w.url;
    try { host = new URL(w.url).hostname.replace(/^www\./, ""); } catch { /* keep raw */ }
    index.set(w.label, { kind: w.label, title: w.about || host, meta: host, excerpt: w.fact, link: w.url, linkText: `Open ${host}` });
  }
  return index;
}

function showSource(label) {
  const src = briefSources.get(label);
  if (!src) return;
  $("#src-kind").textContent = src.kind;
  $("#src-title").textContent = src.title || "";
  $("#src-meta").textContent = src.meta || "";
  $("#src-excerpt").textContent = src.excerpt || "";
  const open = $("#src-open");
  open.hidden = !src.link;
  if (src.link) { open.href = src.link; open.textContent = src.linkText; }
  $("#source").hidden = false;
}

$("#src-close").addEventListener("click", () => { $("#source").hidden = true; });
$("#source").addEventListener("click", (e) => { if (e.target.id === "source") $("#source").hidden = true; });

function cited(text) {
  // "[Email 2, Web 1]" becomes tappable chips that open the source; unknown labels stay plain.
  const parts = String(text).split(/(\[(?:Invite|Email \d+|Attachment \d+|Web \d+)(?:,\s*(?:Invite|Email \d+|Attachment \d+|Web \d+))*\])/g);
  const out = [];
  for (const p of parts.filter(Boolean)) {
    if (!/^\[/.test(p)) { out.push(p); continue; }
    for (const label of p.slice(1, -1).split(/,\s*/)) {
      out.push(briefSources.has(label)
        ? el("button", { class: "cite", type: "button", text: label, onclick: () => showSource(label) })
        : el("span", { class: "cite cite--plain", text: label }));
    }
  }
  return out;
}

function briefSection(title, items) {
  if (!items || !items.length) return null;
  return el("section", { class: "bsec" },
    el("h3", { class: "bsec__title", text: title }),
    el("ul", { class: "bsec__list" }, items.map((i) => el("li", {}, ...cited(i)))));
}

function whoSection(who) {
  if (!who || !who.length) return null;
  return el("section", { class: "bsec" },
    el("h3", { class: "bsec__title", text: "Who" }),
    el("ul", { class: "bsec__list bsec__list--who" }, who.map((w) => {
      const line = [w.role, w.organization].filter(Boolean).join(", ");
      return el("li", {},
        el("span", { class: "who__name", text: w.name }),
        line ? el("span", { class: "who__role", text: line }) : null,
        w.note ? el("span", { class: "who__note" }, ...cited(w.note)) : null);
    })));
}

function renderBrief(data) {
  const b = data.brief || {};
  briefSources = sourceIndex(b.material);
  const content = [
    el("p", { class: "bs-headline" }, ...cited(b.headline || "")),
    whoSection(b.who),
    briefSection("Context", b.context),
    briefSection("Background", b.background),
    briefSection("Prep", b.prep),
    briefSection("Not covered", b.gaps),
  ];
  const sources = [...briefSources.entries()].filter(([label]) => label !== "Invite");
  if (sources.length) {
    content.push(el("section", { class: "bsec" },
      el("h3", { class: "bsec__title", text: `Sources (${sources.length})` }),
      el("ul", { class: "rows" }, sources.map(([label, src]) => el("li", {},
        el("button", { class: "row row--tap", type: "button", onclick: () => showSource(label) },
          el("span", { class: "row__top" },
            el("span", { class: "row__title", text: src.title }),
            el("span", { class: "row__when", text: label })),
          src.meta ? el("span", { class: "row__sub", text: src.meta }) : null))))));
  }
  content.push(el("p", { class: "card__note", text: `Prepared ${ago(data.updated_at)} from your calendar, email and the web.` }));
  $("#bs-content").replaceChildren(...content.filter(Boolean));
}

async function loadBrief(event) {
  const target = $("#bs-content");
  try {
    const data = await api(`/api/briefs/${encodeURIComponent(event.id)}`);
    if (openEvent !== event) return; // sheet moved on
    $("#bs-refresh").hidden = data.status === "preparing";
    if (data.status === "ready") {
      renderBrief(data);
      $("#bs-foot").hidden = false;
      openEvent.conversationId = data.conversation_id;
    } else if (data.status === "preparing") {
      target.replaceChildren(el("p", { class: "thinking", text: "Preparing the brief from your calendar and email" }));
      setTimeout(() => openEvent === event && loadBrief(event), 4000);
    } else {
      target.replaceChildren(el("p", { class: "error", text: `Couldn't prepare this brief (${data.error || "unknown error"}).` }),
        prepareButton(event, "Try again"));
    }
  } catch (err) {
    if (openEvent !== event) return;
    $("#bs-refresh").hidden = true;
    target.replaceChildren(
      el("p", { class: "card__note", text: "No brief for this meeting yet." }),
      prepareButton(event, "Prepare brief"));
  }
}

function prepareButton(event, label, refresh = false) {
  return el("button", { class: "btn btn--sign", type: "button", text: label, onclick: () => prepare(event, refresh) });
}

async function prepare(event, refresh = false) {
  $("#bs-foot").hidden = true;
  $("#bs-content").replaceChildren(el("p", { class: "thinking", text: "Preparing the brief from your calendar and email" }));
  try {
    await api(`/api/briefs/${encodeURIComponent(event.id)}/prepare${refresh ? "?refresh=true" : ""}`, { method: "POST" });
    setTimeout(() => openEvent === event && loadBrief(event), 4000);
    loadToday();
  } catch (err) {
    $("#bs-content").replaceChildren(el("p", { class: "error", text: `Couldn't start: ${err.message}` }));
  }
}

function openBrief(event) {
  openEvent = event;
  $("#bs-title").textContent = event.subject;
  $("#bs-when").textContent = [`${event.start}\u2013${event.end}`, event.location].filter(Boolean).join(" \u00b7 ");
  $("#bs-foot").hidden = true;
  $("#bs-refresh").hidden = true;
  $("#bs-content").replaceChildren(el("p", { class: "thinking", text: "Loading" }));
  $("#briefsheet").hidden = false;
  loadBrief(event);
}

function closeBrief() {
  openEvent = null;
  $("#source").hidden = true;
  $("#briefsheet").hidden = true;
}

$("#bs-close").addEventListener("click", closeBrief);
$("#bs-refresh").addEventListener("click", () => openEvent && prepare(openEvent, true));
$("#bs-ask").addEventListener("click", () => {
  const event = openEvent;
  closeBrief();
  showTab("chat");
  if (event.conversationId) {
    openConversation(event.conversationId);
  } else {
    conversationId = null;
    writeStored(null);
    thread.replaceChildren();
    pendingTopic = { eventId: event.id, subject: event.subject };
    setTopic(event.subject);
    updateEmptyState();
  }
  input.focus();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#briefsheet").hidden) closeBrief(); });

// ── inbox clean-up ─────────────────────────────────────────────────────────────

function renderJunk(moved) {
  const live = moved.filter((m) => !m.undone);
  $("#junk-block").hidden = !moved.length;
  if (!moved.length) return;
  const rows = moved.map((m) => {
    const sub = el("span", { class: "row__sub", text: [m.from, m.reason].filter(Boolean).join(" \u00b7 ") });
    const action = m.undone
      ? el("span", { class: "junk__undone", text: "Put back" })
      : el("button", { class: "link-btn", type: "button", text: "Undo", onclick: async (ev) => {
          const btn = ev.currentTarget;
          btn.disabled = true;
          try {
            await api(`/api/junk/${encodeURIComponent(m.action_id)}/undo/${encodeURIComponent(m.id)}`, { method: "POST" });
            btn.replaceWith(el("span", { class: "junk__undone", text: "Put back" }));
          } catch (err) {
            btn.disabled = false;
            sub.textContent = `Couldn't undo: ${err.message}`;
          }
        } });
    return el("li", { class: "row junk__row" },
      el("span", { class: "junk__text" }, el("span", { class: "row__title", text: m.subject }), sub), action);
  });
  const list = el("ul", { class: "rows" }, rows);
  const summary = el("summary", { text: `Moved ${live.length} to Junk in the last day` });
  $("#junk").replaceChildren(el("details", { class: "card junk" }, summary, list,
    el("p", { class: "card__note", text: "Undo puts it back and I'll leave that sender alone from now on." })));
}

// ── meeting requests ───────────────────────────────────────────────────────────

function renderRequests(requests) {
  $("#requests-block").hidden = !requests.length;
  $("#requests").replaceChildren(...requests.map(requestCard));
}

function requestCard(r) {
  const card = el("div", { class: "card request" });
  const meta = [r.purpose, r.duration_minutes ? `${r.duration_minutes} min` : "",
                r.format && r.format !== "unspecified" ? r.format.replace("_", " ") : ""].filter(Boolean).join(" \u00b7 ");
  const head = [
    el("p", { class: "request__who" }, el("strong", { text: r.from }), " wants to meet"),
    meta ? el("p", { class: "request__meta", text: meta }) : null,
    el("p", { class: "request__subject" }, `\u201c${r.subject}\u201d `,
      r.web_link ? el("a", { href: r.web_link, target: "_blank", rel: "noopener", text: "Open email" }) : null),
  ];
  const body = el("div", { class: "request__body" });
  card.append(...head.filter(Boolean), body);

  if (r.status === "booked") {
    card.classList.add("request--done");
    body.append(el("span", { class: "stamp stamp--done", text: `✓ Booked${r.booked ? ` · ${r.booked.replace(" ET", "")}` : ""}` }));
    return card;
  }
  if (r.status === "waiting") {
    card.classList.add("request--waiting");
    body.append(
      el("p", { class: "request__meta", text: `Times sent. Waiting for ${(r.from || "them").split(" ")[0]} to pick one:` }),
      el("ul", { class: "request__offered" }, r.offered.map((t) => el("li", { text: t.replace(" ET", "") }))),
      el("p", { class: "card__note", text: "I'll book it as soon as they choose one of these." }));
    return card;
  }
  if (r.answer) {
    body.append(el("p", { class: "request__answer", text: `They replied: “${r.answer}”` }));
  }

  if (r.action) {
    body.append(slip(r.action));
    return card;
  }

  const chosen = new Set(r.slots.map((s) => s.start));
  const status = el("p", { class: "card__note" });
  const busyButtons = [];
  const run = async (label, path, payload) => {
    busyButtons.forEach((b) => { b.disabled = true; });
    status.textContent = label;
    try {
      const action = await api(path, { method: "POST", body: JSON.stringify(payload) });
      body.replaceChildren(slip(action));
    } catch (err) {
      busyButtons.forEach((b) => { b.disabled = false; });
      status.textContent = `Couldn't do that: ${err.message}`;
    }
  };

  if (r.proposed) {
    const book = el("button", { class: "btn btn--sign", type: "button", text: `Book ${r.proposed.label.replace(" ET", "")}`,
      onclick: () => run("Checking your calendar\u2026", `/api/requests/${encodeURIComponent(r.id)}/book`, { start: r.proposed.start }) });
    busyButtons.push(book);
    body.append(el("p", { class: "request__meta", text: "They suggested a time:" }), book);
  }

  if (r.slots.length) {
    const chips = el("div", { class: "slots" }, r.slots.map((s) => {
      const chip = el("button", { class: "slot slot--pick", type: "button", "aria-pressed": "true" },
        el("span", { class: "slot__start", text: s.label.replace(/ at /, " \u00b7 ").replace(" ET", "") }));
      chip.addEventListener("click", () => {
        chosen.has(s.start) ? chosen.delete(s.start) : chosen.add(s.start);
        chip.setAttribute("aria-pressed", String(chosen.has(s.start)));
        reply.disabled = !chosen.size;
      });
      return chip;
    }));
    const reply = el("button", { class: "btn " + (r.proposed ? "" : "btn--sign"), type: "button",
      text: r.proposed ? "Reply with other times" : "Reply with these times",
      onclick: () => run("Drafting your reply\u2026", `/api/requests/${encodeURIComponent(r.id)}/reply`,
                         { starts: r.slots.map((s) => s.start).filter((s) => chosen.has(s)) }) });
    busyButtons.push(reply);
    body.append(el("p", { class: "request__meta", text: r.proposed ? "Or offer other times:" : "Times that work for you:" }),
      chips, el("div", { class: "slip__actions" }, reply));
  } else if (!r.proposed) {
    body.append(el("p", { class: "card__note", text: "No free time found in that range." }));
  }

  const dismiss = el("button", { class: "link-btn", type: "button", text: "Dismiss",
    onclick: async () => {
      dismiss.disabled = true;
      try { await api(`/api/requests/${encodeURIComponent(r.id)}/dismiss`, { method: "POST" }); card.remove(); }
      catch (err) { dismiss.disabled = false; status.textContent = err.message; }
      if (!$("#requests").children.length) $("#requests-block").hidden = true;
    } });
  busyButtons.push(dismiss);
  body.append(status, el("div", { class: "request__foot" }, dismiss));
  return card;
}

function renderSignoff(pending) {
  $("#signoff-block").hidden = !pending.length;
  $("#signoff").replaceChildren(...pending.map(slip));
}

// ── cards ──────────────────────────────────────────────────────────────────────

const KIND_LABELS = {
  create_event: "Calendar invite", book_meeting: "Calendar invite",
  move_to_junk: "Inbox clean-up", reply_email: "Email reply",
};

function slip(action) {
  const node = el("div", { class: "slip", "data-action-id": action.action_id });
  fillSlip(node, action);
  return node;
}

function fillSlip(node, action) {
  const [main, ...warnings] = (action.summary || "").split(" · ⚠ ");
  const summary = el("p", { class: "slip__summary" }, main,
    ...warnings.map((w) => el("span", { class: "warn", text: `⚠ ${w}` })));
  const parts = [el("p", { class: "slip__kind", text: KIND_LABELS[action.kind] || action.kind }), summary];
  let emailBox = null;
  if (action.email) {
    parts.push(el("p", { class: "slip__email-meta", text: `To ${action.email.to} \u00b7 ${action.email.subject}` }));
    emailBox = el("textarea", { class: "slip__email", rows: "7", "aria-label": "Email text" });
    emailBox.value = action.email.comment || "";
    emailBox.readOnly = action.status !== "pending";
    parts.push(emailBox);
  }
  const keepIds = new Set();
  if (action.items && action.items.length) {
    const pickable = action.kind === "move_to_junk" && action.status === "pending";
    parts.push(el("ul", { class: "slip__items" }, action.items.map((i) => {
      const text = [el("span", { class: "slip__item-title", text: i.subject }),
                    el("span", { class: "slip__item-sub", text: [i.from, i.reason].filter(Boolean).join(" \u00b7 ") })];
      if (!pickable) return el("li", {}, ...text);
      const box = el("input", { type: "checkbox", "aria-label": `Move \u201c${i.subject}\u201d to Junk` });
      box.checked = true;
      box.addEventListener("change", () => { box.checked ? keepIds.delete(i.id) : keepIds.add(i.id); });
      return el("li", {}, el("label", { class: "slip__pick" }, box, el("span", {}, ...text)));
    })));
    if (pickable) parts.push(el("p", { class: "card__note", text: "Untick anything you want to keep." }));
  }
  node.className = "slip";

  if (action.status === "pending") {
    const approveLabel = action.kind === "reply_email" ? "Send" : "Approve";
    const approve = el("button", { class: "btn btn--sign", type: "button", text: approveLabel });
    const decline = el("button", { class: "btn", type: "button", text: "Decline" });
    const decide = async (verb) => {
      approve.disabled = decline.disabled = true;
      approve.textContent = verb === "approve" ? "Working…" : approve.textContent;
      try {
        const edits = verb !== "approve" ? null
          : emailBox ? { comment: emailBox.value }
          : keepIds.size ? { keep_ids: [...keepIds].join(",") } : null;
        const updated = await api(`/api/actions/${action.action_id}/${verb}`, {
          method: "POST", body: JSON.stringify(edits ? { edits } : {}),
        });
        updateSlips(updated);
        loadToday();
      } catch (err) {
        approve.disabled = decline.disabled = false;
        approve.textContent = approveLabel;
        node.append(el("p", { class: "error", text: `Couldn't ${verb}: ${err.message}` }));
      }
    };
    approve.addEventListener("click", () => decide("approve"));
    decline.addEventListener("click", () => decide("reject"));
    parts.push(el("div", { class: "slip__actions" }, approve, decline));
  } else if (action.status === "executed") {
    node.classList.add("slip--done");
    const r = action.result || {};
    const doneText = action.kind === "move_to_junk"
      ? `✓ Moved ${r.moved ?? ""} to Junk${r.failed && r.failed.length ? ` · ${r.failed.length} failed` : ""}`
      : action.kind === "reply_email" ? "✓ Sent" : "✓ Signed · booked";
    parts.push(el("span", { class: "stamp stamp--done" }, doneText,
      r.join_url ? el("a", { href: r.join_url, target: "_blank", rel: "noopener", text: "Teams link" }) : null,
      r.web_link ? el("a", { href: r.web_link, target: "_blank", rel: "noopener", text: "Outlook" }) : null));
  } else if (action.status === "rejected") {
    node.classList.add("slip--declined");
    parts.push(el("span", { class: "stamp stamp--declined", text: "Declined" }));
  } else if (action.status === "failed") {
    node.classList.add("slip--failed");
    parts.push(el("span", { class: "stamp stamp--failed", text: `Failed: ${action.error || "unknown error"}` }));
  } else {
    parts.push(el("span", { class: "stamp stamp--declined", text: action.status }));
  }
  node.replaceChildren(...parts);
}

function updateSlips(action) {
  document.querySelectorAll(`[data-action-id="${CSS.escape(action.action_id)}"]`).forEach((node) => fillSlip(node, action));
}

function slotsCard(card) {
  const body = [el("p", { class: "card__label", text: `Open times · ${card.duration_minutes} min` })];
  if (!card.slots.length) {
    body.push(el("p", { class: "card__note", text: "No shared free time in that range." }));
  } else {
    const wrap = el("div", { class: "slots" });
    let lastDay = "";
    for (const s of card.slots) {
      const parts = s.start.split(" ");
      const day = parts.slice(0, 3).join(" ");
      const time = parts.slice(3).join(" ");
      const until = s.until.split(" ").slice(3).join(" ");
      if (day !== lastDay) { wrap.append(el("p", { class: "slot-day", text: day })); lastDay = day; }
      wrap.append(el("button", {
        class: "slot", type: "button",
        "aria-label": `Book ${s.start}`,
        onclick: () => send(`Book ${s.start} for ${card.duration_minutes} minutes.`),
      }, el("span", { class: "slot__start", text: time }), el("span", { class: "slot__until", text: `free until ${until}` })));
    }
    body.push(wrap);
  }
  if (card.note) body.push(el("p", { class: "card__note", text: card.note }));
  return el("div", { class: "card" }, body);
}

function emailsCard(card) {
  return el("div", { class: "card" },
    el("p", { class: "card__label", text: "Emails" }),
    el("ul", { class: "rows" }, card.emails.map((m) => el("li", {},
      el("button", {
        class: "row row--tap", type: "button",
        onclick: () => send(`Open the email “${m.subject}” from ${m.from.name || m.from.email}.`),
      },
        el("span", { class: "row__top" },
          el("span", { class: `row__title${m.unread ? " unread" : ""}`, text: m.subject }),
          el("span", { class: "row__when", text: m.received })),
        el("span", { class: "row__sub", text: m.from.name || m.from.email }),
        m.preview ? el("span", { class: "row__preview", text: m.preview }) : null,
        m.attachments && m.attachments.length
          ? el("span", { class: "row__preview", text: `📎 ${m.attachments.join(", ")}` }) : null)))));
}

function emailCard(card) {
  return el("div", { class: "card" },
    el("p", { class: "card__label", text: "Email" }),
    el("div", { class: "row" },
      el("span", { class: "row__top" },
        el("span", { class: "row__title", text: card.subject }),
        el("span", { class: "row__when", text: card.received })),
      el("span", { class: "row__sub", text: (card.from && (card.from.name || card.from.email)) || "" }),
      card.attachments && card.attachments.length
        ? el("span", { class: "row__preview", text: `📎 ${card.attachments.join(", ")}` }) : null));
}

function peopleCard(card) {
  const body = [el("p", { class: "card__label", text: "Who do you mean?" })];
  if (!card.people.length) {
    body.push(el("p", { class: "card__note", text: card.note }));
  } else {
    body.push(el("div", { class: "choices" }, card.people.map((p) => el("button", {
      class: "choice", type: "button",
      onclick: () => send(`I mean ${p.name} (${p.email}).`),
    },
      el("span", {},
        el("span", { text: p.name }),
        el("span", { class: "choice__sub", text: ` ${[p.title, p.company || p.email].filter(Boolean).join(" · ")}` })),
      p.internal ? el("span", { class: "tag", text: "TAG" }) : null))));
  }
  return el("div", { class: "card" }, body);
}

function eventsCard(card) {
  if (!card.events.length) return el("div", { class: "card" }, el("p", { class: "card__note", text: "No events in that range." }));
  return el("div", { class: "card" },
    el("p", { class: "card__label", text: "Calendar" }),
    el("ul", { class: "rows" }, card.events.map((e) => el("li", { class: "row" },
      el("span", { class: "row__top" },
        el("span", { class: "row__title", text: e.subject }),
        el("span", { class: "row__when", text: e.start })),
      e.location ? el("span", { class: "row__sub", text: e.location }) : null))));
}

function renderCard(card) {
  switch (card.type) {
    case "slots": return slotsCard(card);
    case "emails": return emailsCard(card);
    case "email": return emailCard(card);
    case "people": return peopleCard(card);
    case "events": return eventsCard(card);
    case "action": return slip(card);
    case "unsure": return el("div", { class: "card" },
      el("p", { class: "card__label", text: "Not sure \u2014 left in your inbox" }),
      el("ul", { class: "rows" }, card.items.map((i) => el("li", { class: "row" },
        el("span", { class: "row__title", text: i.subject }),
        el("span", { class: "row__sub", text: [i.from, i.reason].filter(Boolean).join(" \u00b7 ") })))));
    case "memory": return el("p", { class: "memory-note", text: `✓ ${card.text}` });
    default: return null;
  }
}

// ── thread ─────────────────────────────────────────────────────────────────────

function addNote(text) {
  thread.append(el("div", { class: "note", text }));
}

function addBrief(text, cards = [], did = []) {
  const textNode = el("div", { class: "brief__text" });
  textNode.innerHTML = renderMarkdown(text); // escaped inside renderMarkdown
  // What it did to get here, collapsed to one quiet line.
  const trail = did.length ? el("p", { class: "steps-done", text: `\u2713 ${did.join(" \u00b7 ")}` }) : null;
  thread.append(el("div", { class: "brief" }, trail, text ? textNode : null, ...cards.map(renderCard)));
}

async function openConversation(id) {
  if (!id) return;
  try {
    const convo = await api(`/api/conversations/${encodeURIComponent(id)}`);
    thread.replaceChildren();
    pendingTopic = null;
    setTopic((convo.title || "").startsWith("Brief: ") ? convo.title.slice(7) : "");
    for (const m of convo.messages) {
      if (m.role === "user") addNote(m.text); else addBrief(m.text, m.cards || []);
    }
    conversationId = id;
    writeStored(id);
    updateEmptyState();
    scrollToEnd();
  } catch {
    conversationId = null;
    writeStored(null);
  }
}

// ── recent conversations ───────────────────────────────────────────────────────

function ago(iso) {
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

function closeRecent() {
  $("#recent").hidden = true;
  $("#recent-btn").setAttribute("aria-expanded", "false");
}

async function showRecent() {
  $("#recent").hidden = false;
  $("#recent-btn").setAttribute("aria-expanded", "true");
  const list = $("#recent-list");
  list.replaceChildren(el("li", { class: "itinerary__empty", text: "Loading…" }));
  try {
    const { conversations } = await api("/api/conversations");
    if (!conversations.length) {
      list.replaceChildren(el("li", { class: "itinerary__empty", text: "No conversations yet." }));
      return;
    }
    list.replaceChildren(...conversations.map((c) => el("li", {},
      el("button", {
        class: `row row--tap${c.id === conversationId ? " row--current" : ""}`, type: "button",
        onclick: () => { closeRecent(); openConversation(c.id); },
      },
        el("span", { class: "row__top" },
          el("span", { class: "row__title", text: c.title || "Untitled" }),
          el("span", { class: "row__when", text: ago(c.updated_at) }))))));
  } catch (err) {
    list.replaceChildren(el("li", { class: "error", text: `Couldn't load: ${err.message}` }));
  }
}

// ── what the assistant remembers ───────────────────────────────────────────────────────────

function closeMemory() {
  $("#memory").hidden = true;
  $("#memory-btn").setAttribute("aria-expanded", "false");
}

async function showMemory() {
  closeRecent();
  $("#memory").hidden = false;
  $("#memory-btn").setAttribute("aria-expanded", "true");
  const list = $("#memory-list");
  list.replaceChildren(el("li", { class: "itinerary__empty", text: "Loading…" }));
  try {
    const { items } = await api("/api/memory");
    if (!items.length) {
      list.replaceChildren(el("li", { class: "itinerary__empty", text: "Nothing yet." }));
      return;
    }
    list.replaceChildren(...items.map((m) => {
      const remove = el("button", { class: "link-btn", type: "button", text: "Remove" });
      const row = el("li", { class: "row memory-row" },
        el("span", { class: "memory-row__text" }, el("span", { class: "tag", text: m.kind }), m.text), remove);
      remove.addEventListener("click", async () => {
        remove.disabled = true;
        try {
          await api(`/api/memory/${encodeURIComponent(m.key)}`, { method: "DELETE" });
          row.remove();
          if (!list.children.length) list.append(el("li", { class: "itinerary__empty", text: "Nothing yet." }));
        } catch (err) {
          remove.disabled = false;
          row.append(el("span", { class: "error", text: err.message }));
        }
      });
      return row;
    }));
  } catch (err) {
    list.replaceChildren(el("li", { class: "error", text: `Couldn't load: ${err.message}` }));
  }
}

$("#memory-btn").addEventListener("click", () => ($("#memory").hidden ? showMemory() : closeMemory()));
$("#memory-close").addEventListener("click", closeMemory);
$("#memory").addEventListener("click", (e) => { if (e.target.id === "memory") closeMemory(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMemory(); });

$("#recent-btn").addEventListener("click", () => { closeMemory(); ($("#recent").hidden ? showRecent() : closeRecent()); });
$("#recent-close").addEventListener("click", closeRecent);
$("#recent").addEventListener("click", (e) => { if (e.target.id === "recent") closeRecent(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeRecent(); });

// Read a streamed response: one JSON event per line.
async function* streamEvents(path, payload) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify(payload),
  });
  if (res.status === 401) { location.href = "/auth/login"; throw new Error("Signed out"); }
  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    throw new Error(detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newline;
    while ((newline = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (line) yield JSON.parse(line);
    }
  }
  if (buffer.trim()) yield JSON.parse(buffer.trim());
}

async function send(text) {
  text = (text || "").trim();
  if (!text || busy) return;
  busy = true;
  sendBtn.disabled = true;
  showTab("chat");
  addNote(text);
  updateEmptyState();

  // Live area: what the assistant is doing, then its answer as it's written.
  const steps = el("ul", { class: "steps" });
  const draft = el("div", { class: "brief__text brief__text--draft" });
  const live = el("div", { class: "brief" }, steps, draft);
  const waiting = el("li", { class: "step step--active", text: "Thinking" });
  steps.append(waiting);
  thread.append(live);
  scrollToEnd();

  const labels = [];
  let draftText = "";
  let painting = false;
  const paint = () => {
    if (painting) return;
    painting = true;
    requestAnimationFrame(() => {
      draft.innerHTML = renderMarkdown(draftText); // escaped inside renderMarkdown
      painting = false;
      scrollToEnd();
    });
  };

  try {
    let finished = null;
    for await (const event of streamEvents("/api/chat/stream", {
      message: text,
      conversation_id: conversationId,
      brief_event_id: !conversationId && pendingTopic ? pendingTopic.eventId : null,
    })) {
      if (event.type === "start") {
        conversationId = event.conversation_id;
        writeStored(conversationId);
        pendingTopic = null;
      } else if (event.type === "step") {
        waiting.remove();
        steps.querySelectorAll(".step--active").forEach((s) => s.classList.replace("step--active", "step--done"));
        steps.append(el("li", { class: "step step--active", text: event.label }));
        labels.push(event.label.split(":")[0]);
        scrollToEnd();
      } else if (event.type === "text") {
        waiting.remove();
        steps.querySelectorAll(".step--active").forEach((s) => s.classList.replace("step--active", "step--done"));
        draftText += event.delta;
        paint();
      } else if (event.type === "discard_text") {
        draftText = "";
        paint();
      } else if (event.type === "done") {
        finished = event;
      } else if (event.type === "error") {
        throw new Error(event.message);
      }
    }
    if (!finished) throw new Error("The connection closed before the answer finished.");
    writeStored(String(Date.now()), ACTIVE_KEY);
    live.remove();
    // The final text is the fact-checked version; the streamed draft was a preview.
    addBrief(finished.text, finished.cards, [...new Set(labels)]);
    if (finished.cards.some((c) => c.type === "action")) loadToday();
  } catch (err) {
    live.remove();
    const retry = el("button", { class: "btn", type: "button", text: "Try again", onclick: () => { errorNode.remove(); thread.lastChild?.remove(); busy = false; send(text); } });
    const errorNode = el("div", { class: "error" }, `Something went wrong: ${err.message}`, retry);
    thread.append(errorNode);
  } finally {
    busy = false;
    sendBtn.disabled = !input.value.trim();
    scrollToEnd();
  }
}

// ── composer ───────────────────────────────────────────────────────────────────

function autosize() {
  input.style.height = "auto";
  const full = input.scrollHeight + 2; // + borders, so a single line never shows a scrollbar
  input.style.height = `${Math.min(full, 160)}px`;
  input.style.overflowY = full > 160 ? "auto" : "hidden";
}

input.addEventListener("input", () => { sendBtn.disabled = busy || !input.value.trim(); autosize(); });
input.addEventListener("keydown", (e) => {
  // Enter sends on a keyboard; Shift+Enter for a new line.
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("#composer").requestSubmit();
  }
});
$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value;
  input.value = "";
  autosize();
  send(text);
});
document.querySelectorAll("#quick .chip").forEach((chip) =>
  chip.addEventListener("click", () => send(chip.dataset.say)));

$("#new-chat").addEventListener("click", () => {
  conversationId = null;
  pendingTopic = null;
  writeStored(null);
  thread.replaceChildren();
  setTopic("");
  updateEmptyState();
  input.focus();
});

loadToday();
openConversation(conversationId);
setInterval(loadToday, 5 * 60 * 1000);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => { /* optional */ });
}
