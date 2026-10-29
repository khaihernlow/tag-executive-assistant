// Geronimo (Dave's assistant) front end. Plain DOM, no framework. All server text goes in
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

const SVG_NS = "http://www.w3.org/2000/svg";
function icon(name) {   // a Lucide icon from the page's sprite
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

// "✓ Sent" and "⚠ Clashes" are written as text everywhere; on screen the marks are icons.
const MARKS = { "\u2713": "check", "\u26a0": "triangle-alert" };
function withMarks(node, text) {
  const parts = String(text).split(/(\u2713|\u26a0)\s?/);
  if (parts.length === 1) { node.textContent = text; return; }
  node.textContent = "";
  for (const part of parts) {
    if (!part) continue;
    node.append(MARKS[part] ? icon(MARKS[part]) : document.createTextNode(part));
  }
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") withMarks(node, value);
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
    let detail = res.statusText || `error ${res.status}`;   // HTTP/2 has no status text
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    throw new Error(detail);
  }
  return res.json();
}

function scrollToEnd() {
  requestAnimationFrame(() => {
    scroller.scrollTop = scroller.scrollHeight;
    if (typeof updateScrollEdges === "function") updateScrollEdges();
  });
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
  const switching = sheet.dataset.tab !== name;
  sheet.dataset.tab = name;
  $("#view-today").hidden = name !== "today";
  $("#view-chat").hidden = name !== "chat";
  document.querySelectorAll(".tab").forEach((t) =>
    t.dataset.tab === name ? t.setAttribute("aria-current", "page") : t.removeAttribute("aria-current"));
  if (name === "chat" && switching) scrollToEnd();
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

// ── the right side (desktop) / full screen (phone): one panel at a time ───────

const PANELS = ["detail", "briefsheet", "notesheet"];

function closePanels(except = "") {
  for (const id of PANELS) {
    if (id === except || $(`#${id}`).hidden) continue;
    $(`#${id}`).hidden = true;
    if (id === "detail") { $("#detail-body").replaceChildren(); markOpen(null); }
    if (id === "briefsheet") { openEvent = null; $("#source").hidden = true; }
    if (id === "notesheet") { openNoteId = null; clearTimeout(notePoll); }
  }
}

// ── Recent and Remembered: a pop-up near the top; nothing behind it moves ──

function closePopovers() {
  for (const id of ["recent", "memory", "source", "notes", "recstart"]) {
    $(`#${id}`).hidden = true;
    $(`#${id}-btn`)?.setAttribute("aria-expanded", "false");
  }
  $("#record-btn").setAttribute("aria-expanded", "false");
  $("#scrim").hidden = true;
  document.body.classList.remove("modal-open");
}

function openPopover(id) {
  closePopovers();
  $(`#${id}`).hidden = false;
  $(`#${id}-btn`)?.setAttribute("aria-expanded", "true");
  if (id === "recstart") $("#record-btn").setAttribute("aria-expanded", "true");
  $("#scrim").hidden = false;   // a click anywhere else closes it
  document.body.classList.add("modal-open");
}

$("#scrim").addEventListener("click", closePopovers);
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || $("#scrim").hidden) return;
  closePopovers();
  e.stopImmediatePropagation();
}, true);

// ── the chat's title, and edges that only show when something scrolls under them ──

function shortTitle(text) {
  text = (text || "").trim();
  return text.length <= 80 ? text : `${text.slice(0, 80).replace(/\s+\S*$/, "")}\u2026`;
}

function setChatTitle(title) {
  $("#chat-title").textContent = title || "New chat";
}

let atBottom = true;
function updateScrollEdges() {
  $(".pane-head--chat").classList.toggle("is-scrolled", scroller.scrollTop > 4);
  $("#composer").classList.toggle("has-more", !atBottom);
  setJumpVisible(!atBottom && thread.children.length > 0);
}
new IntersectionObserver(([entry]) => {
  atBottom = entry.isIntersecting;
  updateScrollEdges();
}, { root: scroller, rootMargin: "0px 0px 32px 0px" }).observe($("#thread-end"));

// The jump button fades rather than vanishing, and hides a moment after it's no longer
// needed: a tap that lands as it goes must hit the button, not the card underneath
// (that once started a renewal opportunity).
let jumpTimer = null;
function setJumpVisible(show) {
  const jump = $("#to-latest");
  clearTimeout(jumpTimer);
  if (show) {
    jump.hidden = false;
    requestAnimationFrame(() => jump.classList.add("is-shown"));
  } else if (!jump.hidden) {
    jump.classList.remove("is-shown");
    jumpTimer = setTimeout(() => { jump.hidden = true; }, 400);
  }
}
scroller.addEventListener("scroll", updateScrollEdges, { passive: true });
$("#view-today").addEventListener("scroll", (e) => {
  e.currentTarget.querySelector(".pane-head").classList.toggle("is-scrolled", e.currentTarget.scrollTop > 4);
}, { passive: true });
// ── desktop: hide the day, or drag its width (remembered in this browser) ─────

const DAY_KEY = "ea.dayLayout";
const dayLayout = (() => { try { return JSON.parse(localStorage.getItem(DAY_KEY)) || {}; } catch { return {}; } })();
const saveDayLayout = () => { try { localStorage.setItem(DAY_KEY, JSON.stringify(dayLayout)); } catch { /* private mode */ } };

function applyDayLayout() {
  sheet.classList.toggle("day-collapsed", Boolean(dayLayout.collapsed));
  if (dayLayout.width) sheet.style.setProperty("--day-width", `${clampDayWidth(dayLayout.width)}px`);
}
function setDayCollapsed(collapsed) {
  dayLayout.collapsed = collapsed;
  saveDayLayout();
  applyDayLayout();
}
$("#day-collapse").addEventListener("click", () => setDayCollapsed(true));
$("#day-expand").addEventListener("click", () => setDayCollapsed(false));
["#rail-date", "#rail-next", "#rail-needs"].forEach((id) => $(id).addEventListener("click", () => setDayCollapsed(false)));
(() => {   // the rail's date: "MON" over "12", from the date the server rendered
  const [weekday, rest = ""] = $("#date").textContent.split(",");
  $("#rail-weekday").textContent = weekday.trim().slice(0, 3);
  $("#rail-day").textContent = rest.trim().split(" ").pop();
})();

const dayMax = () => Math.max(340, Math.min(640, window.innerWidth * 0.5));
function clampDayWidth(px) {
  return Math.round(Math.min(Math.max(px, 340), dayMax()));
}
$("#resizer").addEventListener("pointerdown", (down) => {
  down.preventDefault();
  const resizer = down.currentTarget;
  resizer.setPointerCapture(down.pointerId);
  sheet.classList.add("resizing");
  const left = $("#view-today").getBoundingClientRect().left;
  const move = (e) => sheet.style.setProperty("--day-width", `${clampDayWidth(e.clientX - left)}px`);
  const up = (e) => {
    dayLayout.width = clampDayWidth(e.clientX - left);
    saveDayLayout();
    sheet.classList.remove("resizing");
    resizer.removeEventListener("pointermove", move);
    resizer.removeEventListener("pointerup", up);
  };
  resizer.addEventListener("pointermove", move);
  resizer.addEventListener("pointerup", up);
});
$("#resizer").addEventListener("keydown", (e) => {   // arrows resize for keyboard users
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  const now = $("#view-today").getBoundingClientRect().width;
  dayLayout.width = clampDayWidth(now + (e.key === "ArrowRight" ? 24 : -24));
  saveDayLayout();
  applyDayLayout();
});
applyDayLayout();

$("#to-latest").addEventListener("click", () => scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" }));

// A meeting is joinable from 15 minutes before it starts until it ends.
const joinable = (e) => e.join_url && !e.past && (e.now || (new Date(e.starts_at) - Date.now()) < 15 * 60000);

function whenLabel(e) {
  const day = new Date(e.starts_at).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  return e.all_day ? `${day} \u00b7 all day` : `${day} \u00b7 ${e.start}\u2013${e.end}`;
}

// ── today ──────────────────────────────────────────────────────────────────────

async function loadToday() {
  try {
    const day = await api("/api/today");
    $("#date").textContent = day.date;
    $("#today-skeleton").hidden = true;
    $("#agenda-block").hidden = false;
    const isToday = day.agenda_title === "Today";
    agendaHome = { iso: day.agenda_iso, title: isToday ? "Today" : `${day.agenda_title} · ${day.agenda_date}`,
                   when: isToday ? "today" : day.agenda_title.toLowerCase(), events: day.events, done: day.done_today };
    todayEvents = day.events;
    if (!agendaShown) showHomeDay();   // a refresh doesn't pull Dave back from the day he's looking at
    renderNext(day.events, isToday ? "" : day.agenda_title);
    renderNeeds(day.pending.filter((a) => !["reply_email", "book_meeting"].includes(a.kind)),
                day.requests || [], day.invites || []);
    renderLunch(day.lunch);
    renderJunk(day.junk_moved || [], day.filed || []);
    // While the worker is preparing briefs, check back so the "Brief" buttons appear.
    clearTimeout(loadToday.timer);
    if (day.events.some((e) => e.brief === "preparing")) loadToday.timer = setTimeout(loadToday, 5000);
  } catch (err) {
    $("#today-skeleton").hidden = true;
    $("#agenda-block").hidden = false;
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
    const pill = e.note ? el("span", { class: "pill pill--notes", text: e.note.status === "ready" ? "Notes" : "Notes\u2026" })
      : e.brief === "ready" ? el("span", { class: "pill pill--brief", text: "Brief" })
      : e.brief === "preparing" ? el("span", { class: "pill", text: "Preparing…" }) : null;
    const join = joinable(e)
      ? el("a", { class: "join", href: e.join_url, target: "_blank", rel: "noopener", text: "Join",
                  onclick: (ev) => ev.stopPropagation() })
      : null;
    return el("li", { class: classes.join(" ") },
      el("button", { class: "stop__open", type: "button", "aria-label": `${e.subject}, ${e.start}. ${e.note ? "Open notes" : "Open brief"}`,
                     onclick: () => (e.note ? openNote(e.note.id) : openBrief(e)) },
        el("span", { class: "stop__time", text: e.start }),
        el("span", { class: "stop__what" },
          el("span", { class: "stop__subject", text: e.subject }),
          meta || pill ? el("span", { class: "stop__meta" }, pill, meta) : null)),
      join || el("span"),
    );
  }));
}

// ── other days ─────────────────────────────────────────────────────────────────

let agendaHome = null;    // the day the agenda opens on (today, or tomorrow once today is done)
let agendaShown = null;   // the date being looked at instead, "YYYY-MM-DD"
let agendaAsk = 0;

function showHomeDay() {
  agendaShown = null;
  $("#today-title").textContent = agendaHome.title;
  const done = $("#done-today");
  done.hidden = !agendaHome.done;
  withMarks(done, `✓ ${agendaHome.done} done today`);
  $("#day-back").hidden = true;
  $("#itinerary").classList.remove("itinerary--loading");
  renderItinerary(agendaHome.events, agendaHome.when);
}

