import json
import os
import threading
import uuid
from datetime import datetime, time, timedelta
from pathlib import Path

from authlib.integrations.starlette_client import OAuth, OAuthError
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

load_dotenv(override=True)

from agent.actions import public_action
from agent.briefs import needs_brief
from agent.filing import filed_today, undo_filing
from agent.junk import moved_today, undo as undo_junk
from agent.changes import answer_invite, pending_invites
from agent.hubspot import booking_link
from agent.requests import open_requests_view, propose_booking, propose_hold, propose_nudge, propose_reply
from agent.calendar import agenda_day, event_detail, local_zone, meeting_place, parse_event
from agent.people import internal_domain
from agent.emailtext import excerpt as email_excerpt
from agent.notes import from_plain, move_note, new_meeting_note, note_view, number_lines, parts_of
from agent.notes import retry as retry_parts
from agent.lunch import order_anyway, skip_week as skip_lunch, today_view as lunch_view
from agent.uploads import MAX_BYTES as MAX_UPLOAD_BYTES, UploadError, prepare as prepare_upload
from connectors.graph import EVENT_FIELDS
from app.auth import is_allowed, require_auth, user_email

BASE_DIR = Path(__file__).parent
app = FastAPI(title="TAG Executive Assistant")

app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY", "dev_secret_key"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

oauth = OAuth()
oauth.register(
    name="azure",
    client_id=os.environ.get("AZURE_CLIENT_ID"),
    client_secret=os.environ.get("AZURE_CLIENT_SECRET"),
    server_metadata_url=f'https://login.microsoftonline.com/{os.environ.get("AZURE_TENANT_ID", "common")}/v2.0/.well-known/openid-configuration',
    client_kwargs={"scope": "openid email profile"},
)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_services_lock = threading.Lock()


def get_services(application: FastAPI):
    """Real services are built on first use; tests put fakes on app.state first."""
    if getattr(application.state, "services", None) is None:
        with _services_lock:
            if getattr(application.state, "services", None) is None:
                from app.services import build_services

                application.state.services = build_services()
    return application.state.services


def services(request: Request):
    return get_services(request.app)


@app.on_event("startup")
def start_worker() -> None:
    # The background worker (briefs, later sweeps). Off with WORKER_ENABLED=False.
    if os.environ.get("WORKER_ENABLED", "True").lower() == "true":
        get_services(app).worker.start()


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── auth routes ───────────────────────────────────────────────────────────────

@app.get("/auth/login")
async def login(request: Request):
    redirect_uri = str(request.url_for("auth_callback"))
    if not redirect_uri.startswith("http://localhost") and not redirect_uri.startswith("http://127.0.0.1"):
        redirect_uri = redirect_uri.replace("http://", "https://")
    return await oauth.azure.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.azure.authorize_access_token(request)
        user = token.get("userinfo")
        if user and is_allowed(user):
            request.session["user"] = dict(user)
        elif user:
            return HTMLResponse("This account is not allowed to use the assistant.", status_code=403)
    except OAuthError as e:
        print(f"OAuth Error: {e.error}")
    return RedirectResponse(url="/")


@app.get("/auth/logout")
async def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse(url="/")


# ── app shell ─────────────────────────────────────────────────────────────────

@app.get("/")
async def home(request: Request):
    try:
        user = require_auth(request)
    except HTTPException as e:
        if e.status_code == 401:
            return RedirectResponse("/auth/login")
        raise
    # The date is in the page itself, so it shows before the calendar has loaded.
    today = datetime.now(local_zone()).strftime("%A, %b %d").replace(" 0", " ")
    response = templates.TemplateResponse(request, "app.html", {"user": user, "v": asset_version(), "today": today})
    # The page is tiny and always fresh; it points at the current app.js/app.css.
    response.headers["Cache-Control"] = "no-cache"
    return response


def asset_version() -> str:
    """A fingerprint of app.js and app.css. It goes on their URLs, so a browser
    (or Dave's iPhone) can't keep running old code after an update: a cached
    app.js once silently dropped a new kind of card."""
    import hashlib

    digest = hashlib.sha1()
    for name in ("app.js", "app.css"):
        digest.update((BASE_DIR / "static" / name).read_bytes())
    return digest.hexdigest()[:10]


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(BASE_DIR / "static" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    # Served from the root so its scope covers the whole app.
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="application/javascript")


# ── API ───────────────────────────────────────────────────────────────────────

class ChatIn(BaseModel):
    message: str = ""
    conversation_id: str | None = None
    attachments: list[str] = []  # ids from /api/uploads
    brief_event_id: str | None = None  # start a conversation about this meeting's brief
    note_id: str | None = None         # ...or about one meeting's notes


