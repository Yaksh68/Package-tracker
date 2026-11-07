"""Application settings (pydantic-settings, loaded from environment / .env)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Repo-access PATs. Injected into clone URLs at runtime, never persisted.
    gitlab_token: str | None = None
    github_token: str | None = None
    # Token for query_github_repo (rate-limit); falls back to github_token.
    github_api_token: str | None = None

    # Path to the Claude Code CLI binary the Agent SDK spawns. The subscription
    # login in ~/.claude is used automatically — no ANTHROPIC_API_KEY.
    # If unset, the SDK searches PATH; set this to the VS Code extension's bundled
    # binary when `claude` isn't on PATH, e.g.
    #   ~/.vscode-server/extensions/anthropic.claude-code-<ver>-linux-x64/resources/native-binary/claude
    claude_cli_path: str | None = None

    # Model the agents run on (subscription). Set via AGENT_MODEL in .env; this is
    # just the fallback default.
    agent_model: str = "claude-haiku-4-5-20251001"

    # AI transport backend: "subprocess" (drive the CLI directly, proven robust),
    # "sdk" (claude-agent-sdk), or "auto" (try sdk, fall back to subprocess).
    agent_backend: str = "subprocess"

    # Run the AI extraction agent (fallback for unknown manifest formats).
    # Off by default: deterministic parsers cover the target repos with no AI cost.
    use_extraction_agent: bool = False

    # Run the AI enrichment/research agent for ambiguous packages (no EOL date /
    # unknown / stale). Default ON; toggleable at runtime from the UI.
    use_enrichment_agent: bool = True

    # PostgreSQL connection, e.g.
    #   postgresql+psycopg://packages:<password>@127.0.0.1:5432/packages_tracker
    # Required — all repos, settings and snapshot history live there.
    database_url: str | None = None

    # Legacy file-storage tree. Only read by the one-off importer
    # (`python -m app.import_files`); the app itself no longer writes here.
    eol_data_dir: Path = Path("data")

    # Flag tuning.
    eol_soon_months: int = 6      # EOL_SOON_AFTER_RELEASE window
    stale_years: int = 2          # STALE threshold (no release/commit)

    # Web search cap per enrichment agent conversation.
    web_search_max_uses: int = 5
    # Packages per enrichment agent conversation.
    enrichment_batch_size: int = 18

    # Uploaded source zips (repos with source="upload"): where they're kept,
    # relative to the backend cwd, and the limits an upload must fit.
    upload_dir: Path = Path("uploads")
    upload_max_mb: int = 200            # the .zip itself
    upload_max_unpacked_mb: int = 2048  # files a scan reads (node_modules etc. not counted)
    upload_max_files: int = 100_000

    # Downloaded apt `Packages` indexes (Ubuntu archive + repos Dockerfiles add),
    # relative to the backend cwd. See agents/apt_index.py.
    apt_index_cache_dir: Path = Path("cache/apt-index")

    @property
    def data_dir(self) -> Path:
        return self.eol_data_dir

    def github_read_token(self) -> str | None:
        return self.github_api_token or self.github_token


@lru_cache
def get_settings() -> Settings:
    return Settings()