function shiftDate(iso, days) {
  const d = new Date(`${iso}T12:00:00`);
  d.setDate(d.getDate() + days);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

async function stepDay(days) {
  if (!agendaHome) return;
  const iso = shiftDate(agendaShown || agendaHome.iso, days);
  if (iso === agendaHome.iso) { agendaAsk++; return showHomeDay(); }
  agendaShown = iso;
  const ask = ++agendaAsk;
  const list = $("#itinerary");
  list.classList.add("itinerary--loading");
  try {
    const day = await api(`/api/agenda?date=${iso}`);
    if (ask !== agendaAsk) return;   // he's already moved on to another day
    const named = ["Today", "Tomorrow", "Yesterday"].includes(day.title);
    $("#today-title").textContent = named ? `${day.title} · ${day.date}` : day.date;
    $("#done-today").hidden = true;
    $("#day-back").hidden = false;
    renderItinerary(day.events, named ? day.title.toLowerCase() : day.date);
  } catch (err) {
    if (ask !== agendaAsk) return;
    list.replaceChildren(el("li", { class: "itinerary__empty", text: `Couldn't load that day: ${err.message}` }));
  } finally {
    if (ask === agendaAsk) list.classList.remove("itinerary--loading");
  }
}

$("#day-prev").addEventListener("click", () => stepDay(-1));
$("#day-next").addEventListener("click", () => stepDay(1));
$("#day-back").addEventListener("click", () => { agendaAsk++; showHomeDay(); });

// ── meeting briefs ─────────────────────────────────────────────────────────────

let openEvent = null;

// Sources of the open brief, by label ("Email 2", "Web 1", "Invite").
let briefSources = new Map();

function sourceIndex(material) {
  const index = new Map();
  const m = material || {};
  if (m.invite) index.set("Invite", { kind: "Calendar invite", icon: "calendar", title: "The invite", excerpt: m.invite.excerpt || "No description in the invite." });
  for (const e of m.emails || []) {
    index.set(e.label, { kind: e.label, icon: "mail", title: e.subject, from: e.from, date: e.received, meta: `${e.from} \u00b7 ${e.received}`, excerpt: e.excerpt,
                         link: e.web_link, linkText: "Open in Outlook" });
  }
  for (const a of m.attachments || []) {
    index.set(a.label, { kind: a.label, icon: "paperclip", title: a.name, from: a.from_email, meta: `Attached to ${a.from_email}`, excerpt: a.excerpt,
                         link: a.web_link, linkText: "Open the email in Outlook" });
  }
  for (const n of m.notes || []) {
    index.set(n.label, { kind: n.label, icon: "notebook-pen", title: n.title, meta: `Meeting notes \u00b7 ${n.when}`,
                         excerpt: [n.headline, ...(n.points || []).map((p) => `\u2022 ${p}`)].filter(Boolean).join("\n"),
                         noteId: n.id, linkText: "Open the notes" });
  }
  for (const h of m.hubspot || []) {
    const what = { account: "HubSpot account", note: "HubSpot note", call: "HubSpot call", meeting: "HubSpot meeting",
                   email: "HubSpot email" }[h.kind] || "HubSpot";
    index.set(h.label, { kind: h.label, icon: "database", title: h.title, meta: [what, h.by, h.when].filter(Boolean).join(" · "),
                         excerpt: h.excerpt, link: h.url, linkText: "Open in HubSpot" });
  }
  for (const w of m.web || []) {
    let host = w.url;
    try { host = new URL(w.url).hostname.replace(/^www\./, ""); } catch { /* keep raw */ }
    index.set(w.label, { kind: w.label, icon: "globe", title: w.about || host, from: host, meta: host, excerpt: w.fact, link: w.url, linkText: `Open ${host}` });
  }
  return index;
}

function showSource(label) {
  const src = briefSources.get(label) || lineSource(label);
  if (!src) return;
  $("#src-icon").replaceChildren(icon(src.icon || "mail"));
  $("#src-kind").textContent = src.kind;
  // The invite is this meeting: name it and give its time, rather than "The invite".
  const invite = label === "Invite" && openEvent;
  $("#src-title").textContent = invite ? openEvent.subject : src.title || "";
  $("#src-meta").textContent = invite ? [whenLabel(openEvent), openEvent.location].filter(Boolean).join(" · ") : src.meta || "";
  if (invite) src.meta = $("#src-meta").textContent;
  $("#src-meta").hidden = !src.meta;
  // Nothing written: say what it is rather than show a blank.
  const calendarReply = /^(Accepted|Declined|Tentative|Canceled|Cancelled):/i.test(src.title || "");
  $("#src-excerpt").textContent = src.excerpt
    || (calendarReply ? "A calendar reply, with no message." : "No message text. Open it to see the whole email.");
  $("#src-excerpt").classList.toggle("source__excerpt--none", !src.excerpt);
  $("#src-foot").hidden = !src.link && !src.noteId;
  const open = $("#src-open");
  open.onclick = null;
  if (src.noteId) {   // notes open here, not in another tab
    open.href = "#";
    open.removeAttribute("target");
    open.onclick = (ev) => { ev.preventDefault(); closePopovers(); openNote(src.noteId); };
    $("#src-open-text").textContent = src.linkText;
  } else if (src.link) {
    open.href = src.link;
    open.setAttribute("target", "_blank");
    $("#src-open-text").textContent = src.linkText;
  }
  openPopover("source");
}

$("#src-close").addEventListener("click", closePopovers);

function cited(text) {
  // "[Email 2, Web 1]" becomes tappable chips that open the source; unknown labels stay plain.
  const label = "(?:Invite|Email \\d+|Attachment \\d+|Web \\d+|Notes \\d+|HubSpot \\d+|L\\d+(?:\\s*-\\s*L?\\d+)?)";
  const parts = String(text).split(new RegExp(`(\\[${label}(?:,\\s*${label})*\\])`, "g"));
  const out = [];
  for (const p of parts.filter(Boolean)) {
    if (!/^\[/.test(p)) { out.push(p); continue; }
    out.push(el("span", { class: "cites" }, p.slice(1, -1).split(/,\s*/).map((label) => (briefSources.has(label) || lineSource(label)
      ? el("button", { class: "cite", type: "button", text: label, title: "Show this source", onclick: () => showSource(label) })
      : el("span", { class: "cite cite--plain", text: label })))));
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
    content.push(el("details", { class: "bsec bsources" },
      el("summary", { class: "bsec__title" }, `Sources \u00b7 ${sources.length}`, icon("chevron-down")),
      el("ul", { class: "srclist" }, sources.map(([label, src]) => el("li", {},
        el("button", { class: "srcrow", type: "button", title: src.meta || "", onclick: () => showSource(label) },
          icon(src.icon || "mail"),
          el("span", { class: "srcrow__title", text: src.title }),
          el("span", { class: "srcrow__label", text: label })))))));
  }
  $("#bs-content").replaceChildren(...content.filter(Boolean));
}

async function loadBrief(event) {
  const target = $("#bs-content");
  try {
    const data = await api(`/api/briefs/${encodeURIComponent(event.id)}`);
    if (openEvent !== event) return; // sheet moved on
    $("#bs-status").hidden = data.status !== "ready";   // only a brief that exists can be refreshed
    $("#bs-age").textContent = data.updated_at ? `Prepared ${ago(data.updated_at)}` : "";
    if (data.status === "ready") {
      renderBrief(data);
      $("#bs-foot").hidden = false;
      openEvent.conversationId = data.conversation_id;
    } else if (data.status === "none") {
      target.replaceChildren(briefEmpty(event));
    } else if (data.status === "preparing") {
      target.replaceChildren(el("p", { class: "thinking", text: "Preparing the brief from your calendar and email" }));
      setTimeout(() => openEvent === event && loadBrief(event), 4000);
    } else {
      target.replaceChildren(el("p", { class: "error", text: `Couldn't prepare this brief (${data.error || "unknown error"}).` }),
        prepareButton(event, "Try again"));
    }
  } catch (err) {
    if (openEvent !== event) return;
    $("#bs-status").hidden = true;
    target.replaceChildren(el("p", { class: "error", text: `Couldn't load the brief: ${err.message}` }));
  }
}

function briefEmpty(event) {
  // Briefs are written automatically for meetings with people outside TAG; say which this is.
  const external = event.briefable;
  return el("div", { class: "empty" },
    el("p", { class: "empty__title", text: external ? "No brief yet" : "No brief for internal meetings" }),
    el("p", { class: "empty__text", text: external
      ? "Briefs are usually ready a few minutes after a meeting is booked. You can prepare this one now."
      : "Briefs are prepared automatically for meetings with people outside TAG. This one is internal, but you can still have one prepared." }),
    el("div", { class: "empty__actions" },
      el("button", { class: `btn${external ? " btn--sign" : ""}`, type: "button",
        text: external ? "Prepare brief" : "Prepare one anyway", onclick: () => prepare(event) })));
}

function prepareButton(event, label, refresh = false) {
  return el("button", { class: "btn btn--sign", type: "button", text: label, onclick: () => prepare(event, refresh) });
}

async function prepare(event, refresh = false) {
  $("#bs-foot").hidden = true;
  $("#bs-status").hidden = true;
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
  closePanels("briefsheet");
  openEvent = event;
  $("#bs-title").textContent = event.subject;
  $("#bs-when").textContent = [whenLabel(event), event.location].filter(Boolean).join(" \u00b7 ");
  $("#bs-foot").hidden = true;
  $("#bs-status").hidden = true;
  $("#bs-input").value = "";
  briefFiles.clear();
  $("#bs-content").replaceChildren(el("div", { class: "skel-brief", "aria-label": "Loading the brief" },
    ...Array.from({ length: 7 }, () => el("span", { class: "skel skel--row" }))));
  $("#briefsheet").hidden = false;
  meetingTabs("brief", event, event.note && event.note.id);
  loadBrief(event);
}

function closeBrief() {
  if (!$("#briefsheet").hidden) closePanels();
}

$("#bs-close").addEventListener("click", closeBrief);
$("#bs-refresh").addEventListener("click", () => openEvent && prepare(openEvent, true));
const bsInput = $("#bs-input");
bsInput.addEventListener("input", () => {
  bsInput.style.height = "auto";
  bsInput.style.height = `${Math.min(bsInput.scrollHeight, 160)}px`;
  briefFiles.update();
});
bsInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#bs-foot").requestSubmit(); }
});
$("#bs-foot").addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = bsInput.value.trim();
  const files = briefFiles.ready();
  if ((!question && !files.length) || briefFiles.uploading() || busy) return;
  const event = openEvent;
  briefFiles.clear();
  closeBrief();
  showTab("chat");
  if (event.conversationId) {
    await openConversation(event.conversationId);
  } else {
    conversationId = null;
    writeStored(null);
    thread.replaceChildren();
    pendingTopic = { eventId: event.id, subject: event.subject };
    setTopic(event.subject);
    updateEmptyState();
  }
  send(question, files);
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#briefsheet").hidden) closeBrief(); });

// ── inbox clean-up ─────────────────────────────────────────────────────────────

function undoRow(m, path) {
  const sub = el("span", { class: "tidy__sub", text: m.detail });
  const action = m.undone
    ? el("span", { class: "tidy__done", text: "Moved back" })
    : el("button", { class: "link-btn tidy__undo", type: "button", text: "Move back", onclick: async (ev) => {
        const btn = ev.currentTarget;
        btn.disabled = true;
        try {
          await api(path, { method: "POST" });
          btn.replaceWith(el("span", { class: "tidy__done", text: "Moved back" }));
        } catch (err) {
          btn.disabled = false;
          sub.textContent = `Couldn't move it back: ${err.message}`;
        }
      } });
  return el("li", { class: "tidy__row" },
    el("div", { class: "tidy__text" }, el("span", { class: "tidy__title", text: m.subject }), sub), action);
}

// ── Wednesday lunch ──────────────────────────────────────────────────────────

const LUNCH_STATE = { usual: "usual", to_ask: "will ask", asked: "asked", clarifying: "asked again",
                      ordered: "replied", backup: "backup order", out: "out this week", no_reply: "no reply",
                      unfinished: "didn't finish" };

function lunchSummary(l) {
  const money = `$${(l.total || 0).toFixed(2)}`;
  const asked = l.lines.filter((x) => ["to_ask", "asked", "clarifying"].includes(x.state)).map((x) => x.name);
  switch (l.status) {
    case "no_meeting": return "The leadership meeting isn't on your calendar today, so I won't order lunch.";
    case "skipped": return "No lunch order this week.";
    case "planned": return `Ordering from Saati Deli for the leadership meeting at ${l.order_at}.`
      + (asked.length ? ` I'll ask ${asked.join(" and ")} on Teams at ${l.ask_at}.` : "");
    case "collecting": return asked.length
      ? `Waiting on ${asked.join(" and ")} (replies by ${l.deadline}). Ordering at ${l.order_at}.`
      : `Everyone has answered. Ordering at ${l.order_at}.`;
    case "needs_dave": return `Ready: ${money} of food. Before I order:`;
    case "ready": return `Ready to order: ${money} of food.`;
    case "ordered": return `Ordered: ${money}.`;
    default: return "";
  }
}

async function lunchAction(path) {
  try {
    const { lunch } = await api(path, { method: "POST" });
    renderLunch(lunch);
  } catch (err) {
    $("#lunch").append(el("p", { class: "error", text: err.message }));
  }
}

function renderLunch(l) {
  const block = $("#lunch-block");
  block.hidden = !l;
  if (!l) return;
  $("#lunch-aside").textContent = l.live ? "" : "Practice run \u00b7 nothing sent";
  const showLines = !["no_meeting", "skipped"].includes(l.status);
  const actions = [];
  if (["planned", "collecting", "needs_dave", "ready"].includes(l.status)) {
    actions.push(el("button", { class: "btn", type: "button", text: "Skip this week", onclick: () => lunchAction("/api/lunch/skip") }));
  }
  if (["no_meeting", "skipped"].includes(l.status)) {
    actions.push(el("button", { class: "btn", type: "button", text: l.status === "skipped" ? "Order after all" : "Order lunch anyway",
                               onclick: () => lunchAction("/api/lunch/order-anyway") }));
  }
  $("#lunch").replaceChildren(...[
    el("p", { class: "lunch__summary", text: lunchSummary(l) }),
    l.status === "needs_dave" && l.reasons.length
      ? el("ul", { class: "lunch__reasons" }, l.reasons.map((r) => el("li", { text: r.charAt(0).toUpperCase() + r.slice(1) }))) : null,
    showLines ? el("ul", { class: "lunch__lines" }, l.lines.map((x) => el("li", { class: `lunch__line${x.state === "out" ? " is-out" : ""}` },
      el("span", { class: "lunch__who", text: x.name }),
      el("span", { class: "lunch__what" },
        el("span", { text: x.what || (x.state === "out" ? "No lunch" : "\u2014") }),
        x.note ? el("span", { class: "lunch__note", text: x.note }) : null),
      el("span", { class: "lunch__state", text: LUNCH_STATE[x.state] || x.state }),
      el("span", { class: "lunch__price", text: x.price != null && x.state !== "out" ? `$${x.price.toFixed(2)}` : "" })))) : null,
    showLines && l.lines.some((x) => x.price) ? el("p", { class: "lunch__total" },
      el("span", { text: "Food" }), el("span", { text: `$${(l.total || 0).toFixed(2)} + tax, delivery and tip` })) : null,
    // Mealeo's own numbers once the checkout is filled; what's left is said plainly.
    ["needs_dave", "ready"].includes(l.status) && l.mealeo && l.mealeo.total
      ? el("div", { class: "lunch__mealeo" },
          el("p", { class: "lunch__mealeo-total" }, el("span", { text: "Mealeo checkout" }), el("strong", { text: `$${l.mealeo.total.toFixed(2)}` })),
          el("p", { class: "lunch__note-line", text:
            `Food $${l.mealeo.subtotal.toFixed(2)} + delivery $${l.mealeo.delivery.toFixed(2)} + tax & service $${l.mealeo.tax.toFixed(2)}`
            + ` + tip $${l.mealeo.tip.toFixed(2)}. ${l.mealeo.when}, to ${l.mealeo.address}.` }),
          el("p", { class: "lunch__note-line", text: "Filled in on Mealeo up to payment. Paying needs the assistant's Mealeo login, so for now someone places it by hand." }))
      : ["needs_dave", "ready"].includes(l.status)
        ? el("p", { class: "lunch__note-line", text: "Placing the order on Mealeo isn't connected yet, so for now someone places it by hand." }) : null,
    actions.length ? el("div", { class: "lunch__actions" }, ...actions) : null,
  ].filter(Boolean));
}

function renderJunk(moved, filed) {
  $("#junk-block").hidden = !moved.length && !filed.length;
  if (!moved.length && !filed.length) return;
  const filedLive = filed.filter((m) => !m.undone).length;
  const junkLive = moved.filter((m) => !m.undone).length;
  const parts = [
    filedLive ? `${filedLive} email${filedLive === 1 ? "" : "s"} filed` : "",
    junkLive ? `${junkLive} moved to Junk` : "",
  ].filter(Boolean);
  const groups = [];
  if (filed.length) {
    groups.push(el("p", { class: "tidy__group", text: "Filed after you read them" }),
      el("ul", { class: "tidy__list" }, filed.map((m) => undoRow(
        { ...m, detail: `${m.from} \u00b7 moved to ${m.folder.replace(/^Inbox\//, "")}` },
        `/api/filing/${encodeURIComponent(m.action_id)}/undo/${encodeURIComponent(m.id)}`))));
  }
  if (moved.length) {
    groups.push(el("p", { class: "tidy__group", text: "Moved to Junk" }),
      el("ul", { class: "tidy__list" }, moved.map((m) => undoRow(
        { ...m, detail: [m.from, m.reason].filter(Boolean).join(" \u00b7 ") },
        `/api/junk/${encodeURIComponent(m.action_id)}/undo/${encodeURIComponent(m.id)}`))));
  }
  const wasOpen = $("#junk details")?.open;
  const details = el("details", { class: "card tidy" },
    el("summary", { text: parts.length ? `In the last day: ${parts.join(", ")}` : "In the last day: everything moved back" }),
    ...groups,
    el("p", { class: "card__note", text: "Move back returns an email to your Inbox." }));
  details.open = Boolean(wasOpen);
  $("#junk").replaceChildren(details);
}

// ── meeting requests ───────────────────────────────────────────────────────────


// "Thursday, Oct 22 at 11:00 AM ET" -> "Thu, Oct 22 · 11:00 AM"
const shortSlot = (label) => label.replace(" ET", "").replace(/^(\w{3})\w*/, "$1").replace(" at ", " \u00b7 ");
const firstName = (name) => (name || "").split(" ")[0];
// "Thursday" within the last week, otherwise "Oct 2".
const relDay = (iso) => {
  const d = new Date(iso);
  return Date.now() - d < 6 * 864e5 ? d.toLocaleDateString(undefined, { weekday: "long" })
    : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
};

