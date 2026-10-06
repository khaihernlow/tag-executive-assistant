# TAG Executive Assistant — Build Plan

Status: draft · 2026-10-05 · owner: Kai

## 1. Goal

An AI executive assistant for Dave (CEO) that takes over what Maria does today,
and is built so it can eventually handle all of it without anyone stepping in.

Source of requirements: Dave's task list (2026-09-16) and the scoping call with him.

### What Maria does today (from the call)

| # | Task | What it actually means | Systems |
|---|------|------------------------|---------|
| 1 | Inbox management | Catch spam the filter missed, mark it and move it; file emails into folders (e.g. monthly financials from Joe, employee requests into their folder) | Outlook |
| 2 | Meeting scheduling | (a) Someone emails asking to meet → check the calendar and book it. (b) Dave says "set up a meeting with Kai next week" → contact the person, agree a time, book it. HubSpot meeting link also exists | Outlook calendar, HubSpot |
| 3 | Pre-meeting research | Before a prospect meeting: who the person is (role, background, school), what the company does (About Us page) | Calendar, web, LinkedIn-type data |
| 4 | Autotask opportunities / quotes | Dave emails "enter this opportunity" → create it in Autotask | Autotask |
| 5 | Autotask oversight | Check employees' time entries and that contractually required client meetings are on track | Autotask (shared pool) |
| 6 | Meeting notes | Dave already uses AI note-takers. Ideal: notes end up in HubSpot on the right account | HubSpot |
| 7 | Respond on Dave's behalf | Dave asked whether it can reply the way he would. Later phase, once trust is built | Outlook |
| 8 | Mileage / expense report | Calendar → trips → Excel → send to CFO. Rare, so later | Calendar, Excel |
| 9 | Google review requests | Praise in an Autotask ticket/email → send review link or QR code. Dave: "phase 2" | Autotask, email |
| 10 | Wednesday team lunch | Text the team, order on Grubhub for the management meeting | SMS, Grubhub |

## 2. Product shape

- **Main interface: a web chat** (like ChatGPT/Claude) for planning and back-and-forth:
  "set up meetings with these three prospects", "what's on my plate this week", reviewing drafts.
  It has to work well on a phone, because Dave is often on the move. Make it an installable PWA.
