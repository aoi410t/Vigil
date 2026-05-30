# Vigil — FFLogs progression tracker

A dashboard that ingests FFLogs combat data for one FFXIV static and turns it
into per-pull, per-mechanic signal during ultimate progression. Answers
*where pulls are dying*, *whose originating mistake caused each wipe*, *are
we DPS-gated or mechanics-gated*, *how our prog compares to the field*, and
builds a crowd-mapped model of the boss's deterministic side.

Architecture and roadmap live in [PLAN.md](PLAN.md). Current version /
in-progress work / session log live in [PROGRESS.md](PROGRESS.md). Shipped
features per release in [CHANGELOG.md](CHANGELOG.md). Backlog and decision
log in [IDEAS.md](IDEAS.md). The working contract (session protocol,
versioning, new-idea intake) is in [CLAUDE.md](CLAUDE.md).

**Status:** 1.5.0 — all planned features (Phase 0–3, T-001..T-308) shipped,
plus storage-cleanup, deployment, FFLogs user-OAuth, and cactbot annotations.
342 tests passing.

---

## Quick start (assumes prerequisites met)

```powershell
git clone <this-repo> d:\Misc\Vigil
cd d:\Misc\Vigil

# Python side
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# Postgres database
createdb fflogs_tracker        # or psql -c "CREATE DATABASE fflogs_tracker;"

# Config
Copy-Item .env.example .env
# edit .env — set DATABASE_URL + FFLOGS_CLIENT_ID + FFLOGS_CLIENT_SECRET

# Schema
alembic upgrade head

# Web side
cd web
npm install
cd ..

# Tests
.venv\Scripts\python.exe -m pytest        # expect 342 passing

# Run (dev mode, two servers)
.venv\Scripts\python.exe -m uvicorn api.main:app --reload     # in one terminal
cd web; npm run dev                                            # in another
# dashboard at http://localhost:5173, api at http://127.0.0.1:8000
```

---

## Prerequisites

- **Python 3.11+** (3.12.4 is what the project was built against).
- **Node 20+** (24.15.0 is what the project was built against).
- **PostgreSQL 14+**. Tables use `JSONB` and `ARRAY` so it has to be Postgres,
  not SQLite. Local dev uses Postgres 18.
- **cloudflared** if you plan to expose the dashboard. Install from
  https://github.com/cloudflare/cloudflared/releases.
- **An FFLogs API client.** Create one at https://www.fflogs.com/api/clients/.
  Set redirect URL to `http://127.0.0.1:8800/auth/fflogs/callback` *if* you
  plan to use the Gold-tier user-OAuth flow (see "FFLogs Gold connect" below);
  client-credentials by itself doesn't need a redirect URL.

---

## Setup, step-by-step

### 1. Python environment

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

`pip install -e ".[dev]"` reads `pyproject.toml`, installs FastAPI / uvicorn /
SQLAlchemy / psycopg / alembic / httpx / pydantic-settings, plus pytest and
ruff. Editable install means changes to `api/`, `ingest/`, `analysis/`, etc.
take effect without re-installing.

### 2. PostgreSQL

```powershell
# from a psql shell:
CREATE DATABASE fflogs_tracker;
```

Connection string format SQLAlchemy expects:
`postgresql+psycopg://user:password@localhost:5432/fflogs_tracker`

### 3. `.env`

```powershell
Copy-Item .env.example .env
```

Required values:

- `FFLOGS_CLIENT_ID` / `FFLOGS_CLIENT_SECRET` — from your FFLogs API client.
- `DATABASE_URL` — the connection string from step 2.

Optional values (defaults in parens):

- `API_HOST` (`127.0.0.1`), `API_PORT` (`8000`) — dev FastAPI bind.
- `WEB_ORIGIN` (`http://localhost:5173`) — CORS allow-list for the Vite dev
  server. Only used in dev (single-origin prod mode skips CORS).
