"""Who may use the assistant.

The assistant acts with Dave's mailbox, calendar and CRM access, so a valid
TAG SSO login is not enough on its own: the user must also be on the
ALLOWED_USERS list.
"""

import os

from fastapi import HTTPException, Request


def is_auth_required() -> bool:
    return os.environ.get("REQUIRE_AUTH", "True").lower() == "true"


def allowed_users() -> set[str]:
    raw = os.environ.get("ALLOWED_USERS", "")
    return {email.strip().lower() for email in raw.split(",") if email.strip()}


def user_email(user: dict) -> str:
    # Azure AD v2 id tokens carry the address in different claims depending
    # on account type, so check the usual ones in order.
    for claim in ("email", "preferred_username", "upn"):
        value = user.get(claim)
        if value:
            return str(value).lower()
    return ""


def is_allowed(user: dict) -> bool:
    return user_email(user) in allowed_users()


def require_auth(request: Request) -> dict:
    if not is_auth_required():
        cf_email = request.headers.get("Cf-Access-Authenticated-User-Email")
        if cf_email:
            # Even with SSO bypassed, a real person behind Cloudflare Access
            # must still be on the allowlist.
            user = {"name": cf_email.split("@")[0].replace(".", " ").title(), "email": cf_email}
            if not is_allowed(user):
                raise HTTPException(status_code=403, detail="Not on the assistant allowlist.")
            return user
        return {"name": "Local Admin", "email": "local@localhost"}

    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not is_allowed(user):
        raise HTTPException(status_code=403, detail="Not on the assistant allowlist.")
    return user
