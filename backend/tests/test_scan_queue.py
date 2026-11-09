"""The scan queue (phase 4): a fixed number of workers, one running scan per
workspace in the public mode, workspaces taking turns, duplicates merged.
run_scan is replaced by a fake whose scans finish when the test says so."""
import asyncio

import pytest

from app import pipeline, scan_queue, storage
from app.models import Repo, ScanState


class FakeScans:
    """Stands in for pipeline.run_scan: records starts, holds each scan open
    until finish() lets it go."""

    def __init__(self):
        self.started: list[tuple[str, str]] = []
        self.running: set[tuple[str, str]] = set()
        self.peak: dict[str, int] = {}
        self._gates: dict[tuple[str, str], asyncio.Event] = {}
        self.kwargs: dict[tuple[str, str], dict] = {}

    async def run_scan(self, repo, branch, **kw):
        key = (repo.id, branch)
        self.started.append(key)
        self.running.add(key)
        self.kwargs[key] = kw
        ws = repo.workspace_id
        now = sum(1 for k in self.running if storage.get_repo(k[0]).workspace_id == ws)
        self.peak[ws] = max(self.peak.get(ws, 0), now)
        gate = self._gates.setdefault(key, asyncio.Event())
        await gate.wait()
        self.running.discard(key)

    def finish(self, repo_id, branch="main"):
        self._gates.setdefault((repo_id, branch), asyncio.Event()).set()


@pytest.fixture
def fake(monkeypatch):
    f = FakeScans()
    monkeypatch.setattr(pipeline, "run_scan", f.run_scan)
    return f


def _repo(rid, ws):
    r = Repo(id=rid, url=f"https://x/{rid}", branches=["main"], workspace_id=ws)
    storage.upsert_repo(r)
    return r


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_workspaces_take_turns_and_each_runs_one_scan_at_a_time(fake):
    q = scan_queue.ScanQueue(workers=2, per_workspace=1)
    a = [_repo(f"a{i}", "ws-a") for i in range(3)]
    b = _repo("b0", "ws-b")
    for r in [*a, b]:
        assert q.enqueue(r, "main", "manual")
    # the waiting order alternates between workspaces
    assert [q.position(r.id, "main") for r in [*a, b]] == [1, 3, 4, 2]
    await _settle()
    # two workers, but only one scan per workspace: a0 and b0 run, a1 waits
    assert fake.started == [("a0", "main"), ("b0", "main")]
    assert q.position("a1", "main") == 1 and q.position("a0", "main") is None
    fake.finish("b0")
    await _settle()
    assert fake.started == [("a0", "main"), ("b0", "main")]  # a's turn, but a0 still runs
    fake.finish("a0")
    await _settle()
    assert fake.started[-1] == ("a1", "main")
    fake.finish("a1")
    await _settle()
    fake.finish("a2")
    await _settle()
    assert [k[0] for k in fake.started] == ["a0", "b0", "a1", "a2"]
    assert fake.peak == {"ws-a": 1, "ws-b": 1}
    q.shutdown()


async def test_the_local_mode_may_use_every_worker(fake):
    q = scan_queue.ScanQueue(workers=2, per_workspace=2)
    for i in range(3):
        q.enqueue(_repo(f"l{i}", "local"), "main", "manual")
    await _settle()
    assert len(fake.started) == 2 and q.position("l2", "main") == 1
    q.shutdown()


async def test_a_second_request_for_the_same_branch_is_merged(fake):
    q = scan_queue.ScanQueue(workers=1, per_workspace=1)
    first, second = _repo("m0", "ws"), _repo("m1", "ws")
    assert q.enqueue(first, "main", "manual")
    await _settle()                                      # m0 is running now
    assert q.enqueue(second, "main", "manual")
    assert not q.enqueue(second, "main", "scheduled")    # waiting: merged
    assert not q.enqueue(first, "main", "manual")        # running: merged
    assert q.waiting() == 1
    assert pipeline.get_status("m1", "main").state == ScanState.queued
    q.shutdown()


async def test_a_deleted_repo_leaves_the_queue(fake):
    q = scan_queue.ScanQueue(workers=1, per_workspace=1)
    keep, gone = _repo("k0", "ws"), _repo("g0", "ws")
    q.enqueue(keep, "main", "manual")
    await _settle()
    q.enqueue(gone, "main", "manual")
    q.cancel_repo("g0")
    assert q.waiting() == 0
    # and a job whose repo vanished without a cancel is skipped, not scanned
    q.enqueue(_repo("v0", "ws"), "main", "manual")
    storage.delete_repo("v0")
    fake.finish("k0")
    await _settle()
    assert fake.started == [("k0", "main")]
    assert pipeline.get_status("v0", "main").state == ScanState.idle
    q.shutdown()


async def test_the_scan_learns_when_it_was_queued_and_why(fake):
    q = scan_queue.ScanQueue(workers=1, per_workspace=1)
    q.enqueue(_repo("t0", "ws"), "main", "scheduled")
    await _settle()
    kw = fake.kwargs[("t0", "main")]
    assert kw["trigger"] == "scheduled" and kw["queued_at"] is not None
    q.shutdown()
