// TAG Assistant front end. Plain DOM, no framework. All server text goes in
// through textContent (or the tiny escaped markdown renderer), never raw HTML.

const $ = (sel) => document.querySelector(sel);
const thread = $("#thread");
const scroller = $("#scroll");
const input = $("#input");
const sendBtn = $("#send");

const STORE_KEY = "ea.conversation";
const ACTIVE_KEY = "ea.lastActive";
// After a gap this long, opening the app starts a fresh conversation. Dave
// never has to manage chats; old ones stay under Recent.
const FRESH_AFTER_MS = 2 * 60 * 60 * 1000;
let busy = false;

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
    renderSignoff(day.pending);
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
    return el("li", { class: classes.join(" ") },
      el("span", { class: "stop__time", text: e.start }),
      el("span", { class: "stop__what" },
        el("span", { class: "stop__subject", text: e.subject }),
        meta ? el("span", { class: "stop__meta", text: meta }) : null),
      e.join_url && !e.past ? el("a", { class: "join", href: e.join_url, target: "_blank", rel: "noopener", text: "Join" }) : el("span"),
    );
  }));
}

function renderSignoff(pending) {
  $("#signoff-block").hidden = !pending.length;
  $("#signoff").replaceChildren(...pending.map(slip));
}

// ── cards ──────────────────────────────────────────────────────────────────────

const KIND_LABELS = { create_event: "Calendar invite" };

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
  node.className = "slip";

  if (action.status === "pending") {
    const approve = el("button", { class: "btn btn--sign", type: "button", text: "Approve" });
    const decline = el("button", { class: "btn", type: "button", text: "Decline" });
    const decide = async (verb) => {
      approve.disabled = decline.disabled = true;
      approve.textContent = verb === "approve" ? "Booking…" : approve.textContent;
      try {
        const updated = await api(`/api/actions/${action.action_id}/${verb}`, { method: "POST" });
        updateSlips(updated);
        loadToday();
      } catch (err) {
        approve.disabled = decline.disabled = false;
        approve.textContent = "Approve";
        node.append(el("p", { class: "error", text: `Couldn't ${verb}: ${err.message}` }));
      }
    };
    approve.addEventListener("click", () => decide("approve"));
    decline.addEventListener("click", () => decide("reject"));
    parts.push(el("div", { class: "slip__actions" }, approve, decline));
  } else if (action.status === "executed") {
    node.classList.add("slip--done");
    const r = action.result || {};
    parts.push(el("span", { class: "stamp stamp--done" }, "✓ Signed · booked",
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
    default: return null;
  }
}

// ── thread ─────────────────────────────────────────────────────────────────────

function addNote(text) {
  thread.append(el("div", { class: "note", text }));
}

function addBrief(text, cards = []) {
  const textNode = el("div", { class: "brief__text" });
  textNode.innerHTML = renderMarkdown(text); // escaped inside renderMarkdown
  thread.append(el("div", { class: "brief" }, text ? textNode : null, ...cards.map(renderCard)));
}

async function openConversation(id) {
  if (!id) return;
  try {
    const convo = await api(`/api/conversations/${encodeURIComponent(id)}`);
    thread.replaceChildren();
    for (const m of convo.messages) {
      if (m.role === "user") addNote(m.text); else addBrief(m.text, m.cards || []);
    }
    conversationId = id;
    writeStored(id);
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

$("#recent-btn").addEventListener("click", () => ($("#recent").hidden ? showRecent() : closeRecent()));
$("#recent-close").addEventListener("click", closeRecent);
$("#recent").addEventListener("click", (e) => { if (e.target.id === "recent") closeRecent(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeRecent(); });

async function send(text) {
  text = (text || "").trim();
  if (!text || busy) return;
  busy = true;
  sendBtn.disabled = true;
  addNote(text);
  const thinking = el("p", { class: "thinking", text: "Working on it" });
  thread.append(thinking);
  scrollToEnd();
  try {
    const reply = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message: text, conversation_id: conversationId }),
    });
    conversationId = reply.conversation_id;
    writeStored(conversationId);
    writeStored(String(Date.now()), ACTIVE_KEY);
    thinking.remove();
    addBrief(reply.text, reply.cards);
    if (reply.cards.some((c) => c.type === "action")) loadToday();
  } catch (err) {
    thinking.remove();
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
$("#new-chat").addEventListener("click", () => {
  conversationId = null;
  writeStored(null);
  thread.replaceChildren();
  input.focus();
});

loadToday();
openConversation(conversationId);
setInterval(loadToday, 5 * 60 * 1000);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => { /* optional */ });
}
