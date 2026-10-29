"""Running the apps and workflows people at TAG build in HatzAI, from chat.

"Pull the RFP from that email and run it through the RFP responder": the model finds the
workflow, code checks the inputs against what it asks for and hands Hatz the attachment,
and a card in the chat follows the run (they take minutes) and shows what came out.
Nothing here changes anything outside Hatz, so it runs without an approval card.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from agent.tools import Tool
from llm.provider import ToolSpec

CATALOG_SECONDS = 6 * 3600
MAX_RESULT_CHARS = 15000
UPLOADS = os.environ.get("UPLOADS_DIR", "data/uploads")


def upload_path(upload_id: str) -> Path:
    """Where a file attached in chat is kept as it was, for tools that need the file itself."""
    return Path(UPLOADS) / re.sub(r"[^A-Za-z0-9_-]", "", upload_id)


def step_text(output: Any) -> str:
    """A step's words. A prompt step's output comes as the model API's whole reply, as a JSON string
    ({"choices": [{"message": {"content": ...}}]}); the words are inside it."""
    if not isinstance(output, str):
        return ""
    try:
        reply = json.loads(output)
    except ValueError:
        return output.strip()
    if isinstance(reply, dict):
        choices = reply.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            return str((choices[0].get("message") or {}).get("content") or "").strip()
        for key in ("content", "text", "output"):
            if isinstance(reply.get(key), str):
                return reply[key].strip()
    return output.strip() if isinstance(reply, (dict, list)) else str(reply).strip()


def _input(i: dict[str, Any]) -> dict[str, Any]:
    out = {"name": i["variable_name"], "label": i.get("display_name") or i["variable_name"],
           "type": i.get("variable_type"), "required": bool(i.get("required"))}
    options = [v.get("value") or v.get("label") for v in i.get("variants") or [] if isinstance(v, dict)]
    if options:
        out["options"] = [o for o in options if o]
    return out


def _slim(app: dict[str, Any]) -> dict[str, Any]:
    """What's kept of an app: enough to find it, check its inputs and name its steps."""
    return {"id": app["id"], "name": app.get("name") or "", "description": app.get("description") or "",
            "user_inputs": [{k: i.get(k) for k in ("variable_name", "display_name", "variable_type", "required", "position", "variants")}
                            for i in app.get("user_inputs") or []],
            "files": [{"display_name": f.get("display_name") or f.get("file_key")} for f in app.get("files") or []],
            "steps": [{k: s.get(k) for k in ("id", "display_name", "position", "step_type")} for s in app.get("steps") or []]}


