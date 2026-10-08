import json
import os
import threading
from datetime import datetime, time, timedelta
from pathlib import Path

from authlib.integrations.starlette_client import OAuth, OAuthError
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

load_dotenv(override=True)

from agent.actions import public_action
from agent.briefs import needs_brief
from agent.calendar import agenda_day, local_zone, meeting_place, parse_event
from agent.people import internal_domain
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
    return templates.TemplateResponse(request, "app.html", {"user": user})


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(BASE_DIR / "static" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    # Served from the root so its scope covers the whole app.
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="application/javascript")


# ── API ───────────────────────────────────────────────────────────────────────

class ChatIn(BaseModel):
    message: str
    conversation_id: str | None = None
    brief_event_id: str | None = None  # start a conversation about this meeting's brief


def _agenda_events(svc, day, now) -> list[dict]:
    tz = local_zone()
    start = datetime.combine(day, time.min, tz)
    events = []
    for item in svc.graph.calendar_view(start, start + timedelta(days=1)):
        if item.get("isCancelled"):
            continue
        e = parse_event(item, tz)
        place, join_url = meeting_place(e.location, e.join_url)
        events.append({
            "id": e.id,
            "briefable": needs_brief(e, svc.graph.mailbox, internal_domain()),
            "subject": e.subject,
            "start": "All day" if e.all_day else e.start.strftime("%I:%M %p").lstrip("0"),
            "end": e.end.strftime("%I:%M %p").lstrip("0"),
            "location": place,
            "show_as": e.show_as,
            "join_url": join_url,
            "past": e.end <= now,
            "now": e.start <= now < e.end,
        })
    return events


@app.get("/api/today")
def today(user: dict = Depends(require_auth), svc=Depends(services)):
    now = datetime.now(local_zone())
    todays = _agenda_events(svc, now.date(), now)
    day = agenda_day(now, anything_left_today=any(not e["past"] for e in todays))

    if day == now.date():
        title, events, done_today = "Today", todays, 0
    else:
        title = "Tomorrow" if day == now.date() + timedelta(days=1) else day.strftime("%A")
        events, done_today = _agenda_events(svc, day, now), len(todays)

    statuses = svc.store.brief_statuses([e["id"] for e in events if e["id"]])
    for e in events:
        e["brief"] = statuses.get(e["id"])
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
    }


@app.post("/api/chat")
def chat(body: ChatIn, user: dict = Depends(require_auth), svc=Depends(services)):
    try:
        topic = {"brief": body.brief_event_id} if body.brief_event_id else None
        return svc.assistant.chat(body.message, body.conversation_id, topic=topic)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/chat/stream")
def chat_stream(body: ChatIn, user: dict = Depends(require_auth), svc=Depends(services)):
    """Same as /api/chat, streamed as one JSON object per line: start, step, text,
    discard_text, then done (or error)."""
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")
    topic = {"brief": body.brief_event_id} if body.brief_event_id else None

    def lines():
        try:
            for event in svc.assistant.chat_stream(body.message, body.conversation_id, topic=topic):
                yield json.dumps(event, default=str) + "\n"
        except Exception as e:  # noqa: BLE001 - the app shows this and offers a retry
            yield json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"[:300]}) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/conversations")
def conversations(user: dict = Depends(require_auth), svc=Depends(services)):
    return {"conversations": svc.store.list_conversations()}


@app.get("/api/conversations/{cid}")
def conversation(cid: str, user: dict = Depends(require_auth), svc=Depends(services)):
    convo = svc.store.get_conversation(cid)
    if convo is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"id": convo["id"], "title": convo["title"], "messages": convo["display"]}


@app.post("/api/actions/{aid}/approve")
def approve(aid: str, user: dict = Depends(require_auth), svc=Depends(services)):
    try:
        return public_action(svc.actions.approve(aid, decided_by=user_email(user) or user.get("name", "")))
    except KeyError:
        raise HTTPException(status_code=404, detail="Action not found")


@app.post("/api/actions/{aid}/reject")
def reject(aid: str, user: dict = Depends(require_auth), svc=Depends(services)):
    try:
        return public_action(svc.actions.reject(aid, decided_by=user_email(user) or user.get("name", "")))
    except KeyError:
        raise HTTPException(status_code=404, detail="Action not found")


@app.get("/api/briefs/{event_id}")
def brief(event_id: str, user: dict = Depends(require_auth), svc=Depends(services)):
    row = svc.store.get_brief(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No brief for this meeting yet")
    convo = svc.store.find_conversation({"brief": event_id})
    return {
        "event_id": event_id,
        "status": row["status"],
        "brief": row["brief"],
        "error": row["error"],
        "updated_at": row["updated_at"],
        "conversation_id": convo["id"] if convo else None,
    }


@app.post("/api/briefs/{event_id}/prepare")
def prepare(event_id: str, refresh: bool = False, user: dict = Depends(require_auth), svc=Depends(services)):
    raw = svc.graph.get(f"/users/{svc.graph.mailbox}/events/{event_id}", {"$select": EVENT_FIELDS})
    svc.worker.prepare_now(parse_event(raw, local_zone()), force=refresh)
    return {"event_id": event_id, "status": "preparing"}


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