function requestCard(r) {
  const card = el("div", { class: "card request" });
  const head = el("header", { class: "request__head" },
    el("p", { class: "request__who" }, el("strong", { text: r.from }),
      r.purpose ? el("span", { class: "request__purpose", text: ` \u00b7 ${r.purpose}` }) : null),
    r.who ? el("p", { class: "request__role", text: r.who }) : null,
    el("p", { class: "request__context" }, r.relationship || "",
      r.web_link ? el("a", { href: r.web_link, target: "_blank", rel: "noopener", text: "Open email" }) : null));
  const body = el("div", { class: "request__body" });
  card.append(head, body);

  if (r.status === "booked") {
    card.classList.add("request--done");
    body.append(el("span", { class: "stamp stamp--done", text: `\u2713 Booked${r.booked ? ` \u00b7 ${shortSlot(r.booked)}` : ""}` }));
    return card;
  }
  if (r.status === "waiting") {
    card.classList.add("request--waiting");
    body.append(
      el("p", { class: "request__label", text: `Times sent, waiting for ${firstName(r.from) || "them"}` }),
      el("ul", { class: "request__offered" }, r.offered.map((t) => el("li", { text: shortSlot(t) }))),
      el("p", { class: "card__note", text: "I'll book it as soon as they choose one of these." }));
    return card;
  }
  if (r.handler) {
    body.append(el("p", { class: "request__handler" },
      el("strong", { text: `${r.handler} is handling this.` }), " You\u2019re copied, so there\u2019s nothing to reply to."));
  }
  if (r.answer) body.append(el("p", { class: "request__answer", text: `They replied: \u201c${r.answer}\u201d` }));
  if (r.unconfirmed_pick) {
    body.append(el("p", { class: "card__note", text: "Their reply doesn\u2019t clearly name one of your times, so the invite waits for you. Check it\u2019s the right one." }));
  }
  if (r.action) {
    body.append(slip(r.action));
    return card;
  }

  const status = el("p", { class: "card__note" });
  const buttons = [];
  const run = async (label, path, payload) => {
    buttons.forEach((b) => { b.disabled = true; });
    status.textContent = label;
    try {
      const action = await api(path, { method: "POST", body: payload ? JSON.stringify(payload) : undefined });
      body.replaceChildren(slip(action));
    } catch (err) {
      buttons.forEach((b) => { b.disabled = false; });
      status.textContent = `Couldn't do that: ${err.message}`;
    }
  };
  const busy = r.proposed && r.proposed.free === false;

  // What they asked for, and whether Dave can make it.
  if (r.proposed) {
    const length = r.minutes >= 60 ? `${r.minutes / 60} hr` : `${r.minutes} min`;
    const format = { teams: "Teams", in_person: "In person", phone: "Phone" }[r.format] || "";
    const verdict = r.proposed.free === true
      ? el("p", { class: "avail avail--free", text: "\u2713 You\u2019re free" })
      : r.proposed.free === false
        ? el("p", { class: "avail avail--busy" }, el("strong", { text: "\u26a0 You\u2019re busy: " }), r.proposed.clashes.join("; "))
        : el("p", { class: "avail", text: "Couldn\u2019t check your calendar" });
    const ask = el("div", { class: "request__ask" },
      el("p", { class: "request__label", text: "They asked for" }),
      el("p", { class: "request__time", text: shortSlot(r.proposed.label) }),
      el("p", { class: "request__meta", text: [format, length].filter(Boolean).join(" \u00b7 ") }),
      verdict);
    body.append(ask);

    if (r.handler) {
      if (r.held) {
        ask.append(el("span", { class: "stamp stamp--done", text: "\u2713 Held on your calendar" }));
      } else if (busy && !r.stalled) {
        ask.append(el("p", { class: "card__note", text: `Worth telling ${firstName(r.handler)} before the time is confirmed.` }));
      } else if (!busy) {
        const hold = el("button", { class: "btn", type: "button", text: "Hold it on my calendar",
          onclick: () => run("Preparing the hold\u2026", `/api/requests/${encodeURIComponent(r.id)}/hold`) });
        buttons.push(hold);
        ask.append(hold, el("p", { class: "card__note", text: `Blocks the time while ${firstName(r.handler)} confirms. No invite goes out.` }));
      }
    } else {
      const book = el("button", { class: busy ? "btn" : "btn btn--sign", type: "button", text: busy ? "Book anyway" : "Book it",
        onclick: () => run("Checking your calendar\u2026", `/api/requests/${encodeURIComponent(r.id)}/book`, { start: r.proposed.start }) });
      buttons.push(book);
      ask.append(book);
    }
  }

  // The colleague hasn't answered after a working day: the one point Dave steps in.
  if (r.handler && r.stalled) {
    const who = firstName(r.handler);
    if (r.nudged) {
      body.append(el("p", { class: "request__stall", text: `You nudged ${who} ${r.nudged}. I'll clear this once they reply.` }));
    } else {
      const nudge = el("button", { class: "btn btn--sign", type: "button", text: `Nudge ${who}`,
        onclick: () => run("Drafting a note\u2026", `/api/requests/${encodeURIComponent(r.id)}/nudge`) });
      buttons.push(nudge);
      body.append(el("div", { class: "request__stall" },
        el("p", {}, el("strong", { text: `${who} hasn\u2019t replied yet.` }), ` ${firstName(r.from)} asked ${relDay(r.received)}.`),
        nudge, el("p", { class: "card__note", text: `A short email to ${who}. You\u2019ll see it before it goes.` })));
    }
  }

  // Times to offer instead: a checklist, every ticked time goes in one reply.
  if (!r.handler && r.slots.length) {
    const chosen = new Set(r.slots.map((s) => s.start));
    const reply = el("button", { class: "btn " + (r.proposed && !busy ? "" : "btn--sign"), type: "button",
      onclick: () => run("Drafting your reply\u2026", `/api/requests/${encodeURIComponent(r.id)}/reply`,
                         { starts: r.slots.map((s) => s.start).filter((s) => chosen.has(s)) }) });
    const label = () => {
      reply.textContent = chosen.size ? `Reply with ${chosen.size === 1 ? "this time" : `these ${chosen.size} times`}` : "Tick a time to offer";
      reply.disabled = !chosen.size;
    };
    const rows = r.slots.map((s) => {
      const box = el("input", { type: "checkbox" });
      box.checked = true;
      box.addEventListener("change", () => { box.checked ? chosen.add(s.start) : chosen.delete(s.start); label(); });
      return el("li", {}, el("label", { class: "offer" }, box, el("span", { text: shortSlot(s.label) })));
    });
    label();
    buttons.push(reply);
    const checked = r.team && r.team.length ? `Checked against ${r.team.map((n) => `${firstName(n)}\u2019s`).join(" and ")} calendar${r.team.length > 1 ? "s" : ""} too. ` : "";
    body.append(el("div", { class: "request__offer" },
      el("p", { class: "request__label", text: r.proposed ? "Or offer times you\u2019re free" : "Times you\u2019re free" }),
      el("ul", { class: "offers" }, rows), reply,
      el("p", { class: "card__note", text: `${checked}You\u2019ll see the email before it goes, sent to everyone on the thread.` })));
  } else if (!r.handler && !r.proposed) {
    body.append(el("p", { class: "card__note", text: "No free time found in that range." }));
  }

  const dismiss = el("button", { class: "link-btn", type: "button", text: "Dismiss",
    onclick: async () => {
      dismiss.disabled = true;
      try {
        await api(`/api/requests/${encodeURIComponent(r.id)}/dismiss`, { method: "POST" });
        closeDetail();
      } catch (err) { dismiss.disabled = false; status.textContent = err.message; }
    } });
  buttons.push(dismiss);
  body.append(status, el("div", { class: "request__foot" }, dismiss));
  return card;
}

// ── invitations ────────────────────────────────────────────────────────────────

const INVITE_DONE = { accept: "\u2713 Accepted", tentative: "Maybe sent", decline: "Declined" };


function inviteCard(inv) {
  const card = el("div", { class: "card invite" });
  const others = inv.attendees > 1 ? ` \u00b7 ${inv.attendees} people` : "";
  card.append(
    el("p", { class: "invite__title", text: inv.subject }),
    el("p", { class: "invite__when", text: inv.when }),
    el("p", { class: "invite__meta" }, `From ${inv.organizer}${inv.where ? ` \u00b7 ${inv.where}` : ""}${others}`,
      inv.web_link ? el("a", { href: inv.web_link, target: "_blank", rel: "noopener", text: "Open" }) : null),
    inv.clashes.length
      ? el("p", { class: "avail avail--busy" }, el("strong", { text: "\u26a0 Clashes with: " }), inv.clashes.join("; "))
      : el("p", { class: "avail avail--free", text: "\u2713 You\u2019re free" }));
  const context = el("div", { class: "invite__context" }, el("span", { class: "skel skel--row" }), el("span", { class: "skel skel--row" }));
  card.append(context);
  loadInviteContext(inv, context);
  const status = el("p", { class: "card__note" });
  const row = el("div", { class: "invite__actions" });
  const choices = [["accept", "Accept"], ["tentative", "Maybe"], ["decline", "Decline"]].map(([response, label]) =>
    el("button", { class: response === "accept" && !inv.clashes.length ? "btn btn--sign" : "btn", type: "button", text: label,
      onclick: async () => {
        choices.forEach((b) => { b.disabled = true; });
        status.textContent = "Sending\u2026";
        try {
          const action = await api(`/api/invites/${encodeURIComponent(inv.id)}/respond`, {
            method: "POST", body: JSON.stringify({ response }) });
          if (action.status !== "executed") throw new Error(action.error || "it didn't go through");
          row.replaceWith(el("span", { class: "stamp stamp--done", text: `${INVITE_DONE[response]} \u00b7 ${firstName(inv.organizer)} is told` }));
          status.textContent = "";
        } catch (err) {
          choices.forEach((b) => { b.disabled = false; });
          status.textContent = `Couldn't send that: ${err.message}`;
        }
      } }));
  row.append(...choices);
  const suggest = el("button", { class: "link-btn invite__suggest", type: "button", text: "Suggest another time",
    onclick: () => suggestTimes(inv, suggest, row, choices, status) });
  card.append(row, suggest, status);
  return card;
}

// Can't make it: free times near the day (same length, the TAG people on it free too), sent as Outlook's "propose new time"
async function suggestTimes(inv, link, row, choices, status) {
  const box = el("div", { class: "invite__times" }, el("p", { class: "card__note", text: "Finding times you’re free…" }));
  link.replaceWith(box);
  let t;
  try {
    t = await api(`/api/invites/${encodeURIComponent(inv.id)}/times`);
  } catch (err) {
    box.replaceChildren(el("p", { class: "card__note", text: `Couldn't find times: ${err.message}` }));
    return;
  }
  if (!t.allowed) {
    box.replaceChildren(el("p", { class: "card__note", text: `${firstName(inv.organizer)} turned off new-time suggestions on this invite. Decline, or reply to them directly.` }));
    return;
  }
  if (!t.times.length) {
    box.replaceChildren(el("p", { class: "card__note", text: "No free time in the week after it. Decline, or reply to them directly." }));
    return;
  }
  let picked = null;
  const send = (response) => async () => {
    const all = [...choices, ...sendRow.children, ...slots];
    all.forEach((b) => { b.disabled = true; });
    status.textContent = "Sending…";
    try {
      const action = await api(`/api/invites/${encodeURIComponent(inv.id)}/respond`, {
        method: "POST", body: JSON.stringify({ response, new_start: picked.start }) });
      if (action.status !== "executed") throw new Error(action.error || "it didn't go through");
      row.remove();
      box.replaceWith(el("span", { class: "stamp stamp--done",
        text: `${response === "decline" ? "Declined" : "Maybe sent"}, suggested ${picked.label} · ${firstName(inv.organizer)} is told` }));
      status.textContent = "";
    } catch (err) {
      all.forEach((b) => { b.disabled = false; });
      status.textContent = `Couldn't send that: ${err.message}`;
    }
  };
  const sendRow = el("div", { class: "invite__actions", hidden: true },
    el("button", { class: "btn btn--sign", type: "button", text: "Maybe, suggest this", onclick: send("tentative") }),
    el("button", { class: "btn", type: "button", text: "Decline, suggest this", onclick: send("decline") }));
  const slots = t.times.map((s) => el("button", { class: "slot", type: "button", text: s.label, "aria-pressed": "false",
    onclick: (ev) => {
      picked = s;
      slots.forEach((b) => b.setAttribute("aria-pressed", String(b === ev.currentTarget)));
      sendRow.hidden = false;
    } }));
  const who = t.checked ? ` and the ${t.checked === 1 ? "TAG person" : `${t.checked} TAG people`} on it` : "";
  box.replaceChildren(
    el("p", { class: "invite__label", text: "Suggest another time" }),
    el("p", { class: "card__note", text: `${t.minutes} minutes, when you${who} are free` }),
    el("div", { class: "invite__slots" }, ...slots), sendRow);
}

// Before Dave answers: what the meeting is, who's on it, the latest emails, last time, HubSpot
async function loadInviteContext(inv, box) {
  let c;
  try {
    c = await api(`/api/invites/${encodeURIComponent(inv.id)}/context`);
  } catch (err) {
    box.replaceChildren(el("p", { class: "card__note", text: `Couldn't load the background: ${err.message}` }));
    return;
  }
  const part = (label, ...children) => el("section", { class: "invite__part" }, el("p", { class: "invite__label", text: label }), ...children);
  const parts = [];
  if (c.about) parts.push(part("What it says", el("p", { class: "invite__about", text: c.about })));
  if (c.people && c.people.length) {
    const outside = c.people.filter((p) => p.outside), inside = c.people.filter((p) => !p.outside);
    parts.push(part("Who\u2019s on it",
      outside.length ? el("p", { class: "invite__people", text: `Outside: ${outside.map((p) => p.name).join(", ")}` }) : null,
      inside.length ? el("p", { class: "invite__people", text: `TAG: ${inside.map((p) => p.name).join(", ")}` }) : null,
      c.more_people ? el("p", { class: "card__note", text: `and ${c.more_people} more` }) : null));
  }
  for (const h of c.hubspot || []) {
    parts.push(part("In HubSpot", el(h.url ? "a" : "p", { class: "invite__link", ...(h.url ? { href: h.url, target: "_blank", rel: "noopener" } : {}),
      text: [h.title, h.lifecycle_stage, h.owner && `owner ${h.owner}`, h.last_contacted && `last contacted ${h.last_contacted}`].filter(Boolean).join(" \u00b7 ") })));
  }
  if (c.last_notes && c.last_notes.length) {
    const n = c.last_notes[0];
    parts.push(part("Last time", el("button", { class: "invite__note", type: "button", onclick: () => openNote(n.id) },
      el("span", { class: "invite__note-title", text: `${n.title} \u00b7 ${n.when}` }),
      n.headline ? el("span", { class: "invite__note-text", text: n.headline }) : null)));
  }
  if (c.emails && c.emails.length) {
    parts.push(part("Recent emails", el("ul", { class: "invite__emails" }, c.emails.map((e) => el("li", {},
      el(e.web_link ? "a" : "div", { class: "invite__email", ...(e.web_link ? { href: e.web_link, target: "_blank", rel: "noopener" } : {}) },
        el("span", { class: "invite__email-subject", text: e.subject }),
        el("span", { class: "invite__email-meta", text: [e.from, e.received].filter(Boolean).join(" \u00b7 ") }),
        e.excerpt ? el("span", { class: "invite__email-text", text: e.excerpt }) : null))))));
  }
  const event = todayEvents.find((e) => e.id === inv.id) || c.event;
  if (c.brief_ready && event) {
    parts.push(el("button", { class: "link-btn invite__brief", type: "button", text: "Open the brief", onclick: () => { closeDetail(); openBrief(event); } }));
  }
  box.replaceChildren(...(parts.length ? parts : [el("p", { class: "card__note", text: "No emails or notes about this one yet." })]));
}

// ── next up and "needs you" ────────────────────────────────────────────────────

function untilText(iso) {
  const minutes = Math.round((new Date(iso) - Date.now()) / 60000);
  if (minutes <= 0) return minutes > -2 ? "starting now" : `started ${-minutes} min ago`;
  if (minutes < 60) return `in ${minutes} min`;
  const hours = Math.floor(minutes / 60), rest = minutes % 60;
  return `in ${hours} hr${rest ? ` ${rest} min` : ""}`;
}

function renderNext(events, laterDay) {
  const block = $("#next-block");
  const next = events.find((e) => !e.past && !e.all_day);
  block.hidden = !next;
  // The collapsed day shows it too: "Next 8:30 AM" (or "Tue 8:30 AM" when it's tomorrow's first).
  $("#rail-next").hidden = !next;
  if (next) {
    const [time, ampm = ""] = next.start.split(" ");
    $("#rail-next-label").textContent = next.now ? "Now" : laterDay ? new Date(next.starts_at).toLocaleDateString(undefined, { weekday: "short" }) : "Next";
    $("#rail-next-time").textContent = time;
    $("#rail-next-ampm").textContent = ampm;
    $("#rail-next").title = `${next.subject}, ${next.start}`;
  }
  if (!next) return;
  const label = next.now ? "Now" : laterDay ? `First up ${laterDay.toLowerCase()}` : "Next";
  const meta = [next.location, next.people ? `${next.people} ${next.people === 1 ? "person" : "people"}` : ""]
    .filter(Boolean).join(" \u00b7 ");
  const actions = [];
  if (joinable(next)) actions.push(el("a", { class: "btn btn--sign", href: next.join_url, target: "_blank", rel: "noopener", text: "Join" }));
  if (next.brief === "ready") {
    actions.push(el("button", { class: `btn${actions.length ? "" : " btn--sign"}`, type: "button", text: "Open brief",
      onclick: () => openBrief(next) }));
  }
  block.replaceChildren(...[
    el("p", { class: "next__label" }, label,
      el("span", { class: "next__when", text: ` \u00b7 ${next.start}${laterDay ? "" : ` \u00b7 ${untilText(next.starts_at)}`}` })),
    el("p", { class: "next__title", text: next.subject }),
    meta ? el("p", { class: "next__meta", text: meta }) : null,
    // replaceChildren would print a null as the text "null"
    actions.length ? el("div", { class: "next__actions" }, ...actions) : null].filter(Boolean));
}

