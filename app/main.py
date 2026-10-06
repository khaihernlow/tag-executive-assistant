import os
from pathlib import Path

from authlib.integrations.starlette_client import OAuth, OAuthError
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

load_dotenv(override=True)

from app.auth import is_allowed, require_auth

BASE_DIR = Path(__file__).parent
app = FastAPI(title="TAG Executive Assistant")

app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY", "dev_secret_key"))

oauth = OAuth()
oauth.register(
    name="azure",
    client_id=os.environ.get("AZURE_CLIENT_ID"),
    client_secret=os.environ.get("AZURE_CLIENT_SECRET"),
    server_metadata_url=f'https://login.microsoftonline.com/{os.environ.get("AZURE_TENANT_ID", "common")}/v2.0/.well-known/openid-configuration',
    client_kwargs={"scope": "openid email profile"},
)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


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


# ── app routes ────────────────────────────────────────────────────────────────

@app.get("/")
async def home(request: Request):
    try:
        user = require_auth(request)
    except HTTPException as e:
        if e.status_code == 401:
            return RedirectResponse("/auth/login")
        raise
    return templates.TemplateResponse(request, "home.html", {"user": user})
