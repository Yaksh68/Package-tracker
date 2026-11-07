"""A scan shares its event loop with the API.

`git clone` and the clone-tree walks are blocking calls; run on the loop they
freeze every request for their whole duration — most visibly the repo list the
UI refetches right after POST /repos, which is what makes a newly added repo
take until the first clone finishes to appear.
"""
import asyncio
import time
from datetime import date

import pytest

from app import pipeline
from app.agents.clone import Clone
from app.models import Repo

BLOCK = 0.4  # how long the fake clone blocks the calling thread


async def _loop_ticks(stop: asyncio.Event) -> int:
    """Count how many times the event loop gets to run us. Blocking work on the
    loop pins this at ~0; work in a worker thread leaves it free to tick."""
    n = 0
    while not stop.is_set():
        await asyncio.sleep(0.01)
        n += 1
    return n


@pytest.fixture
def _fake_scan(monkeypatch, tmp_path):
    """Stub the scan's outbound work: a clone that blocks like `git clone` does,
    and no network for extraction/enrichment."""
    (tmp_path / "repo").mkdir()

    def blocking_clone(url, branch, token=None):
        time.sleep(BLOCK)  # stands in for the real subprocess.run(["git", ...])
        return Clone(path=tmp_path / "repo", _tmp=str(tmp_path / "unused"))

    monkeypatch.setattr(pipeline, "shallow_clone", blocking_clone)
    monkeypatch.setattr(Clone, "cleanup", lambda self: None)


async def test_scan_keeps_the_event_loop_responsive(_fake_scan):
    repo = Repo(id="r", url="https://example.com/r.git", branches=["main"],
                release_date=date(2026, 1, 1))
    stop = asyncio.Event()
    ticker = asyncio.create_task(_loop_ticks(stop))

    t0 = time.monotonic()
    await pipeline.run_scan(repo, "main", use_extraction_agent=False)
    elapsed = time.monotonic() - t0
    stop.set()
    ticks = await ticker

    assert elapsed >= BLOCK  # the blocking work really did run
    # Freely ticking gives ~BLOCK/0.01 ticks; blocked on the loop gives ~0.
    assert ticks > (BLOCK / 0.01) * 0.5, (
        f"event loop only ticked {ticks}x during a {elapsed:.2f}s scan — "
        "blocking work is running on the loop instead of a worker thread"
    )


async def test_scan_cleans_up_the_clone_even_when_a_stage_fails(_fake_scan, monkeypatch):
    """The clone was a `with` block; moving it off the loop must not lose the
    temp-dir cleanup on the error path."""
    cleaned: list[bool] = []
    monkeypatch.setattr(Clone, "cleanup", lambda self: cleaned.append(True))

    async def boom(*a, **kw):
        raise RuntimeError("extraction exploded")

    monkeypatch.setattr(pipeline.extraction_agent, "extract", boom)
    repo = Repo(id="r", url="https://example.com/r.git", branches=["main"])

    status = await pipeline.run_scan(repo, "main", use_extraction_agent=False)
    assert cleaned == [True]
    assert status.state.value == "error"
