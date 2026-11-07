# Running the tracker as a service (start / stop / logs)

The backend and frontend run as **systemd services**, and the database as a
**Docker container**, so all three survive closing a terminal, dropping the
SSH/VS Code connection, and a reboot.

| Service | What it runs | Port |
|---|---|---|
| `packages-tracker-backend` | `uvicorn app.main:app` (FastAPI + APScheduler interval scans) | **8001** |
| `packages-tracker-frontend` | `npm run dev` (Vite dev server) | **5173** |
| `packages-tracker-postgres` (container) | PostgreSQL 16 — repos, settings, all scan history | **5432** (127.0.0.1 only) |

The backend needs the database: start order is **Postgres → backend → frontend**.

Open the dashboard at **http://localhost:5173**.

The unit files and commands below assume the repo is checked out at
`/opt/packages-tracker` and the services run as root; adjust the paths in
[`deploy/`](../deploy/) if yours differ.

---

## Why services (the problem this solves)

Started from a terminal, both processes are children of the VS Code integrated
terminal, which itself hangs off the Remote-SSH connection:

```
sshd → bash → vscode-server → integrated terminal (pts/N)
                                └─ npm run dev / uvicorn      ← STAT "Sl+" = foreground group
```

Anything that ends that terminal — closing the tab, reloading the VS Code window,
a network blip, the laptop sleeping — sends `SIGHUP` down the foreground process
group and both die. That matters more than usual here because **APScheduler runs
in-process**: when the backend dies, scheduled interval scans silently stop
firing and the data just goes stale, with no error anywhere.

As services they are parented to PID 1 with no controlling TTY, so no terminal
can signal them, and `Restart=on-failure` brings them back if they crash.

---

## Everyday commands

```bash
# status (add --no-pager to avoid the pager)
systemctl status packages-tracker-backend packages-tracker-frontend --no-pager

# start / stop / restart
systemctl start   packages-tracker-backend packages-tracker-frontend
systemctl stop    packages-tracker-backend packages-tracker-frontend
systemctl restart packages-tracker-backend packages-tracker-frontend

# just one of them
systemctl restart packages-tracker-backend

# quick one-word answers
systemctl is-active packages-tracker-backend      # active | inactive | failed
systemctl is-enabled packages-tracker-backend     # enabled = starts on boot

# database (run docker compose from packages-tracker/)
docker ps --filter name=packages-tracker-postgres                         # Up … (healthy)?
docker compose -f deploy/postgres-compose.yml --env-file .env up -d  # start
docker compose -f deploy/postgres-compose.yml --env-file .env stop   # stop (data kept)
docker restart packages-tracker-postgres
```

### Stopping

Name whichever service you want — they're independent, so you can stop one and
leave the other running:

```bash
systemctl stop packages-tracker-backend                              # backend only
systemctl stop packages-tracker-frontend                             # frontend only
systemctl stop packages-tracker-backend packages-tracker-frontend         # both
docker stop packages-tracker-postgres                                # database (stop the backend first)
```

Confirm it's really down:

```bash
systemctl is-active packages-tracker-backend      # -> inactive
ss -ltnp | grep -E ':(8001|5173)'            # -> no output once both are stopped
```

Things worth knowing:

- **A manual stop stays stopped.** `Restart=on-failure` only fires when the
  process exits *abnormally*; systemd never resurrects a service you stopped
  yourself. You don't need to disable anything first.
- **`stop` is not permanent.** Both services are `enable`d, so they come back on
  the next reboot. To keep one down across reboots too, also disable it:
  ```bash
  systemctl stop packages-tracker-backend && systemctl disable packages-tracker-backend
  ```
- **Stopping the backend pauses all scheduled scans** — APScheduler lives inside
  it, so no repo gets scanned (on any branch) until it's back up. Nothing queues
  in the meantime; the next scan happens on the interval after restart.
