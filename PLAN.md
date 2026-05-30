# PLAN.md — FFLogs Progression Tracker

Architecture, specs, and dependency-ordered roadmap. This is the **what to build**.
`CLAUDE.md` is the **how we work** (session protocol, versioning, new-idea intake).

## How to read this document (for the agent)
- Section anchors (`§N`) and task IDs (`T-xxx`) are **stable** — `PROGRESS.md`,
  `CHANGELOG.md`, and `IDEAS.md` reference them. Do not renumber existing IDs.
- This file holds **spec + roadmap only**. Current status lives in `PROGRESS.md`; do not
  track progress here.
- Build in task order. A task may start only when every task in its `Depends` list is
  shipped. New ideas are scheduled by the dependency rule in §11 / `CLAUDE.md §5`.
- The §3 invariants are non-negotiable and override convenience.

---

## §1 Goal

A dashboard that ingests FFLogs combat data for one FFXIV static and turns it into
per-pull, per-mechanic signal during progression on the new ultimate. It answers:
1. Where are we losing pulls, and whose **originating** mistake caused each wipe.
2. Are we **DPS-gated or mechanics-gated**.
3. How does our prog compare to the field, on the parts that are actually comparable.
It also builds a crowd-sourced model of the fight's boss-side structure since no timeline
exists at launch.

---

## §2 Domain glossary (read before building — the agent will not know these)

- **Static**: a fixed 8-player raid group.
- **Pull**: one attempt at the encounter. **Wipe**: a failed pull (party resets).
  **Clear / kill**: a successful pull.
- **Prog (progression)**: the stretch of repeated pulls before a group's first clear.
- **Ultimate**: the hardest, longest, multi-phase content tier. The new one releases
  ~2026-06-02. **Savage**: the standard hard tier; current Savage logs are the dev dataset.
- **Phase**: a scripted segment of a fight, gated by boss HP% or a scripted transition.
- **Enrage**: a hard timer; if the boss is not dead by it, an unsurvivable cast wipes the
  party. Defines the **DPS check**.
- **Mechanic**: a scripted boss action the party must respond to. Types:
  - **Raidwide**: damage to all 8; survivable with mitigation/healing.
  - **Tankbuster**: large hit aimed at tank(s).
  - **Body-check / tower / soak**: needs a specific number of players in a spot or it fails
    (usually lethal).
  - **Spread / stack / tether**: positional requirements on specific players.
- **Mitigation ("mit")**: defensive cooldowns reducing incoming damage (e.g. Reprisal,
  Feint, Addle, tank invulnerabilities, healer shields/mits).
- **2-minute burst / raid buffs**: most jobs have damage cooldowns on a ~120s cycle; the
  party aligns them so personal burst overlaps party-wide damage buffs (e.g. Divination,
  Battle Litany, Brotherhood, Searing Light, Technical Finish, Embolden). A **burst window**
  is the overlap interval.
- **GCD (global cooldown)**: the recurring ~2.5s cooldown gating weaponskills/spells. A
  **dropped GCD** is a gap with no GCD cast (lost uptime). **oGCD**: off-GCD ability woven
  between GCDs. **Skill/Spell Speed**: stats that shorten the GCD.
- **aDPS / rDPS**: FFLogs damage metrics that account for raid buffs (credit shared with
  enablers). Used for parse trajectory.
- **Parse / percentile**: FFLogs ranks a *kill* against others as a percentile. **Only
  exists on kills** — never on wipes.
- **Vulnerability Up ("vuln")**: a stacking damage-taken debuff common in *lower* content.
  In ultimates, failures mostly kill outright instead — see §3. **Damage Down**: a debuff
  lowering your damage output, sometimes applied as a survive-your-mistake penalty.
- **ACT**: desktop tool that captures combat and uploads to FFLogs. Breaks for ~2–3 days
  after a major patch until its plugin is updated.
- **Report**: one FFLogs logging session (one report `code`) containing many **fights**
  (pulls).

---

## §3 Invariants (MUST / MUST NOT — override convenience)

