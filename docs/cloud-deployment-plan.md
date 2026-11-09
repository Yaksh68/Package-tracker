# Cloud deployment: public multi-user Packages Tracker (implementation plan)

> **Status:** Phases 0 and 1 are merged (2026-11-07), and so are 2a, 2b and 3 (2026-11-08). Phase 4 is done on `phase-4-limits` (2026-11-08). Each phase ships as its own branch and pull request.
>
> **Decisions locked (2026-11-07):**
> 1. **Public multi-user service.** People sign in, work in **workspaces** (teams), and bring their own AI keys.
> 2. **RBAC.** Each workspace has the roles Owner, Admin, Editor and Viewer. A separate **platform admin** role is for whoever runs the service.
> 3. **Sign in with GitHub.** Sign-up is open up to a user cap that the platform admin sets.
> 4. **AI is optional.** Every scan runs fully on the structured sources. AI only fills in ambiguous packages, and an AI failure never throws away the non-AI results.
> 5. **Several AI providers, including free tiers.** There are two adapters. One is a native Anthropic adapter. The other is an OpenAI-compatible adapter (OpenAI, Gemini, Groq, OpenRouter, any custom URL) that runs its own web search through Tavily or Exa.
> 6. **Hosting:** one Oracle Cloud Always Free VM running Docker Compose (app + Postgres + Caddy).
> 7. **`AUTH_MODE=none` stays the local and self-hosted mode** (no login, Claude CLI allowed). The internal VM turned out to run separate code, so it's out of scope (decided 2026-11-07), and phases are verified on the dev machine instead.
> 8. **Admin analytics are built into the app.** Events are recorded server-side in our own Postgres, and the page shows only aggregates and metadata.
> 9. **The repo stays private.** Runtime data stops being tracked from Phase 0 on, and history is not rewritten.
> 10. **Delivery:** one branch per phase, pushed to GitHub and reviewed as a pull request.

## Defaults (ask for a change before the phase that uses it)

| Item | Default | Phase |
|---|---|---|
| Domain | A free DuckDNS name (e.g. `packages-tracker.duckdns.org`) unless you have a domain. GitHub sign-in needs a stable callback URL. | 2a, 5 |
| Claude model | `claude-haiku-5-5`: $0.10 / $0.50 per million tokens. Today's default, Haiku 4.5, costs $1 / $5. | 1 |
| Provider presets | Anthropic, OpenAI, Gemini, Groq, OpenRouter, and a custom URL (covers Mistral, GitHub Models, Ollama) | 1 |
| Search backends | Tavily first, then Exa | 1 |
| User cap | 50 | 2a |
| Per-repo sharing | Not in v1. Workspace roles only. | 2b |
| Limits | See the Phase 4 table | 4 |
| Alerts | Not in v1 | 6 |
| "Common EOL packages" panel | On, but it only shows packages found in 5 or more workspaces | 6 |
| Existing data | The cloud database starts empty. The internal VM and its GitLab repos are out of scope. | 5 |

## Target architecture

```
Browser ──HTTPS──▶ Caddy   (TLS, security headers, upload size cap)
                     │
                     ▼
       app container — one process
         FastAPI: /api + the built React UI (same origin)
         APScheduler ─▶ scan queue (N workers, one scan per workspace at a time)
         pipeline: clone/unzip → extract → structured lookups → AI (optional) → flags → snapshot
         AI adapters: Anthropic │ OpenAI-compatible + Tavily/Exa search
                     │                          │
                     ▼                          ▼
       Postgres 16 container            /data volume: uploads, apt-index cache
```

Everything stays in one process. Scan status, locks and the live scan log are kept in memory. That works on a single VM, and scaling out is listed under *Later*.

## One codebase, two modes

| | `AUTH_MODE=none`: local / self-hosted | `AUTH_MODE=github`: the cloud |
|---|---|---|
| Login | None | Sign in with GitHub |
| Workspaces | One built-in workspace, with the local user as Owner | A personal workspace for each user, plus team workspaces |
| AI providers | Claude CLI (your subscription) and API keys | API keys only. The CLI would bill every user's research to one subscription. |
| Docker image inventory | As today (detected automatically) | Off, because it would run images that users choose |
| Git hosts | An allowlist. Allowlisted hosts may be private, such as your internal GitLab. | An allowlist of public hosts only |
| Analytics | Scans, AI and system health | Everything |

The existing API tests keep running in `AUTH_MODE=none`, so they protect today's behaviour through every phase.

## Phases