- **An in-flight scan gets ~30s to finish** (`TimeoutStopSec=30`) so it can
  write its snapshot before being killed. If it doesn't finish in time it's
  SIGKILLed, and that run is simply lost — earlier snapshots are untouched.
- **Stop the backend before the database.** With Postgres down, a running backend
  fails every request (and a scan in flight fails); a restarting one exits on
  startup and systemd retries every 5s until the database is back.
- **A stopped container stays stopped**, even across reboots
  (`restart: unless-stopped`). Bring it back with `docker compose … up -d`.
- **Stopping never deletes data.** It lives in the `packages-tracker_packages-tracker-pgdata`
  volume. The one destructive command is `docker compose … down -v` — the `-v`
  removes the volume. Don't use it.
- **Stopping only the frontend** leaves the API fully usable on `:8001`
  (`curl`, the CSV endpoints); you just lose the dashboard UI.
- **The dashboard will look broken if only the backend is down** — the page still
  loads from Vite, but every request fails. Check the backend first when the UI
  shows errors or empty lists.

### Logs

Everything goes to the journal (there are no log files to rotate):

```bash
journalctl -u packages-tracker-backend -f              # follow live
journalctl -u packages-tracker-backend -n 200          # last 200 lines
journalctl -u packages-tracker-backend --since "1 hour ago"
journalctl -u packages-tracker-backend -p err          # errors only
journalctl -u packages-tracker-backend -f -u packages-tracker-frontend   # both at once
```

A running scan logs each HTTP lookup it makes, so `-f` is a good way to watch a
scan progress from the shell. (The **Activity** page in the UI is the nicer view.)

The database logs to Docker, not the journal:

```bash
docker logs packages-tracker-postgres --tail 100
docker logs packages-tracker-postgres -f
```

### Start on boot

Both are already `enable`d. To change that:

```bash
systemctl disable packages-tracker-backend packages-tracker-frontend   # don't start at boot
systemctl enable  packages-tracker-backend packages-tracker-frontend   # start at boot again
```

The database comes up through a chain worth knowing, because `docker.service`
itself is **not** enabled on this host (only socket-activated):

1. the backend unit has `Wants=docker.service`, so starting the backend at boot
   starts Docker;
2. Docker restarts the container (`restart: unless-stopped`);
3. the backend's first attempt may beat Postgres to ready — it exits and
   systemd retries 5s later, by which time the database is up.

So if you `disable` the backend, the database won't start at boot either (unless
something else starts Docker). `systemctl enable docker` makes it independent.

---

## After changing code

The backend service deliberately runs **without `--reload`** (the reload watcher
is a dev-loop tool and spawns a child process that complicates supervision), so
backend code changes need a restart:

```bash
systemctl restart packages-tracker-backend
```

**Database schema changes apply themselves**: on every start the backend runs
`alembic upgrade head`, so a restart is all a new migration needs. To make one
(after editing `backend/app/db_models.py`):

```bash
cd /opt/packages-tracker/backend && . .venv/bin/activate
alembic revision --autogenerate -m "what changed"   # review the new file in alembic/versions/
systemctl restart packages-tracker-backend               # applies it
```

The frontend runs the Vite dev server, which **does** hot-reload — frontend edits
appear in the browser with no restart. Restart it only if you change
`vite.config.ts`, `package.json`, or env.

### Tight iteration loop

If you want `--reload` while actively editing backend code, stop the service
first so the port is free, then run it in the foreground and Ctrl-C when done:

```bash
systemctl stop packages-tracker-backend
cd /opt/packages-tracker/backend
.venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8001
# …then hand back to the service:
systemctl start packages-tracker-backend
```

---

## Editing the services

The unit files live in the repo at [`deploy/`](../deploy/) and are installed to
`/etc/systemd/system/`. (The database is defined in
[`deploy/postgres-compose.yml`](../deploy/postgres-compose.yml) instead — after
editing it, re-run `docker compose -f deploy/postgres-compose.yml --env-file .env up -d`,
which recreates the container and keeps the data.) After editing the unit files,
reinstall and reload:

