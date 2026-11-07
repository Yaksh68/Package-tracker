"""AI providers: one interface (`research`, returning an AgentResult), three ways
to run it. The Claude CLI uses the machine's own Claude Code login and is for
the local mode only. The Anthropic API uses Claude's built-in web search. Any
OpenAI-compatible API uses web search the app runs itself. The dispatcher is
`agents.transport.run_text_agent`; the configuration is `ai.config`."""