def _topic(body: "ChatIn") -> dict | None:
    if body.brief_event_id:
        return {"brief": body.brief_event_id}
    if body.note_id:
        return {"note": body.note_id}
    return None


def _agenda_events(svc, day, now) -> list[dict]:
    tz = local_zone()
    start = datetime.combine(day, time.min, tz)
    events = []
    for item in svc.graph.calendar_view(start, start + timedelta(days=1)):
        if item.get("isCancelled"):
            continue
        e = parse_event(item, tz)
        place, join_url = meeting_place(e.location, e.join_url)
        # A block of 12 hours or more (a trip, "up at the lake") reads as all day, not "started 3 days ago".
        long_block = e.all_day or e.end - e.start >= timedelta(hours=12)
        events.append({
            "id": e.id,
            "briefable": needs_brief(e, svc.graph.mailbox, internal_domain()),
            "subject": e.subject,
            "start": "All day" if long_block else e.start.strftime("%I:%M %p").lstrip("0"),
            "end": e.end.strftime("%I:%M %p").lstrip("0"),
            "location": place,
            "show_as": e.show_as,
            "join_url": join_url,
            "past": e.end <= now,
            "now": e.start <= now < e.end,
            "starts_at": e.start.isoformat(),
            "all_day": long_block,
            "people": len([a for a in e.attendee_emails if a and a != svc.graph.mailbox.lower()]),
        })
    return events


@app.get("/api/today")
def today(user: dict = Depends(require_auth), svc=Depends(services)):
    now = datetime.now(local_zone())
    todays = _agenda_events(svc, now.date(), now)
    # All-day items and multi-day blocks don't keep the evening on "today".
    day = agenda_day(now, anything_left_today=any(not e["past"] and not e["all_day"] for e in todays))

    if day == now.date():
        title, events, done_today = "Today", todays, 0
    else:
        title = "Tomorrow" if day == now.date() + timedelta(days=1) else day.strftime("%A")
        events, done_today = _agenda_events(svc, day, now), len(todays)

    try:
        requests_view = open_requests_view(svc.graph, svc.store, svc.store, getattr(svc, "memory", None))
    except Exception:  # noqa: BLE001 - the agenda must load even if this part fails
        requests_view = []
    try:
        invites = pending_invites(svc.graph, now)
    except Exception:  # noqa: BLE001
        invites = []
    statuses = svc.store.brief_statuses([e["id"] for e in events if e["id"]])
    for e in events:
        e["brief"] = statuses.get(e["id"])
        note = svc.store.note_for_event(e["id"]) if e["id"] and hasattr(svc.store, "note_for_event") else None
        e["note"] = {"id": note["id"], "status": note["status"]} if note else None
    pending = [public_action(a) for a in svc.store.list_actions(status="pending", limit=20)]
    return {
        "date": now.strftime("%A, %b %d").replace(" 0", " "),
        "greeting": "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 17 else "Good evening",
        "name": "" if user.get("name") == "Local Admin" else (user.get("name") or "").split(" ")[0],
        "agenda_title": title,
        "agenda_date": day.strftime("%a, %b %d").replace(" 0", " "),
        "done_today": done_today,
        "events": events,
        "pending": pending,
        "requests": requests_view,
        "invites": invites,
        "lunch": lunch_view(svc.store) if hasattr(svc.store, "get_state") else None,
        "junk_moved": moved_today(svc.store) if hasattr(svc.store, "actions_since") else [],
        "filed": filed_today(svc.store) if hasattr(svc.store, "actions_since") else [],
    }


@app.post("/api/chat")
def chat(body: ChatIn, user: dict = Depends(require_auth), svc=Depends(services)):
    try:
        topic = _topic(body)
        return svc.assistant.chat(body.message, body.conversation_id, topic=topic, attachments=body.attachments)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/chat/stream")
def chat_stream(body: ChatIn, user: dict = Depends(require_auth), svc=Depends(services)):
    """Same as /api/chat, streamed as one JSON object per line: start, step, text,
    discard_text, then done (or error)."""
    if not body.message.strip() and not body.attachments:
        raise HTTPException(status_code=400, detail="Empty message")
    topic = _topic(body)

    def lines():
        try:
            for event in svc.assistant.chat_stream(body.message, body.conversation_id, topic=topic,
                                                   attachments=body.attachments):
                yield json.dumps(event, default=str) + "\n"
        except Exception as e:  # noqa: BLE001 - the app shows this and offers a retry
            yield json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"[:300]}) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/uploads")
