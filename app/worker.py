"""The assistant's background worker: work that happens without Dave asking.

Runs inside the web process for now (one server, one SQLite file); the
jobs only touch the store and Graph, so it can move to its own container
later without changes.

Jobs (each cycle):
  filing   Inbox mail Dave has read (1h+ old) from a sender with a learned folder
           is filed there (Undo on Today). Daily: relearn sender -> folder from
           his folders and suggest Outlook rules for unread automated senders.
  junk     inbox mail since the last check: known junk and clear junk moved to
           Junk at once (Undo on Today), less certain junk added to one rolling
           slip. Learns from Dave's Junk folder and filed mail once a day.
  requests one inbox read; new mail that looks like "can we meet?" is classified
           by the fast model and shown on Today with suggested times.
  notes    recordings transcribed (once there's an Azure Speech key), transcripts made into
           meeting notes; also straight away when a recording or transcript arrives.
  lunch    its own loop (every 30s while waiting on Teams replies, else 2 min); Wednesdays
           7:00-14:00: the leadership lunch (agent/lunch.py) - ask Dan and
           Dom on Teams, read replies, fill in backups, total it up for Dave.
  briefs   every BRIEF_INTERVAL (3 min): one calendar read; prepare briefs for
           new qualifying meetings (rest of today + next working day), soonest
           first, and refresh any whose invite changed. Unchanged meetings cost
           nothing, so a meeting added during the day has a brief in ~3 min.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agent.briefs import needs_brief, prepare_brief, upcoming_meetings
from agent.calendar import Event
from agent.people import internal_domain
from agent.filing import file_read_mail, learn_filing, learning_is_stale, suggest_rules
from agent.junk import history_is_stale, learn_history, sweep_new
from agent.requests import scan as scan_requests
from agent.notes import process as notes_process

log = logging.getLogger("assistant.worker")

BRIEF_INTERVAL = int(os.environ.get("BRIEF_INTERVAL_SECONDS", str(3 * 60)))
LUNCH_REPLY_SECONDS = int(os.environ.get("LUNCH_REPLY_SECONDS", "30"))


class Worker:
    def __init__(self, graph: Any, llm: Any, store: Any, interval: int = BRIEF_INTERVAL, searcher: Any = None,
                 fast_llm: Any = None, actions: Any = None, autotask: Any = None, linkedin: Any = None,
                 lunch: Any = None, speech: Any = None, hubspot: Any = None) -> None:
        self.graph, self.llm, self.store = graph, llm, store
        self.searcher = searcher  # web research for briefs; None skips it
        self.fast_llm = fast_llm  # quick classification (meeting requests); None skips the inbox scan
        self.actions = actions    # lets follow-ups book a time the other person picked
        self.autotask = autotask  # an opportunities Directory; None without Autotask credentials
        self.linkedin = linkedin  # LinkedIn backgrounds for briefs; None without an Apify token
        self.lunch = lunch        # Wednesday lunch; None without the assistant's own account
        self.speech = speech      # Azure Speech for recordings; None until there's a key
        self.hubspot = hubspot    # TAG's CRM, for briefs; None without a token
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Briefs take ~20-45s each; two at a time keeps HatzAI and Graph comfortable.
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="brief")
        # Meetings waiting in the pool, so a 3-minute cycle never queues one twice.
        self._queued: set[str] = set()
        self._queued_lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.store.reset_stuck_briefs()
        self._thread = threading.Thread(target=self._loop, name="assistant-worker", daemon=True)
        self._thread.start()
        if self.lunch is not None:
            threading.Thread(target=self._lunch_loop, name="assistant-lunch", daemon=True).start()
        log.info("worker started (briefs every %ss)", self.interval)

    def _lunch_loop(self) -> None:
        """Lunch has its own beat: replies on Teams are a conversation, so while it's waiting
        on them it looks every LUNCH_REPLY_SECONDS; otherwise every couple of minutes."""
        while not self._stop.is_set():
            status = None
            try:
                plan = self.lunch.tick()
                status = plan and plan["status"]
            except Exception:  # noqa: BLE001
                log.exception("lunch step failed")
            self._stop.wait(LUNCH_REPLY_SECONDS if status == "collecting" else 120)

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.queue_briefs()
            except Exception:  # noqa: BLE001 - a bad cycle must not kill the worker
                log.exception("brief cycle failed")
            if self.fast_llm is not None and self.actions is not None:
                try:
                    if history_is_stale(self.store):
                        log.info("learned junk history: %s", learn_history(self.graph, self.store))
                    outcome = sweep_new(self.graph, self.fast_llm, self.store, self.actions)
                    if outcome["moved"] or outcome["asked"]:
                        log.info("junk sweep: moved %s, asked about %s", outcome["moved"], outcome["asked"])
                except Exception:  # noqa: BLE001
                    log.exception("junk sweep failed")
            if self.actions is not None:
                try:
                    if learning_is_stale(self.store):
                        log.info("learned filing: %s", learn_filing(self.graph, self.store))
                        suggest_rules(self.graph, self.store, self.actions)
                    filed = file_read_mail(self.graph, self.store, self.actions, llm=self.fast_llm)
                    if filed:
                        log.info("filed %s read emails", filed)
                except Exception:  # noqa: BLE001
                    log.exception("filing failed")
            if self.fast_llm is not None:
                try:
                    added = scan_requests(self.graph, self.fast_llm, self.store, actions=self.actions)
                    if added:
                        log.info("meeting requests found: %s", added)
                except Exception:  # noqa: BLE001
                    log.exception("meeting request scan failed")
            try:
                from agent.maestro import ingest as maestro_ingest
                got = maestro_ingest(self.graph, self.store)   # TeamsMaestro's write-ups from Dave's inbox
                if got:
                    log.info("TeamsMaestro notes added: %s", got)
            except Exception:  # noqa: BLE001
                log.exception("TeamsMaestro step failed")
            self.process_notes()
            self._stop.wait(self.interval)

    def process_notes(self) -> int:
        try:
            if not hasattr(self, "vocab"):
                from agent.vocab import Vocabulary
                self.vocab = Vocabulary(self.graph, self.autotask)
            done = notes_process(self.store, self.llm, self.speech, vocabulary=self.vocab.for_note)
            if done:
                log.info("meeting notes written: %s", done)
            return done
        except Exception:  # noqa: BLE001
            log.exception("notes step failed")
            return 0

    def process_notes_now(self) -> None:
        """A recording or transcript just arrived: don't make Dave wait for the next cycle."""
        self._pool.submit(self.process_notes)

    def queue_briefs(self) -> int:
        """Queue every upcoming meeting that should have a brief; returns how many qualified."""
        domain = internal_domain()
        due = [e for e in upcoming_meetings(self.graph) if needs_brief(e, self.graph.mailbox, domain)]
        # Soonest first: the pool works in submission order, so the 2:00 brief
        # is written before tomorrow's 4:00.
        for event in sorted(due, key=lambda e: e.start):
            self._submit(event, force=False)
        return len(due)

    def prepare_now(self, event: Event, force: bool = False) -> None:
        """Dave tapped 'Prepare brief' (or 'Refresh'): do it in the background."""
        self._submit(event, force=force)

    def _submit(self, event: Event, force: bool) -> bool:
        with self._queued_lock:
            if event.id in self._queued:
                return False
            self._queued.add(event.id)
        self._pool.submit(self._prepare, event, force)
        return True

    def _prepare(self, event: Event, force: bool) -> None:
        try:
            if prepare_brief(self.graph, self.llm, self.store, event, force=force, searcher=self.searcher,
                             linkedin=self.linkedin, directory=self.autotask, hubspot=self.hubspot):
                log.info("brief done: %s", event.subject)
        except Exception:  # noqa: BLE001
            log.exception("brief failed: %s", event.subject)
        finally:
            with self._queued_lock:
                self._queued.discard(event.id)
