"""What one AI conversation returns, whichever provider ran it, and the tool
events every provider logs to the scan's activity trace."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .. import scanlog


def truncate(v, n: int = 400) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= n else s[:n] + "…"


def log_tool_use(name: str, tool_input) -> None:
    scanlog.emit("agent", f"tool → {name}", level="tool",
                 data={"tool": name, "input": truncate(tool_input or {})})


def log_tool_result(output, *, is_error: bool = False) -> None:
    scanlog.emit("agent", "tool result", level="result",
                 data={"output": truncate(output), "is_error": bool(is_error)})


@dataclass
class AgentResult:
    text: str = ""
    is_error: bool = False
    error_message: str = ""
    num_turns: int = 0
    # USD. None when it can't be estimated (a third-party model we don't price).
    cost_usd: float | None = 0.0
    # Token accounting off the provider's usage report: input_tokens /
    # output_tokens / cache_* variants, plus server_tool_use search counts.
    # `model_usage` breaks the same totals down per model when more than one
    # was involved (the CLI reports it).
    usage: dict = field(default_factory=dict)
    model_usage: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    # Every URL the conversation's searches and fetches actually returned, when
    # the provider lets us see them (our own tools, the Anthropic API's result
    # blocks). None = unknown (the Claude CLI), so no citation check.
    seen_urls: set[str] | None = None

    def tokens(self) -> dict:
        """Flat token summary for the scan log. Cached input is kept separate
        from fresh input: the two bill differently, so folding them into one
        `input_tokens` number would misstate what the scan actually cost."""
        u = self.usage or {}
        fresh_in = int(u.get("input_tokens") or 0)
        cache_read = int(u.get("cache_read_input_tokens") or 0)
        cache_write = int(u.get("cache_creation_input_tokens") or 0)
        out = int(u.get("output_tokens") or 0)
        # Server-side tool calls are billed on their own meter and are what the
        # enrichment agent actually spends most of its budget on, so they are
        # counted here rather than left buried in the raw usage block.
        srv = u.get("server_tool_use") or {}
        return {
            "input_tokens": fresh_in,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
            "output_tokens": out,
            "total_tokens": fresh_in + cache_read + cache_write + out,
            "web_search_requests": int(srv.get("web_search_requests") or 0),
            "web_fetch_requests": int(srv.get("web_fetch_requests") or 0),
            "cost_usd": None if self.cost_usd is None else round(self.cost_usd, 6),
            "num_turns": self.num_turns,
        }


def sum_tokens(totals: dict, one: dict) -> dict:
    """Accumulate one `AgentResult.tokens()` into a running total. A call whose
    cost couldn't be estimated is counted in `cost_unknown`, so a total is
    never presented as complete when part of it is missing."""
    for k, v in one.items():
        if k == "cost_usd":
            if v is None:
                totals["cost_unknown"] = totals.get("cost_unknown", 0) + 1
                totals.setdefault("cost_usd", 0.0)
            else:
                totals[k] = round(totals.get(k, 0.0) + v, 6)
        else:
            totals[k] = totals.get(k, 0) + v
    return totals