1. **Cache-first.** The FFLogs API is a **write-once source, never a read path**. Ingest
   the **delta only** (new fights/events not already stored), driven by the ingestion
   ledger (§6). MUST NOT re-pull data already in Postgres.
2. **Key on game ability IDs**, never localized names (names vary by region/language).
3. **Boss-side vs. player-side separation.** The shared `fight_model` (§6, §8) holds **only
   deterministic boss-side facts** (cast order, timing, phases, enrage, ability→target/type)
   and is crowd-mappable. **Strats differ per group** — `strat_config` is the user's own,
   hand-authored, and MUST NOT be inferred from other groups.
4. **Cross-group comparison uses boss-side outcomes only** (which boss ability killed, phase
   reached, damage vs. boss HP). MUST NOT assume another group's positioning maps to ours.
5. **No vuln stacks** for fault detection in this content. Use **killing-blow ability +
   avoidable damage-taken** as primary signals; **Damage Down** as a secondary
   survive-fault flag.
6. **Parses are kill-only.** During prog, track **normalized aDPS per phase reached**, not
   percentiles.
7. **No June 2 deadline.** Logs do not exist for ~2–3 days post-release and ramp over ~1
   week. Develop and validate against **current-tier (Savage) logs** first.

---

## §4 Stack & repo layout

Stack: **Python + FastAPI** (analysis benefits from numpy/pandas/scikit-learn), **Postgres**,
**React + Recharts + d3** (d3/SVG for the mechanic-timeline Gantt and burst bars). Migrations
via alembic. Do not swap stack components without logging the decision.

```
/ingest    FFLogs OAuth client, paginated event pull, delta logic, ingestion ledger
/db        SQLAlchemy models + alembic migrations
/analysis  wipes, faults, bursts, gcd, mits, recovery, consistency, parse
/model     fight inference: segmentation, consensus, classification (boss-side only)
/api       FastAPI routes serving computed/cached results
/web       React dashboard
/jobs      scheduled delta ingestion (own static) + nightly field backfill
/tests     fixtures use real public report codes (current tier)
```

---

## §5 Architecture (data flow)

```
FFLogs API ──(delta, ledger-gated)──> /ingest ──> Postgres(raw events)
                                                      │
                                          /analysis + /model (read stored)
                                                      │
                                            /api (serves cached results) ──> /web
```
Ingestion writes; everything else reads from Postgres. Expensive computed outputs are
memoized in `analysis_cache`. Two jobs: live polling of the static's open reports, and a
rate-limited nightly field backfill.

---

## §6 Data model

Pseudo-DDL; refine types in migrations. Index notes are mandatory.

