# Packages Tracker — User Guide

A practical guide to what the product does, every field you'll see, what its
values mean, and how to use each screen.

**Dashboard:** http://localhost:5173 — it runs as a background service, so it's
up without you starting anything. To start/stop it or read its logs, see
[RUNNING-AS-A-SERVICE.md](RUNNING-AS-A-SERVICE.md).

---

## 1. What the product does

The tracker answers one question for a codebase: **"which of our open-source
dependencies are end-of-life (or will be by the time we ship?"**

You register a repository (URL + one or more **branches**) and a **planned
release date**. Each branch scans and is scheduled independently, with its own
snapshot history — e.g. track both `develop` and a release branch of the same
project under one repo entry. The system, per branch:

1. **Clones** the branch and **extracts** every dependency (Python, npm, OS/apt,
   Docker base images).
2. **Researches** each one's end-of-life / end-of-support status from
   endoflife.date, PyPI, npm, GitHub, and — for ambiguous cases — an AI agent that
   web-searches for deprecation notices and successor libraries.
3. **Flags** each dependency against your release date (e.g. "this library goes
   EOL in August, but you ship in September → flag it now").
4. Saves the whole run as a timestamped **snapshot**, and repeats on a schedule so
   you can see *when* a package's status changed.

It replaces the manual "list of components + EOL status" spreadsheet process.

---

## 2. Core concepts

| Concept | Meaning |
|---|---|
| **Repository** | One tracked GitHub/GitLab repo, covering one or more **branches**. Identified by an `id` derived from the URL (e.g. `my-service`). |
| **Branch** | One tracked line of the repo. Scans, schedules, and keeps snapshot history **independently** of every other branch on the same repo — picking a different branch on the repo's detail page shows a completely separate scan history. |
| **Planned release date** | The date you intend to ship. The flag engine compares each dependency's EOL date to this. This is the knob that makes "EOL *before release*" meaningful. Shared across all of a repo's branches. |
| **Scan interval** | How often (in days) the pipeline re-runs automatically, per branch. |
| **Scan** | One end-to-end run for one branch: clone → extract → research → flag → snapshot. |
| **Snapshot** | The saved result of one scan of one branch, timestamped in UTC. Nothing is overwritten — each run is a new snapshot, so history is preserved. |
| **Freshness** | There is **no result cache** — every scan re-researches every package against today's upstream data. A package that went EOL, or got a new stable release, since the last scan shows up on the very next run. Within a single scan, identical `ecosystem:name:version` entries are looked up once. |

---

## 3. Adding / editing a repository — every field

Reached via **Add repository** (or **Edit** on a repo card).

| Field | What it is | Example | Notes |
|---|---|---|---|
| **Repository URL** | The clone URL of the repo to track. GitHub or GitLab. | `https://gitlab.com/acme/my-service` | Read-only once created (the `id` derives from it). |
| **Branches** | One or more branches to scan. Required (at least one). | `main`, `release/5.2` | Type a name and press Enter to add another. Each branch scans and is scheduled independently with its own snapshot history — pick which one to view from a dropdown on the repo's detail page. |
| **Planned release date** | Your intended ship date. Optional. | `2026-09-15` | If left blank, the "before/soon" flag rules fall back to "within N months from *today*". Editing it later re-flags instantly (no re-scan) — for every branch. |
| **Scan interval (days)** | How often to auto-rescan. | `1` | Minimum 1 day. Applies to every branch. The interval job only runs while the backend is up. Use **Scan now** for on-demand runs. |
| **Docker image tags** | Values for compose files whose `image:` tags come from variables. One `VAR=image:tag` per line. Optional. | `CORE_TAG=acme/platform-dev:core-latest` | Only needed for deployment repos whose compose file says `image: index.docker.io/${CORE_TAG}` — the concrete tag lives outside the repo, so it can't be auto-detected. See below. |
| **Ignored packages** | Package names to leave out of the risk picture for this repo (every branch). Optional. | `enzyme`, `enzyme-to-json` | For test-only tooling and the like. They stay in the table with a grey **Ignored** flag, but are not counted in risk counts or the "Needs attention" filter. Saving re-flags the latest scans instantly (no re-scan). Names match case-insensitively, and `-`/`_`/`.` are treated alike. |
| **Per-repo token** | An optional access token used only for this repo. Optional. | `glpat-…` | Use when the global `.env` token can't reach this repo. Stored on your disk, **masked** (`****`) everywhere in the UI/API. Leave blank to use the environment token. |

### Upload zip (no git access needed)

**Add repository → Upload zip** scans a `.zip` of a project folder instead of
cloning. Give it a **Name** (the repo id derives from it), the **zip** (≤ 200 MB),
and optionally a **Version** label such as `6.1.0` (blank = `upload`). Every
other field works as for git repos.

- Each version keeps its own history and shows where branches do; pick it from
  the same dropdown.
- **Edit** an uploaded repo to add a version (zip + new label) or replace one
  (zip + existing label). Either starts a scan. Removing a version deletes its zip.
- Zips are kept on the server (`backend/uploads/`), so scheduled rescans keep
  re-checking the same code against fresh EOL data.
- `node_modules`, `.git`, virtualenvs and build folders inside the zip are
  skipped; zips with `../` paths are rejected and symlinks are ignored.

> **Docker image inventory is automatic.** You don't specify an image. During each
> scan the tracker reads the repo's `docker-compose*.yml` files and Dockerfiles,
> **auto-detects the Docker image(s)** (deployed compose images first, then
> Dockerfile `FROM` base images), and runs `dpkg-query` inside each to list
> **every installed OS package with its exact version** (the `dpkg -l` parity with
> the manual CSVs). Requires Docker on the backend host. Inventory rows show
> `source: image:<ref>` and are **transitive** (hide them with "Direct only"). OS
> packages are excluded from AI research — their support window is the base image's
> cycle. Compose images whose `${ENV}` tags you haven't supplied are skipped (their
> tag isn't knowable from the repo alone) — see **Docker image tags** below.

### Docker image tags (variable-tag compose repos)

Some deployment repos pin nothing themselves — the compose file reads

```yaml
core:
  image: index.docker.io/${CORE_TAG}
```

so the repo alone never says *which* image ships. Supply the tags in the
**Docker image tags** box, one per line, exactly as they appear in your
`.env` or release tooling:

```
RABBITMQ_TAG=acme/platform:rabbitmq-6.1.0-4.2.2
MONGO_TAG=acme/platform:mongo-6.1.0-8.0.18
CORE_TAG=acme/platform-dev:core-latest
UI_TAG=acme/platform-dev:ui-latest
```

Each resolved image is then (a) tracked as a `docker-image` component and
(b) inventoried with `dpkg-query` like any other image, so all four containers'
OS packages land in the snapshot. Variables you leave blank are simply skipped,
and the scan's **Activity** trace names them (`missing UI_TAG`), so a forgotten
tag is visible rather than silent.

After the first scan the tracker remembers which variables the compose files
use, so **Edit** pre-fills a line for each one and the repo card shows
`image tags: 2/4 set`. Changing tags doesn't re-scan by itself — hit
**Scan now** (or wait for the interval) to pick up the new images.
`${VAR:-default}` tags fall back to their compose default when you supply
nothing.

Saving a new repo stores it in the tracker's database and **starts the first scan
immediately for every branch**.

---

## 4. The screens

### 4.1 Repositories (home)
A card per tracked repo showing:
- **Repo id** (click to open detail) and a **N branches** tag
- **URL, Release date, Interval**
- **Docker image tags status** (if the repo uses any — see §3)
- **One row per branch** — branch name, its own **scan status pill**
  (`idle` / `queued` / `scanning: <phase>` / `done` / `error`), its own
  **flag-count summary** from its latest snapshot, and its own last-scan time.
  Click a branch's row to jump straight to that branch on the repo's detail page —
  this is how you check scan results for a specific branch without opening every
  card.
- Actions: **View**, **Edit**, **Delete** (delete removes the repo *and every
  branch's* snapshots/results/logs)

### 4.2 Repository detail
- **Branch selector** — next to the repo id, picks which branch's scan history
  the rest of the page shows. Everything below (snapshot picker, component
  table, Activity, History, CSV, Scan now) is scoped to this one branch; switch
  branches and they all update together. The choice is kept in the URL
  (`?branch=...`), so a link to a specific branch's view can be shared/bookmarked.
- **Snapshot picker** — choose which run to view for the selected branch
  (defaults to the latest).
- **Activity** button → the scan trace for this branch (§4.4).
- **History** button → the timeline + diff for this branch (§4.5).
- **Download CSV** — the flat export of the current snapshot (replaces the manual
  spreadsheet).
- **Scan now** — trigger an immediate scan of the selected branch only (other
  branches of the same repo are unaffected).
- **Component table** — one row per dependency (columns in §5), with filters
  (§6). Click a row to open the **Citations drawer**.

### 4.3 Citations drawer (click any component)
Everything known about one dependency:
- Flag badge, ecosystem, confidence
- **AI / research summary** — a one-line plain-English status
- Version, Latest version, Verdict, EOL date, EOS date, Successor, Source file
- **Flag reasons** — why it got its flag (every applicable reason, not just one)
- **Citations** — clickable source URLs (endoflife.date, PyPI, npm, GitHub, or web
  pages the AI used)

### 4.4 Scan activity (the trace)
Scoped to one branch (a selector at the top switches it, same as the detail
page). Live, auto-refreshing view of what a scan is doing:
- **Stage stepper** — `clone → extract → enrich → flag → snapshot`, each showing
  wait / in-progress / done / error.
- **Event table** — timestamp, **stage**, **level**, message, with expandable rows
  showing the underlying data (counts, tool inputs/outputs). Use the snapshot
  dropdown to view a past run's saved trace.

Event **stages**: `scan` (overall), `clone`, `extract`, `enrich`, `source`
(per-package structured lookup), `agent` (AI tool calls + AI verdicts), `flag`,
`snapshot`.

Event **levels**: `info` (normal), `warn` (recoverable, e.g. a degraded batch),
`error` (a stage failed — the scan still finishes with a degraded snapshot),
`tool` (an AI tool call like WebSearch), `result` (an AI tool result / verdict).

### 4.5 Snapshot history
Scoped to one branch (a selector at the top switches it).
- **Flag-count trend** — a row per snapshot with its package count and flag
  breakdown, so you can see status drift over time.
- **Diff** — pick two snapshots; see which packages **changed** status
  (from → to), were **added**, or **removed**. This is how you prove "requests
  became EOL-flagged on July 15".

---

## 5. Component table — every column & value

| Column | Meaning |
|---|---|
| **Name** | Package name. |
| **Ecosystem** | Where it comes from: `pypi` (Python), `npm` (Node), `apt` (OS package installed in the Dockerfile or found in an inventoried image), `docker-image` (a base image from a Dockerfile `FROM`, or a service image from a `docker-compose.yml`), `other`. |
| **Version** | The version pinned/declared in the repo. May be blank for apt packages (the Dockerfile installs them unversioned) — see §8. |
| **Latest** | Latest version available upstream (from the registry / endoflife.date). This is whatever upstream *calls* latest, so it **may be a pre-release** (e.g. `4.0.0rc1`). |
| **Latest stable** | Newest **non-pre-release** version (rc/beta/alpha/dev excluded; on PyPI, yanked releases excluded too). This is the one you'd actually upgrade to. Shows `↑ update` when it's newer than your pinned version — see §5.1. Blank (`—`) means no stable release was found, or the snapshot predates this field (re-scan to populate). |
| **EOL** | End-of-life date — the point after which the version gets no support/fixes at all. Comes **only** from structured sources (never invented by the AI). Blank means none is published. |
| **EOS** | End-of-support (end of *active*/standard support) date — support wound down but not fully dead yet. |
| **Flag** | The verdict of the flag engine (see §7). This is the actionable field. |
| **Conf.** | Confidence in the enrichment: `high` (authoritative structured hit, e.g. endoflife.date), `medium` (registry metadata), `low` (little/no signal). |

### Verdict (shown in the drawer / CSV)
The research layer's raw status, separate from the flag:
- **supported** — active, maintained.
- **deprecated** — the maintainers/registry marked it deprecated, or it's
  superseded (e.g. cybox → STIX 2).
- **eol** — past end-of-life.
- **unknown** — no reliable signal found.

### 5.1 "Update available" — independent of EOL/EOS

A package can be perfectly healthy and *still* be behind. The **`↑ update`** tag
appears whenever a newer **stable** release exists than the version you have
pinned — regardless of flag. So an `OK`, fully-supported package will still tell
you an upgrade is on the table; this is upgrade *hygiene*, not an EOL warning.

- It is **not** a flag and does not affect the severity ladder in §7 — an
  up-to-date-but-EOL package and a behind-but-supported package stay separate
  concerns.
- It compares against **Latest stable**, never the raw **Latest**, so a
  pre-release is never advertised as an upgrade.
- It only appears when both versions can be compared as real version numbers.
  OS/apt packages carry distro-style versions (`3.12.3-1ubuntu0.15`) that can't
  be compared to an upstream number, so they stay deliberately quiet rather than
  flooding you with hundreds of unreliable image-inventory rows.
- Use the **Updates only** toggle (§6) to see just these, and the **Latest
  stable** column header to sort upgradable packages to the top.
- Both `latest_stable_version` and `update_available` are included in the
  **CSV export**.

### is_direct (the "Direct only" filter)
- **direct** — declared explicitly by the project (in `requirements.txt`,
  `package.json` deps, `pyproject.toml`, or the Dockerfile).
- **transitive** — pulled in indirectly, discovered from a lockfile
  (`package-lock.json`). The **Direct only** toggle hides these; it only appears
  when a snapshot actually contains transitive packages.

---

## 6. Filters (component table)
- **Search** — substring match on package name.
- **Ecosystem** — pypi / npm / apt / docker-image / other.
- **Flag** — show only one flag category.
- **Direct only** — hide transitive dependencies (only shown when relevant).
- **Updates only** — show just the packages with a newer stable release
  available (only shown when at least one is upgradable).

---

## 7. Flags — what each one means & what to do

The flag engine is deterministic and **release-date aware**. It evaluates a
severity ladder; the first (most severe) match wins, but every applicable reason
is recorded (visible in the drawer).

| Flag | Colour | Meaning | Action |
|---|---|---|---|
| **ALREADY_EOL** | red | EOL date is on/before today, **or** the project's GitHub `SECURITY.md` "Supported Versions" table marks this release line ❌ (unsupported). | Replace/upgrade now. |
| **EOL_BEFORE_RELEASE** | volcano | EOL falls on/before your **planned release date**. *(The core case: "release in Sept, EOL in Aug".)* | Fix before shipping. |
| **EOL_SOON_AFTER_RELEASE** | orange | EOL within N months (default 6) **after** release. | Plan the upgrade into the next cycle. |
| **DEPRECATED** | purple | Deprecated **and there is somewhere to go**: a newer version exists to upgrade to, or a named successor to migrate to. | Upgrade to the newer version / migrate to the successor (shown in the drawer). |
| **ARCHIVED** | purple | Source repository is archived (no longer maintained). Not raised when the repo says it *moved* (the new home is checked instead) or the package was released after the repo's last push. | Treat as unmaintained; plan replacement. |
| **EOS_PASSED** | gold | Active support ended (EOS date passed) but not fully EOL yet. | Upgrade soon; you're on borrowed time. |
| **STALE** | geekblue | No release in > N years (default 2), **or** deprecated with *nothing* newer to move to (current == latest, no successor — project dormant). | Review; may be fine, may need replacing. |
| **UNKNOWN** | grey | No authoritative EOL signal. | Investigate manually if it matters. |
| **OK** | green | Supported, nothing due. | None. |
| **IGNORED** | grey | On the repo's **Ignored packages** list. Not evaluated and never counted as a risk. | Remove it from the list to track it again. |

> **No release date set?** Rules 2–3 collapse into a single "EOL within N months
> from today" check (surfaced as `EOL_BEFORE_RELEASE`).

**Editing the release date re-flags the latest snapshot instantly** — no re-scan,
no AI cost — because flagging is pure computation over the stored research data.

---

## 8. Special cases you'll notice

- **apt packages take their distribution's support window.** The scan reads the
  same package indexes apt uses (Ubuntu's archive, plus any repo a Dockerfile
  adds with a literal `deb …` line, e.g. Microsoft's):
  - an unpinned `apt-get install curl` gets the version that release installs today;
  - a package Ubuntu publishes for the release is supported until the release's
    end of standard support (24.04: 2029-05-31), cited to its Ubuntu archive entry;
    packages in **universe** say so — Canonical guarantees fixes for those only
    with Ubuntu Pro;
  - virtual names (`libc-dev`, `libncurses5-dev`) resolve to the package that provides them;
  - `msodbcsql17/18` use Microsoft's ODBC driver lifecycle; `mongodb-org-*` use MongoDB's.
  `mongodb-mongosh` and `mongodb-database-tools` stay `UNKNOWN`: MongoDB publishes
  no end-of-life policy for them.
- **Vendored dependencies (`lib/` folders).** Some plugins commit their
  dependencies into a `lib/` folder instead of a `requirements.txt`. These are
  extracted too: pip-installed ones (`*.dist-info`) give a name **and** version;
  source-only folders give the name with a blank version (like apt).
- **docker-compose repos.** A repo whose only manifest is a `docker-compose.yml`
  (no requirements/package.json/Dockerfile) is still covered: every service
  `image:` is recorded as a `docker-image` component (EOL-tracked like a base
  image) **and** auto-inventoried with `dpkg-query`. So it won't show 0 anymore.
  If its images use `${ENV}`-interpolated tags, supply them in the **Docker image
  tags** field (section 3) — without values those services are skipped, since the
  concrete tag isn't in the repo.
- **A repo can still show few/0 packages** if it declares no manifests *and*
  vendors nothing — its dependencies are then covered by the repo or image it runs in.
- **A current major with no EOL date** (e.g. React 19) shows `supported` with an
  endoflife.date citation but a blank EOL date — correct, because none is
  announced yet.
- **AI research** only runs for *ambiguous* packages (unknown / low-confidence /
  stale-with-no-official-data). Toggle it from the header ("AI research"). It never
  sets EOL dates — those always come from structured sources.

---

## 9. Cost & rate limits
The AI runs on your Claude subscription (no API key). Controls in place:
- AI research only for ambiguous packages, batched, with a web-search cap.
- Lookups are deduped by `ecosystem:name:version` within a scan.
- Re-flagging on a release-date change costs nothing (no re-scan, no AI).

Note that results are **not cached between scans** — that's a deliberate trade so
every scan reflects today's upstream state. The practical effect is that the AI
research pass re-runs for ambiguous packages on each scan, so prefer a sensible
scan interval over scanning constantly, and use the **AI research** toggle if you
want structured-only (free) runs.

If a scan hits a rate limit, affected packages degrade to `UNKNOWN`/structured
results — the scan still completes and the snapshot is saved.

---

## 10. Where your results live
Everything is stored in the tracker's **PostgreSQL** database (a local Docker
container, `deploy/postgres-compose.yml`):
- your registered repos and branches, and the UI toggles (e.g. AI research on/off);
- per branch, every scan's snapshot: extracted dependencies, EOL research +
  citations, evaluated flags + reasons, and the activity trace for that run.

The CSV export is generated from the stored snapshot when you download it, so it
always reflects the latest release-date re-flag. Deleting a repo removes all of
its snapshots for every branch.

Results from before the database (the old `data/` folder) are brought in once by
an import (see the README); the folder is left untouched as a backup.
