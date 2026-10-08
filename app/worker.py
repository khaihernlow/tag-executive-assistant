"""The assistant's background worker: work that happens without Dave asking.

Runs inside the web process for now (one server, one SQLite file); the
jobs only touch the store and Graph, so it can move to its own container
later without changes.

Jobs:
  briefs   every BRIEF_INTERVAL: prepare briefs for the rest of today and the
           next working day's external meetings and interviews, and refresh
           any whose invite changed.
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

log = logging.getLogger("assistant.worker")

BRIEF_INTERVAL = int(os.environ.get("BRIEF_INTERVAL_SECONDS", str(15 * 60)))


class Worker:
    def __init__(self, graph: Any, llm: Any, store: Any, interval: int = BRIEF_INTERVAL) -> None:
        self.graph, self.llm, self.store = graph, llm, store
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Briefs take ~15-30s each; two at a time keeps HatzAI and Graph comfortable.
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="brief")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.store.reset_stuck_briefs()
        self._thread = threading.Thread(target=self._loop, name="assistant-worker", daemon=True)
        self._thread.start()
        log.info("worker started (briefs every %ss)", self.interval)

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.queue_briefs()
            except Exception:  # noqa: BLE001 - a bad cycle must not kill the worker
                log.exception("brief cycle failed")
            self._stop.wait(self.interval)

    def queue_briefs(self) -> int:
        """Queue every upcoming meeting that should have a brief; returns how many qualified."""
        domain = internal_domain()
        due = [e for e in upcoming_meetings(self.graph) if needs_brief(e, self.graph.mailbox, domain)]
        for event in due:
            self._pool.submit(self._prepare, event, False)
        return len(due)

    def prepare_now(self, event: Event, force: bool = False) -> None:
        """Dave tapped 'Prepare brief' (or 'Refresh'): do it in the background."""
        self._pool.submit(self._prepare, event, force)

    def _prepare(self, event: Event, force: bool) -> None:
        try:
            if prepare_brief(self.graph, self.llm, self.store, event, force=force):
                log.info("brief done: %s", event.subject)
        except Exception:  # noqa: BLE001
            log.exception("brief failed: %s", event.subject)
