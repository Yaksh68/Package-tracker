"""Application settings (pydantic-settings, loaded from environment / .env)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Where public-mode repos may be cloned from when GIT_HOST_ALLOWLIST is unset.
DEFAULT_PUBLIC_GIT_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # "none": no login, one local user (today's behaviour). "github": the public
    # multi-user mode. Sign-in arrives in a later phase; until then this only
    # tightens the clone and Docker rules (see public_mode()).
    auth_mode: str = "none"

    # Encrypts keys and tokens at rest, and signs nothing else. Required in
    # the public mode. Local installs without it get a generated key in
    # `local_secret_key_file`. To rotate: set the new key here and move the
    # old one to app_secret_key_previous until everything is re-saved.
    app_secret_key: str | None = None
    app_secret_key_previous: str | None = None
    local_secret_key_file: Path = Path("data/secret.key")

    # Public mode (auth_mode "github"): where the app is reached (the GitHub
    # OAuth callback is PUBLIC_BASE_URL/api/auth/github/callback), the OAuth
    # app's credentials, who's a platform admin (GitHub logins,
    # comma-separated), the sign-up cap and how long a session lasts.
    public_base_url: str = "http://localhost:5173"
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = None
    platform_admins: str = ""
    max_users: int = 50
    session_days: int = 30
    # Requests a minute (public mode, ratelimit.py): sign-in per address; API
    # calls per session, per address, and per address without a session.
    rate_limit_signin: int = 10
    rate_limit_session: int = 300
    rate_limit_address: int = 600
    rate_limit_anonymous: int = 60
    # Who runs this service, for the privacy policy and terms pages (shown
    # to anyone, signed in or not).
    operator_name: str = ""
    contact_email: str = ""

    # Hosts repos may be cloned from, comma-separated; "*.example.com" allows
    # that domain's subdomains. Empty = any host, but only when auth_mode is
    # "none"; the public mode falls back to DEFAULT_PUBLIC_GIT_HOSTS.
    git_host_allowlist: str = ""
    # A clone running longer than this is killed.
    clone_timeout_seconds: int = 120
    # Reuse an AI finding for the same ecosystem:name:version from the
    # workspace's scans of the last N days instead of researching it again
    # (0 = always research). The local workspace's default; others start at 7
    # and change it in Settings.
    ai_reuse_days: int = 7
    # How many package lookups one scan runs at a time (registries,
    # endoflife.date, GitHub).
    lookup_concurrency: int = 6
    # How many scans run at once (scan_queue.py). In the public mode a
    # workspace runs one at a time, taking turns with the others.
    scan_workers: int = 2

    # Docker image inventory: "auto" (use Docker when it's there, else skip
    # quietly), "on" (always try; a missing Docker degrades the scan) or "off".
    # Always off in the public mode, where it would run images users choose.
    image_inspect: str = "auto"

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

    # Which AI provider researches ambiguous packages (see app/ai/config.py).
    # Unset = the Claude CLI above, in the local mode only.
    ai_provider: str = ""
    ai_model: str = ""
    ai_api_key: str | None = None
    ai_base_url: str = ""
    # Web search for OpenAI-compatible providers, which have none built in:
    # "tavily" or "exa", with that service's key.
    search_provider: str = ""
    search_api_key: str | None = None
    # Most tool calls one OpenAI-compatible research conversation may make.
    ai_max_tool_rounds: int = 8

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

    def public_mode(self) -> bool:
        """True when the service is open to the public (auth_mode "github"):
        stricter clone rules, no Docker image inventory."""
        return (self.auth_mode or "none").strip().lower() == "github"

    def admin_logins(self) -> set[str]:
        """GitHub logins with platform-admin rights, lower-cased."""
        return {x.strip().lower() for x in (self.platform_admins or "").split(",") if x.strip()}

    def https(self) -> bool:
        return (self.public_base_url or "").lower().startswith("https://")

    def git_hosts(self) -> tuple[str, ...]:
        """The effective clone allowlist (lower-case). Empty means any host,
        which only the local mode allows."""
        hosts = tuple(
            h.strip().lower() for h in (self.git_host_allowlist or "").split(",") if h.strip()
        )
        if not hosts and self.public_mode():
            return DEFAULT_PUBLIC_GIT_HOSTS
        return hosts

    def github_read_token(self) -> str | None:
        return self.github_api_token or self.github_token


@lru_cache
def get_settings() -> Settings:
    return Settings()
