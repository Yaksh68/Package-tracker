"""Agent transport — one interface, two backends, same subscription auth.

    backend = "subprocess"  -> spawn the Claude Code CLI with stream-json I/O
                               (proven to work headless with subscription auth)
    backend = "sdk"         -> claude_agent_sdk.query()
    backend = "auto"        -> try sdk, fall back to subprocess on failure

Both drive the SAME `claude` binary and the SAME ~/.claude subscription login —
no ANTHROPIC_API_KEY. This isolation is what lets us swap the AI layer later
(e.g. to the raw Anthropic SDK) without touching storage/flags/API/UI.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .. import scanlog
from ..config import get_settings


def _truncate(v, n: int = 400) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= n else s[:n] + "…"


def _log_tool_use(block: dict) -> None:
    name = block.get("name", "tool")
    scanlog.emit("agent", f"tool → {name}", level="tool",
                 data={"tool": name, "input": _truncate(block.get("input", {}))})


def _log_tool_result(block: dict) -> None:
    content = block.get("content")
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    scanlog.emit("agent", "tool result", level="result",
                 data={"output": _truncate(content), "is_error": bool(block.get("is_error"))})


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid() == 0 if geteuid else False


class AgentError(RuntimeError):
    pass


@dataclass
class AgentResult:
    text: str = ""
    is_error: bool = False
    error_message: str = ""
    num_turns: int = 0
    cost_usd: float = 0.0
    # Token accounting straight off the result event. Both transports report the
    # same shape (input_tokens / output_tokens / cache_* variants); `model_usage`
    # breaks the same totals down per model when more than one was involved.
    usage: dict = field(default_factory=dict)
    model_usage: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)

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
            "cost_usd": round(self.cost_usd, 6),
            "num_turns": self.num_turns,
        }


def sum_tokens(totals: dict, one: dict) -> dict:
    """Accumulate one `AgentResult.tokens()` into a running total."""
    for k, v in one.items():
        if k == "cost_usd":
            totals[k] = round(totals.get(k, 0.0) + v, 6)
        else:
            totals[k] = totals.get(k, 0) + v
    return totals


def resolve_cli_path() -> str:
    """Locate the `claude` binary: explicit setting → PATH → common install dirs."""
    s = get_settings()
    if s.claude_cli_path:
        return s.claude_cli_path
    found = shutil.which("claude")
    if found:
        return found
    # VS Code extension bundled binary (most common on dev machines w/o CLI on PATH)
    for pat in (
        Path.home() / ".vscode-server/extensions",
        Path.home() / ".vscode/extensions",
        Path.home() / ".cursor-server/extensions",
        Path.home() / ".cursor/extensions",
    ):
        if pat.exists():
            hits = sorted(pat.glob("anthropic.claude-code-*/resources/native-binary/claude"))
            if hits:
                return str(hits[-1])
    raise AgentError(
        "Could not find the `claude` CLI. Install Claude Code and/or set "
        "CLAUDE_CLI_PATH in .env to the binary path."
    )


async def _run_subprocess(
    prompt: str,
    *,
    cwd: str | None,
    allowed_tools: list[str] | None,
    append_system: str | None,
    mcp_config: str | None,
    model: str | None,
    add_dirs: list[str] | None,
    timeout: float,
) -> AgentResult:
    cli = resolve_cli_path()
    cmd = [
        cli, "--print",
        "--output-format", "stream-json",
        "--input-format", "stream-json",
        "--verbose",
        "--model", model or get_settings().agent_model,
    ]
    # `bypassPermissions` is rejected when running as root; in that case we rely
    # solely on the explicit --allowedTools allowlist (all our tools are read-only).
    if not _is_root():
        cmd += ["--permission-mode", "bypassPermissions"]
    if allowed_tools:
        cmd += ["--allowedTools", *allowed_tools]
    if append_system:
        cmd += ["--append-system-prompt", append_system]
    if add_dirs:
        cmd += ["--add-dir", *add_dirs]
    if mcp_config:
        cmd += ["--mcp-config", mcp_config, "--strict-mcp-config"]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )

    user_msg = json.dumps(
        {"type": "user", "message": {"role": "user", "content": prompt}}
    ).encode() + b"\n"

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=user_msg), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise AgentError(f"agent timed out after {timeout}s")

    if proc.returncode not in (0, None) and not stdout:
        raise AgentError(
            f"claude CLI exited {proc.returncode}: {stderr.decode(errors='replace')[:500]}"
        )

    result = AgentResult()
    texts: list[str] = []
    for line in stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        result.events.append(ev)
        etype = ev.get("type")
        if etype == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    texts.append(block["text"])
                elif block.get("type") == "tool_use":
                    _log_tool_use(block)
        elif etype == "user":
            for block in ev.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    _log_tool_result(block)
        elif etype == "result":
            result.is_error = bool(ev.get("is_error"))
            result.num_turns = ev.get("num_turns", 0)
            result.cost_usd = ev.get("total_cost_usd", 0.0) or 0.0
            result.usage = ev.get("usage") or {}
            result.model_usage = ev.get("model_usage") or {}
            if ev.get("result") and not texts:
                texts.append(ev["result"])
            if result.is_error:
                result.error_message = ev.get("result") or ev.get("api_error_status") or "agent error"

    result.text = "\n".join(t for t in texts if t).strip()
    if not result.events:
        raise AgentError(
            f"no events from claude CLI (rc={proc.returncode}); "
            f"stderr: {stderr.decode(errors='replace')[:300]}"
        )
    return result


async def _run_sdk(
    prompt: str,
    *,
    cwd: str | None,
    allowed_tools: list[str] | None,
    append_system: str | None,
    model: str | None,
    add_dirs: list[str] | None,
    timeout: float,
) -> AgentResult:
    from claude_agent_sdk import (  # imported lazily; backend is optional
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    opts = ClaudeAgentOptions(
        cli_path=resolve_cli_path(),
        cwd=cwd,
        allowed_tools=allowed_tools or [],
        append_system_prompt=append_system,
        add_dirs=add_dirs or [],
        model=model or get_settings().agent_model,
        permission_mode="default" if _is_root() else "bypassPermissions",
    )

    result = AgentResult()
    texts: list[str] = []

    async def _drive() -> None:
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        texts.append(b.text)
            elif isinstance(msg, ResultMessage):
                result.is_error = bool(getattr(msg, "is_error", False))
                result.num_turns = getattr(msg, "num_turns", 0) or 0
                result.cost_usd = getattr(msg, "total_cost_usd", 0.0) or 0.0
                result.usage = getattr(msg, "usage", None) or {}
                result.model_usage = getattr(msg, "model_usage", None) or {}

    await asyncio.wait_for(_drive(), timeout=timeout)
    result.text = "\n".join(texts).strip()
    return result


async def run_text_agent(
    prompt: str,
    *,
    cwd: str | None = None,
    allowed_tools: list[str] | None = None,
    append_system: str | None = None,
    mcp_config: str | None = None,
    model: str | None = None,
    add_dirs: list[str] | None = None,
    timeout: float = 300.0,
) -> AgentResult:
    """Run one agent conversation and return the final assistant text."""
    backend = get_settings().agent_backend
    if backend == "sdk":
        return await _run_sdk(
            prompt, cwd=cwd, allowed_tools=allowed_tools,
            append_system=append_system, model=model, add_dirs=add_dirs, timeout=timeout,
        )
    if backend == "auto":
        try:
            return await _run_sdk(
                prompt, cwd=cwd, allowed_tools=allowed_tools,
                append_system=append_system, model=model, add_dirs=add_dirs, timeout=timeout,
            )
        except Exception:
            pass  # fall through to the proven subprocess backend
    return await _run_subprocess(
        prompt, cwd=cwd, allowed_tools=allowed_tools, append_system=append_system,
        mcp_config=mcp_config, model=model, add_dirs=add_dirs, timeout=timeout,
    )