async def upload(file: UploadFile = File(...), user: dict = Depends(require_auth), svc=Depends(services)):
    """A file attached in chat: read now (so a problem shows before sending), used when sent."""
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    try:
        prepared = prepare_upload(data, file.filename or "file", file.content_type or "")
    except UploadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    name = file.filename or "file"
    uid = svc.store.add_upload(name, prepared["kind"], prepared["summary"], prepared["thumb"], prepared["blocks"])
    return {"id": uid, "name": name, "kind": prepared["kind"], "summary": prepared["summary"], "thumb": prepared["thumb"]}


@app.post("/api/lunch/skip")
def lunch_skip(user: dict = Depends(require_auth), svc=Depends(services)):
    """No lunch order this Wednesday."""
    skip_lunch(svc.store)
    return {"lunch": lunch_view(svc.store)}


@app.post("/api/lunch/order-anyway")
def lunch_order_anyway(user: dict = Depends(require_auth), svc=Depends(services)):
    """Lunch is on although the meeting isn't on the calendar (or it was skipped)."""
    order_anyway(svc.store)
    if getattr(svc.worker, "lunch", None) is not None:
        svc.worker.lunch.tick()
    return {"lunch": lunch_view(svc.store)}


# ── meeting notes ──

RECORDINGS = os.environ.get("RECORDINGS_DIR", "data/recordings")
AUDIO_TYPES = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/m4a": ".m4a",
               "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/aac": ".aac",
               "video/mp4": ".mp4", "video/webm": ".webm"}
MAX_RECORDING_BYTES = 300 * 1024 * 1024       # Azure fast transcription's own limit


def _meeting(svc, event_id: str | None) -> dict | None:
    """The calendar meeting a recording or transcript belongs to (its title and who was there)."""
    if not event_id:
        return None
    try:
        raw = svc.graph.get(f"/users/{svc.graph.mailbox}/events/{event_id}",
                            {"$select": "id,subject,start,end,location,attendees,organizer,isAllDay,showAs,onlineMeeting,bodyPreview"},
                            headers={"Prefer": f'outlook.timezone="{local_zone().key}"'})
        return event_detail(parse_event(raw, local_zone())) | {"starts_at": raw["start"]["dateTime"][:16]}
    except Exception:  # noqa: BLE001 - a note without its meeting is still a note
        return None


def _note_out(note: dict, full: bool = False) -> dict:
    out = note_view(note, full=full) | {"error": note.get("error"), "source": note.get("source"),
                                         "event_id": note.get("event_id"),
                                         "parts": [{"n": p["n"], "source": p["source"], "status": p["status"], "error": p.get("error")}
                                                   for p in parts_of(note)] if note["kind"] == "meeting" else []}
    if full and note["kind"] == "meeting" and (note.get("digest") or {}).get("headline"):
        out["written"] = True     # it has notes to show, even while a new part is on its way
    if full and note.get("transcript"):
        out["transcript"] = note["transcript"]
    return out


@app.get("/api/notes")
def list_notes(q: str = "", user: dict = Depends(require_auth), svc=Depends(services)):
    return {"notes": [_note_out(n) for n in svc.store.list_notes(query=q, limit=60)]}


@app.get("/api/notes/{nid}")
def get_note(nid: str, user: dict = Depends(require_auth), svc=Depends(services)):
    note = svc.store.get_note(nid)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return _note_out(note, full=True)


class NoteIn(BaseModel):
    text: str
    title: str = ""
    event_id: str | None = None


@app.post("/api/notes")
def add_notes(body: NoteIn, user: dict = Depends(require_auth), svc=Depends(services)):
    """A transcript or notes pasted in: numbered, then written up."""
    transcript = number_lines(from_plain(body.text))
    if not transcript:
        raise HTTPException(status_code=400, detail="There's nothing to make notes from.")
    meeting = _meeting(svc, body.event_id)
    nid = new_meeting_note(svc.store, title=body.title or (meeting or {}).get("subject") or "Meeting notes",
                           source="pasted", transcript=transcript, event=meeting,
                           started_at=(meeting or {}).get("starts_at"))
    svc.worker.process_notes_now()
    return _note_out(svc.store.get_note(nid))


