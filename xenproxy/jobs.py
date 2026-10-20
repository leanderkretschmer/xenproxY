"""Migration job state: steps, progress, log, persistence."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid as _uuid

from .text import scrub

JOB_DIR = "/var/lib/xenproxy/jobs"

PENDING, RUNNING, DONE, FAILED, SKIPPED, CANCELLED = (
    "pending", "running", "done", "failed", "skipped", "cancelled")


class Step:
    def __init__(self, key: str, title: str) -> None:
        self.key = key
        self.title = title
        self.state = PENDING
        self.detail = ""
        self.started = 0.0
        self.ended = 0.0

    @property
    def duration(self) -> float:
        if not self.started:
            return 0.0
        return (self.ended or time.time()) - self.started

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "state": self.state,
                "detail": self.detail, "started": self.started, "ended": self.ended}


class Job:
    def __init__(self, plan: dict) -> None:
        self.id = time.strftime("%Y%m%d-%H%M%S") + "-" + _uuid.uuid4().hex[:6]
        self.plan = plan
        self.vm_name = plan.get("vm_name", "")
        self.vm_uuid = plan.get("vm_uuid", "")
        self.server = plan.get("server", "")
        self.vmid = plan.get("target_vmid", 0)
        self.mode = plan.get("mode", "offline")
        self.state = PENDING
        self.error = ""
        self.created = time.time()
        self.finished = 0.0
        self.steps: list[Step] = []
        self.log: list[tuple[float, str, str]] = []
        self.bytes_done = 0
        self.bytes_total = 0
        self.rate = 0.0
        self.current_disk = ""
        self.cancel_requested = False
        self._lock = threading.Lock()

    # -- steps -------------------------------------------------------
    def add_steps(self, pairs: list[tuple[str, str]]) -> None:
        for k, t in pairs:
            self.steps.append(Step(k, t))

    def step(self, key: str) -> Step | None:
        for s in self.steps:
            if s.key == key:
                return s
        return None

    def begin(self, key: str, detail: str = "") -> None:
        s = self.step(key)
        if s:
            s.state = RUNNING
            s.started = time.time()
            s.detail = detail
        self.save()

    def finish(self, key: str, state: str = DONE, detail: str = "") -> None:
        s = self.step(key)
        if s:
            s.state = state
            s.ended = time.time()
            if detail:
                s.detail = detail
        self.save()

    # -- log ---------------------------------------------------------
    def say(self, text: str, kind: str = "info") -> None:
        # Messages carry remote error text; scrub once here rather than at
        # every place a log line is rendered or printed.
        with self._lock:
            self.log.append((time.time(), kind, scrub(text)))
            if len(self.log) > 2000:
                del self.log[:500]
        self.save(throttle=True)

    @property
    def progress(self) -> float:
        if not self.bytes_total:
            return 0.0
        return min(1.0, self.bytes_done / self.bytes_total)

    @property
    def duration(self) -> float:
        return (self.finished or time.time()) - self.created

    def cancel(self) -> None:
        self.cancel_requested = True
        self.say("cancel requested", "warning")

    # -- persistence -------------------------------------------------
    _last_save = 0.0

    def save(self, throttle: bool = False) -> None:
        now = time.time()
        if throttle and now - self._last_save < 1.0:
            return
        self._last_save = now
        try:
            os.makedirs(JOB_DIR, exist_ok=True)
            tmp = os.path.join(JOB_DIR, f".{self.id}.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, indent=1)
            os.replace(tmp, os.path.join(JOB_DIR, f"{self.id}.json"))
        except OSError:
            pass

    def to_dict(self) -> dict:
        return {
            "id": self.id, "vm_name": self.vm_name, "vm_uuid": self.vm_uuid,
            "server": self.server, "vmid": self.vmid, "mode": self.mode,
            "state": self.state, "error": self.error, "created": self.created,
            "finished": self.finished, "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total, "current_disk": self.current_disk,
            "steps": [s.to_dict() for s in self.steps],
            "log": [[t, k, m] for t, k, m in self.log[-400:]],
            "plan": {k: v for k, v in self.plan.items() if k != "password"},
        }


def load_jobs(limit: int = 50) -> list[dict]:
    out = []
    try:
        names = sorted(os.listdir(JOB_DIR), reverse=True)
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(JOB_DIR, fn), encoding="utf-8") as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            continue
        if len(out) >= limit:
            break
    return out
