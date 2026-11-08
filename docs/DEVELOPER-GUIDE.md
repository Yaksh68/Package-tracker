# Packages Tracker — Developer Guide

How the product is built: architecture, data flow, and what every file and
function does. Pairs with the [USER-GUIDE](USER-GUIDE.md) (product/fields) and the
[M2 AI plan](m2-ai-enrichment-plan.md).

---

## 1. Architecture at a glance

```
                         repos table (config: url + branches[])
                                │  APScheduler fires per (repo, branch) on its interval
                                ▼
  pipeline.run_scan(repo, branch) ── binds a ScanLogger (activity trace) ──┐
     1. clone.shallow_clone(url, branch, token)                            │
     2. extraction_agent.extract()   deterministic parsers                 │ emits
        (+ optional AI fallback)      → packages                          │ scanlog
     3. enrichment_agent.enrich()    structured sources (always fresh)    │ events
        (+ AI research 2nd pass)      → enrichments                      │
     4. flags.evaluate_all()         pure Python, release-date aware      │
        → flags                                                          │
     5. storage.write_*()            one transaction per write           │
                                ▼                                          ▼
   PostgreSQL: snapshots row (repo, branch, ts) + child rows        scan_logs
                                │
                 FastAPI (api/routes.py) reads via storage.py (CSV built
                 on request), scoped by a required `?branch=` query param
                                │
                 React + Vite + AntD + TanStack Query dashboard
```

Each branch of a repo scans, schedules, and keeps snapshot history
**independently** — the repo is the tracked project (URL + shared config), the
branch is the unit of scanning.

**Design principles**
- **Deterministic core, AI at the edges.** Parsing manifests, structured EOL
  lookups, and flagging are plain Python (reliable, testable, free). AI is used
  only where it adds value: unknown manifest formats and ambiguous EOL cases.
- **PostgreSQL behind one seam.** `storage.py` kept the file version's function
  signatures and Pydantic return types, so routes/pipeline/scheduler didn't
  change. Each call is one transaction. See §4 `db_models.py` for the schema.
- **Isolated AI layer.** All model interaction goes through one transport with two
  interchangeable backends, so the AI provider/mechanism can be swapped without
  touching storage/flags/API/UI.
- **Never lose a scan.** Any stage failure degrades that data (clone fail → empty
  degraded snapshot; enrichment fail → `UNKNOWN`) and is recorded; the scan always
  finishes and writes a snapshot.

---

## 2. Tech stack & prerequisites
- **Backend:** Python **3.11** (this host's 3.12 has a broken asyncio event loop —
  see §9), FastAPI, Uvicorn, APScheduler, Pydantic v2, httpx, `claude-agent-sdk`,
  SQLAlchemy 2.0 (sync) + psycopg 3 + Alembic on PostgreSQL 16
  (`deploy/postgres-compose.yml`).
- **Frontend:** React 18, Vite, TypeScript, Ant Design, TanStack Query, React Router.
- **AI:** the Claude Code CLI driven via subscription auth (no API key). Default
  model `claude-haiku-4-5-20251001`.

---

## 3. Directory map
```
packages-tracker/
├── backend/
│   ├── pyproject.toml            # deps, requires-python >=3.11, pytest config
│   ├── alembic.ini + alembic/    # migrations (env.py, versions/0001_initial_schema.py)
│   └── app/
│       ├── config.py             # env settings (pydantic-settings)
│       ├── models.py             # all Pydantic data contracts
│       ├── db.py                 # engine, session(), JSON types, upgrade()
│       ├── db_models.py          # ORM tables
│       ├── storage.py            # repo registry + snapshot history over the DB
│       ├── legacy_files.py       # read-only reader of the old data/ file tree
│       ├── import_files.py       # one-off importer CLI (data/ → DB)
│       ├── flags.py              # deterministic flag engine
│       ├── scanlog.py            # per-scan activity trace (contextvar logger)
│       ├── settings_store.py     # runtime, UI-editable settings, per workspace
│       ├── git_rules.py          # which repo URLs and branches may reach git
│       ├── safe_http.py          # HTTP that reaches public addresses only
│       ├── crypto.py             # encryption of stored keys and tokens
│       ├── accounts.py           # users, sessions, workspaces, members, invites, secrets
│       ├── auth.py               # Sign in with GitHub, sessions, CSRF, get_principal
│       ├── rbac.py               # roles, permissions, ROUTE_PERMS, authorize
│       ├── audit.py              # the per-workspace audit log
│       ├── scan_context.py       # the workspace and credentials of the running scan
│       ├── pipeline.py           # orchestrates a scan; in-memory scan state
│       ├── scheduler.py          # APScheduler: per-repo interval jobs
│       ├── main.py               # FastAPI app + lifespan
│       ├── api/routes.py         # REST: account, settings, secrets, repos, scans
│       ├── api/workspaces.py     # REST: workspaces, members, invites, audit log
│       ├── api/admin.py          # REST: the platform admin
│       ├── api/common.py         # deleting a repo, workspace or user with its data
│       ├── ai/                   # AI providers (Anthropic, OpenAI-compatible), search, pricing
│       └── agents/
│           ├── clone.py          # shallow git clone + token injection/redaction
│           ├── parsers.py        # deterministic manifest parsers
│           ├── extraction_agent.py  # Agent 1: extraction (parsers + AI fallback)
│           ├── source_apis.py    # httpx helpers: endoflife/PyPI/npm/GitHub
│           ├── enrichment_agent.py  # Agent 2: EOL research (structured + AI)
│           └── transport.py      # dual-backend Claude CLI driver
│   └── tests/                    # pytest: flags, parsers, storage, AI merge, API
└── frontend/
    └── src/
        ├── main.tsx / App.tsx    # bootstrap + routes + layout
        ├── api.ts / types.ts     # typed API client + shared types
        ├── pages/                # RepoList, RepoDetail, ScanActivity, SnapshotHistory
        └── components/           # table, badges, modal, drawer, pickers, toggle
```

---

## 4. Backend modules & functions

### `config.py`
- `Settings` (pydantic-settings) — env/`.env` config: `gitlab_token`,
  `github_token`, `github_api_token`, `claude_cli_path`, `agent_model`,
  `agent_backend`, `use_extraction_agent`, `use_enrichment_agent`, `database_url`
  (required), `eol_data_dir` (legacy tree, importer only), `eol_soon_months`, `stale_years`, `web_search_max_uses`,
  `enrichment_batch_size`.
  - `data_dir` (property), `github_read_token()` (api token or fallback).
- `get_settings()` — `lru_cache`d singleton (cache cleared per test).