class HatzApps:
    """Hatz's list of apps takes minutes to read in full (it has to step around broken entries), so it's
    read in the background and kept in the database; until then a search asks Hatz by name."""

    def __init__(self, client: Any, graph: Any = None, store: Any = None) -> None:
        self.client, self.graph, self.store = client, graph, store
        self._apps: list[dict[str, Any]] = []
        self._loaded = 0.0
        self._loading = threading.Lock()
        self._seen: dict[str, dict[str, Any]] = {}   # found by name before the full list was in
        saved = store.get_state("hatz_apps") if store is not None and hasattr(store, "get_state") else None
        if saved:
            try:
                kept = json.loads(saved)
                self._apps, self._loaded = kept["apps"], kept["at"]
            except (ValueError, KeyError):
                pass

    # catalog ────────────────────────────────────────────────────────────────
    def refresh(self) -> None:
        """Read the whole list again (minutes); run off the request path."""
        if not self._loading.acquire(blocking=False):
            return   # already being read
        try:
            apps = [_slim(a) for a in self.client.list_apps() if a.get("definition_state", "present") == "present"]
            self._apps, self._loaded = apps, time.time()
            if self.store is not None and hasattr(self.store, "set_state"):
                self.store.set_state("hatz_apps", json.dumps({"at": self._loaded, "apps": apps}))
        finally:
            self._loading.release()

    def refresh_later(self) -> None:
        if not self._apps or time.time() - self._loaded > CATALOG_SECONDS:
            def run() -> None:
                try:
                    self.refresh()
                except Exception:  # noqa: BLE001 - searches fall back to asking Hatz by name
                    pass
            threading.Thread(target=run, daemon=True, name="hatz-apps").start()

    def apps(self) -> list[dict[str, Any]]:
        self.refresh_later()
        return self._apps

    def _by_name(self, words: list[str]) -> list[dict[str, Any]]:
        """Before the full list is in: Hatz's own name search, a word at a time (a word that matches a
        broken entry gets an error from Hatz, so it's skipped)."""
        found: dict[str, dict[str, Any]] = {}
        for w in words[:4]:
            try:
                for a in self.client.search_apps(w):
                    found.setdefault(a["id"], _slim(a))
                    self._seen.setdefault(a["id"], found[a["id"]])
            except Exception:  # noqa: BLE001
                continue
        return list(found.values())

    def describe(self, app: dict[str, Any]) -> dict[str, Any]:
        steps = app.get("steps") or []
        return {"app_id": app["id"], "name": app["name"].strip(), "kind": "workflow" if steps else "app",
                "description": app.get("description") or "",
                "inputs": [_input(i) for i in sorted(app.get("user_inputs") or [], key=lambda i: i.get("position") or 0)],
                # Files built into it (a company's canned answers) say whose it is: TAG's own vs a template
                "built_in_files": [f.get("display_name") or f.get("file_key") for f in app.get("files") or []],
                "steps": [s.get("display_name") for s in sorted(steps, key=lambda s: (s.get("position") is None, s.get("position") or 0))]}

    def find(self, query: str) -> list[dict[str, Any]]:
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
        scored = []
        for app in self.apps() or self._by_name(words):
            name = app["name"].lower()
            hits = sum(1 for w in words if w in name) * 3 + sum(1 for w in words if w in (app.get("description") or "").lower())
            if hits:
                scored.append((-hits, -len(app.get("files") or []), len(name), app))
        return [self.describe(a) for *_, a in sorted(scored, key=lambda s: s[:3])[:8]]

    def app(self, app_id: str) -> dict[str, Any]:
        match = next((a for a in self.apps() if a["id"] == app_id), None) or self._seen.get(app_id)
        if match is None and app_id:
            try:
                match = _slim(self.client.get_app(app_id))   # Hatz answers this for apps, not workflows
            except Exception:  # noqa: BLE001
                match = None
        if match is None:
            raise ValueError("No Hatz app with that app_id. Look it up with find_hatz_apps.")
        return match

    # running ────────────────────────────────────────────────────────────────
    def _file(self, value: Any, label: str) -> tuple[bytes, str]:
        """An input file: an email's attachment, or a file Dave attached in this chat."""
        if isinstance(value, dict) and value.get("email_message_id"):
            from agent.mail import attachment_bytes
            return attachment_bytes(self.graph, value["email_message_id"], value.get("attachment") or "")
        name = value.get("chat_file") if isinstance(value, dict) else value
        found = self.store.find_upload(str(name or "")) if self.store is not None and name else None
        if found and upload_path(found["id"]).exists():
            return upload_path(found["id"]).read_bytes(), found["name"]
        raise ValueError(f"For {label}, give {{'email_message_id': ..., 'attachment': <file name>}} or "
                         f"{{'chat_file': <name of a file Dave attached here>}}.")

    def start(self, app_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
        app = self.app(app_id)
        info = self.describe(app)
        wanted = {i["name"]: i for i in info["inputs"]}
        unknown = sorted(set(inputs) - set(wanted))
        if unknown:
            raise ValueError(f"{info['name']} has no input {', '.join(unknown)}. Its inputs: {', '.join(wanted)}")
        missing = [i["label"] for i in info["inputs"] if i["required"] and inputs.get(i["name"]) in (None, "", {})]
        if missing:
            raise ValueError(f"{info['name']} needs: {', '.join(missing)}")
        values: dict[str, Any] = {}
        for name, value in inputs.items():
            if wanted[name]["type"] == "file_upload":
                data, file_name = self._file(value, wanted[name]["label"])
                values[name] = self.client.upload(data, file_name, "workflow" if info["kind"] == "workflow" else "app", app_id)
            elif "options" in wanted[name] and str(value) not in wanted[name]["options"]:
                raise ValueError(f"{wanted[name]['label']} must be one of: {', '.join(wanted[name]['options'])}")
            else:
                values[name] = value
        if info["kind"] == "app":
            return {"app": info["name"], "kind": "app", "output": self.client.query_app(app_id, values)[:MAX_RESULT_CHARS]}
        job_id = self.client.run_workflow(app_id, values)
        return {"app": info["name"], "kind": "workflow", "job_id": job_id, "status": "running", "steps": len(info["steps"])}

    def job(self, job_id: str) -> dict[str, Any]:
        """Where a workflow run stands: steps done, the last step's text, any documents it made."""
        raw = self.client.job(job_id)
        try:
            app = self.app(raw.get("app_id") or "")
            names = {s["id"]: s.get("display_name") for s in app.get("steps") or []}
            order = {s["id"]: (s.get("position") is None, s.get("position") or 0) for s in app.get("steps") or []}
            title = app["name"].strip()
        except ValueError:
            names, order, title = {}, {}, "Hatz workflow"
        outputs = sorted(raw.get("step_outputs") or [], key=lambda s: s.get("end_time") or s.get("start_time") or 0)
        done = [s for s in outputs if s.get("status") == "complete"]
        texts = [(s, step_text(s.get("output_data"))) for s in done if s.get("step_type") != "document_processing"]
        texts = [(s, t) for s, t in texts if t]
        documents = [{"step_id": s["step_id"], "name": names.get(s["step_id"]) or "Document"} for s in done
                     if s.get("step_type") == "document_processing"]
        status = raw.get("status") or "running"
        return {"job_id": job_id, "app": title, "status": status, "steps": raw.get("number_of_steps") or len(names),
                "done": len(done), "result": texts[-1][1] if texts and status == "complete" else "",
                # In the workflow's own order (its branches finish in any order): an RFP's go/no-go before its draft
                "all_steps": [{"name": names.get(s["step_id"]) or s.get("step_type"), "text": t}
                              for s, t in sorted(texts, key=lambda st: order.get(st[0]["step_id"], (True, 0)))]
                             if status == "complete" else [],
                "documents": documents if status == "complete" else [],
                "error": next((s.get("output_data") for s in outputs if s.get("status") == "failed"), "") if status == "failed" else ""}

    def document_url(self, job_id: str, step_id: str) -> str:
        return self.client.output_url(job_id, step_id)


def hatz_tools(hatz: HatzApps) -> list[Tool]:
    def run(args: dict[str, Any]) -> dict[str, Any]:
        out = hatz.start(args["app_id"], args.get("inputs") or {})
        if out["kind"] == "workflow":
            out["note"] = ("Started. It runs for a few minutes in Hatz; a card in this chat shows its progress and "
                           "the result when it's done. Tell Dave it's running; use hatz_job_result if he asks about it.")
        return out

    def result(args: dict[str, Any]) -> dict[str, Any]:
        out = hatz.job(args["job_id"])
        out["result"] = out["result"][:MAX_RESULT_CHARS]
        out.pop("all_steps", None)
        return out

    return [
        Tool(spec=ToolSpec(
            name="find_hatz_apps",
            description=("Find apps and workflows people built in HatzAI (an RFP responder, an HR specialist, research "
                         "workflows...) by words in their name. Shows each one's inputs. One with built_in_files "
                         "(e.g. TAG's canned responses) is usually TAG's own version rather than a generic template."),
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}),
            handler=lambda a: {"apps": hatz.find(a["query"])}),
        Tool(spec=ToolSpec(
            name="run_hatz_app",
            description=("Run a HatzAI app or workflow with its inputs, keyed by input name. For a file input pass "
                         "{\"email_message_id\": <id>, \"attachment\": <file name>} for an email's attachment, or "
                         "{\"chat_file\": <name>} for a file Dave attached in this chat. An app answers straight away; "
                         "a workflow starts and shows its result in a card when done."),
            input_schema={"type": "object", "properties": {
                "app_id": {"type": "string", "description": "From find_hatz_apps"},
                "inputs": {"type": "object", "description": "Input name -> value"}},
                "required": ["app_id"]}),
            handler=run),
        Tool(spec=ToolSpec(
            name="hatz_job_result",
            description="Where a HatzAI workflow run stands, and its result once complete.",
            input_schema={"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}),
            handler=result),
    ]
