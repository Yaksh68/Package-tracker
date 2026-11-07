# Packages Tracker

Automates the manual "list of components + EOL status" CSV process for any
software project. You register a **repo + branch + planned release date + scan
interval**; the system extracts every dependency, researches its EOL/EOS status,
and shows a per-repo dashboard that flags anything that will be **EOL before your
planned release**. Each scan is saved as a timestamped snapshot so you can see
*when* a package's status changed.

- **Backend**: FastAPI + APScheduler, PostgreSQL (SQLAlchemy 2.0 + psycopg 3 + Alembic)
- **Frontend**: React + Vite + TypeScript + Ant Design + TanStack Query
- **AI**: two agents (extraction + EOL research) that run on your **Claude Code
  subscription login** — **no `ANTHROPIC_API_KEY`.** Docker runs only PostgreSQL.

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
(`claude-agent-sdk`), or `auto` (SDK with subprocess fallback).

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
pip install -e .                     # add pytest pytest-asyncio for the tests
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
| `GITLAB_TOKEN` / `GITHUB_TOKEN` | Repo read PATs; injected into the clone URL at runtime, never stored in snapshots or logs |
| `GITHUB_API_TOKEN` | Optional; raises GitHub API rate limit for enrichment (falls back to `GITHUB_TOKEN`) |
| `CLAUDE_CLI_PATH` | Path to the `claude` binary if not on PATH |
| `AGENT_BACKEND` | `subprocess` (default) / `sdk` / `auto` |
| `AGENT_MODEL` | Model the agents use (default `claude-haiku-4-5-20251001`) |
| `USE_EXTRACTION_AGENT` | `true` to run the AI extraction fallback for unusual manifests (off by default — deterministic parsers cover the common manifest formats) |
| `USE_ENRICHMENT_AGENT` | `true` (default) to run the AI research agent for ambiguous packages; also toggleable live from the dashboard header ("AI research") |
| `WEB_SEARCH_MAX_USES` | web-search cap per AI enrichment batch (default 5) |
| `ENRICHMENT_BATCH_SIZE` | packages per AI enrichment conversation (default 18) |
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
| POST | `/api/repos/{id}/scan` | queue a scan (202) |
| GET | `/api/repos/{id}/scan-status` | live scan phase |
| GET | `/api/repos/{id}/scan-log` | activity trace (live current run; `?ts=` for a past snapshot's trace) |
| GET | `/api/repos/{id}/snapshots` | history timeline |
| GET | `/api/repos/{id}/snapshots/{ts}/components` | merged packages+enrichment+flags (filters: `ecosystem`, `status`, `search`, `direct_only`) |
| GET | `/api/repos/{id}/snapshots/{ts}/report.csv` | CSV export (built on request) |
| GET | `/api/repos/{id}/diff?from=&to=` | status changes between snapshots |
| GET/PUT | `/api/settings` | runtime toggles (e.g. `use_enrichment_agent`), UI-editable |

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