### `models.py` (all data contracts)
- **Extraction:** `Ecosystem` (enum), `Package` (`.key` → `ecosystem:name:version`),
  `PackageList`.
- **Enrichment:** `Verdict`, `Confidence`, `Source`, `Enrichment` (`.key`),
  `EnrichmentList`. `latest_version` is whatever upstream calls latest (**may be
  a prerelease**); `latest_stable_version` is the newest non-prerelease release,
  `None` when none exists. Both are stored; the new field defaults to `None`, so
  pre-existing snapshots load unchanged (no migration) — it
  populates on the next scan.
- **AI:** `AiVerdict` (**no date fields** — model never sets dates),
  `AiVerdictBatch`.
- **Flagging:** `FlagStatus` (enum, the 9 flags), `Flag`.
- **Registry:** `Repo`, `RepoCreate`, `RepoUpdate`. `branches: list[str]` — each
  branch scans/schedules independently with its own snapshot history; validated
  by `clean_branches` (strip, dedupe, at least one required) on all three models.
  A `model_validator(mode="before")` (`_migrate_legacy_fields`) accepts older
  `repos.json` entries with a single `branch` string (→ `branches=[branch]`) and
  a single `last_scan` timestamp (→ `last_scan_by_branch={branch: last_scan}`) —
  now only exercised by the importer reading old files.
  `image_tags: dict[str, str]` — user-supplied values for compose image-tag
  variables (`{"CORE_TAG": "acme/app:core-1.0"}`), shared across all of a repo's
  branches, validated by `clean_image_tags` on all three models (name
  `[A-Za-z_]\w*`, ref `[A-Za-z0-9][A-Za-z0-9._:/@+-]*`, blank values dropped) —
  these end up on a `docker run` command line, so validation happens at the edge.
  `detected_tag_vars: list[str]` is server-maintained (written by the pipeline
  each scan) so the UI can prompt for exactly the variables a repo uses.
  `last_scan_by_branch: dict[str, datetime]` — server-maintained, one entry per
  branch.
- **Snapshots/scan:** `SnapshotSummary`, `ScanState`, `ScanStatus` (now carries
  `branch: str`), `LogEntry`, `ScanLog` (also carries `branch: str`).

### `db.py`
- `get_engine()` (cached; `DATABASE_URL`, SQLite gets `PRAGMA foreign_keys=ON`),
  `session()` — context manager = **one transaction** (commit on success,
  rollback on exception), `is_postgres()`, `reset_engine()` (tests).
- `JSONType` = plain `json` (Postgres keeps the text, **key order included** —
  flag-count tags, scan-log entries, image tags are shown in stored order);
  `JSONBType` = `jsonb` on Postgres, used only for enrichment `data` (always
  re-parsed through Pydantic, and queryable).
- `upgrade()` — `alembic upgrade head`; run by `main.py` on every startup.

### `db_models.py` (ORM tables — nothing outside `storage.py` sees these rows)
- `repos` (+ `position` = registry order), `repo_branches` (cascade),
  `snapshots` (unique `(repo_id, branch, ts)`; no FK to `repos`), `packages`,
  `enrichments` (whole `Enrichment` in `data`, so new model fields need no
  migration; scalar copies indexed for querying), `flags`, `scan_logs`,
  `app_settings`, `scan_runs` (one fact row per scan attempt, failed ones
  included, for the admin analytics; no FK to `repos`, so usage history outlives
  a deleted repo).
- Accounts (Phase 2a): `users`, `sessions` (only a SHA-256 of each token),
  `workspaces`, `memberships` (one role per user per workspace),
  `workspace_settings`, `secrets` (encrypted), `user_activity_days` and
  `usage_events`. Phase 2b adds `invites` (a GitHub login, or a link's token
  hash) and `audit_events`. `repos.workspace_id` has no foreign key.
- `position` columns preserve list order exactly as the scan produced it.
  Package keys are **not unique per snapshot** (one package pinned in two
  manifests = two rows with the same key), so nothing is keyed on them.
- `snapshots.indexed` — listed in the history? A failed scan still stores its
  scan log under its `ts` but never becomes a listed snapshot.

### `storage.py` (the DB seam — same signatures as the old file layer)
- Helpers: `branch_dir_name(branch)` (filename-safe slug — CSV download names
  and the legacy layout), `new_snapshot_ts` (UTC, e.g. `2026-07-13T10-30-00Z`),
  `slugify_repo_id`.