| Phase | Branch | Size | Ships |
|---|---|---|---|
| 0 | `phase-0-hygiene` | M | Security fixes, the AI-failure fix, Docker as a detected capability, `scan_runs`, dev setup |
| 1 | `phase-1-ai-providers` | L | Provider layer, free-key providers, app-run search, eval set |
| 2a | `phase-2a-accounts` | L | GitHub sign-in, workspaces, tenancy, encrypted secrets, activity capture |
| 2b | `phase-2b-rbac` | M | Roles, invites, audit log, platform admin |
| 3 | `phase-3-ui` | L | Login, workspace switcher, Settings, Members, audit log, role-aware UI |
| 4 | `phase-4-limits` | M | Scan queue, limits, AI verdict reuse, retention |
| 5 | `phase-5-deploy` | M | Docker image, Compose, Caddy, Oracle runbook, backups, CI |
| 6 | `phase-6-analytics` | M | Admin analytics page |

Sizes: S is about a day, M is a few days, and L is a week or more.

Workflow for each phase:
1. Branch from an up-to-date `main`.
2. Commit, with task IDs (like `0.3`) in the commit messages.
3. Push the branch.
4. You open the PR and merge it.

Phases run in order. If you want an early private deploy, Phase 5 can come right after 2a, with `MAX_USERS=1`.

---

### Phase 0: hygiene, safety fixes, scan facts

Everything in this phase also protects local installs.

**0.1 Repo hygiene**
- Add a `.gitignore` covering `.env`, `.venv/`, `node_modules/`, `frontend/dist/`, `backend/data/`, `backend/uploads/`, `backend/cache/`, `*.egg-info/`, `__pycache__/` and `.DS_Store`.
- Run `git rm --cached` on the committed runtime data: `backend/data/`, `backend/uploads/`, `backend/cache/`, `backend/eol_tracker_backend.egg-info/` and the `.DS_Store` files. The files stay on disk, and history is not rewritten.
- **Caution for other clones:** back up `backend/uploads/` and `backend/data/` before pulling this change into an existing clone, because git deletes files from other clones when a commit stops tracking them. (The internal VM isn't a clone, so this didn't apply there.)
- Add the `.env.example` that the README refers to. It should list every variable, today's and this plan's, with each new one commented out until its phase lands.