function needsRow({ kind, title, sub, urgent, muted, open }) {
  return el("li", {},
    el("button", { class: `need${urgent ? " need--urgent" : ""}${muted ? " need--muted" : ""}`, type: "button",
      onclick: (ev) => { markOpen(ev.currentTarget); open(); } },
      el("span", { class: "need__dot", "aria-hidden": "true" }),
      el("span", { class: "need__text" },
        el("span", { class: "need__kind", text: kind }),
        el("span", { class: "need__title", text: title }),
        sub ? el("span", { class: "need__sub", text: sub }) : null),
      el("span", { class: "need__chevron", "aria-hidden": "true" }, icon("chevron-right"))));
}

function renderNeeds(pending, requests, invites) {
  const rows = [];
  for (const a of pending) {
    const [main, ...warnings] = (a.summary || "").split(" \u00b7 \u26a0 ");
    const title = (a.display && (a.display.title || a.display.heading)) || main.split(" \u00b7 ")[0];
    rows.push(needsRow({
      kind: KIND_LABELS[a.kind] || "Sign-off", title,
      sub: warnings.length ? `\u26a0 ${warnings[0]}` : main.split(" \u00b7 ").slice(1, 3).join(" \u00b7 "),
      urgent: warnings.length > 0,
      open: () => openDetail(KIND_LABELS[a.kind] || "Sign-off", title, slip(a)),
    }));
  }
  for (const r of requests) {
    const waiting = r.status !== "new";
    const busy = r.proposed && r.proposed.free === false;
    const sub = r.status === "booked" ? `\u2713 Booked ${r.booked ? shortSlot(r.booked) : ""}`
      : r.status === "waiting" ? `Times sent, waiting for ${firstName(r.from)}`
      : r.handler ? `${firstName(r.handler)} is handling it${busy ? " \u00b7 you're busy then" : ""}`
      : r.proposed ? `Asked for ${shortSlot(r.proposed.label)}${busy ? " \u00b7 you're busy then" : ""}`
      : "Wants to find a time";
    const title = `${r.from}${r.purpose ? ` \u00b7 ${r.purpose}` : ""}`;
    rows.push(needsRow({ kind: "Meeting request", title, sub, urgent: busy && !r.handler, muted: waiting,
      open: () => openDetail("Meeting request", title, requestCard(r)) }));
  }
  for (const inv of invites) {
    rows.push(needsRow({ kind: "Invitation", title: inv.subject,
      sub: inv.clashes.length ? `${inv.when} \u00b7 \u26a0 clashes` : `${inv.when} \u00b7 from ${inv.organizer}`,
      urgent: inv.clashes.length > 0,
      open: () => openDetail("Invitation", inv.subject, inviteCard(inv)) }));
  }
  const active = rows.filter((r) => !r.querySelector(".need--muted")).length;
  $("#needs-block").hidden = !rows.length;
  $("#needs-count").textContent = active ? String(active) : "";
  $("#rail-count").textContent = active ? String(active) : "";
  $("#rail-needs").hidden = !active;
  $("#needs").replaceChildren(...rows);
}

function markOpen(row) {
  document.querySelectorAll(".need.is-open").forEach((n) => n.classList.remove("is-open"));
  if (row) row.classList.add("is-open");
}

function openDetail(kind, title, node) {
  closePanels("detail");
  $("#detail-kind").textContent = kind;
  $("#detail-label").textContent = kind;
  $("#detail-title").textContent = title;
  $("#detail-body").replaceChildren(node);
  $("#detail").hidden = false;
  $("#detail-close").focus();
}

function closeDetail() {
  if ($("#detail").hidden) return;
  closePanels();
  loadToday();  // whatever was decided inside is reflected in the list
}

$("#detail-close").addEventListener("click", closeDetail);
document.querySelectorAll(".suggestion").forEach((b) => b.addEventListener("click", () => { showTab("chat"); send(b.textContent); }));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDetail(); });

// ── cards ──────────────────────────────────────────────────────────────────────

const KIND_LABELS = {
  create_event: "Calendar invite", book_meeting: "Calendar invite", create_rule: "Outlook rule",
  move_to_junk: "Inbox clean-up", reply_email: "Email reply", hold_time: "Calendar hold", nudge_colleague: "Email to colleague",
  move_event: "Move meeting", cancel_event: "Cancel meeting", respond_invite: "Invite reply",
  create_opportunity: "New opportunity", update_opportunity: "Opportunity update", complete_tasks: "Autotask tasks",
  create_company: "New Autotask account",
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
  const parts = [el("p", { class: "slip__kind", text: KIND_LABELS[action.kind] || action.kind })];
  const pending = action.status === "pending";
  let emailBox = null, titleBox = null, detailsBox = null, ticketTextBox = null;
  const fieldInputs = {};
  if (action.display) {
    // A record: its fields laid out, the editable ones as inputs.
    const d = action.display;
    if ("title" in d) {
      titleBox = el("input", { class: "slip__title-input", type: "text", "aria-label": "Title" });
      titleBox.value = d.title;
      titleBox.readOnly = !pending;
      parts.push(titleBox);
    } else {
      parts.push(el("p", { class: "slip__heading", text: d.heading || "" }));
    }
    const values = d.values || {};
    // How common a choice is, in the same words the server uses (agent/quote_tickets.describe_usage)
    const usageWords = (spec, v) => {
      const total = spec.usage_total || 0, n = (spec.usage || {})[v] || 0;
      const [one, many] = spec.usage_noun || ["quote request", "quote requests"];
      if (!total) return "";
      if (n === 0) return `Unusual: no ${one} used this in the last 3 months`;
      if (n >= total / 2) return `Usual choice: ${n} of the last ${total} ${many}`;
      return `Less common: ${n} of the last ${total} ${many}`;
    };
    const fieldList = (rows, editSpec) => el("dl", { class: "slip__fields" }, ...rows.flatMap(([label, value, note, field]) => {
      let shown = value;
      const spec = field && editSpec[field];
      let noteNode = note ? el("span", { class: "slip__note", text: note }) : null;
      if (pending && spec) {
        // Native pickers: on an iPhone these are the wheel and the calendar.
        const input = spec.options
          ? el("select", { class: "slip__field-input", "aria-label": label },
              ...spec.options.map(([v, text]) => el("option", { value: v, text })))
          : el("input", { class: "slip__field-input", type: spec.type || "text", "aria-label": label,
                          ...(spec.placeholder ? { placeholder: spec.placeholder } : {}),
                          ...(spec.type === "number" ? { min: "0", step: "any", inputmode: "decimal" } : {}) });
        input.value = values[field] ?? "";
        fieldInputs[field] = input;
        shown = input;
        if (spec.estimate_total) {
          // The pipeline value as Dave types: one-time + monthly x months (Autotask's own sum)
          noteNode = el("span", { class: "slip__note" });
          const num = (k) => parseFloat((fieldInputs[k] || {}).value || "0") || 0;
          const total = () => {
            const one = num("est_onetime"), month = num("est_monthly");
            const months = Math.max(1, Math.round(num("est_term") || (month ? 12 : 1)));
            noteNode.textContent = one || month
              ? `Pipeline value $${Math.round(one + month * months).toLocaleString()}${month && !num("est_term") ? " (12 months assumed)" : ""}`
              : "No estimate: it fills in from the quote";
          };
          queueMicrotask(() => {
            for (const k of ["est_onetime", "est_monthly", "est_term"]) fieldInputs[k] && fieldInputs[k].addEventListener("input", total);
            total();
          });
        }
        if (spec.usage) {
          // The note follows the dropdown: change the pick and it says how common the new one is
          noteNode = el("span", { class: "slip__note" });
          const say = () => {
            const words = usageWords(spec, input.value);
            noteNode.textContent = words;
            noteNode.classList.toggle("slip__note--unusual", words.startsWith("Unusual"));
          };
          input.addEventListener("change", say);
          say();
        }
      } else if (spec && spec.options && values[field] !== undefined) {
        shown = (spec.options.find(([v]) => v === values[field]) || [])[1] || value;   // what was chosen, not the first guess
        noteNode = null;   // decided: how common it was no longer matters
      }
      return [el("dt", { text: label }), el("dd", {}, shown, noteNode)];
    }));
    parts.push(fieldList(d.rows, d.edit || {}));
    if ("description" in d) {
      parts.push(el("p", { class: "slip__email-meta", text: d.details_label || "Details" }));
      detailsBox = el("textarea", { class: "slip__email", rows: "7", "aria-label": d.details_label || "Details" });
      detailsBox.value = d.description || "";
      detailsBox.readOnly = !pending;
      parts.push(detailsBox);
    }
    if (d.ticket) {
      // The quote request for pre-sales: best-guess codes from past tickets, each one changeable
      const t = d.ticket;
      const block = el("div", { class: "slip__sub" }, el("p", { class: "slip__sub-title", text: t.heading }));
      const ticketTitle = el("input", { class: "slip__title-input slip__title-input--sub", type: "text", "aria-label": "Ticket title" });
      ticketTitle.value = values.ticket_title || "";
      ticketTitle.readOnly = !pending;
      fieldInputs.ticket_title = ticketTitle;
      const ticketText = el("textarea", { class: "slip__email", rows: "9", "aria-label": "Ticket description" });
      ticketText.value = values.ticket_description || "";
      ticketText.readOnly = !pending;
      fieldInputs.ticket_description = ticketText;
      const rest = el("div", { class: "slip__sub-body" },
        ticketTitle,
        t.why ? el("p", { class: "slip__why", text: `Why: ${t.why}` }) : null,
        fieldList(t.rows.filter((r) => r[3] !== "ticket"), t.edit || {}),
        el("p", { class: "slip__email-meta", text: "What pre-sales should quote" }), ticketText);
      block.append(fieldList(t.rows.filter((r) => r[3] === "ticket"), t.edit || {}), rest);
      const priority = fieldInputs.ticket_priority, due = fieldInputs.ticket_due;
      const dueDays = ((t.edit || {}).ticket_priority || {}).due_days;
      if (priority && due && dueDays) {
        // A new priority moves the due date with it, unless Dave has picked the date himself
        let dueTouched = false;
        due.addEventListener("change", () => { dueTouched = true; });
        priority.addEventListener("change", () => {
          if (dueTouched || dueDays[priority.value] === undefined) return;
          const d = new Date();
          d.setDate(d.getDate() + dueDays[priority.value]);
          due.value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
        });
      }
      const toggle = fieldInputs.ticket;
      const sync = () => { rest.hidden = (toggle ? toggle.value : values.ticket) === "no"; };
      ticketTextBox = ticketText;
      if (toggle) toggle.addEventListener("change", sync);
      sync();
      parts.push(block);
    }
    const contactPick = fieldInputs.contact_id, people = ((d.edit || {}).contact_id || {}).people;
    if (contactPick && people) {
      // A different contact: their name, title and email replace the previous one's in the description
      // and the ticket text, so what's saved doesn't name one person in the field and another in the text
      const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const swap = (text, prev, next) => {
        // The text may say "Richard Marino" where Autotask says "Rick Marino": match the surname with
        // whatever first name is in front, and an "/ title" after it; emails ignoring case
        if (prev.email) text = text.replace(new RegExp(esc(prev.email), "gi"), next.email || "");
        const last = prev.name.trim().split(/\s+/).pop();
        if (!last) return text;
        const who = new RegExp(`(?:\\b[A-Z][\\w.'’()-]*\\s+){1,2}${esc(last)}\\b(\\s*/\\s*[^\\n]*)?`, "g");
        return text.replace(who, (m, title) => (title !== undefined ? `${next.name} / ${next.title || "Unknown"}` : next.name));
      };
      let previous = people[contactPick.value];
      contactPick.addEventListener("change", () => {
        const next = people[contactPick.value];
        if (!next) return;                       // "No contact": leave the text for Dave to edit
        for (const box of [detailsBox, ticketTextBox]) {
          if (box && previous) box.value = swap(box.value, previous, next);
        }
        previous = next;
      });
    }
    if (d.warnings && d.warnings.length) {
      parts.push(el("div", { class: "slip__warnings" }, ...d.warnings.map((w) => el("p", { class: "warn", text: `\u26a0 ${w}` }))));
    }
  } else {
    parts.push(summary);
  }
  if (action.email) {
    parts.push(el("p", { class: "slip__email-meta", text: `To ${action.email.to} \u00b7 ${action.email.subject}` }));
    emailBox = el("textarea", { class: "slip__email", rows: "7", "aria-label": "Email text" });
    emailBox.value = action.email.comment || "";
    emailBox.readOnly = action.status !== "pending";
    parts.push(emailBox);
  }
  const keepIds = new Set();
  if (action.items && action.items.length) {
    const pickable = (action.kind === "move_to_junk" || action.pickable) && action.status === "pending";
    parts.push(el("ul", { class: "slip__items" }, action.items.map((i) => {
      const text = [el("span", { class: "slip__item-title", text: i.subject }),
                    el("span", { class: "slip__item-sub", text: [i.from, i.reason].filter(Boolean).join(" \u00b7 ") })];
      if (!pickable) return el("li", {}, ...text);
      const box = el("input", { type: "checkbox", "aria-label": `Include \u201c${i.subject}\u201d` });
      box.checked = true;
      box.addEventListener("change", () => { box.checked ? keepIds.delete(i.id) : keepIds.add(i.id); });
      return el("li", {}, el("label", { class: "slip__pick" }, box, el("span", {}, ...text)));
    })));
    if (pickable) parts.push(el("p", { class: "card__note", text: action.pick_note || "Untick anything you want to keep." }));
  }
  node.className = "slip";

  if (action.status === "pending") {
    const approveLabel = { reply_email: "Send", nudge_colleague: "Send", create_opportunity: "Save to Autotask",
                           update_opportunity: "Save to Autotask", create_company: "Save to Autotask" }[action.kind] || "Approve";
    const approve = el("button", { class: "btn btn--sign", type: "button", text: approveLabel });
    const decline = el("button", { class: "btn", type: "button", text: "Decline" });
    const decide = async (verb) => {
      approve.disabled = decline.disabled = true;
      approve.textContent = verb === "approve" ? "Working…" : approve.textContent;
      try {
        const edits = verb !== "approve" ? null
          : action.display && (titleBox || detailsBox || Object.keys(fieldInputs).length)
            ? { ...(titleBox ? { title: titleBox.value } : {}), ...(detailsBox ? { description: detailsBox.value } : {}),
                // An emptied estimate is sent as 0 (blank values are ignored, which would keep the old number)
                ...Object.fromEntries(Object.entries(fieldInputs).map(([k, input]) =>
                  [k, input.value === "" && k.startsWith("est_") ? "0" : input.value])) }
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
    const doneText = {
      move_to_junk: `✓ Moved ${r.moved ?? ""} to Junk${r.failed && r.failed.length ? ` · ${r.failed.length} failed` : ""}`,
      reply_email: "✓ Sent", nudge_colleague: "✓ Sent", respond_invite: "✓ Reply sent",
      move_event: "✓ Moved", cancel_event: "✓ Done", hold_time: "✓ Held on your calendar",
      create_opportunity: "✓ Saved in Autotask", update_opportunity: "✓ Updated in Autotask", create_company: "✓ Added to Autotask",
      create_rule: "✓ Rule created",
      complete_tasks: `✓ ${r.completed ?? ""} marked complete${r.failed && r.failed.length ? ` · ${r.failed.length} failed` : ""}`,
    }[action.kind] || "✓ Booked";
    const isAutotask = action.kind.endsWith("_opportunity") || action.kind === "create_company";
    parts.push(el("span", { class: "stamp stamp--done" }, doneText,
      r.join_url ? el("a", { href: r.join_url, target: "_blank", rel: "noopener", text: "Teams link" }) : null,
      r.web_link ? el("a", { href: r.web_link, target: "_blank", rel: "noopener", text: isAutotask ? "Open in Autotask" : "Outlook" }) : null,
      r.ticket_link ? el("a", { href: r.ticket_link, target: "_blank", rel: "noopener", text: "Quote ticket" }) : null));
    if (r.ticket_error) parts.push(el("p", { class: "warn", text: `⚠ The opportunity is saved, but the quote ticket wasn't: ${r.ticket_error}` }));
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
          ? el("span", { class: "row__preview" }, icon("paperclip"), ` ${m.attachments.join(", ")}`) : null)))));
}