```bash
cd /opt/packages-tracker
install -m 644 deploy/packages-tracker-*.service /etc/systemd/system/
systemctl daemon-reload
systemctl restart packages-tracker-backend packages-tracker-frontend
```

A few settings in there are load-bearing and worth knowing about:

- **`VITE_API_TARGET=http://127.0.0.1:8001`** (frontend) — `vite.config.ts`
  defaults its `/api` proxy to port **8000**, but the backend listens on 8001.
  Without this override the UI proxies to the wrong port.
- **`HOME=/root`** (backend) — the AI agents find the `claude` CLI by globbing
  `~/.vscode-server/extensions/anthropic.claude-code-*/…`, and that binary holds
  the subscription login used instead of an API key. Without `HOME`, AI research
  fails with "Could not find the `claude` CLI". The glob is version-independent,
  so extension updates are fine.
- **`DATABASE_URL`** (backend, via `packages-tracker/.env`) — the database connection.
  It must use the same password as `POSTGRES_PASSWORD` in that file. Without it
  the backend refuses to start (see Troubleshooting).
- **`After=`/`Wants=docker.service`** (backend) — what starts the database at
  boot; see [Start on boot](#start-on-boot).
- **node path** (frontend) — `npm`/`node` come from nvm, which isn't on the system
  PATH, so the unit hardcodes `/root/.nvm/versions/node/v22.16.0/bin`. If you
  switch node versions, update `Environment=PATH` and `ExecStart` to match
  `which npm`.

---

## The database (PostgreSQL in Docker)

All repos, settings and scan history live in PostgreSQL. Only the database is
containerised — the backend and frontend stay systemd services.

| | |
|---|---|
| Container | `packages-tracker-postgres` (`postgres:16-alpine`) |
| Defined in | [`deploy/postgres-compose.yml`](../deploy/postgres-compose.yml) |
| Address | `127.0.0.1:5432` — not reachable from other hosts |
| Database / user | `packages_tracker` / `packages` (password: `POSTGRES_PASSWORD` in `packages-tracker/.env`) |
| Data | Docker volume `packages-tracker_packages-tracker-pgdata` |
| Test database | `packages_tracker_test` — scratch DB for `TEST_DATABASE_URL`; tests wipe it |

```bash
docker exec -it packages-tracker-postgres psql -U packages -d packages_tracker      # SQL shell
docker exec packages-tracker-postgres psql -U packages -d packages_tracker \
  -c "select repo_id, branch, count(*) from snapshots group by 1, 2"   # history per branch
```

### Backup and restore

Automatic nightly backups are planned (phase 3); until then, take one by hand —
before risky changes especially:

```bash
mkdir -p /root/packages-tracker-backups
docker exec packages-tracker-postgres pg_dump -U packages -d packages_tracker -Fc \
  > /root/packages-tracker-backups/packages_tracker-$(date +%F).dump
```

Restore (replaces the current contents — stop the backend first):

```bash
systemctl stop packages-tracker-backend
docker exec -i packages-tracker-postgres pg_restore -U packages -d packages_tracker --clean --if-exists \
  < /root/packages-tracker-backups/packages_tracker-YYYY-MM-DD.dump
systemctl start packages-tracker-backend
```

The pre-database JSON tree in `backend/data/` is still there as a read-only
backup of everything up to the switch on 2026-10-04.

### Changing the database password

`POSTGRES_PASSWORD` only takes effect when the volume is first created, so
editing `.env` alone breaks the backend's login. Change it inside Postgres too:

```bash
docker exec -it packages-tracker-postgres psql -U packages -d packages_tracker \
  -c "ALTER USER packages PASSWORD 'new-password'"
# then set the same value in POSTGRES_PASSWORD and DATABASE_URL in .env
systemctl restart packages-tracker-backend
```

### Moving an install from the old `eol-tracker` names

Installs set up before the rename run `eol-tracker-*` services against an
`eol_tracker` database owned by user `eol`. The compose file now creates a new,
empty volume under the new names, so carry the data across once:

```bash
cd /opt/packages-tracker                     # the updated checkout

# 1. back up the old database, then stop and remove everything old
mkdir -p /root/packages-tracker-backups
docker exec eol-tracker-postgres pg_dump -U eol -d eol_tracker -Fc \
  > /root/packages-tracker-backups/pre-rename.dump
systemctl disable --now eol-tracker-frontend eol-tracker-backend
rm /etc/systemd/system/eol-tracker-*.service && systemctl daemon-reload
docker stop eol-tracker-postgres && docker rm eol-tracker-postgres   # its volume is kept

# 2. in .env:
#    DATABASE_URL=postgresql+psycopg://packages:<password>@127.0.0.1:5432/packages_tracker

# 3. start the new database and restore into it *before* the backend first runs —
#    its startup migration would otherwise create empty tables the restore collides with
docker compose -f deploy/postgres-compose.yml --env-file .env up -d --wait
docker exec -i packages-tracker-postgres pg_restore -U packages -d packages_tracker \
  --no-owner --no-acl < /root/packages-tracker-backups/pre-rename.dump

# 4. install and start the renamed services
install -m 644 deploy/packages-tracker-*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now packages-tracker-backend packages-tracker-frontend
```

If the checkout itself moved to `/opt/packages-tracker`, recreate `backend/.venv`
and run `npm install` in `frontend/` there before step 4 — a virtualenv doesn't
survive being moved. Once the dashboard shows your repos and history, the old
volume can go (irreversible): `docker volume rm eol-tracker_eol-tracker-pgdata`.

## Troubleshooting

**Service won't start / keeps restarting**

```bash
systemctl status packages-tracker-backend --no-pager -l
journalctl -u packages-tracker-backend -n 50 --no-pager
```

The backend's last log lines usually name the cause. Database-related ones:

| Error in the journal | Meaning | Fix |
|---|---|---|
| `DATABASE_URL is not set` | `.env` missing the setting, or the service isn't running from `backend/` | Add `DATABASE_URL` to `packages-tracker/.env` |
| `connection to server at "127.0.0.1", port 5432 failed: Connection refused` | Postgres isn't running | `docker compose -f deploy/postgres-compose.yml --env-file .env up -d` — the backend recovers on its own within 5s |
| `password authentication failed for user "packages"` | `DATABASE_URL` password ≠ the database's | See [Changing the database password](#changing-the-database-password) |

**Database container won't start** — `docker logs packages-tracker-postgres --tail 50`.
If port 5432 is taken by something else (`ss -ltnp | grep 5432`), change the
left side of `ports:` in the compose file *and* the port in `DATABASE_URL`.

**`Address already in use`** — something else already holds the port, usually a
leftover manual run in a terminal:

```bash
ss -ltnp | grep -E ':(8001|5173)'    # shows the PID holding it
```
If it's a manual `uvicorn`/`npm` from a terminal, stop that and let the service
own the port.

**UI loads but shows no repos / wrong data** — the Vite proxy is pointing at the
wrong backend. Confirm the override is in effect:

```bash
systemctl show -p Environment packages-tracker-frontend
curl -s localhost:5173/api/repos | head -c 200      # should list your repos
```

**AI research fails but structured results work** — the `claude` CLI wasn't
found; check `HOME=/root` is still in the unit, or set `CLAUDE_CLI_PATH` in
`backend/.env`.

**Scheduled scans aren't running** — interval jobs only fire while the backend is
up, and each repo has its own per-branch job:

```bash
systemctl is-active packages-tracker-backend
journalctl -u packages-tracker-backend | grep -i "scheduled"
```
