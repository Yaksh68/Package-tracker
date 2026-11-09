"""The scan queue (phase 4): every scan waits here for a worker.

Before it, each scan was its own asyncio task, so a burst (a restart's
catch-ups, one workspace adding ten repos) ran all at once, each a clone plus
hundreds of lookups.

- SCAN_WORKERS scans run at once (default 2).
- In the public mode a workspace runs one scan at a time, and workspaces take
  turns, so one busy workspace can't make everyone else wait. The local mode
  has a single workspace, which may use every worker.
- A branch that's already queued or running isn't queued again: the request
  is merged into the one already there.
- The queue lives in memory. A restart drops it: overdue scheduled scans come
  back through the scheduler's catch-ups, manual ones must be started again.
- Free scans without an account (trial.py) have a queue of their own with one
  worker, so they never hold up signed-in users' scans. A trial's job carries
  the visitor's token, in memory only, to that one scan.

Everything here runs on the event loop's thread. Route handlers that run in
the thread pool only read `_positions`, a plain dict replaced (never mutated)
after every change, so they never see a queue mid-update.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import pipeline, storage
from .config import get_settings
from .models import LOCAL_WORKSPACE_ID, Repo

log = logging.getLogger("packages-tracker.queue")


@dataclass(frozen=True)
class Job:
    repo_id: str
    branch: str
    workspace_id: str
    trigger: str
    queued_at: datetime
    # a trial's visitor token: only in memory, only for this job's scan
    token: str | None = field(default=None, repr=False, compare=False)

    @property
    def key(self) -> tuple[str, str]:
        return self.repo_id, self.branch


class ScanQueue:
    def __init__(self, workers: int, per_workspace: int) -> None:
        self.workers = max(1, workers)
        self.per_workspace = max(1, per_workspace)
        self.loop = asyncio.get_running_loop()
        self._pending: dict[str, deque[Job]] = {}   # per workspace, oldest first
        self._turns: deque[str] = deque()           # workspaces with pending jobs, next turn first
        self._running: dict[tuple[str, str], Job] = {}
        self._positions: dict[tuple[str, str], int] = {}
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ---- reading (safe from any thread) ------------------------------------
    def position(self, repo_id: str, branch: str) -> int | None:
        """1 = next to start. None when it isn't waiting."""
        return self._positions.get((repo_id, branch))

    def queued(self, repo_id: str, branch: str) -> bool:
        return (repo_id, branch) in self._positions

    def waiting(self) -> int:
        return len(self._positions)

    # ---- changing (event loop only) -----------------------------------------
    def enqueue(self, repo: Repo, branch: str, trigger: str, token: str | None = None) -> bool:
        """Queue a scan. False when that branch is already queued or running:
        the request is merged into that scan."""
        key = (repo.id, branch)
        if key in self._positions or key in self._running or pipeline.is_scanning(*key):
            return False
        ws = repo.workspace_id or LOCAL_WORKSPACE_ID
        job = Job(repo.id, branch, ws, trigger, datetime.now(timezone.utc), token=token)
        self._pending.setdefault(ws, deque()).append(job)
        if ws not in self._turns:
            self._turns.append(ws)
        pipeline.mark_queued(repo.id, branch)
        self._changed()
        self._start()
        return True

    def cancel_repo(self, repo_id: str) -> None:
        """Drop a deleted repo's waiting scans. A running one finishes (deleting
        refuses while a branch is scanning)."""
        for ws in list(self._pending):
            kept = deque(j for j in self._pending[ws] if j.repo_id != repo_id)
            if kept:
                self._pending[ws] = kept
            else:
                del self._pending[ws]
                if ws in self._turns:
                    self._turns.remove(ws)
        self._changed()

    def _order(self) -> list[Job]:
        """Waiting jobs in the order workers would take them: a workspace's
        oldest job first, one workspace per turn."""
        queues = [self._pending[ws] for ws in self._turns if ws in self._pending]
        out: list[Job] = []
        depth = 0
        while True:
            row = [q[depth] for q in queues if depth < len(q)]
            if not row:
                return out
            out.extend(row)
            depth += 1

    def _changed(self) -> None:
        self._positions = {job.key: i for i, job in enumerate(self._order(), start=1)}
        self._wake.set()

    def _running_in(self, ws: str) -> int:
        return sum(1 for j in self._running.values() if j.workspace_id == ws)

    def _take(self) -> Job | None:
        for _ in range(len(self._turns)):
            ws = self._turns[0]
            self._turns.rotate(-1)  # its next turn comes after everyone else's
            q = self._pending.get(ws)
            if not q or self._running_in(ws) >= self.per_workspace:
                continue
            job = q.popleft()
            if not q:
                del self._pending[ws]
                self._turns.remove(ws)
            self._running[job.key] = job
            self._changed()
            return job
        return None

    def _start(self) -> None:
        self._tasks = [t for t in self._tasks if not t.done()]
        while len(self._tasks) < self.workers:
            self._tasks.append(self.loop.create_task(self._worker()))

    async def _worker(self) -> None:
        while True:
            job = self._take()
            if job is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            try:
                # the repo as it is now: it may have been edited or deleted while waiting
                repo = storage.get_repo(job.repo_id)
                if repo is None or job.branch not in repo.branches:
                    pipeline.mark_idle(job.repo_id, job.branch)
                else:
                    await pipeline.run_scan(repo, job.branch, trigger=job.trigger, queued_at=job.queued_at,
                                            trial_token=job.token)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad scan mustn't stop the worker
                log.exception("scan of %s @ %s failed outside the pipeline", job.repo_id, job.branch)
            finally:
                self._running.pop(job.key, None)
                self._wake.set()  # a workspace's turn may have opened up

    def shutdown(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks = []


_queue: ScanQueue | None = None
_trial: ScanQueue | None = None  # free scans without an account: one worker of their own


def get() -> ScanQueue:
    """This event loop's queue (tests run several loops; production runs one)."""
    global _queue
    loop = asyncio.get_running_loop()
    if _queue is None or _queue.loop is not loop:
        s = get_settings()
        _queue = ScanQueue(s.scan_workers, 1 if s.public_mode() else s.scan_workers)
    return _queue


def trial() -> ScanQueue:
    """The free scans' queue, with a single worker."""
    global _trial
    loop = asyncio.get_running_loop()
    if _trial is None or _trial.loop is not loop:
        _trial = ScanQueue(1, 1)
    return _trial


def _queues() -> list[ScanQueue]:
    return [q for q in (_queue, _trial) if q is not None]


def enqueue(repo: Repo, branch: str, trigger: str = "manual") -> bool:
    return get().enqueue(repo, branch, trigger)


def enqueue_trial(repo: Repo, branch: str, token: str | None) -> bool:
    """Queue a free scan, with the visitor's token for that scan only."""
    return trial().enqueue(repo, branch, "trial", token=token)


def trial_waiting() -> int:
    q = _trial
    return q.waiting() if q else 0


def position(repo_id: str, branch: str) -> int | None:
    for q in _queues():
        if (pos := q.position(repo_id, branch)) is not None:
            return pos
    return None


def stats() -> tuple[int, int]:
    """(waiting, running) right now in both queues, for the admin analytics.
    Safe from any thread: it only reads the sizes."""
    qs = _queues()
    return sum(q.waiting() for q in qs), sum(len(q._running) for q in qs)


def busy(repo_id: str, branch: str) -> bool:
    """Queued or running."""
    return any(q.queued(repo_id, branch) for q in _queues()) or pipeline.is_scanning(repo_id, branch)


def cancel_repo(repo_id: str) -> None:
    """Callable from any thread (repo deletion runs in the thread pool). A
    worker that reaches a deleted repo's job skips it anyway."""
    try:
        here = asyncio.get_running_loop()
    except RuntimeError:
        here = None
    for q in _queues():
        if here is q.loop:
            q.cancel_repo(repo_id)
        elif not q.loop.is_closed():
            q.loop.call_soon_threadsafe(q.cancel_repo, repo_id)


def shutdown() -> None:
    global _queue, _trial
    for q in _queues():
        q.shutdown()
    _queue = _trial = None