function personCard(card) {
  const p = card.person;
  const role = (r) => [r.title, r.company].filter(Boolean).join(" at ") + (r.duration ? ` \u00b7 ${r.duration}` : "");
  const parts = [el("p", { class: "card__label", text: "LinkedIn" })];
  if (p) {
    parts.push(
      el("p", { class: "person__name" }, p.name,
        p.linkedin ? el("a", { href: p.linkedin, target: "_blank", rel: "noopener", text: "Profile" }) : null),
      el("p", { class: "person__headline", text: [p.headline, p.location].filter(Boolean).join(" \u00b7 ") }));
    const section = (title, items) => items.length ? [el("p", { class: "person__section", text: title }),
      el("ul", { class: "person__list" }, ...items.map((t) => el("li", { text: t })))] : [];
    parts.push(...section("Now", p.current.map(role)), ...section("Before", p.past.map(role)),
      ...section("Education", p.education.map((e) => [e.school, [e.degree, e.field].filter(Boolean).join(", ")].filter(Boolean).join(" \u00b7 "))));
  }
  const tag = card.tag || {};
  const known = [];
  if (tag.autotask) known.push(`${tag.autotask.company} is in Autotask` + (tag.autotask.open_opportunities.length
    ? ` \u00b7 ${tag.autotask.open_opportunities.length} open opportunit${tag.autotask.open_opportunities.length === 1 ? "y" : "ies"}` : ""));
  if (tag.emails_with_dave && tag.emails_with_dave.length) known.push(`${tag.emails_with_dave.length} emails mention them, latest ${tag.emails_with_dave[0].received}`);
  if (known.length) parts.push(el("p", { class: "person__section", text: "At TAG" }), el("ul", { class: "person__list" }, ...known.map((t) => el("li", { text: t }))));
  if (card.other_matches && card.other_matches.length) {
    parts.push(el("p", { class: "person__section", text: p ? "Also found" : "Which one?" }),
      el("ul", { class: "rows" }, ...card.other_matches.map((o) => el("li", {},
        el("a", { class: "row row--tap", href: o.linkedin, target: "_blank", rel: "noopener" },
          el("span", { class: "row__title", text: o.name }),
          el("span", { class: "row__sub", text: [o.headline, o.location].filter(Boolean).join(" \u00b7 ") }))))));
  }
  return el("div", { class: "card" }, ...parts);
}

function quotesCard(card) {
  const money = (n) => `$${Math.round(n).toLocaleString()}`;
  return el("div", { class: "card" },
    el("p", { class: "card__label", text: `Quotes \u00b7 ${card.quotes.length}` }),
    el("ul", { class: "rows" }, card.quotes.map((q) => el("li", {},
      el(q.link ? "a" : "div", { class: "row row--tap", ...(q.link ? { href: q.link, target: "_blank", rel: "noopener" } : {}) },
        el("span", { class: "row__top" },
          el("span", { class: "row__title", text: q.name }),
          el("span", { class: "row__when", text: [q.one_time ? money(q.one_time) : "", q.monthly ? `${money(q.monthly)}/mo` : ""]
            .filter(Boolean).join(" + ") })),
        el("span", { class: "row__sub", text: `${q.company} \u00b7 #${q.quote_number} \u00b7 ${q.created}` }),
        el("span", { class: `row__preview${q.status === "Declined" || q.expired ? " overdue" : ""}`,
          text: [q.status, q.expired ? `expired ${q.expires}` : q.expires && q.status !== "Approved" ? `expires ${q.expires}` : ""]
            .filter(Boolean).join(" \u00b7 ") }))))));
}

function tasksCard(card) {
  return el("div", { class: "card" },
    el("p", { class: "card__label", text: `Open tasks \u00b7 ${card.person}` }),
    el("p", { class: "time__lead", text: `${card.open} open \u00b7 ${card.overdue} overdue` }),
    ...card.projects.map((p) => el("details", { class: "task__group", open: card.projects.length === 1 },
      el("summary", {}, el("span", { class: "task__project", text: p.project }),
        el("span", { class: "task__count", text: `${p.tasks.length}` })),
      el("ul", { class: "time__list" }, ...p.tasks.map((t) => el("li", { class: "time__row" },
        el("span", { class: "time__name", text: t.title }),
        el("span", { class: `time__hours${t.overdue ? " overdue" : ""}`, text: t.due ? `due ${t.due}` : "" })))))));
}

function onboardingCard(card) {
  return el("div", { class: "card" },
    el("p", { class: "card__label", text: "Onboarding projects" }),
    el("ul", { class: "rows" }, ...card.projects.map((p) => el("li", { class: "row" },
      el("span", { class: "row__top" },
        el("span", { class: "row__title", text: p.company || p.project.replace(/^TAG - NEW EMPLOYEE ONBOARDING - /i, "New hire: ") }),
        el("span", { class: "row__when", text: `${p.percent}%` })),
      el("span", { class: "progress" }, el("span", { class: "progress__bar", style: `width:${Math.min(100, p.percent)}%` })),
      el("span", { class: "row__sub", text: [p.kind === "client" ? "Client" : "New employee", p.lead && `lead ${p.lead}`,
        p.planned_end && `${p.past_end ? "was due" : "due"} ${p.planned_end}`].filter(Boolean).join(" \u00b7 ") }),
      p.overdue_tasks ? el("span", { class: "row__preview overdue", text: `${p.overdue_tasks} of ${p.open_tasks} open tasks overdue`
        + (p.unassigned ? ` \u00b7 ${p.unassigned} unassigned` : "") }) : null))));
}

function renewalsCard(card) {
  const money = (n) => `$${Math.round(n).toLocaleString()}`;
  return el("div", { class: "card" },
    el("p", { class: "card__label", text: `Renewals \u00b7 ${card.window}` }),
    el("p", { class: "time__lead", text: `${card.count} contracts \u00b7 ${money(card.monthly_value)}/month at stake` }),
    el("ul", { class: "rows" }, card.renewals.map((r) => el("li", {},
      el("button", { class: "row row--tap", type: "button",
        onclick: () => send(`Start a renewal opportunity for ${r.customer}'s contract "${r.contract}" (contract ${r.contract_id}).`) },
        el("span", { class: "row__top" },
          el("span", { class: "row__title", text: r.customer || r.contract }),
          el("span", { class: `row__when${r.days_left <= 30 ? " overdue" : ""}`, text: `ends ${r.ends}` })),
        el("span", { class: "row__sub", text: [r.contract, r.monthly_value ? `${money(r.monthly_value)}/mo` : ""]
          .filter(Boolean).join(" \u00b7 ") }),
        r.needs_review ? el("span", { class: "slip__note", text: "Needs renewal review" }) : null)))),
    el("p", { class: "card__note", text: "Tap one to open a renewal opportunity in Autotask." }));
}

function timesheetCard(card) {
  // Flagged people first, then everyone else; occasional loggers folded away.
  const regular = card.people.filter((p) => !p.occasional);
  const occasional = card.people.filter((p) => p.occasional);
  const row = (p) => el("li", { class: `time__row${p.flagged ? " time__row--flag" : ""}` },
    el("span", { class: "time__name", text: p.name }),
    el("span", { class: "time__hours", text: `${p.hours}h` }),
    p.why ? el("span", { class: "time__why", text: p.why }) : null);
  const one = card.people.length === 1 ? card.people[0] : null;
  return el("div", { class: "card" },
    el("p", { class: "card__label", text: `Time entries \u00b7 ${card.week}` }),
    one
      ? el("ul", { class: "time__days" }, ...Object.entries(one.by_day).map(([d, h]) =>
          el("li", { class: h ? "" : "time__row--flag" }, el("span", { text: d }), el("span", { text: `${h}h` }))))
      : el("p", { class: "time__lead", text: card.flagged
          ? `${card.flagged} of ${regular.length} behind (target ${card.target_hours}h)`
          : `Everyone's logged their time (target ${card.target_hours}h)` }),
    one ? null : el("ul", { class: "time__list" }, ...regular.filter((p) => p.flagged).map(row)),
    !one && regular.some((p) => !p.flagged) ? el("details", { class: "time__more" },
      el("summary", { text: `${regular.filter((p) => !p.flagged).length} on track` }),
      el("ul", { class: "time__list" }, ...regular.filter((p) => !p.flagged).map(row))) : null,
    !one && occasional.length ? el("details", { class: "time__more" },
      el("summary", { text: `${occasional.length} occasional loggers` }),
      el("ul", { class: "time__list" }, ...occasional.map(row))) : null);
}

function opportunitiesCard(card) {
  return el("div", { class: "card" },
    el("p", { class: "card__label", text: "Opportunities" }),
    el("ul", { class: "rows" }, card.opportunities.map((o) => el("li", {},
      el("a", { class: "row row--tap", href: o.link, target: "_blank", rel: "noopener" },
        el("span", { class: "row__top" },
          el("span", { class: "row__title", text: o.title }),
          el("span", { class: `row__when${o.overdue ? " overdue" : ""}`, text: o.close_date ? `closes ${o.close_date}` : "" })),
        el("span", { class: "row__sub", text: [o.company, `${o.stage} (${o.probability}%)`, o.status !== "Active" ? o.status : ""]
          .filter(Boolean).join(" \u00b7 ") }),
        o.overdue ? el("span", { class: "row__preview overdue", text: "Past its close date" }) : null)))));
}

// HubSpot: what was counted (in words), any groups as bars, then the records, each opening in HubSpot
const HS_MAIN = ["owner", "lifecycle_stage", "activity_date", "due_date", "last_contacted", "amount", "create_date", "email"];

function hsMeta(r) {
  // The details that matter for a row, in a steady order, without repeating the title
  const bits = [];
  for (const k of HS_MAIN) {
    if (r[k] === undefined || r[k] === null || r[k] === "") continue;
    if (k === "amount") bits.push(`$${Math.round(Number(r[k])).toLocaleString()}`);
    else if (k === "last_contacted") bits.push(`last contacted ${r[k]}`);
    else if (k === "due_date") bits.push(`due ${r[k]}`);
    else if (k === "create_date") bits.push(`created ${r[k]}`);
    else bits.push(String(r[k]));
  }
  return bits.join(" · ");
}

function hubspotRows(rows) {
  return el("ul", { class: "rows" }, rows.map((r) => el("li", {},
    el(r.url ? "a" : "div", { class: "row row--tap", ...(r.url ? { href: r.url, target: "_blank", rel: "noopener" } : {}) },
      el("span", { class: "row__top" }, el("span", { class: "row__title", text: r.title }),
        r.why && r.owner ? el("span", { class: "row__when", text: r.owner }) : null),
      r.sub ? el("span", { class: "row__sub", text: r.sub }) : null,
      // A ranked person says why they're on the list; anything else shows its usual details
      r.why ? el("span", { class: "row__preview", text: r.why }) : hsMeta(r) ? el("span", { class: "row__preview", text: hsMeta(r) }) : null,
      r.caution ? el("span", { class: "row__preview warn", text: `⚠ ${r.caution}` }) : null))));
}

// Likely prospects: the accounts with several engaged people first, then each person and why
function prospectsCard(card) {
  return el("div", { class: "card" }, [
    el("p", { class: "card__label", text: "HubSpot · most likely prospects" }),
    card.conditions && card.conditions.length ? el("p", { class: "hs-conditions", text: card.conditions.join(" · ") }) : null,
    (card.accounts || []).length ? el("div", { class: "prospects__accounts" },
      el("p", { class: "rep__sub", text: "Accounts with several people engaged" }),
      el("ul", { class: "prospects__chips" }, card.accounts.map((a) => el("li", { class: "prospects__chip", title: a.names.join(", ") },
        el("strong", { text: a.company }), ` · ${a.people} people`)))) : null,
    el("p", { class: "rep__sub", text: "People" }),
    hubspotRows(card.rows || []),
    card.how_ranked ? el("p", { class: "card__note prospects__how", text: card.how_ranked }) : null,
    card.query ? el("div", { class: "hs-foot" },
      el("span", { class: "hs-foot__note", text: `Showing ${(card.rows || []).length} of ${Number(card.in_hubspot || card.considered || 0).toLocaleString()}` }),
      el("a", { class: "btn hs-foot__export", download: "",
        href: `/api/hubspot/export?q=${encodeURIComponent(JSON.stringify({ ...card.query, ranked: true }))}` },
        icon("download"), " Export the ranked list")) : null,
  ].filter(Boolean));
}

function hsMore(title, rows) {
  return rows && rows.length ? el("details", { class: "hs-more" },
    el("summary", {}, title, icon("chevron-down")), hubspotRows(rows)) : null;
}

function hubspotCard(card) {
  const body = [el("p", { class: "card__label", text: `HubSpot · ${Number(card.matching || 0).toLocaleString()} ${card.object}` })];
  if (card.conditions && card.conditions.length) {
    body.push(el("p", { class: "hs-conditions", text: card.conditions.join(" · ") }));
  }
  const groups = card.groups || [];
  if (groups.length) {
    const value = (g) => (g.total !== undefined && g.total !== null ? g.total : g.count);
    const top = Math.max(...groups.map(value), 1);
    body.push(el("ul", { class: "hs-groups" }, groups.slice(0, 12).map((g) => el("li", { class: "hs-group" },
      el("span", { class: "hs-group__name", text: g.group }),
      el("span", { class: "hs-group__bar", "aria-hidden": "true" },
        el("span", { style: `width:${Math.max(2, Math.round((value(g) / top) * 100))}%` })),
      el("span", { class: "hs-group__n", text: g.total !== undefined && g.total !== null
        ? `$${Math.round(g.total).toLocaleString()} · ${g.count}` : Number(g.count).toLocaleString() })))));
    body.push(hsMore(`Examples · ${(card.rows || []).length}`, card.rows));
  } else if (card.rows && card.rows.length) {
    body.push(hubspotRows(card.rows));
  }
  // A page of a bigger list: say so, and offer the whole thing as a spreadsheet
  const matching = Number(card.matching || 0), shown = (card.rows || []).length;
  if (card.query && matching > 0 && (matching > shown || groups.length)) {
    const href = `/api/hubspot/export?q=${encodeURIComponent(JSON.stringify(card.query))}`;
    body.push(el("div", { class: "hs-foot" },
      groups.length ? null : el("span", { class: "hs-foot__note", text: `Showing ${shown} of ${matching.toLocaleString()}` }),
      el("a", { class: "btn hs-foot__export", href, download: "" }, icon("download"),
        ` Export all ${matching.toLocaleString()}${matching > 10000 ? " (first 10,000)" : ""}`)));
  }
  return el("div", { class: "card" }, body.filter(Boolean));
}