**0.2 Safe cloning** ([clone.py:81-85](../backend/app/agents/clone.py#L81-L85), [models.py:295](../backend/app/models.py#L295))
- **The problem today:** the URL is passed to git without a `--` separator. That means a "URL" like `--upload-pack=<cmd>` runs a command on the server, `file:///…` reads files off the server, and a hung clone never times out.
- **Check repo URLs** when a repo is created or edited:
  - `https://` only, with no username or password in the URL;
  - the host must be on `GIT_HOST_ALLOWLIST`;
  - no IP addresses, and no host that resolves to a private, loopback, link-local or CGNAT address.
  - Allowlisted hosts may be private only in `AUTH_MODE=none`, so internal git servers keep working in local installs.
- **Check branch names:** reject a leading `-` and anything git's ref-name rules forbid.
- **Run git safely:**
  - command: `git clone --depth 1 --single-branch --branch <b> -- <url> <dest>`;
  - environment: `GIT_ALLOW_PROTOCOL=https` and `GIT_TERMINAL_PROMPT=0`;
  - in `AUTH_MODE=github`, also pass `-c http.followRedirects=false`;
  - a timeout: `CLONE_TIMEOUT_SECONDS`, default 120.
- **Tests:**
  - these are all rejected: `--upload-pack=…`, `file:///etc`, `http://…`, `https://user:pw@…`, an IP address, a host that resolves to `10.x`, and a host not on the allowlist;
  - an allowlisted private host is accepted only in `AUTH_MODE=none`.

**0.3 AI never throws away non-AI results** ([enrichment_agent.py:416-417](../backend/app/agents/enrichment_agent.py#L416-L417), [enrichment_agent.py:536](../backend/app/agents/enrichment_agent.py#L536), [pipeline.py:306-316](../backend/app/pipeline.py#L306-L316))
- **The problem today:** an error raised by `run_text_agent` escapes `enrich()`. Examples: the CLI is missing or logged out, or a batch hits the 420-second timeout. The pipeline then carries on with an empty enrichment list. Every package is flagged UNKNOWN ([flags.py:176-179](../backend/app/flags.py#L176-L179)) and the snapshot is marked degraded.
- **The fix:** catch errors for each batch and around the whole AI pass, so the structured verdicts always survive. The remaining batches keep going, except after an auth or quota error, which would make them all fail too.
- **Record the AI outcome** as `ai_status` (`off`, `ok`, `partial`, `failed`, `no_key` or `quota`) plus a short reason. It is stored in new snapshot columns and in the scan log. An AI failure no longer marks the snapshot degraded.
- **UI:** a small badge next to the snapshot, such as "AI: failed (timeout)".
- **Tests:**
  - a provider that raises an error still leaves every structured verdict in place; the snapshot is not degraded and `ai_status` is `failed`;
  - an auth error stops the later batches;
  - with the toggle off, no AI call is made at all.

**0.4 Docker as a detected capability** ([pipeline.py:243-244](../backend/app/pipeline.py#L243-L244), [pipeline.py:298](../backend/app/pipeline.py#L298))
- New setting `IMAGE_INSPECT=auto|on|off`. The default, `auto`, turns it on when `docker version` succeeds. It is always off in `AUTH_MODE=github`.
- When it is off or Docker is missing, image inventory and apt version resolution are skipped. The scan log gets an info line instead of the snapshot being marked degraded.
- Base-image EOL and the apt packages named in Dockerfiles still work, because the apt-index lookup only uses HTTP.

**0.5 `scan_runs`: one fact row per scan**
- **The problem today:** a scan's facts are spread across in-memory status ([pipeline.py:55](../backend/app/pipeline.py#L55)), the snapshot row and the scan-log JSON, and failed scans never become snapshots. The analytics page (Phase 6) needs one row per scan.
- **New table**, written at the end of every scan (in the pipeline's `finally`). Each row holds:
  - repo, workspace (empty until 2a), branch and snapshot timestamp;
  - trigger: `scheduled`, `manual`, `initial` or `upload` (the column is `triggered_by`);
  - queued, started and finished times, the outcome, and seconds per stage;
  - which stages had issues, and the first error (redacted and truncated);
  - package counts by ecosystem, and flag counts;
  - AI status, provider, model, batches, tokens, searches and estimated cost;
  - errors from outside sources, such as GitHub rate limits or endoflife.date failures.
- `run_scan()` gets a `trigger` argument, which the scheduler and the routes pass in.

**0.6 Dev setup**
- Backend: uv with Python 3.11 and a project venv in `backend/.venv`, with `pytest` passing.
- Frontend: `npm ci`, `npm test` and `npm run build` all passing.
- Update the developer guide, including notes for Windows.

**Done when:** all existing and new tests pass, and real scans of public repos on the dev machine give the expected results. (Done 2026-11-07: 290 tests, plus scans of psf/requests and docker/welcome-to-docker.)

---

### Phase 1: AI provider layer

**1.1 New package `backend/app/ai/`**
- `base.py`: a `Provider` interface with `research(prompt, system, *, max_searches, timeout) -> AgentResult`. It reuses today's `AgentResult`, so [enrichment_agent.py](../backend/app/agents/enrichment_agent.py) and its tests barely change.
- `errors.py`: `AiAuthError`, `AiQuotaError`, `AiTimeoutError` and `AiProviderError`. These set the `ai_status` values from 0.3.
- `pricing.py`: token prices for each paid model, and prices per search. Free tiers cost 0. Unknown models show "not estimated" and are never guessed.
- `registry.py`: the provider presets (name, adapter, base URL, and whether search is built in).
- `run_text_agent()` ([transport.py:277](../backend/app/agents/transport.py#L277)) keeps its signature and becomes the dispatcher. The Claude CLI options stay, for `AUTH_MODE=none` only.

**1.2 Anthropic adapter** (official `anthropic` SDK)
- One `messages.create` call per batch, using the existing system prompt and Claude's built-in `web_search` and `web_fetch` tools. Their `max_uses` is set from `WEB_SEARCH_MAX_USES`.
- The tool version depends on the model. Haiku models use `web_search_20250305` and `web_fetch_20250910`. Sonnet and Opus 4.6 and later use the `_20260209` versions. Confirm both against the docs when implementing.
- Handle `pause_turn` by sending the turn back to resume, with a cap. Treat `refusal` as a failed batch.
- Copy `usage`, including `server_tool_use.web_search_requests`, into the scan log's token counts. Log each search as a tool event, so the Activity page keeps showing what the AI did.
- Map SDK errors to the error types from 1.1. The SDK already retries 429 and 5xx errors twice.
- Note: `anthropic` 1.x is built on `httpx2`. The project's own `httpx` stays for the structured lookups.

**1.3 OpenAI-compatible adapter** (official `openai` SDK with `base_url`)
- Presets for OpenAI, Gemini, Groq and OpenRouter, plus a custom URL (Mistral, GitHub Models, Ollama…). Check each base URL against the provider's docs when implementing.
- Uses chat completions with two tools that the app runs itself: `web_search(query)` and `fetch_url(url)`. The loop ends at a final answer, at `max_searches`, or at `AI_MAX_TOOL_ROUNDS` (default 8).
- The final answer uses today's JSON format. Parse it, retry once, and fall back to the structured results if it still fails, as today.
- On a 429 error: wait and retry once. If it fails again, raise `AiQuotaError`, so the scan keeps its structured results.

**1.4 Search backends** (`ai/search.py`)
- Tavily and Exa, called over `httpx`. Each returns url, title and snippet. The key comes from env in Phase 1 and moves to the workspace in 2a.
- `fetch_url`:
  - public `http(s)` addresses only, using the same address checks as 0.2;
  - a 15-second timeout and a 5 MB size cap;
  - HTML is reduced to plain text and truncated.

**1.5 Citation check** (when the app runs the search)
- Keep every URL our tools returned during the conversation.
- Drop any AI source that isn't in that set. Drop a verdict other than `supported` if that leaves it with no sources.
- Structured data still wins on dates, as today.

**1.6 Configuration** (env for now, per workspace from 2a)
- `AI_PROVIDER`, `AI_MODEL`, `AI_API_KEY`, `AI_BASE_URL`, `SEARCH_PROVIDER` and `SEARCH_API_KEY`.
- Helpers for Phase 3's "Test connection" button: list the available models (Anthropic `models.list()`, OpenAI-compatible `GET /models`) and run one search query.

**1.7 Eval set** (`backend/evals/eol_research/`)
- About 20 packages with known answers, for example:
  - cybox: deprecated, successor stix2;
  - react 19: supported;
  - node-sass: deprecated, successor sass;
  - tslint: deprecated, successor typescript-eslint;
  - pycrypto: deprecated, successor pycryptodome.
- A runner sends each package's signals straight to a provider, skipping `_needs_ai`. It reports:
  - accuracy;
  - how many wrong `eol` verdicts it gave (the worst kind of error);
  - whether the citations are valid;
  - tokens, searches and cost.
- Run by hand, not in CI, because it spends quota or money. Results are recorded in this doc, and only presets that pass are marked "recommended" in the UI.

**Tests** (fake clients, no network): usage mapping, the `pause_turn` loop, refusals, error mapping, the tool loop, the citation check, dispatcher routing, and pricing.

**Done when:**
- a live run reproduces the M2 `cybox` result. Done 2026-11-07 through the Claude CLI ("eol, successor stix2", with 3 web citations); the Anthropic API run is waiting for a key;
- a scan with a free Gemini or Groq key plus Tavily runs from start to finish;
- the eval results are recorded.

---

### Phase 2a: accounts, workspaces, tenancy

**2a.1 Configuration:**
- `AUTH_MODE`;
- `APP_SECRET_KEY`, plus `APP_SECRET_KEY_PREVIOUS` for rotating it;
- `PUBLIC_BASE_URL`;
- `GITHUB_OAUTH_CLIENT_ID` and `GITHUB_OAUTH_CLIENT_SECRET`;
- `PLATFORM_ADMINS` (a list of GitHub logins);
- `MAX_USERS` (default 50).

**2a.2 Database migration**
- New tables: `users`, `sessions`, `workspaces`, `memberships` (workspace, user, role), `workspace_settings`, `secrets`, `user_activity_days` and `usage_events`.
- `repos` gets `workspace_id`, a display `name` and `created_by`.
- New repos get random IDs. Today the ID comes from the URL, so a second user adding the same repo gets a 409 error ([routes.py:96-98](../backend/app/api/routes.py#L96-L98)). Existing IDs stay as they are.
- In `AUTH_MODE=none`, the migration creates a built-in `local` workspace and user, which own all existing repos.

**2a.3 Sign-in**
- GitHub OAuth, written directly on `httpx`: the flow is short, and that keeps a dependency out and makes it easy to test. The flow: login, then the callback, then create or update the user. On first login the user also gets a personal workspace. Then a session starts.
- The session cookie is `__Host-session`: HttpOnly, Secure, SameSite=Lax. (SameSite=Strict would drop the cookie on the redirect back from GitHub.)
- Requests that change data must send a custom header and a matching `Origin`.
- Once there are `MAX_USERS` accounts, new people see a "sign-ups are full" page. Existing users aren't affected, and disabled users can't sign in.
- Add `/api/me` and logout.

**2a.4 Encrypted secrets** (`secrets_store.py`)
- Encryption uses Fernet, with keys derived from `APP_SECRET_KEY` (HKDF) and MultiFernet for key rotation.
- The API can save a secret but never returns it, only a hint (its last four characters).
- Secrets covered: AI keys, search keys, git tokens, users' GitHub sign-in tokens, and per-repo tokens. The per-repo tokens are moved by the migration out of today's plain-text column ([db_models.py:48](../backend/app/db_models.py#L48)).
- Scan logs and error messages pass through a redactor that removes anything shaped like a key.

**2a.5 Tenancy**
- New dependencies `current_user` and `current_workspace`. Every database query is limited to one workspace.
- **Route paths don't change**, so existing tests and the current UI keep working:
  - list and create routes (`/api/repos`, `/api/settings`) act on the active workspace. The UI sends it in an `X-Workspace` header; it defaults to the user's personal workspace, or the built-in one in `AUTH_MODE=none`;
  - repo routes find the workspace from the repo;
  - workspace management is new, under `/api/workspaces`.
- Settings become per workspace (today they are global: [settings_store.py:16](../backend/app/settings_store.py#L16)). New workspaces start with AI research off.
- The AI provider settings move from env (Phase 1) to workspace settings and secrets. Env stays as the fallback in `AUTH_MODE=none`.
- GitHub lookups during a scan ([source_apis.py:341](../backend/app/agents/source_apis.py#L341)) use the first token available, in this order:
  1. the workspace's GitHub token;
  2. the sign-in token of whoever added the repo;
  3. `GITHUB_API_TOKEN` (local installs);
  4. no token.

  The token travels with the scan in a per-scan context variable, the same way the scan log does today.

**2a.6 Activity capture**
- Middleware records one `user_activity_days` row per user per day, and updates `users.last_seen_at`.
- Route handlers record `usage_events`: CSV export, diff view, release-date edit, ignored-packages edit, zip upload, manual scan, AI toggle, and invites sent and accepted.
- No IP addresses are stored.

**2a.7 Account deletion:** `DELETE /api/me` removes the user's personal workspace (repos, snapshots, uploads and secrets), sessions, activity rows and usage events. It's blocked while the user is the last Owner of a team workspace with other members; they must transfer ownership first.

**Tests:**
- the OAuth callback, with GitHub mocked;
- cookie flags and the user cap;
- isolation: user B gets 404 on every route for user A's repo;
- secret encryption, hints and key rotation;
- the token migration;
- all existing tests still pass in `AUTH_MODE=none`.

---

### Phase 2b: roles, invites, audit log

**2b.1 Permissions** (`rbac.py`)
- Ordered roles, a list of permissions, and one table mapping roles to permissions.
- As built: a second table, `ROUTE_PERMS`, names the permission every route needs, and one router dependency, `authorize`, checks it before the handler runs. A route missing from the table fails closed.
- Non-members get 404, so they can't even tell a repo exists. Members without the permission get 403.

| Action | Viewer | Editor | Admin | Owner |
|---|:-:|:-:|:-:|:-:|
| View repos, results, history and scan activity; export CSV | ✓ | ✓ | ✓ | ✓ |
| Add repos (including a private-repo token that can't be read back), upload zips, edit release date, ignored packages and schedule | | ✓ | ✓ | ✓ |
| Run scans (these spend the workspace's AI key) | | ✓ | ✓ | ✓ |
| Delete repos and their history | | | ✓ | ✓ |
| Turn AI research on or off; choose provider, model and limits | | | ✓ | ✓ |
| Set or remove API keys and git tokens | | | ✓ | ✓ |
| Invite or remove members and change roles (except Owners) | | | ✓ | ✓ |
| Read the audit log | | | ✓ | ✓ |
| Manage Owners; rename, transfer or delete the workspace | | | | ✓ |

**2b.2 Membership**
- List members, change roles, remove members, leave a workspace.
- Create, rename, transfer and delete team workspaces.
- The last Owner can't leave or be demoted.
- Roles are checked on every request, so removing someone takes effect immediately.
- A personal workspace belongs to its account alone: no invites, no transfer, no leaving. Working with others happens in team workspaces.
- Deleting an account also deletes the team workspaces its user was the only member of. When a platform admin deletes the last Owner of a team that has other members, the member with the highest role becomes Owner, and the team's audit log records it.

**2b.3 Invites**
- Invite by GitHub login (it waits until that person signs in) or by a one-time link.
- Links are stored hashed, expire after 7 days, carry the role chosen up front, and work once.
- Invites can be accepted, declined or revoked.

**2b.4 Audit log** (`audit_events`)
- Each entry records the time, workspace, who acted (a user, `scheduler` or the platform admin), the action, its target, and details. Secret values are never recorded.
- It covers repo changes, scans, role and member changes, keys being set or removed, AI settings, and a platform admin viewing a workspace.
- Admins and Owners can read it, page by page.

**2b.5 Platform admin**
- Granted at sign-in to the logins listed in `PLATFORM_ADMINS`.
- New endpoints: `/api/admin/users` (list, disable, enable, delete) and `/api/admin/workspaces` (list, and one workspace's members and repos). `/api/admin/limits` moved to Phase 4, where the limits themselves arrive.
- The platform admin sees usage and metadata, but not scan results or keys, unless they're also a member of that workspace.
- Disabling a user signs them out everywhere at once.

**Tests:**
- every `/api` route either declares a permission or is explicitly public; a test walks the route table to check;
- a test of every role against every endpoint;
- the last-Owner rules;
- invite expiry and single use;
- an audit entry for every action that changes data;
- admin endpoints return 404 to non-admins.

Done 2026-11-08: 405 backend tests pass, 33 of them new in `test_rbac.py`.

---

### Phase 3: UI

- **3.1 Sign-in:**
  - a "Sign in with GitHub" page;
  - a redirect to it when the session has expired ([api.ts](../frontend/src/api.ts));
  - the "sign-ups are full" page;
  - an account menu with sign out and delete account.
- **3.2 Workspace switcher** in the header. Each browser remembers the active workspace. The existing page addresses stay the same, and repo links work for any member.
- **3.3 Settings page** (Admins and Owners can edit; everyone else sees it read-only):
  - the AI research toggle, moved here from [SettingsToggle.tsx](../frontend/src/components/SettingsToggle.tsx);
  - a provider preset and a model list filled in by "Test connection";
  - the API key, which can be saved but not read back (only a hint shows);
  - the search provider and its key;
  - GitHub and GitLab tokens;
  - two notices: "Add a key to enable AI research", and that some free tiers (Gemini's, for example) let the provider use prompts to improve its products. Our prompts contain only package names and versions.
- **3.4 Members page:** members and their roles, invites by GitHub login or link, and pending invites.
- **3.5 Audit log page**, for Admins and Owners.
- **3.6 Role-aware UI:** a `can(permission)` helper, fed by `/api/me`. Controls you can't use are disabled, with a tooltip naming the role you'd need. The server still makes the final check.
- **3.7 Repo pages:**
  - the AI status badge;
  - each scan's AI usage (tokens, searches, estimated cost) in Scan Activity;
  - the queue position in the scan status pill. **Moved to Phase 4**, which builds the queue.
- **3.8 Admin area:** the users list and limits. The charts come in Phase 6. **As built:** users and workspaces; limits move to Phase 4 with the limits themselves.
- **3.9 Privacy policy and terms:** I draft a starting version for you to review.
- **Tests:** handling of expired sessions, the permission helpers, and Settings form validation. `npm run build` must pass.

**Phase 3 as built (2026-11-08):**
- Also in this phase:
  - a public-only HTTP transport for the custom AI base URL and page fetches (`safe_http.py`). Before it, the public mode could have been pointed at internal addresses;
  - `POST /api/settings/ai-test` for Test connection;
  - sign-in errors on the app's own `/login` page, and `?next=` to return where you were;
  - repo names instead of random ids;
  - a GitHub token that GitHub rejects is dropped for the rest of the scan instead of failing every lookup.
- A scheduler fix: a new repo was scanned twice within a minute (its first scan, then a catch-up 30 s later). Now its schedule starts one interval after the first scan.
- Checked in a browser against a seeded public-mode server: signed out, the Owner and platform admin, an Editor and a Viewer. The live sign-in through GitHub still needs the OAuth app.
- Seen on the way, for Phase 4: package lookups run one package at a time, so a 1,200-package lockfile takes about 20 minutes to scan. The queue and limits should account for that, and the lookups could run a few at a time.

---

### Phase 4: limits, queue, cost controls

**4.1 Scan queue**
- Replaces the unlimited `asyncio.create_task` at [scheduler.py:168](../backend/app/scheduler.py#L168).
- `SCAN_WORKERS` scans run at once (default 2), with at most one running scan per workspace.
- Workspaces take turns. Duplicate requests for the same branch are merged into one.
- The UI can show each scan's place in the queue.

**4.2 Limits**
- Platform-wide defaults are editable in the admin area (`/api/admin/limits`), and can be overridden per workspace.
- The server enforces every limit with a clear message, and each time a limit is hit it's recorded in `usage_events`.

| Limit | Default |
|---|---|
| Workspaces created per user | 3 |
| Members per workspace | 10 |
| Repos per workspace | 10 |
| Branches or versions per repo | 3 |
| Shortest scan interval | 1 day |
| Manual scans per workspace per hour | 10 |
| Zip size (packed / unpacked) | 50 MB / 500 MB |
| Upload storage per workspace | 200 MB |
| Snapshots kept per branch | 30 |

**4.3 AI verdict reuse**
- New per-workspace setting `ai_reuse_days`: default 7, and 0 keeps today's behaviour.
- An AI verdict for the same `ecosystem:name:version` from the workspace's recent snapshots is reused instead of being researched again, and the scan log says so.
- Structured lookups still run fresh on every scan.

**4.4 Retention:** a daily job deletes snapshots beyond the per-branch limit. The newest snapshot is always kept.

**4.5 Inactive workspaces:** if no member signs in for 30 days, the workspace's scheduled scans pause. They resume at the next sign-in, and a banner explains what happened. A workspace whose members are all disabled pauses at once, rather than after 30 days.

**4.6 Rate limits** on sign-in and on the API, per IP address and per user, kept in memory.

**Tests:** queue fairness and merging of duplicates, each limit, verdict reuse, retention, and pausing and resuming.

**Phase 4 as built (2026-11-08):**
- **Queue** (`scan_queue.py`):
  - In the local mode, the single workspace may use every worker.
  - A scan request for a branch that's already queued or running gets a 202 that says it was merged; it no longer gets a 409.
  - The queue is in memory: a restart drops it, and scheduled scans come back as catch-ups.
  - The status pill says "Queued · 3rd in line".
- **Limits** (`limits.py`):
  - Members count pending invites, and acceptance is checked again.
  - Upload storage is checked before a new zip replaces the old one, which stays if the new one is refused.
  - Each refusal is a `limit_hit` usage event.
  - The UI has a usage card in Settings, a Limits tab in Admin, and per-workspace values in the workspace drawer.
- **Verdict reuse:**
  - Each finding keeps the AI's own answer (`ai_result`), and reuse re-applies it on fresh structured data.
  - It never crosses workspaces, and reused findings are labelled in the citations drawer.
- **Retention** also deletes failed scans' traces older than the oldest scan kept.
- **Inactive workspaces:**
  - A member's first request of the day resumes their paused workspaces, and overdue branches catch up at once.
  - Pause and resume are both in the audit log.
- **Rate limits** (`ratelimit.py`), per minute:
  - sign-in: 10 per address;
  - API: 300 per session, plus a ceiling of 600 per address;
  - calls without a session: 60 per address.
  The answer is a 429 with `Retry-After`. Addresses are counted in memory only.
- **Also:**
  - package lookups run 6 at a time (`LOOKUP_CONCURRENCY`), which addresses the 20-minute scan seen in Phase 3;
  - `sync_all` no longer removes the scheduler's non-scan jobs.

---

### Phase 5: packaging and deploy

- **5.1 Dockerfile** in stages:
  - a Node LTS stage builds `frontend/dist`;
  - the final image is `python:3.11-slim` plus `git` and the app, running as a non-root user;
  - it starts with `uvicorn --proxy-headers` and has a health check on `/api/health`;
  - it builds for both arm64 (Oracle A1) and amd64.
- **5.2 FastAPI serves the UI** (static files, with every non-API path returning the app's index page) from the same address as the API. CORS ([main.py:32](../backend/app/main.py#L32)) is turned on only when `APP_ENV=dev`.
- **5.3 `deploy/compose.cloud.yml`:**
  - `app`, with an env file and a `/data` volume;
  - `postgres:16-alpine`, with a volume and no port open to the outside;
  - `caddy` on ports 80 and 443, with automatic HTTPS, an upload size cap and security headers.

  It comes with `deploy/Caddyfile` and `.env.cloud.example`.
- **5.4 Backups:** a nightly `pg_dump`, copied off the VM, plus documented steps to test a restore.
- **5.5 CI and deploy:** GitHub Actions runs the backend and frontend tests on every push. Private repos get limited free minutes, so the jobs stay small. The VM builds and deploys itself with `git pull && docker compose up -d --build`, so no image registry is needed.
- **5.6 Runbook** (`docs/DEPLOY-ORACLE.md`), covering:
  - creating the account and a $1 budget alert;
  - an A1 VM running Ubuntu 24.04, with SSH keys only;
  - opening ports 80 and 443 in both Oracle's network settings (the VCN security list) and the VM's own firewall (iptables), which blocks them by default;
  - installing Docker and a read-only deploy key;
  - pointing DuckDNS (or your domain) at the VM;
  - the GitHub OAuth app, with callback `https://<domain>/api/auth/github/callback`;
  - generating secrets and the first start;
  - automatic OS updates and a free uptime monitor.
- Docker isn't installed on the dev machine, so 5.1 to 5.3 get tested on the VM.

**Done when:** following the runbook on a fresh VM gives a working HTTPS site within an hour, where you can sign in, add a GitHub repo, scan it and see the results.

---

### Phase 6: admin analytics

- **6.1 `system_samples`:** recorded every hour: disk use of `/data`, database size (`pg_database_size`), process memory, queue length and scans running.
- **6.2 API:** `/api/admin/analytics/*`, for the platform admin only (everyone else gets 404), with a date range.
  - Queries read `scan_runs`, `user_activity_days`, `usage_events`, `system_samples` and the main tables, and results are cached for five minutes.
  - Daily summary tables come later, and only if the queries get slow.
- **6.3 Panels:**
  - Headline: number tiles showing the change since the previous period and a 30-day mini trend line.
  - Growth: line charts.
  - Activation: one bar per step, in order, labelled with the % of sign-ups.
  - Retention: a grid in one color.
  - Scans: stacked bars by result, in status colors with labels; time per stage; failure reasons.
  - What people scan: bars sorted by size, plus flags in severity order.
  - AI: providers, results, searches and tokens (each on its own chart), estimated spend, how often AI changed a verdict, and error types.
  - System health: fill-level bars against limits, and line charts.
  - Workspaces: a sortable table.
- **6.4 Frontend:** a page at `/admin/analytics`, built with Recharts plus Ant Design cards and tables.
  - The date-range picker sits above the charts.
  - Every panel has hover details, a table view and CSV export.
  - No pie charts and no charts with two y-axes.
  - Colors are checked in both light and dark mode.
- **6.5 Privacy:**
  - only totals and metadata are shown;
  - the common-EOL panel only shows packages found in 5 or more workspaces;
  - opening a workspace's details is written to the audit log.
- **Tests:** access control, metric queries checked against test data with known counts, the 5-workspace threshold, and CSV export.

---

## Later (not scheduled)
- Listing an image's OS packages without Docker, by reading `/var/lib/dpkg/status` from the image's layers through the registry API. Nothing runs, so it's safe in public mode.
- The AI extraction agent on API providers, using read, glob and grep tools that can only see the cloned repo.
- Alerts through Discord, Slack or email webhooks.
- Per-repo sharing.
- A cache of structured lookups shared across workspaces.
- Built-in search for paid OpenAI or Gemini tiers, and Groq's Compound models.
- More than one app instance, which requires moving scan state out of process memory.

## New configuration

| Variable | Default | Phase | Purpose |
|---|---|---|---|
| `GIT_HOST_ALLOWLIST` | `github.com,gitlab.com,bitbucket.org,codeberg.org` | 0 | Hosts that repos may be cloned from |
| `CLONE_TIMEOUT_SECONDS` | `120` | 0 | Stops a hung clone |
| `IMAGE_INSPECT` | `auto` | 0 | Docker image inventory (always off in `AUTH_MODE=github`) |
| `AI_PROVIDER`, `AI_MODEL`, `AI_API_KEY`, `AI_BASE_URL` | unset | 1 | AI provider settings from env (local installs; per workspace from 2a) |
| `SEARCH_PROVIDER`, `SEARCH_API_KEY` | unset | 1 | The search service for OpenAI-compatible providers |
| `AI_MAX_TOOL_ROUNDS` | `8` | 1 | The most tool rounds in one AI conversation |
| `AUTH_MODE` | `none` | 2a | `none` (local / self-hosted) or `github` (cloud) |
| `APP_SECRET_KEY`, `APP_SECRET_KEY_PREVIOUS` | required in `github` mode | 2a | Sessions and secret encryption, with key rotation |
| `PUBLIC_BASE_URL` | — | 2a | The OAuth callback and links |
| `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET` | — | 2a | Sign in with GitHub |
| `PLATFORM_ADMINS` | — | 2a | GitHub logins with platform-admin rights |
| `MAX_USERS` | `50` | 2a | The sign-up cap |
| `SCAN_WORKERS` | `2` | 4 | How many scans run at once |
| `APP_ENV` | `prod` | 5 | `dev` turns on CORS for the Vite dev server |
| `DATA_DIR` | `/data` in the container | 5 | Uploads and the apt-index cache |

## Rules for every phase
- **Secrets** never appear in logs, scan logs, API responses or the audit log. A redactor checks every error message that leaves the server.
- **One address checker** is shared by cloning, `fetch_url`, and any future request to a URL a user supplied.
- **Every phase ships with tests and updated docs** (README, DEVELOPER-GUIDE, USER-GUIDE). Local-mode behaviour stays covered by the existing tests running in `AUTH_MODE=none`.
- **Database migrations** continue Alembic's numbering from `0006` and run at startup, as today.

## Risks
- **Free tiers keep changing.** In 2026 Oracle halved its free ARM tier, Gemini removed search from its free tier, and Brave dropped its free search plan. That's why providers, prices and limits are settings, not code. Check the numbers below again before relying on them.
- **Small free models are worse at research.** The eval set and the citation check are the guardrails.
- **Oracle reclaims idle VMs**: those under 20% CPU, network and memory use for 7 days. Users report that upgrading the account to Pay-As-You-Go stops this while the Always Free resources stay at $0. Confirm this in Oracle's current docs.
- **A public service has obligations:** a privacy policy, deleting data on request, and handling abuse.

## Reference: prices and free tiers (checked 2026-11-07)
- **Anthropic:** web search costs $10 per 1,000 searches, and web fetch costs nothing beyond tokens. Haiku 5.5 is $0.10 / $0.50 per million tokens for prompts up to 100K tokens; Haiku 4.5 is $1 / $5.
- **Gemini:** the free tier has no Google Search grounding. Paid projects get 5,000 grounded requests a month, then $14 per 1,000.
- **Groq:** the free tier allows about 1,000 requests a day per model. The Compound models have built-in search, with about 250 requests a day free; how that search is billed is unclear.
- **OpenRouter:** `:free` models allow 20 requests a minute, and 50 a day unless you've bought at least 10 credits.
- **Search services:** Tavily gives 1,000 free credits a month with no card. Exa gives about $10 of free credit a month (sources disagree). Brave replaced its free plan with a $5 credit that needs a card.
- **Hosting:** Oracle Always Free A1 is reportedly 2 OCPU / 12 GB since mid-2026. Neon's free plan gives 1 GB per project. Render's free Postgres expires after 30 days.
