# Packages Tracker

Automates the manual "list of components + EOL status" CSV process for any
software project. You register a **repo + branch + planned release date + scan
interval**; the system extracts every dependency, researches its EOL/EOS status,
and shows a per-repo dashboard that flags anything that will be **EOL before your
planned release**. Each scan is saved as a timestamped snapshot so you can see
*when* a package's status changed.

- **Backend**: FastAPI + APScheduler, PostgreSQL (SQLAlchemy 2.0 + psycopg 3 + Alembic)
- **Frontend**: React + Vite + TypeScript + Ant Design + TanStack Query
- **AI** (optional; every scan runs without it): EOL research for the packages
  the structured sources can't answer. Locally it can use your **Claude Code
  login**. Otherwise each workspace brings its own key for Anthropic, OpenAI,
  Gemini, Groq, OpenRouter or any OpenAI-compatible API, set on the Settings
  page. Docker runs only PostgreSQL.
- **Two modes**: `AUTH_MODE=none` (default) is a single local user. `AUTH_MODE=github`
  is the public multi-user service: Sign in with GitHub, team workspaces with
  roles, invites, an audit log and a platform admin area.

## How the AI auth works (important)

The agents don't call the Anthropic API directly. They drive the **Claude Code
CLI** as a subprocess, which uses the subscription login already stored in
`~/.claude`. Requirements:

1. Claude Code installed and logged in on the machine running the backend
   (`claude login`, or the VS Code / JetBrains extension).
2. The backend can find the `claude` binary. It auto-discovers PATH and the VS
   Code extension's bundled binary; if neither is found, set `CLAUDE_CLI_PATH` in
   `.env` to the binary, e.g.
   `~/.vscode-server/extensions/anthropic.claude-code-<ver>-linux-x64/resources/native-binary/claude`

`agent_backend` (in `.env` / config) selects the transport:
`subprocess` (default, drives the CLI directly — most robust), `sdk`
(`claude-agent-sdk`), or `auto` (SDK with subprocess fallback). The SDK is an
optional extra, since it bundles the whole CLI: `pip install -e ".[claude]"` to use
`sdk` or `auto`.

## Deploying to the cloud

The public, multi-user service runs from one Docker image (the API serving the
built UI) behind Caddy, with PostgreSQL, on a single VM:
`deploy/compose.cloud.yml`, `deploy/Caddyfile`, `deploy/.env.cloud.example`.
**[docs/DEPLOY-ORACLE.md](docs/DEPLOY-ORACLE.md)** walks through it on Oracle's
Always Free tier: the VM, both firewalls, the domain, GitHub sign-in, the first
start, nightly backups copied off the VM (`deploy/backup.sh`), and updates
(`deploy/update.sh`). CI (`.github/workflows/`) runs the tests on every push and
builds the image when its inputs change.

## Run it

**On this host both halves already run as systemd services** — they survive
closing a terminal, an SSH/VS Code disconnect, and a reboot:

```bash
systemctl status  packages-tracker-backend packages-tracker-frontend --no-pager
systemctl restart packages-tracker-backend        # after changing backend code
journalctl -u packages-tracker-backend -f         # live logs
```
Dashboard: **http://localhost:5173** (backend on **:8001**). Full start/stop/logs
and troubleshooting reference: **[docs/RUNNING-AS-A-SERVICE.md](docs/RUNNING-AS-A-SERVICE.md)**.

Don't run a second copy from a terminal while the services are up — the port will
already be taken. The first-time/manual setup is below.

## First-time setup (local; Docker only for PostgreSQL)

> ⚠️ **Use Python 3.11 on this host, not 3.12.** The `python3.12` build installed
> here has a broken asyncio event loop — it segfaults (general-protection fault)
> the moment any event loop runs, so `uvicorn` won't start under 3.12.
> `python3.11` works. The code supports 3.11+.

### Database
```bash
cp .env.example .env                 # set POSTGRES_PASSWORD and the same password in DATABASE_URL
docker compose -f deploy/postgres-compose.yml --env-file .env up -d   # 127.0.0.1:5432
```
The backend applies migrations on startup (`cd backend && alembic upgrade head`
does the same by hand).