// One rep's calling: the four numbers, how the calls went, then the appointments themselves
function repActivityCard(card) {
  const tile = (n, label) => el("div", { class: "rep__tile" },
    el("span", { class: "rep__n", text: Number(n || 0).toLocaleString() }), el("span", { class: "rep__label", text: label }));
  const groups = card.outcomes || [];
  const top = Math.max(...groups.map((g) => g.count), 1);
  return el("div", { class: "card rep" }, [
    el("p", { class: "card__label", text: `HubSpot · ${card.person} · ${card.period}` }),
    el("div", { class: "rep__tiles" }, tile(card.calls, "calls"), tile(card.conversations, "conversations"),
      tile(card.appointments_booked, "appointments booked"), tile(card.meetings_in_period, "meetings held")),
    groups.length ? el("ul", { class: "hs-groups" }, groups.slice(0, 10).map((g) => el("li", { class: "hs-group" },
      el("span", { class: "hs-group__name", text: g.group }),
      el("span", { class: "hs-group__bar", "aria-hidden": "true" }, el("span", { style: `width:${Math.max(2, Math.round((g.count / top) * 100))}%` })),
      el("span", { class: "hs-group__n", text: g.count.toLocaleString() })))) : null,
    (card.booked || []).length ? el("p", { class: "rep__sub", text: "Appointments booked" }) : null,
    (card.booked || []).length ? hubspotRows(card.booked) : null,
    hsMore(`Meetings held · ${(card.meetings || []).length}`, card.meetings),
  ].filter(Boolean));
}

function hubspotRecordCard(card) {
  const counts = [["meetings", card.meetings_count], ["calls", card.calls_count], ["notes", card.notes_count]]
    .filter(([, n]) => n).map(([w, n]) => `${n} ${n === 1 ? w.slice(0, -1) : w}`).join(" · ");
  return el("div", { class: "card" }, [
    el("p", { class: "card__label", text: "HubSpot" }),
    el(card.url ? "a" : "div", { class: "row row--tap hs-record", ...(card.url ? { href: card.url, target: "_blank", rel: "noopener" } : {}) },
      el("span", { class: "row__top" }, el("span", { class: "row__title", text: card.title }), card.url ? icon("external-link") : null),
      card.sub ? el("span", { class: "row__sub", text: card.sub }) : null,
      el("span", { class: "row__preview", text: [card.lifecycle_stage, card.owner && `owner ${card.owner}`, counts].filter(Boolean).join(" · ") })),
    hsMore(`Coming up · ${(card.upcoming_meetings || []).length}`, card.upcoming_meetings),
    card.contacts ? hsMore(`People · ${card.contacts.count}`, card.contacts.rows) : null,
    hsMore("Recent activity", card.recent_activity),
    card.deals && card.deals.count ? hsMore(`Deals · ${card.deals.count}`, card.deals.rows) : null,
  ].filter(Boolean));
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
        ? el("span", { class: "row__preview" }, icon("paperclip"), ` ${card.attachments.join(", ")}`) : null));
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

// "DOCX Generation" -> "Open the Word document"
function docLabel(name) {
  if (/docx|word/i.test(name)) return "Open the Word document";
  if (/pdf/i.test(name)) return "Open the PDF";
  if (/xlsx|excel|sheet/i.test(name)) return "Open the spreadsheet";
  return `Open ${name.replace(/\s*generation$/i, "")}`;
}

// "🟢 Report Writer - Output a Clean Human-Readable Summary" -> "Output a Clean Human-Readable Summary"
function stepName(name) {
  const plain = String(name || "Step").replace(/^[^\p{L}\p{N}]+/u, "").trim();
  return plain.includes(" - ") ? plain.split(" - ").slice(1).join(" - ") : plain;
}

// A HatzAI workflow running for a few minutes: follows the run, then shows what came out
function hatzRunCard(card) {
  const node = el("div", { class: "card hatz" });
  const state = el("p", { class: "hatz__state", text: "Starting…" });
  const body = el("div", { class: "hatz__body" });
  node.append(el("p", { class: "card__label", text: `Hatz · ${card.app || "workflow"}` }), state, body);
  const url = `/api/hatz/jobs/${encodeURIComponent(card.job_id)}`;
  let misses = 0;
  const check = async () => {
    if (!node.isConnected && misses++ > 2) return;   // the chat was closed: stop asking
    let job;
    try {
      job = await api(url);
    } catch (err) {
      state.textContent = `Couldn't check on it: ${err.message}`;
      setTimeout(check, 15000);
      return;
    }
    if (job.status === "complete") {
      state.textContent = `✓ Done · ${job.steps} step${job.steps === 1 ? "" : "s"}`;
      state.classList.add("hatz__state--done");
      // The made document first: it's the finished thing
      const docs = job.documents.map((d) => el("a", { class: "btn btn--sign", href: `${url}/documents/${encodeURIComponent(d.step_id)}`,
        target: "_blank", rel: "noopener", text: docLabel(d.name) }));
      const summarize = el("button", { class: docs.length ? "btn" : "btn btn--sign", type: "button", text: "Summarize for me",
        onclick: () => send(`Summarize the ${card.app || "Hatz"} run for me: the verdict or answer first, then what I need to decide or do.`) });
      // A preview that opens in place: no box scrolling inside the chat
      const result = el("div", { class: "hatz__result brief__text is-clamped" });
      result.innerHTML = renderMarkdown(job.result || "It finished without any text."); // escaped inside renderMarkdown
      const more = el("button", { class: "link-btn hatz__more", type: "button", text: "Show all of it",
        onclick: () => { const shut = result.classList.toggle("is-clamped"); more.textContent = shut ? "Show all of it" : "Show less"; } });
      const steps = job.all_steps.length > 1 ? el("div", { class: "hatz__steps" },
        el("p", { class: "hatz__steps-title", text: `Each step (${job.all_steps.length})` }),
        ...job.all_steps.map((s) => {
          const text = el("div", { class: "brief__text" });
          text.innerHTML = renderMarkdown(s.text); // escaped inside renderMarkdown
          return el("details", { class: "hatz__step" }, el("summary", { text: stepName(s.name) }), text);
        })) : null;
      body.replaceChildren(el("div", { class: "hatz__actions" }, ...docs, summarize), result, more, ...(steps ? [steps] : []));
      requestAnimationFrame(() => { more.hidden = result.scrollHeight <= result.clientHeight + 4; });
      return;
    }
    if (job.status === "failed") {
      state.textContent = `It failed in Hatz${job.error ? `: ${String(job.error).slice(0, 300)}` : "."}`;
      state.classList.add("error");
      return;
    }
    state.textContent = `Running in Hatz · ${job.done} of ${job.steps || "?"} steps done`;
    setTimeout(check, 5000);
  };
  setTimeout(check, 1500);
  return node;
}

function renderCard(card) {
  switch (card.type) {
    case "slots": return slotsCard(card);
    case "emails": return emailsCard(card);
    case "opportunities": return opportunitiesCard(card);
    case "hubspot": return hubspotCard(card);
    case "hubspot_record": return hubspotRecordCard(card);
    case "rep_activity": return repActivityCard(card);
    case "prospects": return prospectsCard(card);
    case "timesheet": return timesheetCard(card);
    case "renewals": return renewalsCard(card);
    case "tasks": return tasksCard(card);
    case "quotes": return quotesCard(card);
    case "person": return personCard(card);
    case "onboarding": return onboardingCard(card);
    case "email": return emailCard(card);
    case "people": return peopleCard(card);
    case "events": return eventsCard(card);
    case "action": return slip(card);
    case "hatz_run": return hatzRunCard(card);
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

function fileChip(f, extra = null) {
  const visual = f.thumb ? el("img", { class: "file-chip__thumb", src: f.thumb, alt: "" })
    : el("span", { class: "file-chip__icon" }, icon(f.kind === "image" ? "paperclip" : "file-text"));
  return el("li", { class: `file-chip${f.state === "error" ? " file-chip--error" : ""}${f.state === "uploading" ? " file-chip--busy" : ""}` },
    visual,
    el("span", { class: "file-chip__text" },
      el("span", { class: "file-chip__name", text: f.name, title: f.name }),
      el("span", { class: "file-chip__sub", text: f.state === "uploading" ? "Reading\u2026" : f.state === "error" ? f.error : f.summary })),
    extra);
}

function addNote(text, files = []) {
  if (files && files.length) thread.append(el("ul", { class: "note-files" }, files.map((f) => fileChip(f))));
  if (text) thread.append(el("div", { class: "note", text }));
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
      if (m.role === "user") addNote(m.text, m.files); else addBrief(m.text, m.cards || []);
    }
    conversationId = id;
    writeStored(id);
    setChatTitle(convo.title);
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
  closePopovers();
}

const skeletonRows = (n) => Array.from({ length: n }, () =>
  el("li", { class: "skel-row", "aria-hidden": "true" }, el("span", { class: "skel skel--row" }), el("span", { class: "skel skel--short" })));

// The search words in a snippet, marked: text nodes and <mark>s, never HTML from the server.
function marked(text, query) {
  if (!query) return [text];
  const parts = [];
  const low = text.toLowerCase(), needle = query.toLowerCase();
  let from = 0, at;
  while ((at = low.indexOf(needle, from)) >= 0) {
    parts.push(text.slice(from, at), el("mark", { text: text.slice(at, at + needle.length) }));
    from = at + needle.length;
  }
  parts.push(text.slice(from));
  return parts.filter((p) => p !== "");
}

let recentSeq = 0;
async function loadRecent(query) {
  const seq = ++recentSeq;
  const list = $("#recent-list");
  if (!list.children.length || !query) list.replaceChildren(...skeletonRows(5));
  try {
    const { conversations } = await api(`/api/conversations${query ? `?q=${encodeURIComponent(query)}` : ""}`);
    if (seq !== recentSeq) return;   // a newer search already answered
    if (!conversations.length) {
      list.replaceChildren(el("li", { class: "itinerary__empty",
        text: query ? `No conversations mention “${query}”.` : "No conversations yet." }));
      return;
    }
    list.replaceChildren(...conversations.map((c) => el("li", {},
      el("button", {
        class: `row row--tap${c.id === conversationId ? " row--current" : ""}`, type: "button",
        onclick: () => { closePopovers(); closePanels(); openConversation(c.id); },
      },
        el("span", { class: "row__top" },
          el("span", { class: "row__title", text: c.title || "Untitled" }),
          el("span", { class: "row__when", text: ago(c.updated_at) })),
        c.snippet ? el("span", { class: "row__sub" }, ...marked(c.snippet, query)) : null))));
  } catch (err) {
    if (seq === recentSeq) list.replaceChildren(el("li", { class: "error", text: `Couldn't load: ${err.message}` }));
  }
}

async function showRecent() {
  openPopover("recent");
  const search = $("#recent-search");
  search.value = "";
  $("#recent-list").replaceChildren();
  if (matchMedia("(hover: hover)").matches) search.focus();   // no surprise keyboard on a phone
  await loadRecent("");
}

let searchTimer = null;
$("#recent-search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadRecent(e.target.value.trim()), 200);
});

// ── meeting notes ───────────────────────────────────────────────────────────────
// Recorded in the app, uploaded, pasted, or (once allowed) from Teams; written up by the
// assistant with every point citing the transcript line it came from.

let todayEvents = [];
let openNoteId = null;
let openNote_ = null;
let notePoll = null;

const NOTE_STATUS = {
  waiting_transcript: "Waiting to be transcribed",
  transcribing: "Transcribing\u2026",
  summarizing: "Writing up\u2026",
  failed: "Couldn't write these up",
};

// A meeting with both a brief and notes: one page, two tabs. Notes open first once there are some.
function meetingTabs(which, event, noteId) {
  const tabs = $(`#${which === "brief" ? "bs" : "ns"}-tabs`);
  const label = $(`#${which === "brief" ? "bs" : "ns"}-label`);
  const both = Boolean(event && noteId);
  tabs.hidden = !both;
  label.hidden = both;
  if (!both) return;
  const tab = (name, text, open) => el("button", {
    class: "meeting-tabs__tab", type: "button", role: "tab", text,
    "aria-selected": String(which === name), tabindex: which === name ? "0" : "-1",
    onclick: () => { if (which !== name) open(); } });
  tabs.replaceChildren(tab("notes", "Notes", () => openNote(noteId)), tab("brief", "Brief", () => openBrief(event)));
}

// Which meeting the Start box (or the paste form) is about to add to, if it already has notes
function joinsHint(select, hint) {
  const event = todayEvents.find((e) => e.id === select.value);
  hint.hidden = !(event && event.note);
}

function partsLabel(parts) {
  const count = (source) => parts.filter((p) => p.source === source).length;
  const words = [];
  const rec = count("recording"), pasted = count("pasted"), teams = count("teams"), maestro = count("maestro");
  if (maestro) words.push("TeamsMaestro");
  if (rec) words.push(rec === 1 ? "a recording" : `${rec} recordings`);
  if (pasted) words.push(pasted === 1 ? "pasted notes" : `${pasted} sets of pasted notes`);
  if (teams) words.push("the Teams transcript");
  return words.length ? `From ${words.join(" and ")}` : "";
}

function noteStatusText(n) {
  if (n.status === "waiting_transcript" && /speech-to-text key/.test(n.error || "")) {
    return "Saved. It'll be transcribed once the speech-to-text service is set up.";
  }
  return NOTE_STATUS[n.status] || "";
}

// "L12" or "L18-L20": the transcript lines a note's point came from.
function lineSource(label) {
  const m = /^L(\d+)(?:\s*-\s*L?(\d+))?$/.exec(label || "");
  if (!m || !openNote_ || !openNote_.transcript) return null;
  const lines = openNote_.transcript.split("\n");
  const from = Number(m[1]), to = Number(m[2] || m[1]);
  if (from < 1 || to > lines.length || to < from) return null;
  const strip = (l) => l.replace(/^\[L\d+\]\s*/, "");
  return { kind: "Transcript", icon: "notebook-pen", title: from === to ? `Line ${from}` : `Lines ${from}\u2013${to}`,
           meta: openNote_.title, excerpt: lines.slice(from - 1, to).map(strip).join("\n") };
}

function meetingOptions(select, preferNow = true) {
  // Today's meetings, the one happening now (or next) chosen; or none.
  const timed = todayEvents.filter((e) => !e.all_day);
  const now = timed.find((e) => e.now) || (preferNow ? timed.find((e) => !e.past) : null) || null;
  select.replaceChildren(el("option", { value: "", text: "Not a calendar meeting" }),
    ...timed.map((e) => el("option", { value: e.id, text: `${e.start} \u00b7 ${e.subject}` })));
  select.value = now ? now.id : "";
}

async function openNote(id) {
  closePanels("notesheet");
  closePopovers();
  openNoteId = id;
  $("#ns-title").textContent = "";
  $("#ns-when").textContent = "";
  $("#ns-source").textContent = "";
  $("#ns-foot").hidden = true;
  $("#ns-move").hidden = true;
  closeMove();
  $("#ns-content").replaceChildren(el("div", { class: "skel-brief", "aria-label": "Loading the notes" },
    ...Array.from({ length: 7 }, () => el("span", { class: "skel skel--row" }))));
  $("#notesheet").hidden = false;
  showTab("chat");
  await loadNote(id);
}

async function loadNote(id) {
  clearTimeout(notePoll);
  let n;
  try {
    n = await api(`/api/notes/${encodeURIComponent(id)}`);
  } catch (err) {
    if (openNoteId === id) $("#ns-content").replaceChildren(el("p", { class: "error", text: `Couldn't load the notes: ${err.message}` }));
    return;
  }
  if (openNoteId !== id) return;
  // The agenda marks meetings that have notes: refresh it when a note is new or just finished.
  if (n.event_id && (!openNote_ || openNote_.id !== n.id || openNote_.status !== n.status)) loadToday();
  openNote_ = n;
  briefSources = new Map();
  renderNote(n);
  if (["waiting_transcript", "transcribing", "summarizing"].includes(n.status) && !/speech-to-text key/.test(n.error || "")) {
    notePoll = setTimeout(() => openNoteId === id && loadNote(id), 4000);
  }
}