- Registry: `load_repos`, `save_repos`, `get_repo`, `upsert_repo`, `delete_repo`
  (**also deletes every branch's snapshots**; child rows cascade).
- Index: `load_index(repo_id, branch)` (newest-first, `indexed` only),
  `save_index`, `upsert_index_entry`, `latest_snapshot_ts` — all branch-scoped.
- Snapshot payloads (all take `(repo_id, branch, ts, ...)`): `write/read_packages`,
  `write/read_enrichment`, `write/read_flags`, `write/read_scan_log`. A write
  replaces that payload wholesale and creates the snapshot row if needed.
  Session-level `put_*` / `get_or_create_snapshot` / `apply_summary` let the
  importer write one snapshot in one transaction.
- CSV: `build_report_csv` (+ `REPORT_COLUMNS`), `read_report_csv` — **built on
  request** from current rows (no stored `report.csv`), so it always reflects the
  latest re-flag; `None` for an unlisted snapshot.

### `legacy_files.py` / `import_files.py` (one-off migration)
- `legacy_files` reads the old `data/` tree (incl. the pre-multi-branch flat
  layout, attributed to the repo's first branch); never writes.
- `python -m app.import_files [--dry-run] [--data-dir PATH] [--overwrite-repos]`
  — idempotent: existing repos kept (unless `--overwrite-repos`), settings only
  filled where missing, existing `(repo, branch, ts)` skipped, one transaction
  per snapshot.

### No enrichment cache (by design)
There used to be a `cache.py` holding a TTL'd, cross-repo store of enrichment
results keyed `ecosystem:name:version`. It was **removed**: a scan must reflect
today's upstream state, and a stale entry silently masked both newly-announced
EOL dates and newly-released versions. It also made adding a field to
`Enrichment` a trap — existing entries deserialized with the new field defaulted
to `None`, so a fresh scan still served pre-field values until the TTL lapsed
(exactly what happened when `latest_stable_version` was introduced).

The remaining cost controls are per-scan dedup by `Package.key`, the
ambiguous-only `_needs_ai` gate, `enrichment_batch_size`, and
`web_search_max_uses`. Trade-off: the AI research pass re-runs for ambiguous
packages on every scan, so `ai_checked` is now a within-scan guard only.

### `flags.py` (deterministic engine — the actionable output)
- `add_months(date, n)` — month arithmetic with day-clamping.
- `newer_available(current, latest)` — PEP 440 compare with a string-inequality
  fallback. Used **only** by the DEPRECATED branch of the ladder.
- `upgrade_available(pkg, enrichment)` — the informational "a newer **stable**
  release exists" signal, compared against `latest_stable_version`. Deliberately
  stricter than `newer_available`: **no** string-inequality fallback, so an
  unparseable pair yields `False`. That strictness is what keeps apt rows quiet —
  Debian versions (`3.12.3-1ubuntu0.15`) don't parse, and a fallback would mark
  every one of the hundreds of image-inventory rows upgradable. **Not part of the
  severity ladder** — no `FlagStatus`, no effect on `evaluate()`.
- `evaluate(pkg, enrichment, release_date, *, today, soon_months, stale_years)` →
  `Flag`. Runs the severity ladder (ALREADY_EOL → … → OK); first match sets
  status, all reasons recorded. If no `release_date`, "before/soon" become "within
  N months from today". **UNKNOWN only when truly no signal**; a future EOL date →
  OK.
- `evaluate_all(packages, enrich_by_key, release_date, …)` — batch.
- `count_flags(flags)` — `{status: count}` for summaries.

### `scanlog.py` (activity trace)
- `ScanLogger` — accumulates `LogEntry`s (capped 5000); `emit(stage, msg, level,
  data)`, `to_model()`.
- Module: `start(repo_id, branch)` (creates + binds as current via a
  **contextvar** so any layer can log without passing a logger), `current()`,
  `get_repo_log(repo_id, branch)`, keyed internally by `(repo_id, branch)` tuple
  so each branch's live trace is independent; `clear_repo(repo_id, branch=None)`
  drops one branch's trace, or every branch's when `branch` is omitted (repo
  deletion); free function `emit(...)`.

### `settings_store.py` (runtime, UI-editable settings, per workspace)
- `get_runtime_settings(workspace_id=None)` returns the workspace's defaults
  overlaid with its `workspace_settings` rows. Without an argument it uses the
  current scan's workspace, or the local workspace outside a scan.
  - Defaults: the local workspace in the local mode takes them from the env.
  - Any other workspace starts with AI research off and no provider.
- `update_runtime_settings(patch, workspace_id=None)` persists an allowlisted
  set: `use_enrichment_agent`, `use_extraction_agent`, `ai_provider`, `ai_model`,
  `ai_base_url`, `search_provider`. It raises `ValueError` for a bad value (the
  route returns 422). Keys and tokens aren't settings: see `accounts.set_secret`.

### `accounts.py`, `auth.py`, `crypto.py`, `scan_context.py` (Phase 2a)
- **Modes.** `AUTH_MODE=none` runs every request as the built-in `local` user in
  the `local` workspace (migration 0008 creates both; `ensure_local_rows()`
  re-creates them on demand). `AUTH_MODE=github` is the public mode.
- **`auth.py`**
  - Sign in with GitHub, written directly on httpx:
    - `/api/auth/github/login` redirects with a random `state`, also kept in
      an HttpOnly cookie;
    - `/api/auth/github/callback` checks the state, swaps the code for a
      token, reads `/user`, applies the `MAX_USERS` cap (platform admins are
      exempt) and refuses disabled accounts;
    - then `accounts.sign_in_github_user` (a personal workspace on first
      sign-in, the OAuth token stored encrypted) and a session cookie.
  - `get_principal` is the dependency every route uses: user plus active
    workspace (the `X-Workspace` header, defaulting to the personal one; a
    workspace you're not in is a 404).
  - `CsrfMiddleware`: in the public mode, data-changing `/api` calls need
    `X-Requested-With: packages-tracker`, and an Origin matching
    `PUBLIC_BASE_URL` when one is sent.
- **`accounts.py`** holds:
  - users, and sessions (only a SHA-256 of each token is stored);
  - workspaces and memberships;
  - per-workspace settings, and secrets (encrypted; `secret_hints` is all the
    API ever returns about them);
  - `touch_activity` (one `user_activity_days` row per user per day) and
    `record_event` (`usage_events`). Neither can break a request.
  - `delete_workspace` and `delete_user`, used by `DELETE /api/me`.
- **`crypto.py`** stores values as `enc:v1:` Fernet tokens.
  - The key is derived with HKDF from `APP_SECRET_KEY`, and rotates through
    `APP_SECRET_KEY_PREVIOUS`. The public mode won't start without it; a local
    install generates `data/secret.key`.
  - Legacy plain-text values still decrypt, and per-repo tokens are
    re-encrypted at startup (`storage.encrypt_plaintext_tokens`).
- **`scan_context.py`**: the pipeline binds a `ScanContext` (the repo's
  workspace and creator) for every scan. Everything inside a scan gets its
  credentials from there:
  - the AI provider (`ai.config.resolve`), from the workspace's settings and
    secrets;
  - clone tokens, from the workspace secret, else the env (local mode only);
  - the GitHub API token, from the workspace secret, else the repo creator's
    sign-in token, else the env (local mode only).

  The public mode never uses the server's own tokens.

### `rbac.py`, `audit.py`, `api/workspaces.py`, `api/admin.py` (Phase 2b)
- **Roles.** `ROLES` = viewer < editor < admin < owner. `ROLE_PERMS` gives each
  role a strict superset of the one below; `can`, `permissions` and `min_role`
  read it. `Principal.role_in(workspace_id)` is the caller's role (always
  `owner` in the local mode).
- **One table, one check.** `ROUTE_PERMS` maps every `(method, path)` to a
  `Perm`, `SIGNED_IN` or `PLATFORM_ADMIN`. Each API router has
  `dependencies=[Depends(rbac.authorize)]`, which runs before the handler:
  - the role counts in the repo's workspace for `{repo_id}` paths, the named
    workspace for `{workspace_id}` paths, and the active workspace otherwise;
  - not a member there: 404. Member without the permission: 403, naming the
    role needed;
  - `PLATFORM_ADMIN` routes are a 404 for everyone else;
  - a route with no `ROUTE_PERMS` entry is a 500. A new route must be added
    to the table, and `test_every_api_route_declares_a_permission` fails
    until it is.
- **Rules a permission can't express** live in `api/workspaces.py`:
  - only Owners make, change or remove Owners, or invite one;
  - the last Owner can't leave, step down or be removed;
  - a personal workspace takes no invites and can't be transferred, left or
    deleted;
  - an invitee who is already a member keeps the higher role.
- **Invites** (`accounts.create_invite` and friends): addressed to a GitHub
  login, or a one-time link whose token is returned once and stored as a
  SHA-256. Pending for 7 days (`INVITE_DAYS`). Used, revoked or expired: 410.
  Someone else's: 404.
- **`audit.py`**: `record(action, workspace_id=..., actor=..., ...)` writes one
  `audit_events` row and never raises. The actor is a user, `scheduler` or
  `platform_admin`. Details hold the values a change set, except secrets:
  a secret's name only, and a repo token as `set` / `removed`. `events()` pages
  newest first by id. Platform-level actions (deleting a workspace or a user)
  are stored with no workspace.
- **`api/common.py`** deletes things with everything kept about them:
  `forget_repo`, `delete_workspace_everything` and `delete_user_everything`.
  The last one also deletes the team workspaces the user was the only member
  of. Where the user was the last Owner of a team with other members, it
  makes the highest-ranked member Owner and returns those promotions for the
  caller to audit. Both refuse with 409 while an affected repo is scanning.
- **`api/admin.py`**: users (list; disable, which ends their sessions; enable;
  delete) and workspaces (list with counts; one workspace's members and repos,
  recorded in that workspace's log as `admin.view_workspace`). No scan results
  or keys.

### `pipeline.py` (scan orchestration)
- In-memory state: `_status` (`ScanStatus` keyed by `(repo_id, branch)` tuple),
  `_locks` (asyncio lock per `(repo_id, branch)` = overlap guard — one branch
  scanning doesn't block another branch of the same repo).
- `get_status(repo_id, branch)`, `clear_repo_state(repo_id, branch=None)` (one
  branch, or every branch on delete), `_set`, `_lock`, `is_scanning`.
- Helpers: `_now`/`_since` (timings), `_extract_summary` (by-ecosystem/by-file),
  `_enrich_summary` (by-verdict).
- **`run_scan(repo, branch, *, use_extraction_agent=None, trigger="manual")`** — the core: overlap
  guard on `(repo.id, branch)` → binds a `ScanLogger` → clone `branch` → extract →
  enrich → flag → write snapshot + index (all under `branch`'s slug) → touch
  `repo.last_scan_by_branch[branch]`. Emits scanlog events at each
  stage. Catches everything; degrades, never raises.
- **AI is optional.** `enrich()` fills in an `enrichment_agent.AiOutcome`. Its
  status is `off`, `ok`, `partial`, `failed`, `quota` or `no_key`, and it's
  stored on the snapshot as `ai_status` / `ai_detail`.
  - A failed AI batch never raises. Its packages keep their structured verdicts,
    and the snapshot is **not** marked degraded.
  - The pass stops early on errors that would fail every remaining batch: a
    rejected key, an exhausted quota or a missing CLI.
- In its `finally`, the scan persists the scan log and writes one `scan_runs`
  row (`_record_run`):
  - why it ran (`trigger`) and the outcome (`done` / `degraded` / `error`);
  - seconds per stage, the stages with issues and the first error;
  - package and flag counts;
  - AI usage;
  - failed outside lookups, from `source_apis.start_error_tally()`.

  A failed write of that row is only logged.

### `scheduler.py` (APScheduler, in FastAPI lifespan)
- `start`/`shutdown`; `_scan_job(repo_id, branch)` → `run_scan`.
- `sync_repo(repo, *, scanning_now=())` — adds/replaces one interval job **per branch**
  (`_job_id(repo_id, branch)` = `scan:<repo_id>::<branch>`), and removes jobs for
  branches no longer in `repo.branches` (diffs against the `scan:<repo_id>::`
  prefix, so editing a repo down to fewer branches actually unschedules the
  dropped ones).
  - A never-scanned or overdue branch gets a catch-up 30 s later; that's how a
    branch added by an edit gets its first scan.
  - Branches in `scanning_now` (a new repo, an uploaded zip: their scan starts
    right away) are scheduled one interval out instead. Otherwise small repos
    were scanned twice within a minute.
  - `_scan_job` skips a branch that's already scanning, without an audit entry.
- `unsync_repo(repo_id)` — removes every branch's job for that repo.
- `sync_all()` (reconcile all repos' jobs), `trigger_now(repo, branch)`
  (fire-and-forget task for one branch).

### `main.py`
- `lifespan` — runs `db.upgrade()` (pending migrations), re-creates the local
  user and workspace, checks that encryption works (the public mode fails here
  without `APP_SECRET_KEY`), encrypts any per-repo token still in plain text,
  then starts/stops the scheduler. If Postgres is down, startup fails and
  systemd retries.
- `app` — FastAPI + CORS (Vite origins) + `CsrfMiddleware` + the routers
  (`auth`, then `routes`, `workspaces` and `admin`, all three behind
  `rbac.authorize`); `GET /api/health`.

### `api/routes.py` (REST — reads/writes via `storage.py`)
- Every route takes `me: Principal = Depends(get_principal)`, and the router's
  `rbac.authorize` has already checked the caller's role. Repos are scoped to
  the caller's workspaces, and in the public mode new repos get random IDs
  (`storage.new_repo_id`).
- Helpers:
  - `_mask` (hides the token override);
  - `_require_repo(repo_id, me)`: 404 if the repo is missing *or* in a
    workspace the caller isn't in;
  - `_require_branch(repo, branch)`: 404 if the repo doesn't track that
    branch;
  - `_event` (a usage event) and `_audit` (an audit entry in the repo's or
    the active workspace).
- Settings and secrets: `GET/PUT /api/settings` (per workspace; secret hints
  only for roles with the `secrets` permission) and `PUT/DELETE
  /api/secrets/{name}` (write-only).
- Account: `GET /api/me` (including `permissions`, which the UI uses to
  disable what the caller can't do), and `DELETE /api/me` (public mode; 409
  while the caller is the last Owner of a team with other members).
- Repos: `list_repos` (adds `branches_info: list[{branch, latest_snapshot,
  flag_counts, scan_state, last_scan}]`, one entry per branch), `create_repo`
  (derives id, schedules every branch, triggers a first scan **per branch**),
  `get_repo`, `update_repo` (PUT `release_date` → `_reflag_latest` for every
  branch, **instant re-flag**; changing `branches` reschedules via `sync_repo`),
  `delete_repo` (409 if any branch is scanning; else remove data + unschedule +
  clear in-memory for every branch).
- Scans (all take a required `branch` **query param**, never a path segment —
  branch names can contain `/`): `scan_now` (202), `scan_status`, `scan_log`
  (live or `?ts=` persisted).
- Snapshots (same `branch` query param): `list_snapshots`, `components` (merged
  packages+enrichment+flags with filters), `report_csv`, `diff`
  (changed/added/removed between two snapshots, in snapshot order). `components` also emits
  `update_available`, computed **live** via `flags.upgrade_available` rather than
  stored — it's a pure function of two already-persisted values, so existing
  snapshots gain it with no re-scan (same spirit as `_reflag_latest`).

### `agents/clone.py` and `git_rules.py` (user input that reaches git)
- `shallow_clone(url, branch, token_override)` returns a `Clone` context manager
  (`git clone --depth 1 --single-branch --branch <b> -- <url> <dest>`).
  `_token_for` picks the token by URL host; `_auth_url` injects it (`oauth2:<t>@`
  for GitLab, `<t>@` for GitHub); `_redact` scrubs tokens and userinfo from
  errors. Raises `CloneError` on failure.
- `git_rules.check_repo_url` / `check_branch` run before git sees either value,
  both in `RepoCreate`/PUT (a 422) and again at clone time. They reject:
  - option-like or control characters;
  - anything but http(s), or https only in the public mode;
  - credentials in the URL;
  - hosts not on `GIT_HOST_ALLOWLIST` (with an empty allowlist, the local mode
    accepts any host);
  - IP-address hosts, in the public mode;
  - names git's ref-name rules forbid.
- Git always runs with `GIT_ALLOW_PROTOCOL` (http:https, or https only in the
  public mode), `GIT_TERMINAL_PROMPT=0` and a `CLONE_TIMEOUT_SECONDS` timeout.
- In the public mode (`AUTH_MODE=github`), `require_public_address` also refuses
  hosts that resolve to private, loopback, link-local or CGNAT addresses, and git
  gets `-c http.followRedirects=false`. Without these rules, a "URL" like
  `--upload-pack=<cmd>` ran a command, and `file:///…` copied server files into a
  scan.
- Upload repos never clone. Their "branches" are free-form version labels, so
  the branch rules apply to git repos only.

### `agents/parsers.py` (deterministic — run first, cover the common formats)
- `parse_requirements` (pinned `==` and unpinned names), `parse_package_json`
  (deps/devDeps/optional, strips `^~`), `parse_package_lock` (v2/v3 `packages` +
  v1 `dependencies` → transitive), `parse_dockerfile` (`FROM` base images with
  multi-stage/alias/`scratch` handling + `_split_image`; explicit `apt-get install`
  packages, **stripping `#` comments before joining `\` continuations**),
  `parse_pyproject` (PEP 621 + poetry), `parse_compose(text, rel, tag_vars)`
  (docker-compose service `image:` refs → `docker-image` packages, via
  `compose_image_refs` — the single source of truth also used by
  `image_inspect.detect_images`). `parse_file(path, rel, tag_vars)` routes by
  filename.
  Compose interpolation: `resolve_image_ref(ref, tag_vars)` substitutes
  `${VAR}` / `${VAR:-default}` / `$VAR` from the repo's `image_tags` and returns
  `None` if any variable has no value (unresolved refs stay skipped — the tag isn't
  knowable from the repo alone); `compose_tag_vars(text)` lists the variable names
  a compose file needs.
  Vendored (`lib/`): `parse_dist_info` (name+version from `*.dist-info` METADATA /
  dir name, bogus versions rejected), `parse_source_package_dir` (source-only
  package folder → name, best-effort digit-validated `__version__`).

### `agents/extraction_agent.py` (Agent 1)
- `walk_manifests`, `_is_manifest` (skips `node_modules/.git/…`), `dedup`
  (prefer versioned/direct).
- `extract_vendored(clone_path)` — dependencies committed under `lib/` folders
  (for repos that ship dependencies instead of a `requirements.txt`): `*.dist-info` → authoritative
  name+version; other package folders → name only (best-effort `__version__`,
  digit-validated). Uses `parsers.parse_dist_info` / `parse_source_package_dir`.
- `extract_deterministic(clone_path, tag_vars=None)` — manifest parsers +
  `extract_vendored`.
- `extract_with_agent(clone_path, known_files)` — AI fallback for unknown formats
  (Read/Glob/Grep, JSON → `PackageList`, retry, degrade). Logs to scanlog.
- **`extract(clone_path, *, use_agent=False, tag_vars=None)`** — deterministic +
  optional AI. `tag_vars` is `repo.image_tags`, forwarded to the compose parser.

### `agents/image_inspect.py` (dpkg parity, auto-detected per repo)
- `detect_images(clone_path, tag_vars=None)` — auto-detects images to inventory
  from the clone: runtime `image:` refs in `docker-compose*.y*ml`/`compose.y*ml`
  (via `parsers.compose_image_refs`, pyyaml) first, then real `FROM` base images
  from Dockerfiles (`_images_from_dockerfile`, skips stage aliases/`scratch`).
  Deduped, order-preserving, `${VAR}` tags resolved from `tag_vars` (still-unresolved
  refs skipped), filtered through `validate_image_ref`.
- `detect_tag_vars(clone_path)` — the compose image-tag variable names the repo
  uses; the pipeline logs which are missing and stores them on
  `repo.detected_tag_vars` so the UI can prompt for them.
- `validate_image_ref` (blocks flag-injection refs), `parse_dpkg_output` (pure),
  `list_image_packages(image)` — `docker run --rm --network none --entrypoint ""
  <img> dpkg-query -W …` → apt Packages with exact versions, `is_direct=False`,
  `source_file="image:<ref>"`. Raises `ImageInspectError`; pipeline degrades.
- The pipeline calls `detect_images` inside the clone `with`-block (needs files
  on disk), then inventories each detected image after. `repo.inspect_image`
  remains a manual override (API-only) that short-circuits auto-detection.
- **Docker is a capability, not a requirement.** `inspect_mode()` returns
  `(enabled, reason)` from `IMAGE_INSPECT`:
  - `auto`, the default, uses Docker when `docker version` answers
    (`docker_available()`, cached for 10 minutes);
  - `on` always tries, so a missing Docker degrades the scan;
  - `off` never uses Docker;
  - the public mode is always off.
- When Docker is off, the pipeline skips the image inventory and the Docker-based
  apt-version fallback, logs a line saying why, and does **not** mark the scan
  degraded. Base-image EOL and Dockerfile apt packages still come from the
  HTTP-only apt-index path.

### `agents/source_apis.py` (deterministic httpx lookups)
- `query_endoflife(client, name, version)` — resolves the product slug
  (`EOL_ALIASES` + products index via `resolve_eol_slug`/`endoflife_products`),
  matches the release cycle, returns eol/eos dates, latest, citation.
- **Latest-stable helpers** (pure, unit-tested without HTTP):
  `_overall_latest(releases, *, stable_only=False)` — `stable_only` skips
  `Version.is_prerelease` and returns `None` if no stable exists (no prerelease
  fallback — that's what plain `latest` is for), so existing callers/tests are
  unaffected; `latest_stable_pypi(releases)` — highest non-prerelease,
  non-yanked from PyPI's already-fetched `releases` map (no extra HTTP);
  `latest_stable_npm(versions)` — highest version with no semver `-` prerelease
  suffix. **No semver dependency added**: the hyphen convention is the whole
  test, and `packaging` parses the remaining cores. Each `query_*` returns the
  stable value as an *additional* key; the pre-existing `latest_version` keys are
  untouched.
- `query_pypi` (latest, last release, yanked, inactive classifier, repo URL),
  `query_npm` (latest, deprecated, time, repo URL), `query_github_repo` (archived,
  pushed_at, `moved_to` from an archived repo's "moved to <github url>"
  description; uses `scan_context.github_api_token()`. If GitHub rejects that
  token (401: revoked or expired), the scan drops it, retries without one,
  warns once in the scan log and counts `api.github.com:auth_rejected`).
  `_apply_github` follows one move and
  ignores `archived` when the registry released after the repo's last push. `github_slug`/`pick_github_repo` normalise
  registry links (incl. `/issues`-style sub-pages) to `owner/repo`.
  `query_github_security_policy` reads SECURITY.md from raw.githubusercontent.com
  (no API quota); `parse_security_policy` + `security_support_for` map the pinned
  version to a table row (ranges/wildcards first, else longest shared version
  prefix) → `Enrichment.security_supported` / `security_policy_row`. A ❌ row
  sets verdict `eol` and flags `ALREADY_EOL`; the AI pass can't overrule it.
  No vulnerability/advisory lookups — SECURITY.md is read for EOL only. `make_client()`, `_to_date`, `_get_json` helpers.

### `agents/enrichment_agent.py` (Agent 2)
- **Structured pass:** `_enrich_one(client, pkg)` — endoflife + registry (+ GitHub)
  → `Enrichment`; records the endoflife match/citation **even when there's no EOL
  date** (dates stay structured-only). `_apply_github`, `_summarize`, `_conf_rank`.
- **`enrich(packages, *, today)`** — dedups by `Package.key`, runs the structured
  pass for every unique package (no cache), then the AI pass if enabled.
- **AI research pass:** `_needs_ai(e, stale_years)` (candidate selection —
  unknown / low-confidence / stale-no-endoflife), `_chunk`, `_signal_line`
  (feeds the model our structured data), `_run_ai_batch` (WebSearch/WebFetch,
  JSON → `AiVerdictBatch`, retry, degrade), `_merge_ai` (**never sets dates**;
  adopts verdict/successor/summary, bumps confidence, dedups sources), `_ai_pass`
  (batches candidates, merges, logs per-package verdicts).

### `agents/transport.py` (the one place that talks to the model)
- **`run_text_agent(prompt, *, cwd, allowed_tools, append_system, mcp_config,
  model, add_dirs, timeout)`** returns an `AgentResult`. It dispatches on the
  configured provider (`ai.config.resolve()`):
  - `claude-cli` goes to `_run_cli`, which picks a backend by `agent_backend`:
    `subprocess` (the default), `sdk`, or `auto` (sdk, falling back to
    subprocess);
  - `anthropic` goes to `ai.anthropic_provider`;
  - `openai-compatible` goes to `ai.openai_compat`.
- Raises `AiConfigError` when no provider is usable. An API provider asked for
  file tools (the AI extraction agent) is refused, since only the CLI can read
  the clone.
- `resolve_cli_path()` looks in order at the explicit setting, PATH, then the
  newest VS Code / Cursor extension binary, chosen by version number
  (`claude.exe` on Windows).
- `_is_root()`: root can't use `bypassPermissions`, so it falls back to
  `--allowedTools`.
- `_run_subprocess(...)` drives `claude --print --output-format stream-json
  --input-format stream-json …`. It parses events, collects the assistant text
  and the final result, and emits `agent` tool events to the scanlog. It
  counts WebSearch/WebFetch from the stream, because the CLI reports 0 for
  them in `usage.server_tool_use`.
- `_run_sdk(...)`: the same contract, through `claude_agent_sdk.query()`.

### `ai/` (AI providers, Phase 1 of the cloud plan)
- `config.py`: `resolve()` turns `AI_PROVIDER` and the related settings into
  an `AiConfig`, or `(None, why)` (see the README configuration table). Without
  `AI_PROVIDER`, the local mode uses the Claude CLI, and the public mode has no
  provider. Presets: openai, gemini, groq, openrouter.
- `anthropic_provider.py` (official `anthropic` SDK):
  - uses the server-side `web_search` / `web_fetch` tools; Haiku gets the
    basic versions, Opus and Sonnet 4.6+ get the `_20260209` ones;
  - resumes `pause_turn`, and treats `refusal` as a failed batch;
  - sends `fallbacks="default"` for Fable 5.1, Opus 5.5 / 5 and Sonnet 5.5;
  - sums usage, estimates cost (`pricing.py`), and collects every URL search
    and fetch returned;
  - `list_models()` checks a key at no token cost.
- `openai_compat.py` (official `openai` SDK): a tool loop with web search the
  app runs itself.
  - `web_search` goes through `search.py` (Tavily or Exa, with the user's
    key), capped at `WEB_SEARCH_MAX_USES`.
  - `fetch_url` only accepts URLs a search returned and only reaches public
    addresses, re-checking every redirect, with time and size caps.
  - When the rounds run out, `tool_choice="none"` asks for the answer.
  - In the public mode a custom base URL is a URL a user typed, so the SDK's
    HTTP client is `safe_http.public_client()` (see below). The presets'
    URLs are our own constants. Page fetches always use it.
  - `list_models()` backs Test connection. It checks OpenRouter's key with
    `GET /key` (its model list needs no key), keeps only tool-capable models
    where the listing says, and puts free ones first.
- `probe.py`: `test_connection(provider, api_key, base_url)` for
  `POST /api/settings/ai-test`. Listing models costs no tokens with any
  provider.
- **`safe_http.py`** (not in `ai/`): `PublicOnlyTransport` resolves the host
  itself on every request and refuses unless every address is public
  (`git_rules.public_addresses`). It then connects to the address it checked,
  keeping the hostname for TLS and the Host header, so a DNS answer that
  changes after a check (DNS rebinding) can't redirect the request.
  `public_client()` doesn't follow redirects or use environment proxies.
  `settings_store.check_ai_base_url` checks the same things when the URL is
  saved: https only, a hostname rather than an IP, no credentials in the URL.
- `citations.py`:
  - every deprecated or eol verdict and every successor needs a citation,
    from every provider;
  - when the provider exposes what its searches returned (the two API
    adapters), each citation must be one of those URLs;
  - an unbacked verdict is dropped, so the package keeps its structured
    result.
- `errors.py`: typed `AiAuthError`, `AiQuotaError`, `AiTimeoutError`,
  `AiConfigError` and `AiProviderError`, which drive `ai_status`.
  `redact_keys()` scrubs key-shaped strings from every logged error.
- `result.py`: `AgentResult` (including `seen_urls`; `cost_usd` None means
  "not estimated"), `sum_tokens` (which counts unpriced calls in
  `cost_unknown`), and the tool-event logging.
- The eval in `backend/evals/eol_research/` scores a provider against 18 known
  packages. Its README has how to run it and the results so far.

---

## 5. Frontend

- **`main.tsx`** — mounts React, `QueryClientProvider`, AntD `ConfigProvider`,
  `BrowserRouter`.
- **`App.tsx`** — two kinds of routes:
  - open to anyone: `/login`, `/privacy`, `/terms`;
  - everything else, behind `SessionGate`: the header and `/`,
    `/repos/:id`, `/repos/:id/activity`, `/repos/:id/history`, `/settings`,
    `/members`, `/audit`, `/admin` and `/invite/:token`.

  The header has the menu, the workspace switcher, invites for you, the theme
  toggle and the account menu (the last three in the public mode only). The
  active branch on the repo pages is a `?branch=` **query param**, not part
  of the route path (branch names can contain `/`), read/written via
  `useSearchParams`.
- **`session.tsx`** — `SessionGate` loads `/api/me`:
  - no session (401): a redirect to `/login?next=<this page>`;
  - a remembered workspace you've since left (404): forgotten, and the
    server falls back to the personal one;
  - otherwise it provides `useSession()`: `me`, `can(perm, workspaceId?)`,
    `why(perm, workspaceId?)` (the tooltip naming the role a control needs),
    `switchWorkspace`, `signOut`.

  Any later 401 from another call re-checks `/api/me`. A 401 from `/api/me`
  itself must not, or a signed-out page would re-check forever.
  `safeNext` keeps `next` on this site.
- **`lib/permissions.ts`** — `can`, `minRole`, `needsRole`, `roleIn`. The role
  table comes from `/api/me` (`role_permissions`), so it can't drift from the
  server's. A repo's page asks about the repo's own workspace (`roleIn`),
  since a link can open it while another workspace is active.
- **`lib/aiSettings.ts`** — the providers the Settings page offers, and
  `aiProblems()`: what stops AI research from running. "invalid" blocks
  saving; "missing" only means research won't run yet. A saved key counts
  only for the provider it was saved for.
- **`api.ts`** — typed `fetch` wrapper + `api.*` methods. Every request sends
  `X-Requested-With` and, once one is chosen, `X-Workspace`. The active
  workspace is kept in `localStorage`, and switching clears the query cache:
  query keys don't include the workspace. Every per-branch method
  (`scanNow`, `scanStatus`, `scanLog`, `snapshots`, `components`,
  `reportCsvUrl`, `diff`) takes `branch` as an explicit argument and sends
  it via `URLSearchParams` (never string-concatenated — branch names need
  URL-encoding).
- **`types.ts`** — TS mirrors of the backend models + `FLAG_ORDER`. `Repo.branches:
  string[]`, `Repo.branches_info?: BranchInfo[]` (per-branch scan_state/
  flag_counts/latest_snapshot/last_scan, as returned by `GET /api/repos`).
- **Pages:**
  - `RepoList` — cards with one `BranchRow` per tracked branch (branch tag,
    `ScanStatusPill`, `FlagSummary`, last-scan time; click → that branch's
    detail page), `ImageTagSummary` (`n/m set` for compose variable tags,
    still repo-level), add/edit modal, delete, 5s poll.
  - `RepoDetail` — **the repo-level branch filter**: a `Select` next to the repo
    id, sourced from `repo.branches` (fetched via `api.getRepo`), synced to
    `?branch=` in the URL. Every query below it (scan-status, snapshots,
    components) is keyed on `[id, branch, ...]` so switching branches
    re-fetches everything for that branch; an effect normalizes the URL to a
    real branch (fills in the default, or corrects a stale `?branch=` from an
    old link). Snapshot picker, scan-now, CSV, and the Activity/History links
    (which carry `?branch=` forward) are all scoped to the selected branch.
  - `ScanActivity` / `SnapshotHistory` — same branch-selector pattern as
    `RepoDetail` (own `Select`, own `?branch=` sync), since both show
    branch-scoped history reached either directly via URL or via the links from
    `RepoDetail`. `ScanActivity` also shows that scan's AI usage (`AiUsageCard`).
  - Repos show `repo.name` (`format.repoName`); the public mode's ids are
    random. Add, edit, delete and scan buttons are disabled by role, with a
    tooltip naming the role needed.
  - `Login` — Sign in with GitHub, the meaning of each `?error=` code, and the
    "sign-ups are full" page.
  - `Settings` — AI research (provider, key, Test connection, model, web
    search) and the git tokens. Keys are write-only: only a hint shows. Read
    only below Admin.
  - `Members` — members and roles, invites by login or one-time link, pending
    invites, and rename, transfer, delete and leave. A personal workspace
    shows how to create a team instead.
  - `Audit` — the workspace's log, paged by "Load older entries".
  - `Admin` — users and workspaces, for platform admins only (a 404 page for
    everyone else).
  - `Invite` — accepts a one-time link (`/invite/:token`) and switches to
    that workspace.
  - `Legal` — the privacy policy and terms, drafts for the operator to
    review. They read `OPERATOR_NAME` / `CONTACT_EMAIL` via `/api/info`.
- **Components:** `ComponentTable` (filters + `hasTransitive`-gated Direct-only
  toggle + `hasUpdates`-gated Updates-only toggle; a **Latest stable** column
  carrying the `↑ update` tag, sortable to float upgradable packages to the top),
  `FlagBadge`, `ScanStatusPill`, `RepoFormModal` (branches as an AntD
  `Select mode="tags"` — free-form entry, `type: "array", min: 1` validation;
  Docker image tags as a `VAR=image:tag` textarea — `tagsToText`/`textToTags`,
  prefilled from `detected_tag_vars`, client-side validation mirroring
  `clean_image_tags`), `CitationsDrawer`, `SnapshotPicker`, `HeaderControls`
  (workspace switcher, invites for you, account menu with sign out and delete
  account), `CreateWorkspaceModal`, `AiUsage`, `Brand`.

Dev server proxies `/api` → backend (`VITE_API_TARGET`, default `:8000`).

---

## 6. Data flow of one field (worked example)

`react 19.1.0` in `package.json`:
1. `parse_package_json` → `Package(name=react, version=19.1.0, ecosystem=npm,
   is_direct=true)`.
2. `_enrich_one`: `query_endoflife("react","19.1.0")` matches cycle 19 (no EOL
   date) → keeps citation + `latest=19.2.7`; `query_npm` adds last_release →
   `verdict=supported, confidence=medium`.
3. `_needs_ai` → false (has endoflife source) → no AI call.
4. `flags.evaluate` → no eol/eos, verdict supported → **OK**.
5. Written to the `packages` / `enrichments` / `flags` tables; served by
   `components` endpoint; rendered in the table + drawer.

---

## 7. Extending the system
- **New ecosystem/manifest:** add a `parse_*` in `parsers.py` and route it in
  `parse_file`. Add an `Ecosystem` enum value if needed.
- **New EOL source:** add a `query_*` in `source_apis.py`, call it in
  `_enrich_one`, append a `Source`.
- **New flag rule:** add a `FlagStatus` and a `mark(...)` block in `flags.py`
  (mind the severity order) + a colour in `FlagBadge`/`flags` map.
- **Swap the AI model/provider:** change `agent_model`, or add a backend in
  `transport.py` — nothing else changes.
- **Schema change:** edit `db_models.py` → `cd backend && alembic revision
  --autogenerate -m "..."` → review the file (autogenerate renders a `now()`
  server default as text — use `sa.func.now()` so SQLite tests work) →
  `alembic upgrade head` (or just restart; startup upgrades).
  `test_db_storage` checks migrations match the models.
- **Tune AI cost:** `_needs_ai` (candidate scope), `enrichment_batch_size`,
  `web_search_max_uses`, and the repo's scan interval (there is no cache, so
  interval length directly sets how often research re-runs).

---

## 8. Testing
```bash
cd backend && . .venv/bin/activate && pytest
```
**On Windows**, or on any machine without a working Python 3.11, use
[uv](https://docs.astral.sh/uv/). It brings its own Python 3.11 and leaves the
system one alone:
```bash
python -m pip install --user uv
python -m uv python install 3.11
cd backend
python -m uv venv --python 3.11 .venv
python -m uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
.venv/Scripts/python.exe -m pytest
```
(`.venv/bin/python` on Linux/macOS.) The tests never load `claude-agent-sdk`,
which bundles the whole Claude Code CLI. On a slow connection, install
everything else first and add it later. Frontend: `cd frontend && npm ci && npm
test && npm run build`.

Phase 0 of the cloud plan added these tests:
- `test_clone_rules.py`: URL and branch rules, the git invocation, and the
  public-address check;
- `test_ai_optional.py`: AI failures keep every structured verdict;
- `test_docker_capability.py`: no Docker means skipped, not degraded;
- `test_scan_runs.py`: one fact row per scan, plus the failed-lookup tally.

Phase 2 added:
- `test_auth.py` and `test_accounts.py`: sign-in with GitHub mocked, cookies,
  the user cap, CSRF, workspace isolation (404s), secrets and key rotation;
- `test_rbac.py`: every route declares a permission, every role against
  every kind of route, the Owner and personal-workspace rules, invites,
  account deletion, the audit log, and the platform admin.

Phase 3 added:
- `test_settings_ai.py`: the base URL rules, the public-only transport
  (including a DNS answer that changes after saving), Test connection and
  its model lists, and each scan's AI usage in the scan log;
- frontend: `lib/permissions.test.ts`, `lib/aiSettings.test.ts` (the Settings
  form's validation), `session.test.ts` (`safeNext`), more of `api.test.ts`
  (the workspace header, ended sessions), and `App.test.tsx`, which renders
  the whole app against a fake backend: signed out, a session ending mid-use
  (without bouncing between pages or re-checking forever), the sign-up cap
  page, and a Viewer's disabled controls. It has no testing-library: React's
  `act` and `createRoot` do the rendering. The first render of the whole app
  is slow in jsdom, so it raises the test timeout.

Each test gets a throwaway SQLite DB (schema from the ORM models);
`TEST_DATABASE_URL=postgresql+psycopg://…` runs them on Postgres instead (via
migrations, tables truncated between tests — use a scratch DB).
Covers: flag boundaries (incl. the release-date core case), all parsers (incl. the
Dockerfile-comment regression), DB storage (order, duplicate keys, delete-wipes-data,
migrations = models) + the legacy-file importer (incl. legacy-branch/flat-layout data), per-scan lookup dedup, `_needs_ai`/
`_merge_ai`/`_ai_pass` (mocked agent), the latest-stable helpers + `upgrade_available`
(incl. the Debian-version regression guard), per-branch APScheduler job add/remove
(`test_scheduler.py`), and the REST surface end-to-end via `TestClient`
(`test_api_branches.py` — required/validated `branch` query param, 404 on an
untracked branch, multi-branch create/update/delete; `test_api_components.py` —
`latest_stable_version`/`update_available` in the components response + CSV
columns; `scheduler.trigger_now` is monkeypatched so neither spawns a real
background clone). AI/network paths are mocked in CI; live smoke (e.g. `cybox`)
is run manually.

---

## 9. Known environment constraints
- **Python 3.12 on this host is broken** — its asyncio event loop segfaults
  (GP fault), which kills uvicorn/agents. Use **3.11**. `pyproject` targets
  `>=3.11`. Both `venv`s here can't self-bootstrap pip → create with
  `--without-pip` and `get-pip.py`.
- **Runs locally; Docker only for PostgreSQL** — the AI needs the machine's
  Claude Code login. Single instance for now; multi-instance, retention and
  backups are planned (DB phases 2–3).
  APScheduler is in-process, so scheduled scans only fire while uvicorn runs.
- **Port 8001** — the backend runs on 8001 rather than uvicorn's default 8000;
  the Vite proxy defaults to 8000, so set `VITE_API_TARGET` accordingly.
- **Run it as a service, not from a terminal.** Both halves are managed by
  systemd (`packages-tracker-backend`, `packages-tracker-frontend`; units version-controlled
  in [`deploy/`](../deploy/)). Started from a VS Code integrated terminal they sit
  in that terminal's foreground process group, so closing the tab / reloading the
  window / dropping the SSH connection `SIGHUP`s them — and because APScheduler is
  in-process, interval scans then stop firing silently. See
  [RUNNING-AS-A-SERVICE.md](RUNNING-AS-A-SERVICE.md) for commands, the two
  load-bearing env vars (`VITE_API_TARGET`, `HOME`), and troubleshooting.
  The backend service runs **without `--reload`**, so
  `systemctl restart packages-tracker-backend` after backend edits.
