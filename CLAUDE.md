# CLAUDE.md — FFLogs Progression Tracker

This is the operating contract for building this project. Read it fully at the start of
every session. It imports the living state files below, which are the source of truth for
*what we're building*, *where we are*, and *what shipped*.

@PLAN.md
@PROGRESS.md
@CHANGELOG.md
@IDEAS.md

If any of `PROGRESS.md`, `CHANGELOG.md`, or `IDEAS.md` do not exist yet, this is the first
session: create them from the templates in §6 before doing anything else.

---

## 1. What this project is

A dashboard that ingests FFLogs data to track a high-end FFXIV static's progression on the
new ultimate — wipe location, fault attribution, burst alignment, GCD drops, parse
trajectory, a DPS-gated-vs-mechanics-gated diagnostic, and a crowd-mapped model of the
fight. `PLAN.md` is the full architecture and roadmap and is the authority on scope. This
file governs *how we work*, not *what we build*.

---

## 2. Source-of-truth files

- **PLAN.md** — architecture + phased roadmap. The spec. Changes only with a clear reason,
  and every change is logged in PLAN's own intent (see §5 for new-idea intake).
- **PROGRESS.md** — current version, what's shipped / in progress / next, and a dated
  session log of where we left off. Update it every session.
- **CHANGELOG.md** — the shipped record, versioned (see §4). Append on every ship.
- **IDEAS.md** — inbox for new ideas before they're scheduled into PLAN (see §5).

Never let these drift from reality. If code and docs disagree, fix the docs in the same
session.

---

## 3. Session protocol

**At session start:**
1. Read PLAN.md, PROGRESS.md, CHANGELOG.md, IDEAS.md (auto-imported above).
2. State the current version, current phase, and the single next task — confirm with the
   user before writing code.

**During the session:**
3. Build strictly toward the next task in PROGRESS / PLAN. Don't skip ahead to a feature
   whose prerequisites haven't shipped.
4. If a new idea or scope change comes up, route it through §5 — don't silently implement it.

**At session end (or whenever something ships):**
5. Update PROGRESS.md: move items between Shipped / In progress / Next up, and add a dated
   session-log entry describing where we left off, decisions made, and any blockers.
6. If anything shipped, append a CHANGELOG.md entry and bump the version (§4).
7. Leave the repo in a runnable state; note any half-finished work explicitly in PROGRESS.

---

## 4. Versioning

Semantic versioning, pre-1.0 while in development. Format `MAJOR.MINOR.PATCH`, starting
`0.1.0`.

- **MINOR** (`0.x.0`) — a roadmap feature/module from PLAN ships. Tag it to its PLAN item.
- **PATCH** (`0.x.y`) — fixes, refactors, or tweaks to already-shipped features.
- **1.0.0** — the full planned feature set (through PLAN Phase 3) is shipped and stable.
- After 1.0.0, normal semver: breaking change → MAJOR, new feature → MINOR, fix → PATCH.

The current version lives at the top of PROGRESS.md and as the latest entry in CHANGELOG.md.
Keep them in sync. (Optionally mirror it in a `VERSION` file or `package.json`/`pyproject`.)

**CHANGELOG entry format (Keep-a-Changelog style):**
```
## [0.2.0] — 2026-05-25
### Added
- Wipe location histogram (PLAN §6)
### Changed / Fixed / Removed
- ...
```
CHANGELOG holds only *shipped* versions. In-progress and next-up work lives in PROGRESS.md.

---

## 5. New-idea intake (dependency-ordered)

Any new idea must be **added to the plan, not just built on the spot**, and scheduled so it
**cannot negatively impact development**:

1. **Capture** it in IDEAS.md immediately (one line + a sentence of intent). Capturing is
   not committing to build it.
2. **Find its prerequisites** — which PLAN phase or feature must exist first for this to be
   built cleanly (e.g. anything needing the fight model must come after the inference
   engine ships; anything strat-aware comes after `strat_config` exists).
3. **Schedule by dependency, not excitement.** Place it in PLAN's roadmap *after* its latest
   prerequisite. An idea with no prerequisites that won't force rework of unshipped
   foundations may join the current phase's backlog; anything that would destabilize
   in-flight or unbuilt foundations is deferred until that foundation ships.
4. **Record the reasoning** in IDEAS.md when you move an idea into PLAN (what it depends on,
   where it landed, why). Then update PLAN.md's roadmap accordingly.

Rule of thumb: order the backlog so each item only depends on things already shipped or
shipping before it. Never schedule a feature ahead of its prerequisite.

---

## 6. First-session bootstrap (create these if missing)

**PROGRESS.md**
```
# Progress

Current version: 0.1.0
Current phase: Phase 0 — Foundation + Mode 1

## Shipped
(see CHANGELOG.md for detail)
- (nothing yet)

## In progress
- Project scaffold per PLAN §2

## Next up (dependency-ordered, from PLAN roadmap)
1. OAuth client-credentials module + delta ingestion + ingestion ledger
2. Postgres schema + migrations
3. Mode-1 dashboard: pull list, wipe histogram, death/avoidable-damage view

## Session log
### 2026-05-DD — kickoff
- Created repo, state files, confirmed stack. Where we left off: <fill in>.
```

**CHANGELOG.md**
```
# Changelog

All notable changes are recorded here. Versioning per CLAUDE.md §4.

## [0.1.0] — 2026-05-DD
### Added
- Repo scaffold, project state files, development contract.
```

**IDEAS.md**
```
# Ideas backlog

New ideas land here first, then get scheduled into PLAN.md by dependency (CLAUDE.md §5).

## Unscheduled
- (none yet)

## Scheduled into PLAN
- (none yet)
```

---

## 7. Technical guardrails (details in PLAN §2–§3, §7)

- Stack: Python + FastAPI, Postgres, React (Recharts + d3). Don't swap without logging it.
- **Caching is inviolable:** the FFLogs API is a write-once source, never a read path.
  Ingest the delta only, driven by the ingestion ledger; raw events live in Postgres; never
  re-pull data already stored.
- Key all game data on **ability IDs**, never names.
- **Boss-side vs. player-side:** the shared fight model holds only deterministic boss-side
  facts (crowd-mappable). Strats differ per group — `strat_config` is the user's own and is
  never inferred from other groups. Compare only boss-side outcomes across groups.
- **No vuln stacks** in this content: fault detection keys on killing-blow ability +
  avoidable damage, with Damage Down as a secondary survive-fault flag.
- Reality of the ACT launch blackout: no logs exist for ~2–3 days after release, real flow
  ~1 week. There is no June 2 deadline; harden against current-tier logs first.

## 8. Definition of done (per feature)

A feature is "shipped" (and earns a version bump) when it: runs against real FFLogs data
(current-tier until the ultimate's logs exist), reads from stored events rather than
re-querying the API, is surfaced in the UI where applicable, and is reflected in PROGRESS.md
+ CHANGELOG.md with the version bumped.