function dueLabel(iso) {
  const d = new Date(`${iso}T12:00:00`);
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

function renderNote(n) {
  $("#ns-title").textContent = n.title;
  const when = n.when ? new Date(n.when).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
  $("#ns-when").textContent = [when, (n.people || []).join(", ")].filter(Boolean).join(" \u00b7 ");
  const parts = n.parts || [];
  $("#ns-source").textContent = n.kind === "quick" ? "Saved note" : partsLabel(parts);
  const event = n.event_id ? todayEvents.find((e) => e.id === n.event_id) : null;
  meetingTabs("notes", event, n.id);
  $("#ns-move").hidden = n.kind === "quick";
  const content = [];
  // A part still on its way, or one that couldn't be transcribed, while there are notes to show
  const waiting = parts.filter((p) => p.status === "waiting");
  const failed = parts.filter((p) => p.status === "failed");
  const many = parts.length > 1;
  const partName = (p) => (many ? `Part ${p.n}` : "The recording");
  if (n.written && n.status !== "ready" && n.status !== "failed") {
    const which = (waiting.length ? waiting : parts.slice(-1)).map(partName).join(" and ");
    content.push(el("p", { class: "ns-notice", text: /speech-to-text key/.test(n.error || "")
      ? `${which} is saved and will be added once the speech-to-text service is set up. These notes cover the rest.`
      : `Adding ${which.toLowerCase()}: these notes will be rewritten to include it.` }));
  }
  if (failed.length && (n.written || n.status === "ready")) {
    content.push(el("div", { class: "ns-notice ns-notice--failed" },
      el("span", { text: `${failed.map(partName).join(" and ")} couldn't be transcribed: ${failed[0].error || "unknown error"}` }),
      el("button", { class: "link-btn", type: "button", text: "Try again",
        onclick: async () => { await api(`/api/notes/${encodeURIComponent(n.id)}/retry`, { method: "POST" }); loadNote(n.id); } })));
  }
  if (n.kind === "quick") {
    content.push(el("p", { class: "bs-headline", text: n.text }));
  } else if (n.status !== "ready" && !n.written) {
    content.push(el("div", { class: "empty" },
      el("p", { class: "empty__title", text: noteStatusText(n) }),
      n.status === "failed" ? el("p", { class: "empty__text", text: n.error || "" }) : null,
      n.status === "failed" ? el("div", { class: "empty__actions" }, el("button", { class: "btn btn--sign", type: "button", text: "Try again",
        onclick: async () => { await api(`/api/notes/${encodeURIComponent(n.id)}/retry`, { method: "POST" }); loadNote(n.id); } })) : null));
  } else {
    content.push(el("p", { class: "bs-headline" }, ...cited(n.headline || "")));
    const mine = (n.action_items || []).filter((a) => a.dave);
    const others = (n.action_items || []).filter((a) => !a.dave);
    const actions = (title, items) => items.length ? el("section", { class: "bsec" },
      el("h3", { class: "bsec__title", text: title }),
      el("ul", { class: "bsec__list notes-actions" }, items.map((a) => el("li", {},
        el("span", {}, ...cited(a.what)),
        el("span", { class: "notes-actions__meta" },
          a.owner && !a.dave ? el("span", { text: a.owner }) : null,
          a.due ? el("span", { class: "notes-due", text: `Due ${dueLabel(a.due)}` }) : a.due_text ? el("span", { text: a.due_text }) : null))))) : null;
    content.push(actions("For you", mine), actions("Others' to-dos", others));
    content.push(briefSection("Decisions", n.decisions),
                 briefSection(n.sections && n.sections.length ? "Takeaways" : "Discussed", n.summary),
                 briefSection("Still open", n.open_questions));
    if (n.sections && n.sections.length) {
      // TeamsMaestro's detailed summary: its own sections, folded so the takeaways lead
      content.push(el("details", { class: "bsec bsources" },
        el("summary", { class: "bsec__title" }, `Detailed summary \u00b7 ${n.sections.length} topics`, icon("chevron-down")),
        ...n.sections.map((s) => el("div", { class: "ns-topic" },
          s.heading ? el("p", { class: "ns-topic__title", text: s.heading }) : null,
          el("ul", { class: "bsec__list" }, s.points.map((p) => el("li", { text: p })))))));
    }
    if (n.link) {
      content.push(el("p", { class: "ns-link" }, el("a", { href: n.link, target: "_blank", rel: "noopener" },
        "Open in TeamsMaestro (full transcript)", icon("external-link"))));
    }
  }
  // TeamsMaestro's notes come as a write-up, not a transcript (that's behind its link): no "Transcript" here
  const onlyMaestro = parts.length && parts.every((p) => p.source === "maestro");
  if (n.transcript && !onlyMaestro) {
    content.push(el("details", { class: "bsec bsources" },
      el("summary", { class: "bsec__title" }, `Transcript \u00b7 ${n.transcript.split("\n").length} lines`, icon("chevron-down")),
      el("ol", { class: "transcript" }, n.transcript.split("\n").map((line) => {
        const part = many && /^\[L(\d+)\]\s*(Part \d+: .*)$/.exec(line);
        if (part) return el("li", { id: `line-${part[1]}`, class: "transcript__part", text: part[2] });
        const m = /^\[L(\d+)\]\s*(?:(\d\d:\d\d)\s+)?(?:([^:]{1,40}):\s)?(.*)$/.exec(line) || [];
        return el("li", { id: `line-${m[1]}` },
          m[2] ? el("span", { class: "transcript__time", text: m[2] }) : null,
          m[3] ? el("span", { class: "transcript__who", text: m[3] }) : null,
          el("span", { text: m[4] || line }));
      }))));
  }
  $("#ns-content").replaceChildren(...content.filter(Boolean));
  $("#ns-foot").hidden = n.kind === "quick" || !(n.status === "ready" || n.written);
}

// Notes saved against the wrong meeting: move them (they join that meeting's notes if it has some)
function closeMove() {
  $("#ns-move-form").hidden = true;
  $("#ns-move-open").hidden = false;
}
$("#ns-move-open").addEventListener("click", () => {
  const n = openNote_;
  const select = $("#ns-move-meeting");
  meetingOptions(select, false);
  if (n.event_id && ![...select.options].some((o) => o.value === n.event_id)) {
    select.add(el("option", { value: n.event_id, text: n.title }), 1);   // a meeting from another day
  }
  select.value = n.event_id || "";
  $("#ns-move-hint").hidden = true;
  $("#ns-move-open").hidden = true;
  $("#ns-move-form").hidden = false;
  select.focus();
});
$("#ns-move-meeting").addEventListener("change", (e) => {
  const event = todayEvents.find((x) => x.id === e.target.value);
  const hint = $("#ns-move-hint");
  hint.hidden = !(event && event.note && event.note.id !== openNote_.id);
  hint.textContent = "That meeting already has notes. These will be added to them as another part.";
});
$("#ns-move-cancel").addEventListener("click", closeMove);
$("#ns-move-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const n = openNote_;
  const eventId = $("#ns-move-meeting").value || null;
  if (eventId === (n.event_id || null)) { closeMove(); return; }
  const button = e.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const moved = await api(`/api/notes/${encodeURIComponent(n.id)}`, { method: "PATCH", body: JSON.stringify({ event_id: eventId }) });
    closeMove();
    await loadToday();
    if (moved.id !== n.id) openNote(moved.id); else loadNote(n.id);
  } catch (err) {
    $("#ns-move-hint").textContent = `Couldn't move them: ${err.message}`;
    $("#ns-move-hint").hidden = false;
  } finally {
    button.disabled = false;
  }
});

$("#ns-close").addEventListener("click", () => closePanels());
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#notesheet").hidden) closePanels(); });

// Asking about a meeting's notes: a conversation that starts with those notes in view.
const nsInput = $("#ns-input");
nsInput.addEventListener("input", () => {
  nsInput.style.height = "auto";
  nsInput.style.height = `${Math.min(nsInput.scrollHeight, 160)}px`;
  $("#ns-send").disabled = !nsInput.value.trim();
});
nsInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#ns-foot").requestSubmit(); }
});
$("#ns-foot").addEventListener("submit", (e) => {
  e.preventDefault();
  const question = nsInput.value.trim();
  if (!question || busy || !openNote_) return;
  const note = openNote_;
  nsInput.value = "";
  closePanels();
  conversationId = null;
  writeStored(null);
  thread.replaceChildren();
  pendingTopic = { noteId: note.id, subject: note.title };
  setTopic(note.title);
  updateEmptyState();
  send(question);
});

// The list: search, upload a recording, paste notes
let notesSeq = 0;
async function loadNotesList(query) {
  const seq = ++notesSeq;
  const list = $("#notes-list");
  if (!list.children.length || !query) list.replaceChildren(...skeletonRows(4));
  try {
    const { notes } = await api(`/api/notes${query ? `?q=${encodeURIComponent(query)}` : ""}`);
    if (seq !== notesSeq) return;
    if (!notes.length) {
      list.replaceChildren(el("li", { class: "itinerary__empty",
        text: query ? `No notes mention \u201c${query}\u201d.` : "No notes yet. Tap Record during a meeting, or use one of the options above." }));
      return;
    }
    list.replaceChildren(...notes.map((n) => el("li", {},
      el("button", { class: "row row--tap", type: "button", onclick: () => openNote(n.id) },
        el("span", { class: "row__top" },
          el("span", { class: "row__title", text: n.title }),
          el("span", { class: "row__when", text: n.when ? ago(n.when) : "" })),
        el("span", { class: "row__sub" }, ...marked(n.status === "ready" ? (n.headline || n.text || "") : noteStatusText(n), query))))));
  } catch (err) {
    if (seq === notesSeq) list.replaceChildren(el("li", { class: "error", text: `Couldn't load: ${err.message}` }));
  }
}

// Notes | To-dos: the action items from all those meetings, Dave's first, then what others owe him
let notesTab = "notes";
function setNotesTab(tab) {
  notesTab = tab;
  $("#notes-tab-notes").setAttribute("aria-selected", String(tab === "notes"));
  $("#notes-tab-todos").setAttribute("aria-selected", String(tab === "todos"));
  $("#notes-view").hidden = tab !== "notes";
  $("#todos-view").hidden = tab !== "todos";
  $("#notes-search").placeholder = tab === "todos" ? "Search to-dos" : "Search notes";
  if (tab === "todos") loadTodos($("#notes-search").value.trim());
  else loadNotesList($("#notes-search").value.trim());
}
$("#notes-tab-notes").addEventListener("click", () => setNotesTab("notes"));
$("#notes-tab-todos").addEventListener("click", () => setNotesTab("todos"));

let todosSeq = 0;
async function loadTodos(query) {
  const seq = ++todosSeq;
  const box = $("#todos-view");
  if (!box.children.length) box.replaceChildren(el("ul", { class: "modal__list rows" }, ...skeletonRows(4)));
  try {
    const t = await api(`/api/todos${query ? `?q=${encodeURIComponent(query)}` : ""}`);
    if (seq !== todosSeq) return;
    $("#todos-count").textContent = t.counts.mine ? ` \u00b7 ${t.counts.mine}` : "";
    const ago = (days) => (days === 0 ? "today" : days === 1 ? "yesterday" : `${days} days ago`);
    const item = (x, showOwner) => {
      const box = el("input", { type: "checkbox", "aria-label": `Done: ${x.what}` });
      box.checked = x.done;
      box.addEventListener("change", async () => {
        box.disabled = true;
        try {
          await api(`/api/todos/${encodeURIComponent(x.id)}`, { method: "POST", body: JSON.stringify({ done: box.checked }) });
          loadTodos($("#notes-search").value.trim());
        } catch (err) {
          box.checked = !box.checked;
          box.disabled = false;
        }
      });
      const meta = [showOwner ? x.owner : "", x.shared_with && x.shared_with.length ? `with ${x.shared_with.join(", ")}` : "",
                    x.meeting, x.held ? ago(x.age_days) : ""].filter(Boolean).join(" \u00b7 ");
      return el("li", { class: `todo${x.done ? " todo--done" : ""}` },
        el("label", { class: "todo__check" }, box),
        el("button", { class: "todo__body", type: "button", title: "Open the meeting's notes", onclick: () => openNote(x.note_id) },
          el("span", { class: "todo__what", text: x.what }),
          el("span", { class: `todo__meta${x.old ? " todo__meta--old" : ""}`, text: x.old ? `${meta} \u00b7 open 2+ weeks` : meta })));
    };
    const parts = [];
    parts.push(el("p", { class: "notes-list-title", text: `Yours \u00b7 ${t.counts.mine} open` +
      (t.counts.mine_old ? ` \u00b7 ${t.counts.mine_old} open two weeks or more` : "") }));
    parts.push(t.mine.length ? el("ul", { class: "todos__list" }, t.mine.map((x) => item(x, false)))
      : el("p", { class: "itinerary__empty", text: query ? `None of yours mention \u201c${query}\u201d.` : "Nothing open from your meetings." }));
    if (t.others.length) {
      parts.push(el("p", { class: "notes-list-title", text: `Waiting on others \u00b7 ${t.counts.others}` }));
      parts.push(...t.others.map((g) => el("details", { class: "todos__group" },
        el("summary", {}, el("span", { text: g.owner }), el("span", { class: "todos__n", text: String(g.items.length) }), icon("chevron-down")),
        el("ul", { class: "todos__list" }, g.items.map((x) => item(x, false))))));
    }
    if (t.done.length) {
      parts.push(el("details", { class: "todos__group todos__group--done" },
        el("summary", {}, el("span", { text: "Done" }), el("span", { class: "todos__n", text: String(t.done.length) }), icon("chevron-down")),
        el("ul", { class: "todos__list" }, t.done.map((x) => item(x, !x.mine)))));
    }
    parts.push(el("p", { class: "todos__foot", text: `From the action items in your meeting notes, last ${t.days} days. Tick what's done.` }));
    $("#todos-view").replaceChildren(...parts);
  } catch (err) {
    if (seq === todosSeq) $("#todos-view").replaceChildren(el("p", { class: "error", text: `Couldn't load: ${err.message}` }));
  }
}

function showNotes() {
  openPopover("notes");
  $("#notes-search").value = "";
  $("#notes-paste-form").hidden = true;
  $("#notes-paste").setAttribute("aria-expanded", "false");
  $("#notes-list").replaceChildren();
  setNotesTab(notesTab);
}
$("#notes-btn").addEventListener("click", () => ($("#notes").hidden ? showNotes() : closePopovers()));
$("#notes-close").addEventListener("click", closePopovers);
let notesTimer = null;
$("#notes-search").addEventListener("input", (e) => {
  clearTimeout(notesTimer);
  notesTimer = setTimeout(() => (notesTab === "todos" ? loadTodos : loadNotesList)(e.target.value.trim()), 200);
});
$("#notes-paste").addEventListener("click", () => {
  const form = $("#notes-paste-form");
  form.hidden = !form.hidden;
  $("#notes-paste").setAttribute("aria-expanded", String(!form.hidden));
  if (!form.hidden) {
    meetingOptions($("#notes-paste-meeting"), false);
    joinsHint($("#notes-paste-meeting"), $("#notes-paste-joins"));
    $("#notes-paste-text").focus();
  }
});
$("#notes-paste-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#notes-paste-text").value.trim();
  if (!text) return;
  const button = e.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const note = await api("/api/notes", { method: "POST", body: JSON.stringify({ text, event_id: $("#notes-paste-meeting").value || null }) });
    $("#notes-paste-text").value = "";
    openNote(note.id);
  } catch (err) {
    e.currentTarget.append(el("p", { class: "error", text: err.message }));
  } finally {
    button.disabled = false;
  }
});

