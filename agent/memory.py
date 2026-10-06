"""What the assistant knows about Dave, across all conversations.

Three kinds, each applied by code rather than left to the model's recall:
  alias  "Kai" -> a person. find_person resolves it with full confidence.
  pref   scheduling defaults (day_start, day_end, meeting_minutes) that the
         free-time tools use whenever the model doesn't pass a value.
  note   anything else Dave tells it to remember; added to every prompt.
"""

from __future__ import annotations

import hashlib
from datetime import time
from typing import Any

from agent.tools import Tool
from llm.provider import ToolSpec
from store.db import Store

PREFS = {
    "day_start": ("Earliest meeting time, HH:MM", "08:00"),
    "day_end": ("Latest meeting end time, HH:MM", "17:00"),
    "meeting_minutes": ("Default meeting length in minutes", "30"),
}


class Memory:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ── reads ────────────────────────────────────────────────────────────────

    def alias(self, name: str) -> dict[str, str] | None:
        for row in self.store.list_memory("alias"):
            if row["key"] == _alias_key(name):
                return row["value"]
        return None

    def pref(self, name: str) -> str:
        for row in self.store.list_memory("pref"):
            if row["key"] == f"pref:{name}":
                return str(row["value"])
        return PREFS[name][1]

    def items(self) -> list[dict[str, str]]:
        """Everything remembered, worded for Dave (the Remembered panel)."""
        out = []
        for row in self.store.list_memory():
            v, name = row["value"], row["key"].split(":", 1)[1]
            if row["kind"] == "alias":
                out.append({"key": row["key"], "kind": "Nickname", "text": f'\u201c{v.get("said", name)}\u201d means {v["name"]} ({v["email"]})'})
            elif row["kind"] == "pref":
                out.append({"key": row["key"], "kind": "Preference", "text": f"{PREFS.get(name, (name,))[0]}: {v}"})
            else:
                out.append({"key": row["key"], "kind": "Note", "text": str(v)})
        return out

    def delete(self, key: str) -> bool:
        return self.store.delete_memory(key)

    def prompt_section(self) -> str:
        lines = []
        for row in self.store.list_memory():
            v = row["value"]
            if row["kind"] == "alias":
                lines.append(f'- "{v.get("said", row["key"].split(":", 1)[1])}" means {v["name"]} <{v["email"]}>')
            elif row["kind"] == "pref":
                lines.append(f"- {PREFS.get(row['key'].split(':', 1)[1], (row['key'],))[0]}: {v}")
            else:
                lines.append(f"- {v}")
        if not lines:
            return ""
        return "What Dave has told you to remember (applies everywhere):\n" + "\n".join(lines)

    # ── writes ───────────────────────────────────────────────────────────────

    def remember(self, args: dict[str, Any]) -> dict[str, Any]:
        kind = args.get("kind")
        if kind == "alias":
            name, email = (args.get("name") or "").strip(), (args.get("email") or "").strip().lower()
            if not name or "@" not in email:
                raise ValueError("An alias needs the name Dave uses and the person's email (from find_person).")
            full = (args.get("full_name") or email).strip()
            self.store.set_memory(_alias_key(name), "alias", {"name": full, "email": email, "said": name})
            return {"remembered": f'"{name}" means {full} <{email}>'}
        if kind == "pref":
            key, value = args.get("key"), str(args.get("value", "")).strip()
            if key not in PREFS:
                raise ValueError(f"Unknown preference. Choose one of: {', '.join(PREFS)}")
            _validate_pref(key, value)
            self.store.set_memory(f"pref:{key}", "pref", value)
            return {"remembered": f"{PREFS[key][0]}: {value}"}
        if kind == "note":
            text = (args.get("text") or "").strip()
            if not text:
                raise ValueError("Nothing to remember.")
            key = "note:" + hashlib.sha1(text.lower().encode()).hexdigest()[:10]
            self.store.set_memory(key, "note", text)
            return {"remembered": text}
        raise ValueError("kind must be alias, pref or note")

    def forget(self, args: dict[str, Any]) -> dict[str, Any]:
        """Forget by alias name, preference key, or words from a note."""
        what = (args.get("what") or "").strip().lower()
        removed = []
        for row in self.store.list_memory():
            label = row["key"].split(":", 1)[1]
            text = row["value"] if isinstance(row["value"], str) else ""
            if what and (what == label.lower() or (row["kind"] == "note" and what in text.lower())):
                self.store.delete_memory(row["key"])
                removed.append(text or label)
        return {"forgot": removed} if removed else {"forgot": [], "note": "Nothing matched."}


def _alias_key(name: str) -> str:
    return "alias:" + " ".join(name.lower().split())


def _validate_pref(key: str, value: str) -> None:
    if key in ("day_start", "day_end"):
        time.fromisoformat(value)
    elif not (value.isdigit() and 5 <= int(value) <= 480):
        raise ValueError("meeting_minutes must be a number of minutes between 5 and 480")


def memory_tools(memory: Memory) -> list[Tool]:
    return [
        Tool(
            spec=ToolSpec(
                name="remember",
                description=(
                    "Save something Dave wants remembered across all conversations. Use when he says "
                    "'remember', 'from now on', 'always', or corrects who someone is. kinds: "
                    "alias (name he uses -> person, e.g. 'Kai' -> Khaihern Low; get the email from find_person), "
                    "pref (key: day_start | day_end | meeting_minutes), note (anything else)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["alias", "pref", "note"]},
                        "name": {"type": "string", "description": "alias: the name Dave uses"},
                        "email": {"type": "string", "description": "alias: the person's email"},
                        "full_name": {"type": "string", "description": "alias: the person's full name"},
                        "key": {"type": "string", "enum": list(PREFS)},
                        "value": {"type": "string", "description": "pref value, e.g. 08:30 or 45"},
                        "text": {"type": "string", "description": "note text"},
                    },
                    "required": ["kind"],
                },
            ),
            handler=memory.remember,
        ),
        Tool(
            spec=ToolSpec(
                name="forget",
                description="Remove a remembered alias (by the name), preference (by key) or note (by words in it).",
                input_schema={"type": "object", "properties": {"what": {"type": "string"}}, "required": ["what"]},
            ),
            handler=memory.forget,
        ),
    ]