```
reports(
  code TEXT PK,            -- FFLogs report code
  owner TEXT, region TEXT, is_public BOOL,
  start_time TIMESTAMPTZ, end_time TIMESTAMPTZ,
  ingested_at TIMESTAMPTZ
)

ingestion_ledger(          -- the delta/dedup engine (Invariant 1)
  report_code TEXT PK REFERENCES reports(code),
  fights_ingested INT[],   -- fight IDs already stored
  last_event_ts BIGINT,    -- last event timestamp pulled
  status TEXT,             -- 'open' (poll for delta) | 'complete' (never refetch)
  last_polled_at TIMESTAMPTZ
)

fights(
  id BIGINT PK,
  report_code TEXT REFERENCES reports(code),
  fight_id_in_report INT,
  encounter_id INT,
  is_kill BOOL,
  fight_percentage NUMERIC,  -- progress reached
  last_phase INT,
  start_time BIGINT, end_time BIGINT, duration_ms BIGINT
)  -- INDEX (encounter_id), (report_code, fight_id_in_report) UNIQUE

combatants(
  fight_id BIGINT REFERENCES fights(id),
  player_id INT, name TEXT, server TEXT, job TEXT,
  stats JSONB,               -- from CombatantInfo: skill/spell speed, etc.
  PRIMARY KEY (fight_id, player_id)
)

events(                      -- append-only, the big table
  id BIGSERIAL PK,
  fight_id BIGINT REFERENCES fights(id),
  ts BIGINT,
  type TEXT,                 -- damage|heal|cast|applybuff|applydebuff|death|combatantinfo
  source_id INT, target_id INT,
  ability_game_id INT,       -- KEY ON THIS, never names (Invariant 2)
  amount BIGINT,
  raw JSONB
)  -- INDEX (fight_id, ts), (ability_game_id), (fight_id, type)

fight_model(                 -- boss-side ONLY (Invariant 3), versioned
  encounter_id INT, version INT,
  phase INT, seq INT,
  ability_game_id INT,
  relative_t_ms BIGINT,      -- median time from phase start
  time_variance_ms BIGINT,
  type_label TEXT,           -- raidwide|tankbuster|tower|spread|stack|tether|enrage|unknown
  confidence NUMERIC,        -- 0..1
  meta JSONB,                -- e.g. expected soak count, target role
  updated_at TIMESTAMPTZ,
  PRIMARY KEY (encounter_id, version, phase, seq)
)

strat_config(               -- user's own (Invariant 3), never inferred
  encounter_id INT, mechanic_ref TEXT,  -- references a fight_model row
  assignments JSONB,        -- who soaks/tethers/positions
  mit_plan JSONB,           -- expected mit ability_game_ids + window
  PRIMARY KEY (encounter_id, mechanic_ref)
)

fault_scores(               -- derived
  fight_id BIGINT, player_id INT,
  score NUMERIC, reasons JSONB,
  PRIMARY KEY (fight_id, player_id)
)

prog_points(                -- supports manual backfill of ACT-dark days
  id BIGSERIAL PK, ts TIMESTAMPTZ, phase INT, fight_percentage NUMERIC,
  pull_count INT, source TEXT  -- 'auto' | 'manual'
)

analysis_cache(
  fight_id BIGINT, module TEXT, result JSONB, computed_at TIMESTAMPTZ,
  PRIMARY KEY (fight_id, module)
)
```

---

## §7 FFLogs API integration

- **Base**: GraphQL v2. Public data at `https://www.fflogs.com/api/v2/client` via **OAuth
  client-credentials** (exchange client_id/client_secret for a token). Private reports use
  the user flow at `/api/v2/user` — only if needed.
- **Expected query shapes** (confirm exact field names against the self-documenting GraphQL
  schema before relying on them):
  - `reportData.report(code) { fights { id, encounterID, kill, fightPercentage, lastPhase,
    startTime, endTime } }`
  - `reportData.report(code).events(dataType, fightIDs, startTime, endTime) { data,
    nextPageTimestamp }` — dataTypes needed: `DamageDone`, `DamageTaken`, `Casts`, `Buffs`,
    `Debuffs`, `Deaths`, `CombatantInfo`.
  - `reportData.report(code).masterData` / `.playerDetails` for actor/role mapping.
  - `worldData.encounter(id) { fightRankings, characterRankings }` → public report codes +
    fight IDs for the field backfill.
- **Pagination**: loop on `nextPageTimestamp` until null; persist as you go.
- **Rate limits**: points-based; events are the costly call. Request only needed fields,
  obey the ledger (Invariant 1), and rate-limit the nightly backfill.
- **Open reports**: a live session appends fights to the same `code`. Re-poll an `open`
  report for new fight IDs / events past `last_event_ts`; flip to `complete` when the
  session ends.

---

## §8 Operating modes

- **Mode 1 — strat-free, kill-signal based.** Works once logs exist (~day 3–7) with zero
  fight knowledge: avoidable damage-taken, killing-blow ability, GCD drops, death-by-ability,
  Damage Down where present. No fight model or strat needed.
- **Mode 2 — strat-aware + model-backed.** Activates when `fight_model` has confidence and
  `strat_config` is filled: true fault attribution, mit audit, gated/not-gated diagnostic.
- Never block Mode 1 on Mode 2. There is no launch-night data (parser down for everyone).

---

## §9 Analysis & model module specs

Each module: inputs (event types) → method → output. Mode and task ID noted.

- **M-WIPE wipe-location histogram** (Mode 1, T-006): `fights` + boss `Casts` → bucket wipes
  by phase/mechanic via timestamp→cast mapping → counts per phase/mechanic.