async function uploadRecording(blob, name, eventId) {
  const form = new FormData();
  form.append("file", blob, name);
  form.append("event_id", eventId || "");
  const res = await fetch("/api/recordings", { method: "POST", body: form, credentials: "same-origin" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}
$("#notes-upload").addEventListener("click", () => $("#notes-upload-input").click());
$("#notes-upload-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  const button = $("#notes-upload");
  button.disabled = true;
  button.querySelector(".notes-choice__title").textContent = "Uploading\u2026";
  try {
    const note = await uploadRecording(file, file.name, "");
    openNote(note.id);
  } catch (err) {
    $("#notes-list").prepend(el("li", { class: "error", text: `Couldn't upload: ${err.message}` }));
  } finally {
    button.disabled = false;
    button.querySelector(".notes-choice__title").textContent = "Upload a recording";
  }
});

// Recording in the app: the phone (or laptop) mic, saved when Dave stops.
// Record opens a small pop-up (which meeting, then Start); while it runs, a pill floats over the whole
// app, so he can look at the brief or the day without losing it. The header button never stops it.
let recorder = null;
let recChunks = [];
let recStarted = 0;
let recTick = null;
let recDiscarded = false;
let recFailed = null;     // { blob, name, eventId } when saving didn't work
let recEventId = "";      // the meeting chosen when it started
let recWake = null;       // keeps the screen on while recording: on an iPhone a locked screen stops the mic
let recAway = 0;          // when the mic went quiet (screen off, another app), to say how much was missed
let recMissed = 0;        // seconds missed so far

const isIPhone = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

async function holdScreen() {
  try {
    if (recorder && "wakeLock" in navigator && !recWake && document.visibilityState === "visible") {
      recWake = await navigator.wakeLock.request("screen");
      recWake.addEventListener("release", () => { recWake = null; });
    }
  } catch { /* not allowed here (low battery mode): the hint still says to keep it on screen */ }
}

function micAway() {
  if (recorder && !recAway) recAway = Date.now();
}

function micBack() {
  if (!recorder || !recAway) return;
  const gone = Math.round((Date.now() - recAway) / 1000);
  recAway = 0;
  if (gone < 3) return;   // a blip, not a gap
  recMissed += gone;
  const m = Math.floor(recMissed / 60), s = recMissed % 60;
  $("#rec-gap-text").textContent = `The screen was off or another app was open, so about ${m ? `${m} min ` : ""}${m && !s ? "" : `${s} sec `}wasn't recorded. Keep Geronimo on screen.`;
  $("#rec-gap").hidden = false;
  placePill(pillPlace());
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") micAway();
  else { micBack(); holdScreen(); }   // the lock is dropped whenever the page is hidden
});

function recordingType() {
  for (const t of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"]) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(t)) return t;
  }
  return "";
}

function recElapsed() {
  const s = Math.floor((Date.now() - recStarted) / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function recMeetingLabel() {
  const select = $("#rec-meeting");
  return select.value ? select.selectedOptions[0].textContent.replace(/^.*?\u00b7\s*/, "") : "Not a calendar meeting";
}

function showRecStart() {
  if (recorder || recFailed) { showRecPill(); nudgePill(); return; }   // already recording: point at it, don't stop
  meetingOptions($("#rec-meeting"));
  joinsHint($("#rec-meeting"), $("#recstart-joins"));
  $("#recstart-error").hidden = true;
  $("#recstart-phone").hidden = !isIPhone;
  $("#rec-start").disabled = false;
  openPopover("recstart");
}

async function startRecording() {
  const error = (text) => { $("#recstart-error").textContent = text; $("#recstart-error").hidden = false; $("#rec-start").disabled = false; };
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    error("This browser can't record audio. Upload a recording from Notes instead.");
    return;
  }
  $("#rec-start").disabled = true;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    error("The microphone isn't available. Allow microphone access for this site and try again.");
    return;
  }
  const type = recordingType();
  recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
  recChunks = [];
  recDiscarded = false;
  recorder.ondataavailable = (e) => { if (e.data && e.data.size) recChunks.push(e.data); };
  recorder.onstop = finishRecording;
  // If the phone takes the mic away (a call, the app closed), keep what was recorded
  stream.getAudioTracks().forEach((t) => {
    t.addEventListener("ended", () => stopRecording(false));
    t.addEventListener("mute", micAway);     // iPhone: the screen locked or Dave switched apps
    t.addEventListener("unmute", micBack);
  });
  recorder.start(10000);   // a chunk every 10s, so a long meeting isn't one huge buffer
  recStarted = Date.now();
  recAway = recMissed = 0;
  $("#rec-gap").hidden = true;
  holdScreen();

  recEventId = $("#rec-meeting").value;
  $("#rec-for").textContent = recMeetingLabel();
  closePopovers();
  $("#record-btn").classList.add("is-recording");
  const tick = () => {
    $("#rec-time").textContent = recElapsed();
    $("#record-btn-label").textContent = recElapsed();
  };
  tick();
  recTick = setInterval(tick, 1000);
  showRecPill();
}

function showRecPill() {
  $("#recbar").hidden = false;
  setRecPanel(recFailed ? "failed" : null);
  placePill(loadPillPlace());
}

function nudgePill() {
  const pill = $("#recbar");
  pill.classList.remove("is-nudged");
  void pill.offsetWidth;   // restart the animation
  pill.classList.add("is-nudged");
}

// Under the bar: nothing, the discard question, or a save that failed
function setRecPanel(which) {
  $("#rec-confirm").hidden = which !== "confirm";
  $("#rec-failed").hidden = which !== "failed";
  $("#rec-discard").hidden = !!recFailed;
  $("#rec-discard").setAttribute("aria-expanded", String(which === "confirm"));
  placePill(pillPlace());   // keep it on screen as it grows
}

function stopRecording(discard) {
  if (!recorder || recorder.state === "inactive") return;
  recDiscarded = discard;
  recorder.stop();
  recorder.stream.getTracks().forEach((t) => t.stop());
  clearInterval(recTick);
}

function endRecordingUi() {
  recorder = null;
  if (recWake) recWake.release().catch(() => {});
  recAway = recMissed = 0;
  $("#rec-gap").hidden = true;
  $("#recbar").hidden = true;
  $("#record-btn").classList.remove("is-recording");
  $("#record-btn-label").textContent = "Record";
  $("#rec-stop").disabled = false;
  $("#rec-stop").hidden = false;
  $("#recbar").classList.remove("is-failed");
}

async function finishRecording() {
  const type = recorder.mimeType || "audio/webm";
  const eventId = recEventId;
  if (recDiscarded || !recChunks.length) { endRecordingUi(); return; }
  const blob = new Blob(recChunks, { type });
  const ext = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
  await saveRecording({ blob, name: `recording.${ext}`, eventId });
}

async function saveRecording(rec) {
  $("#rec-stop").disabled = true;
  $("#rec-time").textContent = "Saving\u2026";
  try {
    const note = await uploadRecording(rec.blob, rec.name, rec.eventId);
    recFailed = null;
    endRecordingUi();
    openNote(note.id);
  } catch (err) {
    // Don't lose the meeting: keep the pill up with Try again
    recFailed = rec;
    recorder = null;
    $("#rec-time").textContent = "Not saved";
    $("#record-btn-label").textContent = "Not saved";
    $("#recbar").classList.add("is-failed");
    $("#rec-failed-text").textContent = `The recording couldn't be saved: ${err.message}`;
    $("#rec-stop").hidden = true;
    setRecPanel("failed");
  }
}

$("#record-btn").addEventListener("click", () => ($("#recstart").hidden ? showRecStart() : closePopovers()));
$("#recstart-close").addEventListener("click", closePopovers);
$("#rec-start").addEventListener("click", startRecording);
$("#rec-meeting").addEventListener("change", () => joinsHint($("#rec-meeting"), $("#recstart-joins")));
$("#notes-paste-meeting").addEventListener("change", () => joinsHint($("#notes-paste-meeting"), $("#notes-paste-joins")));
$("#rec-stop").addEventListener("click", () => stopRecording(false));
$("#rec-discard").addEventListener("click", () => {
  if ($("#rec-confirm").hidden) { setRecPanel("confirm"); $("#rec-keep").focus(); } else setRecPanel(null);
});
$("#rec-keep").addEventListener("click", () => setRecPanel(null));
$("#rec-discard-yes").addEventListener("click", () => stopRecording(true));
$("#rec-retry").addEventListener("click", () => { $("#rec-failed").hidden = true; saveRecording(recFailed); });
$("#rec-failed-drop").addEventListener("click", () => {
  recFailed = null;
  endRecordingUi();
});
window.addEventListener("beforeunload", (e) => {   // closing the page mid-meeting would lose it
  if (recorder || recFailed) { e.preventDefault(); e.returnValue = ""; }
});

// Dragging the pill: anywhere on its bar except the buttons' taps; it stays on screen and remembers where it was.
let pillDragged = false;
function pillPlace() {
  const pill = $("#recbar");
  return { x: pill.offsetLeft, y: pill.offsetTop };
}
function loadPillPlace() {
  try {
    const saved = JSON.parse(localStorage.getItem("recPill") || "null");
    if (saved && typeof saved.x === "number") return saved;
  } catch { /* no storage: use the default */ }
  const pill = $("#recbar");
  return { x: innerWidth - pill.offsetWidth - 24, y: 72 };   // just under the Record button
}
function placePill({ x, y }) {
  const pill = $("#recbar");
  const pad = 8;
  pill.style.left = `${Math.round(Math.min(Math.max(pad, x), innerWidth - pill.offsetWidth - pad))}px`;
  pill.style.top = `${Math.round(Math.min(Math.max(pad, y), innerHeight - pill.offsetHeight - pad))}px`;
}
(() => {
  const handle = $("#rec-handle");
  let start = null;
  handle.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || e.target.closest("button")) return;
    const at = pillPlace();
    start = { px: e.clientX, py: e.clientY, x: at.x, y: at.y, id: e.pointerId };
    pillDragged = false;
  });
  window.addEventListener("pointermove", (e) => {
    if (!start || e.pointerId !== start.id) return;
    const dx = e.clientX - start.px, dy = e.clientY - start.py;
    if (!pillDragged && Math.hypot(dx, dy) < 6) return;   // a tap, not a drag
    if (!pillDragged) { pillDragged = true; $("#recbar").classList.add("is-dragging"); }
    placePill({ x: start.x + dx, y: start.y + dy });
  });
  const end = () => {
    if (!start) return;
    start = null;
    $("#recbar").classList.remove("is-dragging");
    if (pillDragged) {
      try { localStorage.setItem("recPill", JSON.stringify(pillPlace())); } catch { /* fine */ }
      setTimeout(() => { pillDragged = false; }, 0);   // swallow the click that ends a drag
    }
  };
  window.addEventListener("pointerup", end);
  window.addEventListener("pointercancel", end);
  window.addEventListener("resize", () => { if (!$("#recbar").hidden) placePill(pillPlace()); });
})();

// ── what the assistant remembers ───────────────────────────────────────────────────────────

function closeMemory() {
  closePopovers();
}

async function showMemory() {
  openPopover("memory");
  const list = $("#memory-list");
  list.replaceChildren(...skeletonRows(4));
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
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMemory(); });

$("#recent-btn").addEventListener("click", () => { closeMemory(); ($("#recent").hidden ? showRecent() : closeRecent()); });
$("#recent-close").addEventListener("click", closeRecent);
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

async function send(text, files = null) {
  text = (text || "").trim();
  if (files === null) {
    if (chatFiles.uploading()) return;
    files = chatFiles.ready();
  }
  if ((!text && !files.length) || busy) return;
  busy = true;
  sendBtn.disabled = true;
  chatFiles.clear();
  showTab("chat");
  if (!conversationId) setChatTitle(pendingTopic ? `${pendingTopic.noteId ? "Notes" : "Brief"}: ${pendingTopic.subject}` : shortTitle(text || files.map((f) => f.name).join(", ")));
  addNote(text, files);
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
      attachments: files.map((f) => f.id),
      conversation_id: conversationId,
      brief_event_id: !conversationId && pendingTopic ? pendingTopic.eventId || null : null,
      note_id: !conversationId && pendingTopic ? pendingTopic.noteId || null : null,
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
    const retry = el("button", { class: "btn", type: "button", text: "Try again", onclick: () => { errorNode.remove(); thread.lastChild?.remove(); if (files.length) thread.lastChild?.remove(); busy = false; send(text, files); } });
    const errorNode = el("div", { class: "error" }, `Something went wrong: ${err.message}`, retry);
    thread.append(errorNode);
  } finally {
    busy = false;
    updateSend();
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

input.addEventListener("input", () => { updateSend(); autosize(); });

// ── attaching files: the paperclip, drag and drop, or paste ───────────────────
// Each file is read as soon as it's added, so one that can't be read says so before sending.
// The chat's input and a brief's "Ask about this meeting" box each have their own.

const MAX_ATTACHMENTS = 10;

function attachable({ list, button, picker, field, sendButton }) {
  const items = [];   // { id, name, kind, summary, thumb, state: uploading | ready | error, error }
  const box = {
    ready: () => items.filter((a) => a.state === "ready"),
    uploading: () => items.some((a) => a.state === "uploading"),
    update() {
      sendButton.disabled = busy || box.uploading() || (!field.value.trim() && !box.ready().length);
    },
    render() {
      list.hidden = !items.length;
      list.replaceChildren(...items.map((a) => fileChip(a,
        el("button", { class: "file-chip__remove", type: "button", "aria-label": `Remove ${a.name}`, title: "Remove",
                       onclick: () => { items.splice(items.indexOf(a), 1); box.render(); field.focus(); } },
          icon("x")))));
      box.update();
    },
    clear() {
      items.length = 0;
      box.render();
    },
    async add(fileList) {
      for (const file of [...fileList].slice(0, MAX_ATTACHMENTS - items.length)) {
        const entry = { name: file.name || "Pasted image", state: "uploading" };
        items.push(entry);
        box.render();
        const form = new FormData();
        form.append("file", file, entry.name === "Pasted image" ? "pasted-image.png" : file.name);
        try {
          const res = await fetch("/api/uploads", { method: "POST", body: form, credentials: "same-origin" });
          if (res.status === 401) { location.href = "/auth/login"; return; }
          const body = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(body.detail || res.statusText);
          Object.assign(entry, body, { state: "ready" });
        } catch (err) {
          Object.assign(entry, { state: "error", error: err.message });
        }
        if (items.includes(entry)) box.render();
      }
    },
  };
  button.addEventListener("click", () => picker.click());
  picker.addEventListener("change", (e) => { box.add(e.target.files); e.target.value = ""; field.focus(); });
  field.addEventListener("paste", (e) => {
    const files = [...(e.clipboardData?.files || [])];
    if (!files.length) return;   // ordinary text paste
    e.preventDefault();
    box.add(files);
  });
  return box;
}

const chatFiles = attachable({ list: $("#attach-list"), button: $("#attach-btn"), picker: $("#attach-input"),
                               field: input, sendButton: sendBtn });
const briefFiles = attachable({ list: $("#bs-attach-list"), button: $("#bs-attach-btn"), picker: $("#bs-attach-input"),
                                field: $("#bs-input"), sendButton: $("#bs-send") });
const updateSend = () => chatFiles.update();

(() => {   // drop files anywhere on the assistant's side: into the brief's box if one is open
  const pane = $(".pane--chat");
  const zone = $("#dropzone");
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  pane.addEventListener("dragenter", (e) => { if (!hasFiles(e)) return; e.preventDefault(); depth += 1; zone.hidden = false; });
  pane.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  pane.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) zone.hidden = true; });
  pane.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    zone.hidden = true;
    if (!$("#briefsheet").hidden && !$("#bs-foot").hidden) {
      briefFiles.add(e.dataTransfer.files);
      return;
    }
    closePanels();
    showTab("chat");
    chatFiles.add(e.dataTransfer.files);
  });
})();
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

$("#new-chat").addEventListener("click", () => {
  closePanels();
  chatFiles.clear();
  closePopovers();
  setChatTitle("");
  showTab("chat");
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