- `FFLOGS_REDIRECT_URI` (`http://127.0.0.1:8800/auth/fflogs/callback`) — used
  only if you connect a Gold FFLogs account; must match what's registered on
  your FFLogs API client.

`.env` is gitignored. Never commit it.

### 4. Migrations

```powershell
alembic upgrade head
```

Creates 15 tables. Re-runnable safely. Migrations live in `alembic/versions/`.

### 5. React bundle

```powershell
cd web
npm install
npm run build          # for prod
# or: npm run dev      # for dev (live reload)
cd ..
```

`npm run build` produces `web/dist/` — what the prod runner serves.
`npm run dev` runs Vite at `http://localhost:5173` with hot reload and a
proxy to `http://127.0.0.1:8000`.

### 6. Smoke test

```powershell
.venv\Scripts\python.exe -m pytest
# expect: 342 passed
```

---

## Running the dashboard

### Dev mode (two servers, hot reload)

In two terminals from the project root:

```powershell
# terminal 1 — FastAPI
.venv\Scripts\python.exe -m uvicorn api.main:app --reload

# terminal 2 — Vite
cd web; npm run dev
```

Browse to `http://localhost:5173`. The Vite dev server proxies `/api/*` to
FastAPI at `:8000`, so no CORS pain.

### Prod mode (single origin + Cloudflare quick tunnel)

The prod runner serves the built React bundle from FastAPI at `/`, gates
everything except `/healthz` with HTTP Basic auth, and opens a Cloudflare
quick tunnel for outside access.

One-time setup — create `.env.prod` (gitignored):

```ini
WEB_STATIC_DIR=d:\Misc\Vigil\web\dist
AUTH_USERNAME=<pick one>           # legacy dev fallback (see below)
AUTH_PASSWORD=<long random string> # shared with the static (user mode)
DEV_PASSWORD=<another long random> # dev-only (you, not shared)
```

Then:

```powershell
.\scripts\run_prod.ps1                        # default port 8800
# or: .\scripts\run_prod.ps1 -Port 9000
```

The script will:

1. Build `web/dist/` if it's missing (`npm run build`).
2. Load `.env.prod` into the process env.
3. Warn loudly if `AUTH_USERNAME` / `AUTH_PASSWORD` aren't set.
4. Start uvicorn on `127.0.0.1:<Port>`.
5. Start `cloudflared tunnel --url http://127.0.0.1:<Port>`.
6. Print the `*.trycloudflare.com` URL — bookmark it.

Ctrl-C cleans up both processes.