- **M-FAULT fault attribution** (Mode 2 core, T-302): pipeline —
  (a) classify wipe type from `fights` (mechanics vs. enrage/DPS-check vs. body-check);
  (b) primary signals = killing-blow ability on `Deaths` + avoidable `DamageTaken`; secondary
  = `Damage Down` debuff (survived-but-faulted);
  (c) causal ordering: root vs. cascade distinguished because their killing abilities differ
  (avoidable mechanic's own hit = root; follow-up raidwide = cascade), mapped via
  `fight_model` ability roles;
  (d) disambiguate raidwide deaths with the mit audit (mits up → amplified by earlier
  failure; mits down → mitigation problem);
  (e) emit `fault_scores` (score + reasons), aggregated weekly. Never a single-name verdict.
- **M-BURST 2-min burst alignment** (Mode 1, T-105): `Buffs` for raid-buff IDs → define
  shared windows → per-player burst-in-window vs. drift.
- **M-GCD gcd drops** (Mode 1, T-008): `Casts` → isolate GCDs → gaps vs. expected GCD (from
  `CombatantInfo` speed) → dropped-GCD count + timeline positions. Validate vs. XIVAnalysis.
- **M-PARSE parse trajectory** (Mode 1, T-106): normalized aDPS per phase per pull; real
  percentiles only after clear (Invariant 6).
- **M-GATE DPS-gated vs mechanics-gated** (Mode 2, T-207): our phase-X aDPS vs. progressing
  groups + M-CART → verdict per phase.
- **M-MIT mitigation audit** (Mode 2, T-303): `Buffs` for mit IDs around each raidwide window
  vs. `strat_config.mit_plan` → missed-mit list. Feeds M-FAULT(d).
- **M-RECOV recovery/resilience** (Mode 2, T-305): deaths followed by recovery vs. wipe; rez
  speed; Swiftcast usage.
- **M-CONS consistency per mechanic** (Mode 2, T-306): clean-execution rate per recurring
  mechanic across pulls.
- **M-CART failure cartography** (Mode 2, T-206): global death-by-**boss**-ability map (field)
  cross-referenced with ours → universal wall vs. you-problem (Invariant 4).
- **M-INFER fight-structure inference** (T-103/104/202/203/204): boss-side only (Invariant 3):
  1. **Phase segmentation**: untargetable windows + recurring transition casts + HP thresholds;
     `last_phase` as coarse label.
  2. **Consensus timeline**: zero boss `Casts` to phase start; abilities recurring in-order at
     low time-variance across pulls (>~70% of qualifying pulls) are canonical.
  3. **Classification**: by effect signature — hits all 8 → raidwide; big single tank hit →
     tankbuster; wrong-count → tower/body-check; end-of-phase mass death regardless of HP →
     enrage.
  4. **Empirical DPS check**: boss HP at enrage timestamp across groups (estimate from
     furthest-progressing groups pre-kill).
  Use robust stats (medians, consensus thresholds); down-weight outlier pulls.

---

## §10 UI / screens (build per phase gate, not upfront)

- **Home / prog dashboard** (T-009 basic; enrich later): gated-vs-mechanics headline verdict,
  prog-point curve, pulls/time invested, consistency-per-mechanic.
- **Pulls** (T-009): session-grouped list → **pull detail** (timeline: deaths by killing
  ability, avoidable damage, GCD drops, mit audit, burst windows, fault breakdown).
- **Fight map** (T-208): inferred boss-side timeline (Gantt) with labels + confidence, phase
  markers, enrage; failure-cartography heatmap.
- **Compare** (T-208): prog-vs-field curve, phase-X DPS vs. clearing groups, death-by-boss-
  ability you-vs-field.
- **Strat** (T-301): assignment + mit-plan editor (feeds Mode 2).
- **Reports** (T-307): Discord session-summary generator.
Do not build Fight map / Compare before their Phase-2 data exists.

---

## §11 Roadmap (dependency-ordered task graph)

A task may start only when all `Depends` tasks are shipped. Acceptance criteria (AC) are the
ship test. Phase gates are hard.

### Phase 0 — Foundation + Mode 1 (now → through the ACT blackout)

| ID | Task | Depends | AC |
|----|------|---------|----|
| T-001 | Repo scaffold (§4 layout), dev env, version 0.1.0 | — | Dirs exist; app boots; `0.1.0` recorded |
| T-002 | FFLogs OAuth client-credentials module | T-001 | Obtains + refreshes token; fetches a known public report |
| T-003 | Postgres schema + migrations (all §6 tables) | T-001 | Migrations apply clean; ledger present |
| T-004 | Delta ingestion + ingestion ledger | T-002, T-003 | Ingests a public report once; rerun adds 0 rows for `complete`; `open` fetches only new fights |
| T-005 | Event normalization into `events` | T-004 | All §7 dataTypes stored, keyed on `ability_game_id` |
| T-006 | M-WIPE wipe histogram (analysis + API) | T-005 | Wipes bucketed by phase/mechanic for a report |
| T-007 | Mode-1 fault basics (death-by-ability + avoidable dmg) | T-005 | Per-pull deaths w/ killing ability + avoidable-damage takers |
| T-008 | M-GCD gcd-drop detection | T-005 | Per-player dropped-GCD count + positions; matches XIVAnalysis on a sample |
| T-009 | Mode-1 dashboard (Home basic, Pulls list, pull detail) | T-006, T-007, T-008 | Screens render real current-tier data |
| T-010 | Manual prog-point entry | T-003 | User adds dated prog points; show on curve |
| T-011 | Static roster + character aliases (schema + CRUD editor) | T-003 | User can add/edit members; each member has 1+ character names (sub-accounts allowed); **job is never stored on the member** |

**Phase 0 gate:** ingest a real report and view wipe histogram + per-pull death/avoidable
view + GCD drops, entirely from stored data.

### Phase 1 — Capture + early inference (when logs flow, ~day 3–7+)

| ID | Task | Depends | AC |
|----|------|---------|----|
| T-101 | Live polling of static's open reports | T-004 | New pulls auto-ingest within a session (delta only) |
| T-102 | M-PARSE-less prog-point tracker (auto + manual) | T-101, T-010 | Furthest phase/session over time; pulls + hours |
| T-103 | Phase segmentation (boss-side) | T-005 | Phase boundaries detected on current-tier sample |
| T-104 | Consensus timeline from own pulls | T-103 | Provisional boss-side timeline w/ variance |
| T-105 | M-BURST burst alignment | T-005, T-108 | Per-player burst-in-window vs. drift |
| T-106 | M-PARSE parse trajectory (normalized aDPS/phase) | T-005, T-103 | Per-phase aDPS trend per player |
| T-107 | Combatant → member resolution (job derived per fight from CombatantInfo) | T-005, T-011 | Each fight's combatants link to roster members by character name; per-fight member→job mapping queryable; member can have multiple aliases / sub-accounts |
| T-108 | Ability DB (XIVAPI Action+Status) + wiki trait scrape + auto-classifier with review queue | T-005 | `abilities` populated from XIVAPI keyed on `ability_game_id`; `ability_labels` populated by rule-based classifier with `(label, confidence, source)`; low-confidence rows surface in a UI review queue; M-BURST/M-MIT read `auto-high` + `user-confirmed` only |
| T-109 | Tighten T-004 combatant filter for Ultimate reports | T-004 | `combatants` only contains actors that appear as source_id in this fight's events (intersect masterData.actors with the per-fight active-source set); on a re-ingest of a known Ultimate report the row count drops from ~12k/fight to the actual party size; downstream "active players" workarounds in T-203/T-206 still work but become redundant |

**Phase 1 gate:** static pulls auto-ingest; prog curve live; provisional boss-side timeline
from own pulls.

### Phase 2 — Inference matures + field comparison (~week 2+)

| ID | Task | Depends | AC |
|----|------|---------|----|
| T-201 | Field backfill job (rankings→codes→fetch, rate-limited, ledger-deduped) | T-004 | Pulls new public reports nightly; no dupes |
| T-202 | Cross-group consensus timeline + confidence | T-104, T-201 | `fight_model` rows w/ confidence per mechanic |
| T-203 | Mechanic classification (unsupervised) | T-202 | Each canonical ability labeled w/ type |
| T-204 | Empirical DPS check derivation | T-202 | Per-phase DPS target, self-updating |
| T-205 | Prog-vs-field curve | T-102, T-201 | Our prog vs. field distribution |
| T-206 | M-CART failure cartography (boss-ability-keyed) | T-203, T-201 | Universal-wall vs. you-problem per mechanic |
| T-207 | M-GATE gated vs mechanics-gated diagnostic | T-204, T-106, T-206 | Per-phase verdict |
| T-208 | Fight map UI + Compare UI | T-202, T-205, T-206 | Screens render real model + field data |

**Phase 2 gate:** confidence-rated boss-side model from field; gated/not-gated verdict live;
comparison screens render.

### Phase 3 — Strat-aware + polish (ongoing → 1.0.0)

| ID | Task | Depends | AC |
|----|------|---------|----|
| T-301 | `strat_config` editor (UI + schema use) | T-003 | User authors assignments + mit plan per mechanic |
| T-302 | M-FAULT strat-aware attribution | T-203, T-301, T-007 | `fault_scores` with root-vs-cascade separation |
| T-303 | M-MIT mitigation audit | T-203, T-301, T-108 | Missed-mit list per raidwide window |
| T-304 | Fault disambiguation via mit audit | T-302, T-303 | Raidwide deaths classed amplified vs. mit-failure |
| T-305 | M-RECOV recovery/resilience | T-005, T-203 | Recovered-vs-fatal deaths; rez metrics |
| T-306 | M-CONS consistency per mechanic | T-203, T-104 | Clean-execution rate per recurring mechanic |
| T-307 | M-REPORT session export (Discord) | T-006, T-007, T-303, T-102 | Pasteable per-session summary |
| T-308 | Post-clear optimization mode | T-105, T-106 | Real percentiles + burst/uptime polish on kills |
| T-309 | Drag-and-drop visual strat editor (polish on T-301) | T-301 | Drag mit slots onto a phase timeline; visual overlap indicators; click-to-add from labeled abilities. Defer until form-based T-301 is in use and proves to be the bottleneck. |

**1.0.0:** all Phase-3 tasks shipped and stable (T-309 optional polish; not gating).

---

## §12 Ordering rule for new tasks (summary; full intake in `CLAUDE.md §5`)

New ideas are captured in `IDEAS.md`, then assigned the next free `T-` ID and inserted into
the table above **after their latest prerequisite** — never before. An idea that would force
rework of unshipped foundations is deferred until that foundation ships. Record the
dependency reasoning in `IDEAS.md` when scheduling.

---

## §13 Open questions / assumptions (confirm with user or live API)

1. Exact GraphQL field names and the `Deaths` event's killing-ability field — verify against
   the live schema (§7) before coding queries.
2. ~~Canonical lists of **raid-buff** and **mitigation** `ability_game_id`s~~ — **decided
   2026-05-23 (revised):** scheduled as **T-108** — pull full action + status catalog from
   XIVAPI (`/Action`, `/Status`) keyed on `ability_game_id`; secondary wiki scrape for
   trait-modified values; rule-based classifier emits labels with confidence; low-confidence
   rows surface in a UI review queue. M-BURST (T-105) and M-MIT (T-303) updated to depend
   on T-108. See IDEAS.md.
3. ~~Static size, job composition, region, log visibility~~ — **decided 2026-05-23:** static
   is user-editable; members are not tied to a job; job is derived per fight from
   CombatantInfo. See **T-011** (Phase 0) and **T-107** (Phase 1). Region and log visibility
   still TBD.
4. ~~Hosting target~~ — **decided 2026-05-23:** local Postgres for dev, plan for a cloud
   deploy after Phase 1. Keep secrets/config deploy-friendly.
5. Whether to seed the boss-side model from community-datamined timelines once available
   (optional hybrid; inference is the default per §9 M-INFER).
