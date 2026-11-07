"""Per-scan activity trace.

A `ScanLogger` accumulates timestamped entries (stages, tool calls, results).
The pipeline binds one as the "current" logger for the duration of a scan; any
layer — transport, extraction, enrichment, source APIs — can call
`scanlog.emit(...)` and it lands in that scan's trace without threading a logger
argument through every function. Uses a contextvar so it's async-task scoped.
"""
from __future__ import annotations

import contextvars
from datetime import datetime, timezone

from .models import LogEntry, ScanLog, ScanState

_MAX_ENTRIES = 5000
_current: contextvars.ContextVar["ScanLogger | None"] = contextvars.ContextVar(
    "scanlog_current", default=None
)
# most recent logger per (repo, branch), for the live "scan activity" endpoint
_by_repo: dict[tuple[str, str], "ScanLogger"] = {}


class ScanLogger:
    def __init__(self, repo_id: str, branch: str) -> None:
        self.repo_id = repo_id
        self.branch = branch
        self.snapshot_ts: str | None = None
        self.state: ScanState = ScanState.running
        self.entries: list[LogEntry] = []

    def emit(self, stage: str, message: str, *, level: str = "info", data: dict | None = None) -> None:
        if len(self.entries) >= _MAX_ENTRIES:
            return
        self.entries.append(
            LogEntry(ts=datetime.now(timezone.utc), stage=stage, level=level, message=message, data=data)
        )

    def to_model(self) -> ScanLog:
        return ScanLog(
            repo_id=self.repo_id,
            branch=self.branch,
            snapshot_ts=self.snapshot_ts,
            state=self.state,
            entries=self.entries,
        )


def start(repo_id: str, branch: str) -> ScanLogger:
    logger = ScanLogger(repo_id, branch)
    _by_repo[(repo_id, branch)] = logger
    _current.set(logger)
    return logger


def current() -> ScanLogger | None:
    return _current.get()


def get_repo_log(repo_id: str, branch: str) -> ScanLogger | None:
    return _by_repo.get((repo_id, branch))


def clear_repo(repo_id: str, branch: str | None = None) -> None:
    """Drop the in-memory trace for one branch, or every branch of `repo_id`
    when `branch` is omitted (repo deletion)."""
    if branch is not None:
        _by_repo.pop((repo_id, branch), None)
    else:
        for key in [k for k in _by_repo if k[0] == repo_id]:
            _by_repo.pop(key, None)


def emit(stage: str, message: str, *, level: str = "info", data: dict | None = None) -> None:
    logger = _current.get()
    if logger is not None:
        logger.emit(stage, message, level=level, data=data)