**Quick-tunnel URL changes only when the tunnel restarts.** Want a stable
hostname? See [Deployment upgrade path](#deployment-upgrade-path) below.

### Dev mode vs. user mode (v1.7.1)

The dashboard splits into two experiences depending on which password
matches at login:

- **`AUTH_PASSWORD` → user mode.** Whoever you share this with sees a clean
  onboarding flow on Home, gets their own auto-created static (named
  "`{username}`'s raid"), and never sees dev-flavored surfaces. Tabs:
  Home / Reports / Encounters / Roster. No Abilities, no Field data, no
  "show all encounters" toggle, no Default Static in the switcher.
- **`DEV_PASSWORD` → dev mode.** You keep everything: yellow `dev mode`
  pill in the header, Abilities tab + bulk review queue, Field data panel,
  "show all encounters" toggle, and the Default Static (id=1) holding all
  pre-1.6.0 ingested data + field backfill. Adds a +1 tab beyond user
  mode.
- **Backwards compat.** If `DEV_PASSWORD` is unset, the user matching
  `AUTH_USERNAME` (with `AUTH_PASSWORD`) keeps dev mode — so a pre-1.7.1
  deploy with just `AUTH_USERNAME=aoi` + `AUTH_PASSWORD` carries on
  exactly as before.

Username is free-form. Any name + the matching password works; the
`users` row is auto-created on first sighting. Switching passwords on a
subsequent login flips the `is_developer` flag accordingly.

---

## First-time data setup

### Bootstrap the ability database

```powershell
.venv\Scripts\python.exe -m scripts.bootstrap_abilities
```

Fetches FFXIV actions + statuses from XIVAPI keyed on `ability_game_id`,
runs the rule-based classifier, and populates the review queue. M-BURST and
M-MIT consume the labels. Idempotent. Pass `--force` to refetch known IDs,
`--limit N` to cap, `--skip-fetch` to re-classify without network.

After bootstrapping, low-confidence rows surface in the **Abilities** tab of
the dashboard — click into each to confirm or correct.

### Add your static to the roster

Dashboard → **Roster** tab.

- Add each member by name.
- For each member, add one or more character aliases — `(character_name,
  server)` pairs. Sub-accounts are fine; one member can have many aliases.
- **Don't store job on the member** — job is derived per-fight from
  CombatantInfo (T-107).

### Watch a report

Dashboard → **Reports** tab → **Watched** panel.

- Paste a report code or a `https://www.fflogs.com/reports/<code>` URL.
- Click **Poll now** to ingest immediately, or wait for a scheduled sweep
  (see "Scheduled polling" below).

The first ingest pulls report meta + all fights + all event types. Re-polls
only fetch new events past the ingestion ledger's cursor (PLAN Invariant 1
— cache-first, never re-pull what's stored).

### Optional: field backfill

For prog-vs-field comparison + boss-side fight model from cross-group
consensus:

```powershell
# one pass with defaults (25 reports/encounter, events for top 5)
.venv\Scripts\python.exe -m jobs.backfill_field

# customize
.venv\Scripts\python.exe -m jobs.backfill_field `
  --encounters 1079 101 102 103 104 105 1068 1065 `
  --reports-per-encounter 25 `
  --events-top-n 5

# dry run to see what would be fetched without writing
.venv\Scripts\python.exe -m jobs.backfill_field --dry-run
```

Run nightly via Task Scheduler if you want continuous field coverage.
Polite pacing (0.5s between FFLogs queries) is built in.

### Optional: FFLogs Gold connect

Free-tier client-credentials can't read archived or private reports — FFLogs
paywalls those behind their `/user` GraphQL endpoint. If you have a Gold
account, connect it once:

1. Make sure `FFLOGS_REDIRECT_URI` matches what's on your API client's
   "Redirect URLs" list (default: `http://127.0.0.1:8800/auth/fflogs/callback`).
2. Start the prod runner (`.\scripts\run_prod.ps1`).
3. Click **FFLogs Gold: Connect** in the dashboard header.
4. Approve on FFLogs → redirected back, status flips to **FFLogs Gold ✓**.

After that, every ingest path automatically falls back to `/user` for
archived reports. Refresh token rotates per FFLogs spec; we update on each
refresh.

### Scheduled polling (optional)

If you'd rather not click **Poll now** during raid:

```powershell
# one pass
.venv\Scripts\python.exe -m jobs.poll_watched
```

Wrap in Windows Task Scheduler (or cron on Linux) — runs through all active
watched reports, ingests deltas, captures errors per-row. The script is a
single pass; you control cadence externally.

---

## Daily operations

### During prog

- **Watched** panel — see the static's live reports. Click **Poll now**
  between pulls for fresh data.
- **Pull detail** (open a report → click a pull) shows wipe location, deaths
  with killing ability, damage takers, GCD drops, burst alignment, phase
  strip, parse table, consensus timeline, mit audit, fault scores, M-GATE
  verdict, cactbot expected-vs-actual timeline diff.
- **Manual prog points** under **Home** — record where the static reached
  by hand on days the parser was dark (post-patch ACT blackout).

### Strat editor

**Encounters** tab → pick encounter → **Strat** sub-tab.

- For each mechanic occurrence (`{ability_id}_{occurrence_idx}`), define a
  **mit plan** (`slots: [{ability_id, expected_role, window_offset_ms}, …]`)
  and **role assignments** (`{slot_name: role}`).
- 8 fixed roles: MT/OT/H1/H2/D1/D2/D3/D4, plus `any` / null.
- Recurring mechanics like Akh Morn carry per-occurrence configs by design —
  Akh Morn cast 3 doesn't have to mit the same way as cast 1.

The strat plan feeds **M-MIT** (mitigation audit) and **M-FAULT**
disambiguation (raidwide deaths with mits missed → mit-failure instead of
cascade).

### Session report

**Reports** tab → click **report** on a watchlist row → modal with a
Discord-pasteable Markdown summary covering: pulls/kills/wipes/duration,
best fp%, wipe-type breakdown, top killing abilities, mit hit rate,
per-player fault scores, worst per-mechanic consistency.

---

## Architecture (one page; full version in PLAN.md)

```
FFLogs API ──(delta, ledger-gated)──> /ingest ──> Postgres(raw events)
                                                       │
                                          /analysis + /model (read stored)
                                                       │
                                            /api (serves cached) ──> /web
```

- `/ingest` writes. Everything else reads. The ingestion ledger gates
  network calls so we never re-pull data already stored (PLAN §3 Invariant 1).
- `/analysis` has one module per metric: `wipes.py`, `faults.py`, `gcd.py`,
  `burst.py`, `phases.py`, `parse_trajectory.py`, `consensus.py`,
  `mechanic_classifier.py`, `cartography.py`, `dps_check.py`,
  `gate_diagnostic.py`, `prog_trajectory.py`, `strat_config.py`,
  `fault_attribution.py`, `mit_audit.py`, `fault_disambiguation.py`,
  `recovery.py`, `consistency.py`, `session_report.py`, `optimization.py`,
  `timeline_diff.py`.
- `/model` holds boss-side facts only (crowd-mappable). User strats are
  player-side and live in `strat_config` (never inferred from other groups).
- `/jobs` — `poll_watched.py` (live watchlist sweep), `backfill_field.py`
  (rate-limited nightly).
- `/vendor/cactbot/` — vendored cactbot timeline files for human-readable
  mechanic names; see [vendor/cactbot/NOTICE](vendor/cactbot/NOTICE).

Repo layout follows PLAN §4. Tests in `tests/` use real public FFLogs
report codes plus synthetic fixtures.

---

## Scripts tour

Run any of these with `.venv\Scripts\python.exe -m scripts.<name>`.

**Ingest / data:**

- `bootstrap_abilities` — fetch XIVAPI catalog, classify, populate review queue.
- `verify_fflogs` — sanity-check FFLogs OAuth + a sample report fetch.
- `verify_delta` — exercise T-004 ingest end-to-end on a discovered report.
- `verify_events` — exercise T-005 event normalization.
- `rescan_events` — wipe a report's events + reset ledger cursor, re-ingest.
- `ingest_fru_kills` — one-off used during T-104 dev; ingest extra FRU kills.
- `backfill_prune_combatants` — clean up T-109 combatant bloat on old reports.

**Verify / debug analysis:**

- `verify_wipes` — M-WIPE histogram on a report code.
- `verify_faults` — M-FAULT Mode-1 deaths + damage takers.
- `verify_gcd` — M-GCD drop detection.
- `debug_casts` — inspect raw cast events in a fight.
- `debug_death` — drill into one death event's surrounding window.

**Reference:**

- `list_zones` — list FFLogs zones with current-tier encounters.

**Job runners** (under `jobs/`, not `scripts/`):

- `poll_watched` — one sweep of active watched reports.
- `backfill_field` — one sweep of field-data discovery + ingest.

---

## Testing

```powershell
.venv\Scripts\python.exe -m pytest                 # all 342 tests
.venv\Scripts\python.exe -m pytest tests/test_burst.py    # one file
.venv\Scripts\python.exe -m pytest -k "consensus"  # by name pattern
```

Tests use a savepoint-rolled session fixture (`tests/conftest.py`) so they
don't trample dev DB data — but they DO run against the real Postgres in
`DATABASE_URL`. Don't point tests at a prod DB.

---

## Deployment upgrade path

The Cloudflare quick tunnel works but the URL is ephemeral and
`AUTH_USERNAME`/`AUTH_PASSWORD` is a stopgap. The named-tunnel upgrade gets
you a stable hostname + Cloudflare Access for auth.

**Outline** (all of this is operator work, no code change):

1. **Get a hostname** — three options:
   - Free: file a PR at https://github.com/is-a-dev/register for a
     `<name>.is-a.dev` subdomain. ~24–48 h merge time. Set the CNAME / A
     record to point at the Cloudflare tunnel's CNAME (Cloudflare gives you
     one when you create the named tunnel).
   - Free: `eu.org` subdomain — slower than is-a.dev to approve.
   - Paid: ~$10/yr for a domain (Namecheap, Porkbun, Cloudflare Registrar).
     Set nameservers to Cloudflare.
