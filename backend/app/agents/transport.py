"""Agent transport: the one place that talks to a model.

`run_text_agent` dispatches on the configured provider (see ai/config.py):

    claude-cli          -> the Claude Code CLI and the machine's own login
                           (local mode only), through one of three backends:
                           "subprocess" (stream-json I/O, the proven default),
                           "sdk" (claude_agent_sdk.query()), or "auto" (try
                           sdk, fall back to subprocess)
    anthropic           -> ai.anthropic_provider (API key, server-side web search)
    openai-compatible   -> ai.openai_compat (API key, web search the app runs)

Every path returns the same AgentResult, so storage/flags/API/UI never know
which provider ran. The API providers only research packages (web search and
fetch); the AI extraction agent's file tools (Read/Glob/Grep) need the CLI.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path

from ..ai import anthropic_provider, openai_compat
from ..ai import config as ai_config
from ..ai.errors import AiConfigError
from ..ai.result import AgentResult, log_tool_result, log_tool_use, sum_tokens  # noqa: F401 - re-exported
from ..config import get_settings

_RESEARCH_TOOLS = {"WebSearch", "WebFetch"}


def _log_tool_use(block: dict) -> None:
    log_tool_use(block.get("name", "tool"), block.get("input", {}))


def _log_tool_result(block: dict) -> None:
    content = block.get("content")
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    log_tool_result(content, is_error=bool(block.get("is_error")))


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid() == 0 if geteuid else False


class AgentError(RuntimeError):
    pass


def _extension_version(path: Path) -> tuple[int, ...]:
    m = re.search(r"anthropic\.claude-code-(\d+(?:\.\d+)*)", str(path))
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def resolve_cli_path() -> str:
    """Locate the `claude` binary: explicit setting → PATH → common install dirs."""
    s = get_settings()
    if s.claude_cli_path:
        return s.claude_cli_path
    found = shutil.which("claude")
    if found:
        return found
    # The VS Code / Cursor extension's bundled binary: the most common case on
    # dev machines without the CLI on PATH. It's `claude.exe` on Windows.
    # Several versions are often installed side by side; take the newest by
    # version number, not by string order (2.1.99 < 2.1.314).
    names = ("claude.exe", "claude") if os.name == "nt" else ("claude",)
    for base in (
        Path.home() / ".vscode-server/extensions",
        Path.home() / ".vscode/extensions",
        Path.home() / ".cursor-server/extensions",
        Path.home() / ".cursor/extensions",
    ):
        if base.exists():
            hits = [p for name in names
                    for p in base.glob(f"anthropic.claude-code-*/resources/native-binary/{name}")]
            if hits:
                return str(max(hits, key=_extension_version))
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
    tool_counts = {"WebSearch": 0, "WebFetch": 0}
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
                    if block.get("name") in tool_counts:
                        tool_counts[block["name"]] += 1
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
    # The CLI's WebSearch / WebFetch run as its own tools, and it reports them
    # as 0 in usage.server_tool_use. Count them from the event stream instead,
    # so the scan log and analytics show the searches that really happened.
    srv = (result.usage or {}).get("server_tool_use") or {}
    if not (srv.get("web_search_requests") or srv.get("web_fetch_requests")) and any(tool_counts.values()):
        result.usage = {**(result.usage or {}), "server_tool_use": {
            "web_search_requests": tool_counts["WebSearch"],
            "web_fetch_requests": tool_counts["WebFetch"],
        }}
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


async def _run_cli(
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
    """Run one agent conversation with the configured provider and return the
    final assistant text. Raises AiConfigError when no provider is usable."""
    cfg, why = ai_config.resolve()
    if cfg is None:
        raise AiConfigError(why or "no AI provider is configured")
    if cfg.provider == "claude-cli":
        return await _run_cli(
            prompt, cwd=cwd, allowed_tools=allowed_tools, append_system=append_system,
            mcp_config=mcp_config, model=model or cfg.model, add_dirs=add_dirs, timeout=timeout,
        )
    if not set(allowed_tools or []) <= _RESEARCH_TOOLS:
        raise AiConfigError(
            f"{cfg.label} can only research packages; the AI extraction agent needs the Claude CLI"
        )
    max_searches = get_settings().web_search_max_uses
    system = append_system or ""
    if cfg.provider == "anthropic":
        return await anthropic_provider.research(cfg, prompt, system,
                                                 max_searches=max_searches, timeout=timeout)
    return await openai_compat.research(cfg, prompt, system,
                                        max_searches=max_searches, timeout=timeout)