@app.post("/api/recordings")
async def add_recording(file: UploadFile = File(...), event_id: str = Form(""), title: str = Form(""),
                        user: dict = Depends(require_auth), svc=Depends(services)):
    """A meeting recorded in the app (or a recording uploaded): kept, transcribed, written up."""
    kind = (file.content_type or "").split(";")[0].lower()
    ext = AUDIO_TYPES.get(kind) or os.path.splitext(file.filename or "")[1].lower()
    if ext not in set(AUDIO_TYPES.values()):
        raise HTTPException(status_code=400, detail="That isn't an audio recording I can use (try m4a, mp3, wav or webm).")
    data = await file.read(MAX_RECORDING_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="The recording is empty.")
    if len(data) > MAX_RECORDING_BYTES:
        raise HTTPException(status_code=400, detail="The recording is over 300 MB (about two hours).")
    os.makedirs(RECORDINGS, exist_ok=True)
    path = os.path.join(RECORDINGS, f"{uuid.uuid4().hex}{ext}")
    with open(path, "wb") as out:
        out.write(data)
    meeting = _meeting(svc, event_id or None)
    nid = new_meeting_note(svc.store, title=title or (meeting or {}).get("subject") or "Recording", source="recording",
                           audio_path=path, event=meeting, started_at=(meeting or {}).get("starts_at"))
    svc.worker.process_notes_now()
    return _note_out(svc.store.get_note(nid))


@app.post("/api/notes/{nid}/retry")
def retry_note(nid: str, user: dict = Depends(require_auth), svc=Depends(services)):
    note = svc.store.get_note(nid)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    retry_parts(svc.store, nid)
    svc.worker.process_notes_now()
    return _note_out(svc.store.get_note(nid))


class NoteMove(BaseModel):
    event_id: str | None = None


@app.patch("/api/notes/{nid}")
def move_notes(nid: str, body: NoteMove, user: dict = Depends(require_auth), svc=Depends(services)):
    """Notes saved against the wrong meeting: move them (joining that meeting's notes if it has some)."""
    note = svc.store.get_note(nid)
    if note is None or note["kind"] != "meeting":
        raise HTTPException(status_code=404, detail="Note not found")
    meeting = _meeting(svc, body.event_id) if body.event_id else None
    if body.event_id and meeting is None:
        raise HTTPException(status_code=400, detail="That meeting couldn't be found on the calendar.")
    kept = move_note(svc.store, nid, meeting, started_at=(meeting or {}).get("starts_at"))
    svc.worker.process_notes_now()
    return _note_out(svc.store.get_note(kept), full=True)


@app.get("/api/conversations")
def conversations(q: str = "", user: dict = Depends(require_auth), svc=Depends(services)):
    """Recent conversations, or with q, those whose title or text mention it."""
    return {"conversations": svc.store.list_conversations(limit=50 if q else 30, query=q)}


@app.get("/api/conversations/{cid}")
def conversation(cid: str, user: dict = Depends(require_auth), svc=Depends(services)):
    convo = svc.store.get_conversation(cid)
    if convo is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"id": convo["id"], "title": convo["title"], "messages": _current_cards(svc, convo["display"])}


def _current_cards(svc, messages: list[dict]) -> list[dict]:
    """A conversation keeps its cards as they were when written. An approval card shows where it
    stands now: one approved or declined since (on Today, another device) must not reopen as pending,
    with live dropdowns and a Save button."""
    out = []
    for m in messages or []:
        if not m.get("cards"):
            out.append(m)
            continue
        cards = []
        for card in m["cards"]:
            if card.get("type") == "action" and card.get("action_id"):
                current = svc.store.get_action(card["action_id"])
                if current is not None:
                    card = {**card, **public_action(current)}
            cards.append(card)
        out.append({**m, "cards": cards})
    return out


class ApproveIn(BaseModel):
    edits: dict[str, str] | None = None


@app.post("/api/actions/{aid}/approve")
def approve(aid: str, body: ApproveIn | None = None, user: dict = Depends(require_auth), svc=Depends(services)):
    try:
        return public_action(svc.actions.approve(aid, decided_by=user_email(user) or user.get("name", ""),
                                                 edits=body.edits if body else None))
    except KeyError:
        raise HTTPException(status_code=404, detail="Action not found")


@app.post("/api/actions/{aid}/reject")
def reject(aid: str, user: dict = Depends(require_auth), svc=Depends(services)):
    try:
        return public_action(svc.actions.reject(aid, decided_by=user_email(user) or user.get("name", "")))
    except KeyError:
        raise HTTPException(status_code=404, detail="Action not found")


def clean_material(brief: dict | None) -> dict | None:
    """Stored sources from before excerpts were cleaned read like new ones."""
    material = (brief or {}).get("material")
    if not material:
        return brief
    for e in material.get("emails") or []:
        e["excerpt"] = email_excerpt(e.get("excerpt") or "", e.get("subject") or "")
    if material.get("invite"):
        material["invite"]["excerpt"] = email_excerpt(material["invite"].get("excerpt") or "")
    return brief