2. **Add the zone to Cloudflare DNS** if it isn't there.
3. **Create a named tunnel:**
   ```powershell
   cloudflared tunnel login           # one-time, opens browser
   cloudflared tunnel create vigil
   cloudflared tunnel route dns vigil vigil.<your-domain>
   ```
   This writes a credentials file (default `~/.cloudflared/<tunnel-id>.json`).
4. **Run with the named tunnel:**
   ```powershell
   cloudflared tunnel --config <config.yml> run vigil
   ```
   `config.yml`:
   ```yaml
   tunnel: vigil
   credentials-file: C:\Users\<you>\.cloudflared\<tunnel-id>.json
   ingress:
     - hostname: vigil.<your-domain>
       service: http://127.0.0.1:8800
     - service: http_status:404
   ```
5. **Enable Cloudflare Access** in the Cloudflare dashboard → Zero Trust →
   Access → Applications. Add the hostname, set policies (e.g. allow emails
   in a list).
6. **Unset `AUTH_USERNAME` / `AUTH_PASSWORD`** in `.env.prod` — Access is
   now in front, double auth would be obnoxious. The middleware no-ops when
   either is empty.

Restart the prod runner. The static now reaches the dashboard at
`https://vigil.<your-domain>` and gets the Cloudflare Access SSO challenge.