### Backend
```bash
cd backend
# This host's python3.11 venv can't self-bootstrap pip, so create it without pip
# and install pip via get-pip:
python3.11 -m venv .venv --without-pip
curl -sS https://bootstrap.pypa.io/get-pip.py | .venv/bin/python
. .venv/bin/activate
pip install -e ".[dev]"              # the tests' tools too; add ,claude for AGENT_BACKEND=sdk
# in ../.env also fill in GITLAB_TOKEN / GITHUB_TOKEN
# the tracker listens on :8001 (the deploy units and VITE_API_TARGET assume it)
uvicorn app.main:app --reload --port 8001        # http://127.0.0.1:8001
```
> If your machine has a healthy Python (working `python3 -m venv` + 3.12 asyncio),
> the normal `python3 -m venv .venv && . .venv/bin/activate && pip install -e .`
> is fine — the get-pip dance is only needed to work around this host's broken
> venv/asyncio.

**Existing file data** (`data/repos.json` + `data/snapshots/`) — import once:
```bash
cd backend && . .venv/bin/activate
python -m app.import_files --dry-run   # report only; also --data-dir PATH, --overwrite-repos
python -m app.import_files             # idempotent: re-run skips what's already imported
```
Stop the backend first (`systemctl stop packages-tracker-backend`) and start it after:
a scan running mid-import would race it, and imported repos only get their
scheduled scans once the backend starts. `data/` is only read, never modified —
keep it as a backup.

### Frontend
```bash
cd frontend
npm install
# VITE_API_TARGET is required: the proxy defaults to :8000, but the backend
# above listens on :8001.
VITE_API_TARGET=http://127.0.0.1:8001 npm run dev    # http://127.0.0.1:5173
```

Open http://127.0.0.1:5173 → **Add repository** → enter a GitLab/GitHub URL,
branch, planned release date, and interval. The first scan starts immediately.

## Configuration (`.env`)

