import os
import threading
from datetime import datetime, time, timedelta
from pathlib import Path

from authlib.integrations.starlette_client import OAuth, OAuthError
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

load_dotenv(override=True)

from agent.actions import public_action
from agent.calendar import local_zone, parse_event
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


def services(request: Request):
    """Real services are built on first use; tests put fakes on app.state first."""
    if getattr(request.app.state, "services", None) is None:
        with _services_lock:
            if getattr(request.app.state, "services", None) is None:
                from app.services import build_services

                request.app.state.services = build_services()
    return request.app.state.services


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


@app.get("/api/today")
def today(user: dict = Depends(require_auth), svc=Depends(services)):
    tz = local_zone()
    now = datetime.now(tz)
    start = datetime.combine(now.date(), time.min, tz)
    raw = svc.graph.calendar_view(start, start + timedelta(days=1))
    events = []
    for item in raw:
        if item.get("isCancelled"):
            continue
        e = parse_event(item, tz)
        events.append({
            "subject": e.subject,
            "start": "All day" if e.all_day else e.start.strftime("%I:%M %p").lstrip("0"),
            "end": e.end.strftime("%I:%M %p").lstrip("0"),
            "location": e.location,
            "show_as": e.show_as,
            "join_url": e.join_url,
            "past": e.end <= now,
            "now": e.start <= now < e.end,
        })
    pending = [public_action(a) for a in svc.store.list_actions(status="pending", limit=20)]
    return {
        "date": now.strftime("%A, %b %d").replace(" 0", " "),
        "greeting": "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 17 else "Good evening",
        "name": "" if user.get("name") == "Local Admin" else (user.get("name") or "").split(" ")[0],
        "events": events,
        "pending": pending,
    }


@app.post("/api/chat")
def chat(body: ChatIn, user: dict = Depends(require_auth), svc=Depends(services)):
    try:
        return svc.assistant.chat(body.message, body.conversation_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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


@app.get("/api/activity")
def activity(user: dict = Depends(require_auth), svc=Depends(services)):
    return {"actions": [
        {**public_action(a), "created_at": a["created_at"], "decided_by": a.get("decided_by"),
         "executed_at": a.get("executed_at")}
        for a in svc.store.list_actions(limit=100)
    ]}