@app.get("/api/briefs/{event_id}")
def brief(event_id: str, user: dict = Depends(require_auth), svc=Depends(services)):
    row = svc.store.get_brief(event_id)
    if row is None:
        # Not an error: most meetings simply don't have one (yet).
        return {"event_id": event_id, "status": "none", "brief": None, "error": None,
                "updated_at": None, "conversation_id": None}
    convo = svc.store.find_conversation({"brief": event_id})
    return {
        "event_id": event_id,
        "status": row["status"],
        "brief": clean_material(row["brief"]),
        "error": row["error"],
        "updated_at": row["updated_at"],
        "conversation_id": convo["id"] if convo else None,
    }


@app.post("/api/briefs/{event_id}/prepare")
def prepare(event_id: str, refresh: bool = False, user: dict = Depends(require_auth), svc=Depends(services)):
    raw = svc.graph.get(f"/users/{svc.graph.mailbox}/events/{event_id}", {"$select": EVENT_FIELDS})
    svc.worker.prepare_now(parse_event(raw, local_zone()), force=refresh)
    return {"event_id": event_id, "status": "preparing"}


class SlotsIn(BaseModel):
    starts: list[str]


class SlotIn(BaseModel):
    start: str


@app.post("/api/requests/{message_id}/reply")
def request_reply(message_id: str, body: SlotsIn, user: dict = Depends(require_auth), svc=Depends(services)):
    """Draft a reply proposing these times; it waits on a sign-off slip."""
    try:
        return propose_reply(svc.graph, svc.llm, svc.store, svc.actions, message_id, body.starts,
                             booking_link=booking_link(getattr(svc, "hubspot", None)))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/requests/{message_id}/book")
def request_book(message_id: str, body: SlotIn, user: dict = Depends(require_auth), svc=Depends(services)):
    try:
        return propose_booking(svc.graph, svc.store, svc.actions, message_id, body.start, getattr(svc, "memory", None))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/requests/{message_id}/hold")
def request_hold(message_id: str, user: dict = Depends(require_auth), svc=Depends(services)):
    """A colleague is answering: hold the time they asked for on Dave's calendar (a sign-off slip)."""
    try:
        return propose_hold(svc.graph, svc.store, svc.actions, message_id, getattr(svc, "memory", None))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/requests/{message_id}/nudge")
def request_nudge(message_id: str, user: dict = Depends(require_auth), svc=Depends(services)):
    """Draft a short email asking the colleague handling it to reply (a sign-off slip)."""
    try:
        return propose_nudge(svc.graph, svc.store, svc.actions, message_id, getattr(svc, "memory", None))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class InviteIn(BaseModel):
    response: str


@app.post("/api/invites/{event_id:path}/respond")
def invite_respond(event_id: str, body: InviteIn, user: dict = Depends(require_auth), svc=Depends(services)):
    """Dave tapped Accept, Maybe or Decline on Today: that tap is his decision, so it goes straight out."""
    try:
        return answer_invite(svc.graph, svc.actions, event_id, body.response, decided_by=f"dave: {user.get('email', '')}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/requests/{message_id}/dismiss")
def request_dismiss(message_id: str, user: dict = Depends(require_auth), svc=Depends(services)):
    if svc.store.get_request(message_id) is None:
        raise HTTPException(status_code=404, detail="Not found")
    svc.store.update_request(message_id, status="dismissed")
    return {"dismissed": message_id}


@app.post("/api/junk/{action_id}/undo/{message_id:path}")
def junk_undo(action_id: str, message_id: str, user: dict = Depends(require_auth), svc=Depends(services)):
    """Put one email back where it was, and keep that sender from now on."""
    try:
        return undo_junk(svc.graph, svc.store, action_id, message_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/filing/{action_id}/undo/{message_id:path}")
def filing_undo(action_id: str, message_id: str, user: dict = Depends(require_auth), svc=Depends(services)):
    """Back to the Inbox; that sender won't be filed automatically again."""
    try:
        return undo_filing(svc.graph, svc.store, action_id, message_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/memory")
def memory_list(user: dict = Depends(require_auth), svc=Depends(services)):
    return {"items": svc.memory.items()}


@app.delete("/api/memory/{key:path}")
def memory_delete(key: str, user: dict = Depends(require_auth), svc=Depends(services)):
    if not svc.memory.delete(key):
        raise HTTPException(status_code=404, detail="Not found")
    return {"deleted": key}


@app.get("/api/activity")
def activity(user: dict = Depends(require_auth), svc=Depends(services)):
    return {"actions": [
        {**public_action(a), "created_at": a["created_at"], "decided_by": a.get("decided_by"),
         "executed_at": a.get("executed_at")}
        for a in svc.store.list_actions(limit=100)
    ]}