---

## Project state files (read these)

- [PLAN.md](PLAN.md) — architecture spec + dependency-ordered roadmap.
  Stable section anchors + task IDs (`T-xxx`).
- [PROGRESS.md](PROGRESS.md) — current version, what's shipped / in
  progress / next, and the session log.
- [CHANGELOG.md](CHANGELOG.md) — every shipped version with what changed.
- [IDEAS.md](IDEAS.md) — unscheduled + scheduled backlog. New ideas land
  here first and get inserted into PLAN.md by dependency order.
- [CLAUDE.md](CLAUDE.md) — working contract: session protocol, versioning,
  new-idea intake, invariants.

---

## Cactbot vendor

Timeline files for 8 encounters live under `vendor/cactbot/`, vendored from
https://github.com/OverlayPlugin/cactbot (Apache 2.0). They're used to
annotate the boss-side `fight_model` with human-readable mechanic names
and expected phase-relative timings, and to compute per-pull
expected-vs-actual drift (the **TimelineDiff** panel in PullDetail).

Refresh by re-fetching each file from cactbot's `ui/raidboss/data/`
subdirectories on `raw.githubusercontent.com`. See
[vendor/cactbot/NOTICE](vendor/cactbot/NOTICE) for the file inventory.

---

## License + attribution

This project is private. Cactbot timeline files are redistributed under
Apache 2.0 (see [vendor/cactbot/NOTICE](vendor/cactbot/NOTICE)). FFLogs data
remains the property of FFLogs / the report owners — only consume reports
you have permission to view.