- **Second way in: email.** Dave forwards or CCs the assistant ("Maria-style": "can you get this
  opportunity into Autotask?"), and outside contacts reply on scheduling threads. Each email thread
  becomes a conversation that also shows in the web chat.
- **Approvals inbox** in the web UI: anything the assistant wants to *do* (send, book, create, move)
  shows up for one-tap approval until that kind of action is allowed to run on its own.
- **Activity log**: every action it took, when, why, and a link to the result.

### How much it does on its own (the path to replacing Maria)

Each action type has its own setting, and it can only move up after a track record:

```
off  →  draft (Dave/Kai approves)  →  auto + notify  →  auto silent
```

Example: "move obvious spam to Junk" goes to auto quickly. "Reply to a client as Dave" stays at
draft for a long time.

## 3. Architecture

Separate app, separate repo, separate container. It shares infrastructure with tag-tool-suite but
not code at runtime.

```
                ┌──────────── tag-executive-assistant (container) ────────────┐
 Dave (web/PWA) │  FastAPI web: chat UI · approvals · activity · settings     │
 Dave (email) ──┼─► Channels: web chat, email gateway (Graph mailbox watcher)  │
                │        │                                                    │
                │        ▼                                                    │
                │  Agent core: conversation loop · tool registry ·            │
                │              action policy (off/draft/auto) · memory        │
                │        │                       ▲                            │
                │        ▼                       │                            │
                │  Tasks (long-running, resumable: "schedule X with Y")       │
                │  Scheduler (daily brief, pre-meeting briefs, Wed lunch)     │
                │        │                                                    │
                │  Connectors: Graph (mail/calendar) · Autotask · HubSpot ·   │
                │              Web research · Tool-suite data (read-only)     │
                └────────┬───────────────────────────────┬───────────────────┘
                         │ n8n-network                   │ HTTPS
                  Postgres (own ea_* schema       HatzAI · Microsoft Graph ·
                  + read-only shared pool)        Autotask · HubSpot
```

### Key design decisions

1. **Stack matches tag-tool-suite**: Python 3.12, FastAPI, Jinja2 + small vanilla JS for the chat
   (SSE streaming), Postgres, Azure AD SSO, Docker image deployed via `docker save` the same way,
   on `n8n-network` behind the Cloudflare Tunnel.
2. **Login is limited to an allowlist** (Dave, Kai) on top of Azure SSO. The app holds Dave's mailbox
   access, so a normal TAG login alone must not get anyone in.
3. **The LLM provider sits behind an interface** (`llm/provider.py`). HatzAI is first. If HatzAI
   can't do native tool calling, a JSON-based tool protocol runs on the same interface (see risk R1).
   Swapping providers later is just a config change.
4. **Tasks, not just chat turns.** Scheduling with an outside person can take days of emails. A
   `task` row holds the goal, its state, and links to the related threads. An incoming email or a
   timer resumes it. This is the core of "acting like Maria", and it's also what makes it the
   hardest part.
5. **Every write goes through the action policy** (approve / auto) and is written to the activity
   log, which records what was proposed, who approved it, and the API result.
6. **Tool-suite integration is read-only**: it queries the shared pool directly (autotask_tickets,
   ms_customers_meta, vCIO data) for briefs and oversight reports. `autotask_client.py` gets copied
   in for now. Only extract a shared package if keeping two copies starts to cost effort.
7. **Polling before webhooks.** Check the mailbox every 1–2 minutes first. Graph change
   notifications through the tunnel come later if the delay matters.

8. **Code for anything with a right answer; the LLM only for language and judgment.**
   The LLM turns Dave's words into tool calls, reads unstructured text into fixed fields, drafts
   prose and adds short commentary. Code finds people, searches, does all time math, checks
   permissions, runs multi-step flows and executes actions. **Tool results are shown to Dave as UI
   elements (cards, slot buttons), never re-typed by the LLM.** On the first real-calendar test the
   free-time search was correct, but the LLM dropped two of its windows when writing the answer.
9. **Interface = home screen + chat, not a bare chat box.** Home (built by code): today's agenda,
   "Needs you" (triaged email, pending approvals), quick actions. Chat for ad-hoc requests; replies
   come back as cards with buttons ("Book", "Approve", "Edit"). Email in is a second way in.
   An activity log shows everything it did.

### Data model (first pass)

- `conversations`, `messages` — chat + email threads, channel tag
- `tasks` — goal, status, state JSON, next_check_at, linked conversation/threads
- `actions` — proposed/approved/executed/failed, tool, args, result, policy level, approver
- `memory` — Dave's preferences and facts (meeting lengths, buffer rules, VIP contacts, tone notes)
- `contacts` — people the assistant deals with, plus how to reach them
- `briefs` — generated pre-meeting research, cached per event

## 3a. Tool catalog

R = read, W = write (W always goes through the action policy: approval first, auto later).
"Code" / "LLM" = who does the core work.

### People & directory
| Tool | R/W | Core | Notes |
|---|---|---|---|
| `find_person(name)` | R | Code | Entra directory + People (relevance) + Outlook contacts, later Autotask/HubSpot contacts. Fuzzy ("Khai"/"Kai"); ambiguous → choice buttons |
| `get_free_busy(people, range)` | R | Code | Graph `getSchedule`, works for internal staff |
| `find_mutual_time(people, range, duration, prefs)` | R | Code | Intersects free/busy with Dave's working hours and saved preferences |

### Mail
| Tool | R/W | Core | Notes |
|---|---|---|---|
| `search_mail(from, about, since, folder)` | R | Code | Graph search; returns cards (sender, subject, date, snippet) |
| `read_email(id)` | R | Code | HTML → clean text, quoted history stripped, attachments listed |
| `extract_request(email)` | R | LLM | Fixed fields: what's asked, who, duration, preferences, deadline |
| `triage_inbox()` (job) | R/W | Code + LLM | Microsoft junk filter + rules first; LLM only for borderline. Feeds "Needs you" |
| `move_email` / `mark_junk` | W | Code | Spam cleanup, foldering |
| `suggest_inbox_rule` | W | Code | Repeated filing (e.g. Joe's monthly financials → Finance) becomes a real Outlook rule instead of an LLM call per email |
| `draft_reply(id, intent)` | W | LLM | Saved to Drafts in Dave's voice (style from Sent Items); sending is a separate approved action |
| `send_email` | W | Code | Only sends approved drafts |

### Calendar
| Tool | R/W | Core | Notes |
|---|---|---|---|
| `list_calendar_events`, `find_free_time` | R | Code | ✅ built |
| `create_event` (Teams link, invitees, thread reply) | W | Code | First write action |
| `update_event` / `cancel_event` / `respond_to_invite` | W | Code | |
| `meeting_brief(event)` | R | Code + LLM | Code gathers attendees → people/company → Autotask, HubSpot, shared pool, web; LLM writes the brief |

### Research
| Tool | R/W | Core | Notes |
|---|---|---|---|
| `research_company(name/domain)` | R | Code + LLM | Company site "About", recent news. HatzAI's built-in search tools (firecrawl/tavily on `/chat/completions`) can do the fetching |
| `research_person(name, company)` | R | Code + LLM | Web search results only (no LinkedIn scraping); enrichment API later if needed |

### Autotask
| Tool | R/W | Core | Notes |
|---|---|---|---|
| `find_company` / `find_contact` | R | Code | |
| `create_opportunity` / `create_quote` | W | Code + LLM | LLM fills fields from Dave's message/email; code validates (company, owner = Dave's resource, stage, amount) |
| `time_entry_report(week)` | R | Code | Who's missing hours, unusual entries; LLM writes a 2-line summary at most |
| `client_meeting_compliance()` | R | Code | Contractual client meetings done vs required, per account: on track / off track |
| `renewals_due(days)` | R | Code | Contracts ending soon / `NeedsRenewalReview` UDF |
| `onboarding_status()` | R | Code | Overdue onboarding projects/tasks |
| `positive_feedback()` | R | LLM | Phase 5: spot praise in tickets → Google review request |

### HubSpot
| Tool | R/W | Core | Notes |
|---|---|---|---|
| `find_company` / `find_contact` | R | Code | |
| `log_meeting_note(company, notes)` | W | Code + LLM | Notes onto the right account (Dave's "perfect world") |
| `meeting_link()` | R | Code | Dave's HubSpot scheduling link for outside contacts, an easy win |

### TAG data (shared pool / tool suite)
| Tool | R/W | Core | Notes |
|---|---|---|---|
| `client_health(company)` | R | Code | Churn score, vCIO signals, open/recent tickets, for client meeting briefs |

### Teams, tasks, files
| Tool | R/W | Core | Notes |
|---|---|---|---|
| `send_teams_message(person/group)` | W | Code | Nudges, Wednesday lunch poll |
| `meeting_transcript(event)` | R | Code | Teams transcript → notes → HubSpot |
| `create_todo(text, due)` | W | Code | "Remind me to…" via Microsoft To Do |
| `mileage_report(month)` | W | Code | Phase 5: trips from calendar locations → distances → Excel in OneDrive → draft to CFO |

### Assistant memory
| Tool | R/W | Core | Notes |
|---|---|---|---|
| `remember` / `recall` | W/R | Code | Dave's preferences (meeting lengths, no meetings before X, protected blocks, VIPs). Applied by code, e.g. inside `find_mutual_time` |

### Scheduled jobs (code-triggered, not chat)
Morning brief · pre-meeting briefs the evening before · inbox triage every few minutes ·
Monday oversight report (time entries + client meetings) · renewals heads-up · follow-ups on
unanswered scheduling emails.

### Milestone 1 (first end-to-end slice)
Dave: *"Check the recent email from Khai about Project X, can you get us a meeting on the calendar?"*
Needs: `find_person`, `search_mail` + `read_email` + `extract_request`, `find_mutual_time`,
`create_event` behind an approval card, and the home + chat web page that shows results as cards.

## 4. Phases

### Phase 0 — Foundations (verify the risky assumptions first)
- [ ] Repo skeleton, `.env` (gitignored), Docker, SSO + allowlist, deploy works on the server
- [ ] **Probe HatzAI tool calling**: send `tools=[...]` to `/chat/completions` and check for
      `tool_calls` in the response. Decides R1.
- [ ] **Probe Graph access to Dave's mailbox/calendar** (see R2): read inbox, read calendar,
      create a draft
- [ ] Probe Autotask API user permissions for creating Opportunities; HubSpot private-app token
- [ ] LLM provider interface + tool registry + action/activity log tables

### Phase 1 — Chat MVP, read-only (low risk, builds trust)
- [ ] Web chat with streaming, mobile-friendly
- [ ] Tools: search/read inbox, read calendar/free-busy, look up Autotask company/opportunity,
      look up HubSpot contact/company, web research
- [ ] "Brief me on my 2pm" → person + company research
- [ ] Daily morning brief (today's meetings + briefs, inbox items that need Dave)

### Phase 2 — Actions with approval
- [ ] Inbox triage: spam → Junk, file emails into folders (rules learned from Dave's existing folders)
- [ ] Draft replies in Dave's voice (style samples from Sent Items), approve-to-send
- [ ] Book meetings when the time is already agreed / internal attendees (free-busy)
- [ ] Create Autotask opportunities from a chat message or forwarded email
- [ ] Push meeting notes to HubSpot on the right company/contact

### Phase 3 — Email gateway + long-running tasks
- [ ] Watch the assistant/Dave mailbox, map threads to conversations
- [ ] Scheduling task: propose times, handle replies, book, confirm, follow up if no answer
- [ ] Pre-meeting briefs generated automatically the evening before

### Phase 4 — Oversight & proactive
- [ ] Weekly Autotask oversight report: time-entry gaps, contractual client meetings on/off track
      (shared pool)
- [ ] vCIO / churn signals included in client meeting briefs
- [ ] Raise action types to auto when their approval history supports it

### Phase 5 — Later
- [ ] Google review requests from positive feedback in Autotask
- [ ] Mileage/expense report from calendar → Excel → email to CFO
- [ ] Wednesday lunch: text the team for orders, then the order itself (see R4)
- [ ] Autonomous replies as Dave for well-understood categories

## 5. Risks / open questions

- **R1 — HatzAI tool calling. RESOLVED 2026-10-05: supported, on the right endpoint.**
  HatzAI has three gateways ([docs](https://api-docs.hatz.ai/)):
  - `/v1/chat/completions`: Hatz-native, and only runs Hatz's own built-in tools
    (`tools_to_use`). It silently drops our own `tools`, and the model then *makes up* tool calls and
    results in its text. **Never use it for the agent.**
  - `/v1/anthropic/messages`: Anthropic Messages format, tools we define and run ourselves.
    **The agent uses this one** (`llm/hatzai.py`).
  - `/v1/openai/responses`: OpenAI Responses format, also with our own tools.
  `scripts/hatzai_tools_probe.py` passes all four checks (tool_use, tool_result round trip, two tools
  in one turn) on claude-sonnet-4-6, claude-sonnet-5 and gpt-5.5, so we can pick models freely.
  Rule that still applies: the activity log, not the model's text, is the record of what was done.
- **R2 — Getting into Outlook with a password.** Microsoft has turned off basic auth for
  IMAP/SMTP/EWS, and password-only token flows (ROPC) fail when MFA (Duo) is on. In practice, Graph
  access needs an Entra app registration anyway. Two routes:
  - *Application permissions + an access policy limited to Dave's mailbox*. Admin consent once, and
    Dave is never asked again. Best fit for "don't keep bothering him", and TAG administers the tenant.
  - *Delegated sign-in*: Dave signs in once, then a refresh token keeps it working.
  The password is still useful for poking around in the UIs directly.
- **R3 — LinkedIn.** Automated scraping breaks LinkedIn's terms and gets accounts restricted. Use web
  search results, the company site and HubSpot enrichment first. Consider an enrichment API
  (e.g. Apollo / People Data Labs) if profiles really need more depth.
- **R4 — Grubhub.** There's no public ordering API, so it would mean automating the website,
  including payment. Fragile. Phase 5, and maybe "build the cart, Dave/Kai taps order".
- **R7 — Voice. Checked 2026-10-05: HatzAI has no audio support.** No transcription endpoint, and
  audio sent to models through its gateways is silently dropped (one model then invented a
  transcript). For now Dave uses iPhone keyboard dictation in the text box. Options for a real mic
  button later: self-hosted Whisper on TAG's server (private, free), or a paid speech-to-text API.
- **R5 — SMS.** Maria texts people. Texting needs Twilio or similar (cost, number registration).
  Not needed until scheduling-by-text or lunch.
- **R6 — Sending as Dave vs. as the assistant.** Decide whether scheduling emails go from Dave's
  mailbox ("Dave would like to meet…", the way Maria writes them) or from an assistant mailbox.
- **Q — Which mailbox does the email gateway watch?** Its own address (assistant@…) is cleaner:
  Dave CCs/forwards to it. Watching Dave's inbox directly is needed for triage anyway.
- **Q — Who approves while Dave is busy?** Kai as a fallback approver during the pilot?

## 6. Proposed repo layout

```
tag-executive-assistant/
  app/            FastAPI app, routes, templates, static (chat UI, approvals, activity)
  agent/          conversation loop, tool registry, action policy, memory
  llm/            provider interface, hatzai adapter
  connectors/     graph.py, autotask.py, hubspot.py, research.py, toolsuite.py
  tasks/          long-running task engine + task types (scheduling, ...)
  jobs/           scheduler entrypoints (daily brief, pre-meeting briefs, mailbox poll)
  store/          db connect, migrations
  scripts/        probes (hatzai_tools_probe.py, graph_probe.py, ...)
  tests/
  docs/
```