| Var | Purpose |
|---|---|
| `AUTH_MODE` | `none` (default: no login, the built-in local user and workspace) or `github` (public mode: Sign in with GitHub, workspaces, per-workspace keys, and stricter clone and Docker rules) |
| `APP_SECRET_KEY` | Encrypts stored keys and tokens. **Required in the public mode** (generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`). Local installs without one get a generated key in `LOCAL_SECRET_KEY_FILE` (default `data/secret.key`) |
| `APP_SECRET_KEY_PREVIOUS` | The old key while rotating: values encrypted with either key still decrypt |
| `PUBLIC_BASE_URL` | Where the app is reached (default `http://localhost:5173`). The GitHub OAuth callback is `PUBLIC_BASE_URL/api/auth/github/callback`; `https://` turns on Secure cookies |
| `GITHUB_OAUTH_CLIENT_ID` / `GITHUB_OAUTH_CLIENT_SECRET` | The GitHub OAuth app used for Sign in with GitHub (public mode) |
| `PLATFORM_ADMINS` | Comma-separated GitHub logins with platform-admin rights: the `/api/admin` routes (users and workspaces across the service). They can always sign up, and they get the repos from before the public mode |
| `MAX_USERS` | Sign-up cap (default 50) |
| `SESSION_DAYS` | How long a session lasts (default 30) |
| `OPERATOR_NAME` / `CONTACT_EMAIL` | Who runs the service, shown on the privacy policy and terms pages |
| `GIT_HOST_ALLOWLIST` | Comma-separated hosts repos may be cloned from (`*.example.com` = subdomains). Empty = any host in the local mode; the public mode defaults to github.com, gitlab.com, bitbucket.org, codeberg.org |
| `CLONE_TIMEOUT_SECONDS` | Kill a clone that runs longer than this (default 120) |
| `SCAN_WORKERS` | Scans that run at once (default 2). In the public mode each workspace runs one at a time and workspaces take turns |
| `LOOKUP_CONCURRENCY` | Package lookups one scan runs at a time (default 6) |
| `RATE_LIMIT_SIGNIN` / `RATE_LIMIT_SESSION` / `RATE_LIMIT_ADDRESS` / `RATE_LIMIT_ANONYMOUS` | Public mode, requests a minute: sign-in per address (10), API calls per session (300), per address (600) and per address without a session (60) |
| `IMAGE_INSPECT` | Docker image inventory: `auto` (default: use Docker when it's there, otherwise skip without degrading the scan), `on`, or `off`. Always off in the public mode |
| `GITLAB_TOKEN` / `GITHUB_TOKEN` | Repo read PATs; injected into the clone URL at runtime, never stored in snapshots or logs |
| `GITHUB_API_TOKEN` | Optional; raises GitHub API rate limit for enrichment (falls back to `GITHUB_TOKEN`) |
| `CLAUDE_CLI_PATH` | Path to the `claude` binary if not on PATH |
| `AI_PROVIDER` | Who researches ambiguous packages. Unset = the Claude CLI and its login (local mode only); `anthropic` = Anthropic API; `openai`, `gemini`, `groq`, `openrouter` = that provider's OpenAI-compatible API; `openai-compatible` = any other OpenAI-compatible endpoint at `AI_BASE_URL` |
| `AI_MODEL` | Model for `AI_PROVIDER` (Anthropic default `claude-haiku-5-5`; required for the OpenAI-compatible ones) |
| `AI_API_KEY` | The provider's API key (`anthropic` also accepts `ANTHROPIC_API_KEY`) |
| `AI_BASE_URL` | Endpoint for `AI_PROVIDER=openai-compatible` (e.g. a local Ollama) |
| `SEARCH_PROVIDER` / `SEARCH_API_KEY` | `tavily` or `exa`, plus its key. Required for the OpenAI-compatible providers, which have no built-in web search |
| `AI_MAX_TOOL_ROUNDS` | Most tool rounds per OpenAI-compatible research conversation (default 8) |
| `AGENT_BACKEND` | `subprocess` (default) / `sdk` / `auto` |
| `AGENT_MODEL` | Model the agents use (default `claude-haiku-4-5-20251001`) |
| `USE_EXTRACTION_AGENT` | `true` to run the AI extraction fallback for unusual manifests (off by default — deterministic parsers cover the common manifest formats) |
| `USE_ENRICHMENT_AGENT` | `true` (default) to run the AI research agent for ambiguous packages; also toggleable live from the dashboard header ("AI research") |
| `WEB_SEARCH_MAX_USES` | web-search cap per AI enrichment batch (default 5) |
| `ENRICHMENT_BATCH_SIZE` | packages per AI enrichment conversation (default 18) |
| `AI_REUSE_DAYS` | Reuse an AI finding for the same package version from the last N days' scans instead of researching it again (default 7; 0 = always research). The local workspace's default; each workspace sets its own in Settings |
| `APT_INDEX_CACHE_DIR` | Cache for downloaded apt package indexes (default `cache/apt-index`, ~26 MB per Ubuntu release) |
| `UPLOAD_DIR` | Where uploaded source zips are kept (default `uploads`, relative to `backend/`) |
| `UPLOAD_MAX_MB` / `UPLOAD_MAX_UNPACKED_MB` / `UPLOAD_MAX_FILES` | Upload limits (defaults 200 MB zip, 2048 MB unpacked, 100 000 files; skipped dirs don't count) |
| `DATABASE_URL` | Required. `postgresql+psycopg://packages:<password>@127.0.0.1:5432/packages_tracker` |
| `POSTGRES_PASSWORD` | Password for `deploy/postgres-compose.yml`; must match `DATABASE_URL` |
| `EOL_DATA_DIR` | Legacy file tree, read only by the importer (default `./data`) |
| `EOL_SOON_MONTHS` | "soon after release" window (default 6) |

## How it works

```
repos table ──APScheduler(per-repo interval)──▶ pipeline.run_scan()
   clone (token injected at runtime)
   → Agent 1 extraction   (parsers + AI fallback)     → packages
   → Agent 2 EOL research (endoflife/PyPI/npm/GitHub + AI, always fresh) → enrichments
   → flag engine (pure Python, release-date aware)    → flags
   → snapshot row (one transaction per write) + scan log
FastAPI reads PostgreSQL (CSV built on request) ──▶ React dashboard
```

- **Extraction** parses known manifests deterministically (requirements.txt,
  package.json, package-lock.json, pyproject.toml, Dockerfile) and only calls the
  AI agent for formats it doesn't recognise. Reproducible and cheap.
- **APT/OS scope**: base-image EOL (via endoflife.date) + the apt packages
  explicitly installed in the Dockerfile. It does **not** reproduce a full
  `dpkg -l` of the built image (that would require building the image).
- **Enrichment has no cache** — every scan re-researches against today's upstream
  data, so a newly-announced EOL or a new stable release shows up on the next run
  rather than waiting out a TTL. Lookups are deduped by `ecosystem:name:version`
  within a scan; AI cost is bounded by the ambiguous-only gate, the batch size,
  and the web-search cap.
- **AI research pass** (M2) runs *only* for ambiguous packages (unknown verdict /
  low confidence / stale with no endoflife match). It web-searches for
  deprecation, archival, and successor libraries and returns a cited verdict.
  **It never sets dates** — EOL/EOS dates come only from structured sources — and
  any failure degrades gracefully to the structured result. Toggle it live from
  the dashboard header ("AI research").
- **Flagging** is pure Python and release-date aware. Editing the release date
  re-flags the latest snapshot instantly from stored enrichment — **no re-scan,
  no AI cost.**

### Flag severity ladder
`ALREADY_EOL` → `EOL_BEFORE_RELEASE` → `EOL_SOON_AFTER_RELEASE` → `DEPRECATED`
→ `ARCHIVED` → `EOS_PASSED` → `STALE` → `UNKNOWN` → `OK`
(`ALREADY_EOL` also fires when the upstream GitHub SECURITY.md marks the pinned
release line as unsupported — read only as an EOL signal; the tracker does not
check vulnerabilities.)
(no release date → the "before/soon" rules become "EOL within N months from today").

## Tests
```bash
cd backend && . .venv/bin/activate
pytest                 # flag boundaries, parsers, storage, migrations, API surface
```
Each test gets a throwaway SQLite DB; set `TEST_DATABASE_URL=postgresql+psycopg://…`
to run against Postgres (tables are truncated between tests — use a scratch DB).

## API
| Method | Path | |
|---|---|---|
| GET/POST | `/api/repos` | list / register (branch, release_date, interval_days) |
| GET/PUT/DELETE | `/api/repos/{id}` | PUT release_date → instant re-flag |
| POST | `/api/repos/{id}/scan` | queue a scan (202, with `queue_position`). A branch already queued or running isn't queued again: `merged: true` |
| GET | `/api/repos/{id}/scan-status` | live scan phase |
| GET | `/api/repos/{id}/scan-log` | activity trace (live current run; `?ts=` for a past snapshot's trace) |
| GET | `/api/repos/{id}/snapshots` | history timeline |
| GET | `/api/repos/{id}/snapshots/{ts}/components` | merged packages+enrichment+flags (filters: `ecosystem`, `status`, `search`, `direct_only`) |
| GET | `/api/repos/{id}/snapshots/{ts}/report.csv` | CSV export (built on request) |
| GET | `/api/repos/{id}/diff?from=&to=` | status changes between snapshots |
| GET/PUT | `/api/settings` | the active workspace's settings (AI research on/off, AI provider, model, search provider) plus which secrets are set. Admins and Owners also see each secret's hint |
| GET | `/api/limits` | the active workspace's limits and what it uses now (public mode) |
| POST | `/api/settings/ai-test` | Test connection: `{provider, base_url?, api_key?}` → the provider's models, or why it refused (`ok: false`, a `kind` and a message). Uses the typed key, else the saved one. Costs no tokens |
| PUT/DELETE | `/api/secrets/{name}` | save or remove a workspace secret: `ai_api_key`, `search_api_key`, `github_token`, `gitlab_token`. Write-only: values are never returned |
| GET/DELETE | `/api/me` | the caller, their active workspace, their role and permissions there, and their workspaces; DELETE deletes the account (public mode) |
| GET/POST | `/api/workspaces` | your workspaces; create a team workspace (public mode) |
| PUT/DELETE | `/api/workspaces/{id}` | rename; delete a team workspace with everything in it |
| POST | `/api/workspaces/{id}/transfer` | make another member Owner and step down to Admin |
| POST | `/api/workspaces/{id}/leave` | leave a team workspace |
| GET | `/api/workspaces/{id}/members` | members and their roles |
| PUT/DELETE | `/api/workspaces/{id}/members/{user_id}` | change a member's role; remove a member |
| GET/POST | `/api/workspaces/{id}/invites` | open invites; invite by GitHub login, or leave the login out for a one-time link (its URL is shown only in this response) |
| DELETE | `/api/workspaces/{id}/invites/{invite_id}` | revoke an invite |
| GET | `/api/invites` | invites waiting for you |
| POST | `/api/invites/{id}/accept`, `/api/invites/{id}/decline`, `/api/invites/accept-link` | answer an invite, or use a link's token |
| GET | `/api/workspaces/{id}/audit?before=&limit=` | the audit log, newest first; pass the last entry's id as `before` for the next page |
| GET | `/api/admin/users` | platform admin: every user |
| PUT/DELETE | `/api/admin/users/{id}` | platform admin: disable or enable (`{"status": "disabled"}` also signs them out); delete |
| GET | `/api/admin/workspaces`, `/api/admin/workspaces/{id}` | platform admin: every workspace with member and repo counts; one workspace's members and repos (metadata only, and recorded in that workspace's audit log) |
| GET/PUT | `/api/admin/limits` | platform admin: the limits every workspace gets |
| GET/PUT | `/api/admin/workspaces/{id}/limits` | platform admin: one workspace's own limits (`null` = the platform's value), with its usage. Recorded in that workspace's audit log |
| GET | `/api/info` | open to anyone: the mode, and the operator's name and contact |
| GET | `/api/auth/github/login?next=/page`, `/api/auth/github/callback` | Sign in with GitHub (public mode), returning to `next` (a path on this site). A failed sign-in lands on `/login?error=` `cancelled`, `expired`, `github`, `disabled` or `full` |
| POST | `/api/auth/logout` | end the session |

In the public mode, every `/api` call except health and sign-in needs a session cookie. Data-changing calls also need the header `X-Requested-With: packages-tracker`, and `X-Workspace: <id>` picks the workspace (the default is your personal one). Repos and workspaces you're not in return 404, and the admin routes return 404 to everyone but the platform admins.

### Roles
Each route needs a role in the workspace it touches (for `/api/repos/{id}`, the repo's workspace). Each role can do everything the one above it can. Without the role, the call returns 403.

| Role | Can |
|---|---|
| Viewer | see repos, results, history and scan activity; export CSV; see the settings and the members |
| Editor | also add and edit repos, upload zips, and run scans (these spend the workspace's AI key) |
| Admin | also delete repos; change settings; set or remove keys and tokens; invite and remove members and change their roles; read the audit log |
| Owner | also make, change or remove Owners; rename, transfer or delete the workspace |

The last Owner can't leave, step down or be removed. A personal workspace belongs to its account alone: it takes no invites, and deleting the account deletes it. Deleting an account also deletes any team workspace its user was the only member of. In the local mode the built-in user is the Owner of everything.

### Limits and upkeep (public mode)
The platform admin sets these for everyone (Admin, then Limits) and can give a workspace its own values. A workspace's members see their usage in Settings.

| Limit | Default |
|---|---|
| Team workspaces a user can create | 3 |
| Members per workspace (with pending invites) | 10 |
| Repositories per workspace | 10 |
| Branches or versions per repository | 3 |
| Shortest scan interval | 1 day |
| Manual scans per workspace per hour | 10 |
| Zip size, packed / unpacked | 50 MB / 500 MB |
| Upload storage per workspace | 200 MB |
| Scans kept per branch | 30 (older ones are deleted daily) |

Scans wait in a queue: `SCAN_WORKERS` run at once, one per workspace, and workspaces take turns. When nobody in a workspace has used the service for 30 days, its scheduled scans pause until a member comes back. The local mode has no limits, pausing or deletion of history.

## Milestone status
- **M0 skeleton — done**: storage/config/models, flag engine, deterministic
  parsers, clone, dual-backend agent transport, pipeline, scheduler, full REST
  API, React dashboard (RepoList / RepoDetail / SnapshotHistory). Extraction
  verified against real-world repos (a Python service: 160 pypi + apt +
  ubuntu:24.04; a web UI: 59 npm + ubuntu:22.04). Backend boots on Python 3.11; full CRUD verified over HTTP.
- **Scan activity trace — done**: per-scan stage timeline + event log (incl. AI
  tool calls and per-package verdict derivation), `GET /scan-log`, `/repos/:id/activity`.
- **M2 AI enrichment — done**: AI research pass for ambiguous packages
  (WebSearch/WebFetch, cited verdicts, no AI-set dates, graceful degrade, UI
  toggle). Verified live: `cybox` → `deprecated`, successor `python-stix2`, 4
  citations. 32 backend tests pass.
- **Next**: AI extraction fallback polish; per-repo AI toggle; richer citation UI;
  DB phases 2–3 (multi-instance, retention, backups).

> **Note for reboots (v1):** APScheduler runs in-process, so interval scans only
> fire while `uvicorn` is running. Leave it running, or wrap it in a
> systemd/launchd service.
