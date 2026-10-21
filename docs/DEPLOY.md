# Deploying Geronimo (Dave's assistant)

For Matt. One web app plus a background worker in a single container. Python 3.12 + FastAPI, SQLite on
disk, no other services. Khai has the secrets and the current state; hand-over is section 4.

## 1. What it needs

| | |
|---|---|
| Container | `docker compose up -d --build` from this repo (Dockerfile + docker-compose.yml). Port 8000 inside, 8001 on the host. |
| Size | Small. ~300 MB RAM normally; ~1 GB briefly while the Mealeo checkout runs (Chromium). 1 vCPU is plenty. |
| Disk | Two folders mounted as volumes: `data/` (database + meeting recordings, grows with recordings) and `.secrets/` (sign-in tokens, Mealeo profile, ~80 MB). |
| HTTPS | Required: Dave uses it on his phone as an installed web app, and Microsoft sign-in needs HTTPS. Put it behind the existing reverse proxy on the n8n network with a hostname like `geronimo.tagsolutions.com`. |
| One copy only | Run exactly one container. The worker sweeps Dave's inbox, writes briefs and sends Teams messages; two copies would do everything twice. Don't scale it. |
| Outbound internet | graph.microsoft.com, login.microsoftonline.com, the HatzAI API, webservices*.autotask.net, api.hubapi.com, eastus.api.cognitive.microsoft.com, api.apify.com, www.mealeo.com. No inbound except HTTPS from the proxy. |
| Time zone | America/New_York (set in compose and `.env`). |

## 2. Microsoft sign-in (Entra): one change needed

People sign in to the app with their TAG Microsoft account (only the emails in `ALLOWED_USERS` get in).
The Entra app registration in `AZURE_CLIENT_ID` needs the production redirect URI added:

    https://<hostname>/auth/callback      (type: Web)

Without it, sign-in fails after the Microsoft login page. Dom or whoever administers Entra can add it.

## 3. Settings

Copy `.env.example` to `.env` on the server and fill it in. Every setting is described there. Khai will
send the values, or they're in IT Glue: HatzAI, Autotask API user, HubSpot token, Azure Speech key,
Apify, Mealeo login, the Entra app IDs. Generate a fresh `SECRET_KEY` (for example `openssl rand -hex 32`).

Leave these as they are unless told otherwise:
- `LUNCH_LIVE` empty: the Wednesday lunch runs as a dry run (nobody is messaged) until Dave turns it on.
- `ACTION_AUTO` empty: everything that changes the outside world waits for Dave's approval.
- `WORKER_ENABLED=True` on this one container.

## 4. Moving the current state from Khai's machine

Copy these two folders to the server next to `docker-compose.yml` (securely: they hold Dave's and
Geronimo's Microsoft tokens; treat them like passwords):

- `data/`: the database. It already holds 150+ meeting notes, what the inbox sweep has learned, open
  meeting requests and lunch settings. Starting empty would rebuild most of it, but would also re-surface
  meeting requests that were already handled. Copy it.
- `.secrets/msal_token_cache.json`: the Microsoft sign-ins for Dave's mailbox and Geronimo's account.
  It's a plain file and works on any machine. The tokens stay valid while the app keeps using them;
  if the app is off for ~90 days, or Dave's password or MFA is reset, that account signs in again:
  `docker compose exec web python scripts/connect_microsoft.py --device-code` prints a code to enter at
  microsoft.com/devicelogin. It signs in as `GRAPH_SIGNIN_AS` (or Dave, if that's empty); set it to the
  account that needs signing in for that one run.

Expect to redo once on the server:

- **The Mealeo login.** Its saved browser login comes from Chrome on Windows, which encrypts cookies so
  that only that Windows account can read them; it won't work on Linux. Mealeo also shows a reCAPTCHA
  at login, so it has to be a person once: run `python scripts/connect_mealeo.py` somewhere with a
  screen (it opens a browser to log in as assistant@tagsolutions.com), then copy the resulting
  `.secrets/mealeo-profile/` folder to the server. Doing it on a Linux machine, or in the container
  with a virtual display, avoids the Windows encryption problem. Only needed for the lunch checkout.

## 5. Check it works

1. `docker compose ps` shows healthy; `curl https://<hostname>/health` returns `{"status":"ok"}`.
2. Sign in on a phone as one of the `ALLOWED_USERS`. Today shows Dave's calendar and Needs you.
3. Ask Geronimo "what's on my calendar tomorrow?"; open a brief; open Notes (TeamsMaestro notes listed).
4. In the logs (`docker compose logs -f`), within a few minutes: brief cycles, the junk sweep and the
   TeamsMaestro check run without tracebacks.
5. On the phone, "Add to Home Screen" installs it as an app.

## 6. Running it

- Logs: `docker compose logs -f web`.
- Update: `git pull && docker compose up -d --build`. Data and sign-ins survive (volumes).
- Back up `data/assistant.db` daily (a copy while running is fine for SQLite at this size); keep a week.
  Recordings in `data/recordings/` are optional to back up (the transcripts are in the database).
- Stop the worker without taking the app down: set `WORKER_ENABLED=False` and restart.
- If Microsoft calls start failing with sign-in errors: the token expired; sign that account in again
  (section 4, `connect_microsoft.py --device-code`).

## 7. Costs (pay-as-you-go)

HatzAI (TAG's existing plan), Azure Speech (~$1/hour of recorded audio, only for meetings Dave records
in the app; TeamsMaestro meetings cost nothing), Apify (capped at $4/month in `.env`). Nothing else.
