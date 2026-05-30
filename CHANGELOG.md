# Changelog

All notable changes are recorded here. Versioning per CLAUDE.md §4.

## [1.17.1] — 2026-05-27 — Auto-refresh fight_model on report ingest

### Why
v1.17.0 unified cloned encounter IDs but the fight_model only rebuilt when an operator explicitly POSTed `/api/encounters/{id}/fight-model/persist` (then `classify` + `annotate-cactbot`). The user wants this auto-triggered on each new report ingest so every fresh pull contributes to the canonical model immediately — particularly important during new-ultimate prog where every kill is rare and load-bearing.

### Added — `analysis/fight_model_refresh.py`
- `refresh_fight_model_for_encounter(session, encounter_id, *, throttle_seconds=60, force=False)` — runs the 3-step pipeline `write_consensus_to_fight_model` → `classify_canonical_abilities` → `annotate_fight_model_for_encounter` for one canonical encounter (v1.17.0 helpers canonicalize input). Returns a structured summary `{encounter_id, skipped, persist?, classify?, annotate?, error?}`.
- **Throttle**: queries `MAX(FightModel.updated_at)` for the canonical encounter; skips if last refresh < `throttle_seconds` ago. State lives in the table itself, so the throttle is durable across server restarts.
- **`skipped` outcomes**: `"throttle"` (recent refresh), `"no_data"` (consensus produced 0 rows — need ≥3 kills with events), `"error_persist"` / `"error_classify"` / `"error_annotate"` (each step's failure is captured + rolled back without crashing the caller), or `None` on full success.
- `refresh_for_report(session, code, ...)` — convenience wrapper that finds every canonical encounter the report has fights for and refreshes each. Used by the poll path; returns one entry per encounter touched.
- `encounter_ids_for_report` — exported as a helper so callers can pre-filter.

### Changed — `jobs/poll_watched._poll_one_row` auto-refreshes on success
After `ingest_events_for_report` commits, the poll path now calls `refresh_for_report` for any encounter this report contributes to. Only fires when `meta.new_fights > 0` OR `ev.events_inserted > 0` (no-op polls skip the refresh entirely). The new optional kwarg `auto_refresh_fight_model=True` lets tests / ad-hoc scripts bypass.

Failure semantics: refresh errors land in the response as `fight_model_refresh_error` but don't flip the poll's `status` away from `"ok"` — the ingest already committed. This applies to both the live poller (`poll_once`) and the Poll-now endpoint (`POST /api/watched-reports/{code}/poll`), since both go through `_poll_one_row`.

### Changed — `jobs/backfill_field.backfill_once` refreshes per-encounter
After each encounter's reports + events are pulled, the canonical fight_model refreshes with `force=True` (nightly cadence already debounces; we want every backfill pass to land in the model). Failures land in `per_enc["errors"]` without aborting the encounter loop. Opt-out via the new `auto_refresh_fight_model=False` kwarg.

### Cost note
`classify_canonical_abilities` re-scans every kill's damage events on each refresh. On an Ultimate with hundreds of kills this is multi-second. Acceptable at the 60s debounce default (≤1× per encounter per polling cycle); if a future deployment polls every ~30s during prog sessions, incremental classification could be added later. Not in scope for v1.17.1.

### Tests
- 8 new in [tests/test_fight_model_refresh_v1_17_1.py](tests/test_fight_model_refresh_v1_17_1.py): end-to-end (persist + classify + annotate), throttle skips recent runs, `force=True` bypasses throttle, `no_data` outcome on encounters with too few kills, `refresh_for_report` returns one per canonical encounter, canonicalizes legacy aliases, empty result for unknown report, poll path skips refresh when no new data was ingested, `auto_refresh_fight_model=False` disables the wire-in.
- **536 tests passing** (528 → 536, +8). No regressions.

### Operator notes
- No schema migration, no new env vars. Existing operators get auto-refresh for free on next Python restart.
- The 60s throttle is per-canonical-encounter; a multi-encounter prog day (running both M9S + DSR) refreshes both independently.
- Field backfill bypasses the throttle on purpose — the nightly cadence makes the debounce moot, and we want maximum model freshness from each pass.

## [1.17.0] — 2026-05-27 — Canonical encounter unification (cloned-encounter merge)

### Why
FFLogs occasionally cuts the same logical encounter into a new `encounter_id` after a re-cut. DSR today is split across **1065 (legacy)** and **1076 (current)**. Before this ship, every per-encounter analytics function filtered `Fight.encounter_id == X`, so a fight_model built for 1076 only learned from 1076 reports — missing ~27 field reports / 15 kills_with_events that live under 1065. Same problem will hit any future re-cut. Project scope per memory: all ultimates + Dawntrail savages — DSR is in-scope, so the gap was material.

### Added — `analysis/_encounter.py`
Two helpers + a tiny lookup table:
- `canonical_encounter_id(eid)` — returns the canonical ID for any member of a cloned group (1065 → 1076), or `eid` unchanged.
- `encounter_id_group(eid)` — returns the sorted tuple of every ID in the group (e.g. `(1065, 1076)`), or `(eid,)` for non-cloned.
- `_CLONED_GROUPS: tuple[tuple[int, ...], ...] = ((1076, 1065),)` — append to this when FFLogs re-cuts another encounter. First element is canonical, remaining are legacy aliases.
- 7 unit tests in [tests/test_encounter_canonical.py](tests/test_encounter_canonical.py).

### Changed — analysis layer unions across the cloned group
Every encounter-scoped analysis function now uses `Fight.encounter_id.in_(encounter_id_group(X))` instead of `== X`, and uses `canonical_encounter_id(X)` for `FightModel` / `StratConfig` reads and writes (those tables live at the canonical ID only). Updated modules:
- [analysis/consensus.py](analysis/consensus.py) — `consensus_timeline_for_encounter`, `write_consensus_to_fight_model`, `read_fight_model`. Consensus now learns from kills under EITHER half of the group; writes only to canonical.
- [analysis/cartography.py](analysis/cartography.py) — `cartography_for_encounter` unions fights + scoped watchlist queries.
- [analysis/mechanic_classifier.py](analysis/mechanic_classifier.py) — `_kill_fight_ids` + `classify_canonical_abilities`.
- [analysis/dps_check.py](analysis/dps_check.py) — `dps_check_for_encounter`, `dps_comparison_for_encounter`, `compare_fight_to_target`.
- [analysis/mit_audit.py](analysis/mit_audit.py) — `_raidwide_casts` (fight_model lookup), strat_config lookups, `mit_audit_aggregate_for_encounter`.
- [analysis/fault_attribution.py](analysis/fault_attribution.py) — `classify_wipe_type`, `_aoe_party_casts`, `compute_fault_scores_for_fight`, `fault_aggregate_for_encounter` (5 sites total).
- [analysis/fault_breakdown.py](analysis/fault_breakdown.py) — `fault_breakdown_for_encounter` unions FaultScores across the group.
- [analysis/fault_disambiguation.py](analysis/fault_disambiguation.py) — `_raidwide_ability_ids` reads at canonical.
- [analysis/consistency.py](analysis/consistency.py) — `_our_fight_ids` + `consistency_for_encounter` reads.
- [analysis/prog_trajectory.py](analysis/prog_trajectory.py) — our-sessions + field-distribution queries union across group.
- [analysis/optimization.py](analysis/optimization.py) — `_our_kill_fights` unions across group.
- [analysis/death_inference.py](analysis/death_inference.py) — `build_inference_context` reads fight_model + cactbot timeline at canonical.
- [analysis/timeline_diff.py](analysis/timeline_diff.py) — `timeline_diff_for_fight` looks up fight_model + cactbot timeline at canonical.
- [analysis/strat_config.py](analysis/strat_config.py) — `list_for_encounter`, `get_one`, `upsert`, `delete_one` all key on canonical so a strat authored for DSR 1076 applies to fights from 1065 too.
- [analysis/gate_diagnostic.py](analysis/gate_diagnostic.py) — surfaces canonical encounter_id in response.

Every updated function also returns the canonical ID in its `encounter_id` response field. UI dedupe (below) reads this back consistently.

### Changed — cactbot vendor map keyed on canonical
[ingest/cactbot.py](ingest/cactbot.py) `TIMELINE_FILES` rekeyed: `1065 → 1076` for DSR (the current canonical). `load_timeline_for_encounter` canonicalizes its input before lookup so legacy-aliased callers still resolve. `annotate_fight_model_for_encounter` reads + writes fight_model at canonical.

### Changed — API endpoints dedupe cloned IDs in listings
- [api/main.py](api/main.py) `GET /api/me/encounters` — cloned encounter rows from the underlying `Fight` aggregate collapse into one canonical row. Counts merge (pulls / kills / wipes / latest_end_time).
- [api/main.py](api/main.py) `GET /api/encounters` — same dedupe, with distinct-report-code counts so reports under both halves of a group aren't double-counted.
- [jobs/backfill_field.py](jobs/backfill_field.py) `field_stats` — input encounter IDs canonicalize first; duplicate canonical entries (e.g. passing both 1065 and 1076) collapse into one row.
- `DEFAULT_ENCOUNTERS` keeps BOTH 1076 and 1065 so backfill discovery hits both halves of the FFLogs `fightRankings` (rankings are served separately per encounter_id).

### Changed — React UI removes "(alt)" rows
Cloned encounter labels collapse to canonical only. [web/src/Encounters.jsx](web/src/Encounters.jsx), [web/src/FieldStats.jsx](web/src/FieldStats.jsx), and [web/src/Home.jsx](web/src/Home.jsx) lose the `1065: 'DSR (alt)'` entry. The API now only emits canonical IDs in encounter-listing endpoints, so 1065 should never surface in the picker.

### Added — `scripts/migrate_canonical_encounters.py`
One-shot script to clean up orphan `fight_model` / `strat_config` rows that landed under legacy aliases before v1.17.0. Walks every cloned group, reports per-(table, alias) counts vs. canonical, prompts for confirmation, then deletes. Supports `--dry-run` and `--yes`. Live AC on dev DB: identified 162 `fight_model` orphans under DSR 1065 (the bulk-clone leftover the user noted in v1.16.5). Not auto-run — operator decision.

### Tests
- **5 new** end-to-end tests in [tests/test_canonical_merge_v1_17.py](tests/test_canonical_merge_v1_17.py): consensus unions both halves of the DSR group; DPS check pools kills across the group; cartography aggregates deaths across the group; fight_model writes at canonical only and readers under either alias return the same rows (uses a fake test-only clone group via monkeypatch to avoid clobbering real dev DSR data); helper identity for non-cloned encounters.
- **7 new** unit tests in [tests/test_encounter_canonical.py](tests/test_encounter_canonical.py) for the helpers.
- **528 tests passing** (516 → 528, +12). No regressions across the full suite.

### Operator notes
- No schema migration needed — pure query-side normalization.
- Existing fight_model rows under canonical IDs are unchanged. Orphan rows under legacy aliases (e.g. DSR 1065 fight_model) become invisible to the analytics layer but stay in the DB until you run `python -m scripts.migrate_canonical_encounters` (dry-run first to inspect).
- DSR strats authored before v1.17.0 against either ID still work: `strat_config` reads now canonicalize. Strats stored at the alias (1065) will be invisible until migrated; check `--dry-run` output to see if the operator has any.
- The 162 orphan fight_model rows on the dev DB are the v1.16.5-era bulk-clone from 1065 → 1076; canonical (1076) already has 163 rows. Running the migration drops the orphans cleanly.

### Adding a new cloned group later
Append a new tuple to `_CLONED_GROUPS` in [analysis/_encounter.py](analysis/_encounter.py) with the canonical ID first. Everything else (lookups, queries, UI dedupe) flows from that one change. Add a fresh entry to `TIMELINE_FILES` in `ingest/cactbot.py` if cactbot ships a timeline for the new canonical ID.

## [1.16.5] — 2026-05-27 — Per-phase tabs use wipe counts; phase inference for "Unknown"; guessed-pill tooltip distinguishes mechanic vs phase

### Why
User feedback on v1.16.4: "The table all (X) should be for wipes not death" + "how are there unknowns? Use the timeline of the report and cactbot's timeline, taking into account differences in kill times between phases to guess" + "under the guessed icon make it so that if you hover over it you can see what was guessed. Right now we have mechanic guessed and we will have phase guessed".

### Changed — chip counts now show wipes, not deaths
[analysis/cartography.py](analysis/cartography.py) response now includes `wipes_by_phase: {phase: count}` — a tally of `Fight.last_phase` across the static's watched wipes. [web/src/Home.jsx](web/src/Home.jsx) uses these counts in the phase chips: `All (467 wipes)`, `P2 (262 wipes)`, etc. Tooltip on each chip explains what the number means ("N wipes ended in PX").

### Added — phase inference for "Unknown" buckets
Per-bucket phase resolution priority:
1. **T-103 per-pull inference** — for each death, look up which T-103 phase boundary contains the death's timestamp in that fight. Most accurate when events are ingested per fight; falls through silently when they aren't.
2. **cactbot_phase_label** — parsed from the v1.3.0 annotation (e.g. `'P3'` → 3). Reliable per-ability fallback.
3. **fight_model.phase** when raw ≥ 1 (i.e. promoted phase ≥ 2) — consensus; less trusted because some encounters were cloned with bulk-tagged 0 defaults.
4. None → genuine "Unknown" tab.

Per bucket: `phase_source` ∈ `{fight_model, inferred, unknown}` + `phase_inferred_deaths` count. When T-103 / cactbot OVERRIDE a stale fight_model phase, the bucket is marked `inferred`.

### Changed — phase numbering normalized to 1-indexed throughout
The output `bucket.phase` and the chip labels are now both 1-indexed (P1, P2, ...) — matching FFLogs `Fight.last_phase` and cactbot's labeling, which is what users see in-game. Previously `fight_model.phase` was 0-indexed (so a bucket displayed as "P0" while the chip showed "P1" — confusing mismatch). `bucket.fight_model_phase` keeps the raw 0-indexed DB value for back-compat. T-103 phase indices are also 0-indexed internally; we +1 on output.

### UI — guessed pill differentiates mechanic vs phase
The yellow `guessed` pill on each cartography row now has two faces:
- `mechanic guessed` / `N mech-guess` — non-attributable deaths whose killing ability was inferred (v1.16.1/v1.16.3 work).
- `phase guessed` / `N phase-guess` — deaths whose phase came from T-103 or cactbot rather than fight_model consensus.

Both can show together (`N mech-guess · M phase-guess`). Tooltip explains the inference source for each.

In the table's Phase column, buckets with `phase_source === 'inferred'` get a small `?` indicator next to the phase number with a tooltip noting the phase was guessed.

### Tests
3 new in [tests/test_cartography_phase_inference_v1_16_5.py](tests/test_cartography_phase_inference_v1_16_5.py): wipes_by_phase shape, fight_model phase passthrough (with 1-index normalization), unknown phase when nothing is available. **516 tests passing** (513 → 516, +3).

### Live AC against DSR data
- `wipes_by_phase`: P1=37, P2=262, P3=76, P4=15, P5=38, P6=31, P7=8 (total 467, matches `total_wipes`).
- Buckets now show consistent P1+ labels. Where the dev DB's `fight_model.phase=0` bulk-tagged values were getting overridden by cactbot (e.g. Skyblind), `phase_source` is `inferred` with `phase_inferred_deaths` set.

### Note on DSR data quality
The dev DB's DSR `fight_model` was bulk-cloned from encounter 1065 without re-running phase consensus, so most rows have `phase=0` (or no cactbot_phase_label). Inference helps but the underlying consensus is sparse — running T-203 + cactbot annotation against 1076 specifically would give cleaner phase tags. Not in scope here.

## [1.16.4] — 2026-05-27 — Per-phase tabs on "What's killing us"

Frontend-only ([web/src/Home.jsx](web/src/Home.jsx) `WipeMechanicsSection`). Added a row of phase chips above the mechanic table: `All (N)`, `P0 (n)`, `P1 (n)`, …, plus `Unknown (n)` if any mechanics aren't phase-labeled. `All` is the default (matches the prior view). Clicking a phase filters the table to mechanics tagged with that `fight_model_phase`. Phase counts in each tab label show that phase's total death count so you can see at a glance where the prog wall is. Per-phase filter persists until you switch encounters. Tab row hidden when only one phase has data.

If a selected phase has zero attributable deaths, the table area shows a "No attributable deaths in this phase yet" hint with the chips still visible so you can pick another phase. No backend change — uses the existing `fight_model_phase` field already on each cartography bucket.

## [1.16.3] — 2026-05-27 — Cartography uses inference for non-attributable deaths

### Why
v1.16.1 wired non-attributable-death inference (cast proximity + cactbot drift) into `compute_fault_scores_for_fight`, but the cartography "What's killing us" view read death events raw — so FFLogs-sourceID=-1 deaths still lumped into a single bucket and usually dominated the chart. User asked: "Can we fix non-attributable by attributing it to the closest cactbot mechanic? Just label it as guessed or something."

### Added — `analysis/death_inference.py`
Shared inference module pulled out of `fault_attribution`. Exports:
- `INFER_LOOKBACK_MS = 8_000` / `INFER_CACTBOT_TOLERANCE_MS = 2_500` / `INFER_ACTIONABLE_LABELS`
- `infer_killer_from_cast_proximity(death_ts, enemy_casts, label_of)` — most recent enemy cast within 8s whose type_label is actionable
- `infer_killer_from_cactbot_drift(death_ts, phase, phase_start, cactbot_entries, drift, label_of)` — drift-adjusted cactbot lookup, ±2.5s
- `build_phase_drift_map(timeline_diff_result)` — phase_index → median_drift_ms
- `boss_cast_events(session, fight_id)` — enemy-sourced casts for one fight
- `build_inference_context(session, fight_id, encounter_id)` — pre-loads everything needed for `infer_killer`
- `infer_killer(ctx, death_ts) → (aid, label, source)` — two-layer pipeline: cast_proximity first, cactbot_drift fallback

`fault_attribution` re-exports the prior private names (`_infer_killer_from_cast_proximity`, etc.) so existing tests keep working.

### Changed — `cartography_for_encounter` uses inference
[analysis/cartography.py](analysis/cartography.py). For each death with `ability_id IS NULL`, build a per-fight inference context (lazy-loaded only for fights that actually have non-attributable deaths) and call `infer_killer`. If a match is found, increment that ability's bucket and add to `inferred_deaths`. Otherwise the death stays in the non-attributable bucket.

Per-bucket response gained `inferred_deaths: int` — how many of this row's `deaths` came from inference (vs real FFLogs attribution).

### UI — "(guessed)" pill on cartography rows
[web/src/Home.jsx](web/src/Home.jsx). When a mechanic row has `inferred_deaths > 0`, a yellow pill appears next to the mechanic name: just `guessed` if every death in the bucket was inferred, or `N guessed` showing how many of the total were guesses. Tooltip explains the inference source.

### Tests
2 new in [tests/test_cartography_inference_v1_16_3.py](tests/test_cartography_inference_v1_16_3.py): cast-proximity attribution end-to-end + fallback to non-attributable bucket when inference can't match. **513 tests passing** (511 → 513).

### Live AC against DSR data
Top mechanic table before this ship had `non-attributable` as a giant lump at the top. After:
- Sacred Sever: 156 deaths (70 guessed, recovered from non-attributable)
- Holy Bladedance: 106 deaths (80 guessed)
- Ancient Quaga: 84 deaths (72 guessed)
- Heavensflame: 105 deaths (31 guessed)

Non-attributable residual: 615 deaths. These are real "we can't tell" cases — sub-cast VFX outside both our events table and cactbot's timeline body. Future improvement: harvest cactbot's commented-out `#Ability` entries (already done for label fallback in v1.4.1) into the drift inference too.

## [1.16.2] — 2026-05-26 — Cross-report player_id collision bugfix

### Why
User-caught attribution bug. Their per-job breakdown showed PLD 336 / DRK 203 / WAR 13 for "Aoi Bomber", but they remembered being Paladin almost the whole prog. Empirical check against the Combatant table showed the real distribution was PLD 359 / DRK 24 / WAR 13. The 203 DRK number was fabricated by aggregation bug.

**Root cause**: FFLogs `player_id` is **report-scoped**, not character-scoped. The same numeric pid maps to different characters across different reports. pid=12 specifically was:
- "Aoi Bomber" PLD/DRK in one report (60 fights)
- "Louis Moinet" Dancer in another (51 fights)
- "Zun Lapix" Samurai in another (22 fights)
- "Multiple Players" / "LimitBreak" actor elsewhere (22 fights)

The v1.16.1 `fault_aggregate_for_encounter` keyed everything by pid alone — so ALL 203 of pid=12's fights got attributed to whichever (name, job) was seen first in `name_job_lookup`. Result: Aoi Bomber's wipe count inflated by other players' fights, and the per-job breakdown placed them on jobs they never played.

### Changed — `fault_aggregate_for_encounter` keys by (name, server) not pid
[analysis/fault_attribution.py](analysis/fault_attribution.py). The aggregate now:
1. Builds `char_at_fight[(fid, pid)] → (name, server, job)` from Combatant rows.
2. Converts per-fight active pids into per-character attendance: `(name, server) → [(fid, pid)]`.
3. Walks attendance per CHARACTER, not per pid. Per-fight FaultScore lookup still uses (pid, fid), but the per-fight pid is now correct for the character we're aggregating.
4. Emits one row per `(name, server)` with `jobs_breakdown: {job_name: {fights, root, cascade, mit_failure, heal_failure, heal_failure_caused, enrage, unknown, avoidable_damage, damage_downs, score}}` nested inside.

Output row schema additions: `name`, `server`, `primary_job` (most-played), `player_ids` (representative list), `jobs_breakdown`. Legacy `player_id` field retained as the first pid for backward compat with the breakdown drill-down.

### Changed — `fault_breakdown_for_encounter` keys by name not pid
[analysis/fault_breakdown.py](analysis/fault_breakdown.py). Joint key switched from `(player_id, ability_game_id)` to `(name, ability_game_id)`. Also folded the v1.16.1 inferred_ability_id into the joint key so non-attributable deaths get bucketed under the inferred mechanic (was producing a single "ability_id: null" lump).

### Changed — Frontend FaultSection reads `jobs_breakdown` from backend
[web/src/Home.jsx](web/src/Home.jsx). Previously the frontend rebuilt the per-job table by aggregating `p.job` across player rows — broken when the same pid showed up under different jobs in different reports. Now reads the backend's per-character `jobs_breakdown` and sums across the merged-group's characters. Also: `topMechanicsForPlayerSet` and `topOffendersForMechanicSet` switched from pid-based filters to name-based filters (same root cause).

### Tests
- 2 new in [tests/test_pid_attribution_v1_16_2.py](tests/test_pid_attribution_v1_16_2.py):
  - `test_same_pid_different_characters_does_not_conflate_attendance`: seed pid=42 as Alice (PLD, report A, 5 wipes) AND Bob (Dancer, report B, 3 wipes). Assert Alice has 5 fights / PLD, Bob has 3 fights / Dancer, no cross-contamination.
  - `test_same_pid_same_character_multiple_jobs_breakdown`: Alice plays 4 PLD + 2 DRK wipes under one pid. Assert `jobs_breakdown` surfaces both with correct counts and `primary_job = Paladin`.
- 1 test fixed: `test_create_with_aliases_and_list` no longer assumes the dev DB starts empty (asserts `"Alice" in names` not `len == 1`).
- **511 tests passing** (509 → 511, +2).

### Live AC against dev DSR data
Pre-fix Aoi Bomber row: 552 fights (PLD 336, DRK 203, WAR 13). Post-fix: 396 fights (PLD 359, DRK 24, WAR 13) — matches the ground-truth count from the Combatants table exactly. Aoi Bomberman correct at 49 PLD.

### Operator note
Existing FaultScore rows are unaffected. The fix is entirely at read-time in `fault_aggregate_for_encounter`. No recompute needed — open Home, your numbers correct on next refresh.

## [1.16.1] — 2026-05-26 — Near-wall plateau + non-attributable inference + scoped contributors

### Changed — `_prog_relevance` gains a near-wall plateau
[analysis/fault_attribution.py](analysis/fault_attribution.py). New `NEAR_WALL_TOLERANCE = 0.5` constant: when a wipe's prog distance is within half a unit of the running-best wall (e.g. a P4 fp=50% wipe vs P5 wall, delta=0.5), prog relevance stays at 1.0. Decay only kicks in BEYOND the plateau. Previously even a tiny backslide got immediate exp decay: P4 fp=50% was 0.78×; now 1.0×. The math beyond the plateau is `max(0.3, exp(-PROG_DECAY_K * (delta - NEAR_WALL_TOLERANCE)))` with `PROG_DECAY_K = 0.3` — same K as v1.16.0, gentler effective curve because the plateau eats the first half-unit. `fight_score_multiplier(3, 50, 5)` went from 1.48 to 1.72.

### Changed — running-best is now continuous, not integer phase
In `fault_aggregate_for_encounter`, the running-best tracker switched from integer `last_phase` to continuous `prog_distance = phase + (1 - fp/100)`. A pull that reaches P5 fp=20% (prog 5.8) sets the wall higher than a pull that reached P5 fp=80% (prog 5.2). Subsequent wipes are de-weighted relative to actual furthest progress, not just phase number. `is_past_wall` similarly uses the prog delta + the NEAR_WALL_TOLERANCE so a wipe within half a unit of the wall isn't treated as past-wall for the repeat-offender amplifier either.

### Added — non-attributable death inference
[analysis/fault_attribution.py](analysis/fault_attribution.py) gains two helpers: `_infer_killer_from_cast_proximity` (most recent enemy cast within `INFER_LOOKBACK_MS = 8000` whose type_label is `raidwide` / `aoe_party` / `tankbuster` / `enrage`) and `_infer_killer_from_cactbot_drift` (per-phase cactbot expected_t + this pull's median drift, ±`INFER_CACTBOT_TOLERANCE_MS = 2500`).

`compute_fault_scores_for_fight` runs the inference for every death with `ability_game_id is None` and `source_id = -1` (FFLogs' "could not attribute" pattern). The inferred `(ability_id, label)` is fed into `_death_kind` so the death classifies as root / mit_failure / heal_failure / cascade based on the real mechanic it most likely hit — instead of defaulting to cascade. The original `ability_game_id = None` is preserved in the death record alongside three new fields:
- `inferred_ability_id` — the guess (int or null)
- `inferred_ability_label` — the type_label from fight_model
- `inferred_from` — `"cast_proximity"` | `"cactbot_drift"` | `null` (no match)

Cactbot fallback is lazy-loaded: only fights that actually have non-attributable deaths trigger the cactbot timeline + timeline_diff + phase-boundary queries. Most fights pay zero cost.

### Added — scoped top contributors per player
[analysis/fault_attribution.py](analysis/fault_attribution.py) `fault_aggregate_for_encounter` now emits per-player `scoped_top_contributors: list[{name, member_id, score}]` and `scoped_wipes_count: int`. Computed by:
1. Building per-(player, fight) weighted scores during the existing aggregate loop.
2. Folding into member-aware identities (player_id alone if no member; member_id otherwise — sub-accounts merge with their main).
3. For each focal identity, summing OTHER identities' weighted contributions across the focal's attended fight_ids.
4. Top 5 by total contribution, self excluded.

Home row expansion ([web/src/Home.jsx](web/src/Home.jsx)) renders this as "Top contributors across the N wipes {focal} attended" — answers "in Alice's attended wipes, who's actually driving the score?" Useful when someone has uneven attendance: a player who shows clean overall might still be in the room when their static's real problem-spot keeps wiping.

### Fixed — `worst_wipes.fight_percentage` crashed the row expansion
The v1.16.0 worst_wipes payload was serializing `fight_percentage` as a JSON string (SQLAlchemy `Numeric` → Python `Decimal` → FastAPI JSON encoder emits quoted). Frontend's `w.fight_percentage.toFixed(1)` threw on a string. Cast to `float` in the backend before stashing in worst_wipes; frontend also wraps every numeric field in `Number()` as a safety net.

### Tests
- 6 in [tests/test_nonattributable_inference_v1_16_1.py](tests/test_nonattributable_inference_v1_16_1.py) — cast-proximity matching (most-recent wins; outside-window skipped; future-casts ignored; no-actionable returns None) + end-to-end reclassification + cascade fallback when no match.
- 3 in [tests/test_scoped_contributors_v1_16_1.py](tests/test_scoped_contributors_v1_16_1.py) — fields present on every player, full-attendance sees global ranking, partial-attendance filters out unseen wipes.
- 1 new + several updated in `test_fault_phase_weighting_v1_14_5.py` for the plateau + softer curve.
- **509 tests passing** (499 → 509, +10).

### Note on roster-classification reset
The user reported that v1.15.0 roster classifications (core / sub / substitute attributions on the Roster page) were getting reset on browser refresh. Root cause: the pre-v1.15.1 autouse `_clean_roster` fixture in `tests/test_roster_api.py` ran `delete(Member)` globally, wiping any real user-curated members alongside test fixtures whenever the test suite ran. The fix landed in v1.15.1 (scoped cleanup) but historic data lost during pre-v1.15.1 test runs is unrecoverable. The 6 IgnoredCharacter rows in Default Static survived because they're in a separate table. Re-classification needed.

## [1.16.0] — 2026-05-26 — Fault scoring refinement sweep + heal_failure attribution

### Why
v1.14.6 left the scoring stack at four multipliers (phase × within × prog × repeat) — defensible per factor but compounding wildly (a single freak wipe could land 23× a baseline). A design pass identified eight friction points; this ship lands seven of them as mechanical cleanups plus a new **heal_failure** classification the user asked for: when a raidwide kills a player but mits successfully fired, the failure was on the healers (the raid wasn't topped), not the dying player.

### Changed — `fight_score_multiplier` capped at 8× combined
[analysis/fault_attribution.py](analysis/fault_attribution.py). Phase × within × prog now clamps at `COMBINED_MULTIPLIER_CAP = 8.0` before the repeat amplifier composes on top. P5 wipe at 5% HP at the wall used to compute 4.55×; cap doesn't bite. P7 wipe at 1% HP would have been 7.5×; still fine. P9 hypothetical → cap bites. Repeat amplifier (cap 5×) means a worst-case combined is now 8 × 5 = 40× per wipe, vs the prior uncapped composition that could exceed 100× in pathological corners.

### Changed — continuous `prog_distance` for prog relevance
`_prog_relevance(phase, best_phase, fp)` now uses `_prog_distance(phase, fp) = phase + (1 - fp/100)` and exp decay `max(0.3, exp(-0.3 * delta))`. Previously phase-only delta with linear decay (`max(0.3, 1.0 - 0.15 * delta)`) had a sharp cliff at each phase boundary: P4 fp=1% (almost cleared the phase) and P5 fp=99% (just entered) both delta=1 → 0.85×. Now P4 fp=1% with wall=P5 → prog 4.99 vs 5.0 → 0.99×. Smooth. Side benefit: multi-boss phases (DSR P4 / P6) where fp is the lagging boss's HP no longer cliff-jump.

### Changed — `repeat_offender_multiplier` cold-start floor + mit_failure coverage
- Denominator now `max(wipes_attended, REPEAT_RATE_MIN_DENOMINATOR=20)`. Old: 1 offense in 2 wipes = 50% rate → capped 5× multiplier. New: max(2,20)=20 → 1/20 = 5% rate → exp(0.2) ≈ 1.22× nudge. Once attendance exceeds 20 the floor stops applying.
- Past-wall counter now sums roots + mit_failures together. A mit lead who keeps dropping Reprisal post-wall triggers the amplifier same as a serial root offender. Field `past_wall_roots` renamed to `past_wall_offenses` in the aggregate response (frontend updated).

### Changed — avoidable damage per-hit floor
`_avoidable_damage_by_player` skips tankbuster events under `AVOIDABLE_DAMAGE_MIN_HIT = 50_000`. Tankbusters splash to neighboring rows for trivial amounts (a few thousand HP); those tiny ticks used to accumulate against whichever non-tank caught the AoE wash. 50k is roughly a third of a base raidwide hit at current iLvl — clearly intentional impact, not splash.

### Added — `heal_failure` death classification
[analysis/fault_attribution.py](analysis/fault_attribution.py).
- `_death_kind` returns `heal_failure` when `ability_label == "raidwide"` AND a mit plan exists AND it fully fired with no missed mits. Reasoning: if mits fired, the raidwide should be heal-survivable from full HP; if it killed, HP wasn't topped → healers missed the recover.
- `compute_fault_scores_for_fight` then identifies the healers (jobs in `HEALER_JOBS`) and their death timestamps, computes who's alive at each `heal_failure` death's `ts`, splits `HEAL_FAILURE_TOTAL_WEIGHT = 1.0` evenly across alive healers as `heal_failure_caused_score`. Dying player gets `HEAL_FAILURE_VICTIM_SCORE = 0.0` for this death (they're counted in `heal_failure` count but contribute nothing to score). Per-incident attribution is persisted in `reasons.heal_failure_incidents` for future drill-down.
- Edge: if 0 healers alive at death_ts (both dead), falls back to `cascade` — chain too broken to blame healers fairly. If only 1 healer alive, that healer takes the full 1.0.

### Added — per-wipe score decomposition
Aggregate response now carries a `worst_wipes` array per player (top 5 by weighted contribution) with `fight_id, last_phase, fight_percentage, best_phase_at_time, raw, phase_severity, within_phase, prog_relevance, repeat_multiplier, weighted`. Home `FaultSection` row expansion renders this as a table at the top: "Top 5 worst-weighted wipes (raw × multipliers → weighted)" with each multiplier shown as `Nx`. The user can finally see WHY a row scored where it did — particularly useful for "why is Bob above Alice when she's eating more avoidables".

### Changed — `FaultSection` table columns
[web/src/Home.jsx](web/src/Home.jsx). Cascade column dropped from the main table (cascade is 0.1 weight — it looked misleading at-a-glance next to roots in headline view). Replaced with `Heal fail` (heal_failure_caused). Both cascades and the dying-side of heal failures surface in the row expansion under "Also tracked but not score-relevant". Intro copy rewritten to explain the v1.16.0 mechanics + nudge users at the mit audit section for the mit-side view.

### Tests
- **4 new** in [tests/test_heal_failure_v1_16.py](tests/test_heal_failure_v1_16.py): 2-healer 50/50 split; 1-healer-dead → other healer takes full 1.0; both-healers-dead → falls back to cascade; amplifier counts mit_failure.
- **1 new** in [tests/test_fault_repeat_offender_v1_14_6.py](tests/test_fault_repeat_offender_v1_14_6.py) for the cold-start floor behavior.
- **6 updated** for renamed field (`past_wall_roots` → `past_wall_offenses`), new formula values (prog relevance exp decay vs linear), and renamed test (`raidwide_mits_all_fired_is_cascade` → `_is_heal_failure`).
- **499 tests passing** (495 → 499, +4 net visible after renames).

### Side cleanup — non-destructive test_roster_api
[tests/test_roster_api.py](tests/test_roster_api.py) had an autouse fixture `_clean_roster` that ran `delete(Member)` globally after each test. On a dev DB with real user-curated roster data this blanket-wiped legitimate members. Scoped the cleanup to a `TEST_MEMBER_NAMES` allow-list and ran it both before and after each test so test_empty_list doesn't flake on residue, and renamed `members[0]` indexed accesses to name-based lookups for the same reason.

### Operator note
Existing `fault_scores` rows persisted before v1.16.0 still read correctly (the v1.16.0 reasons schema is additive — new fields default to 0/empty when absent). To get heal_failure attribution and per-wipe decomposition on existing wipes, click "Analyse all wipes" on Home (bulk recompute). New wipes ingested going forward get v1.16.0 semantics automatically.

## [1.15.1] — 2026-05-26 — Roster polish + sub-account merge in fault contributors
### Fixed — classifier bug: both halves of a main+sub pair showed as each other's sub
[analysis/roster_discovery.py](analysis/roster_discovery.py). Before: a member with ≥2 aliases had ALL of them classified as `sub` (so P1 marked core + P2 attached as sub of P1 made BOTH P1 and P2 render as each other's sub). After: pick the PRIMARY alias per member (min `alias.id` — the one created first, by convention the member's main character) and only non-primary aliases get `sub`. Primary keeps `member.kind` ('core' or 'substitute'). The fix preserves the existing test (Bob → `sub`) and adds a regression assertion that the primary (Alice → `core`) stays correctly classified.

### Fixed — copy clarification + "sub-stitute" hyphen
[web/src/Members.jsx](web/src/Members.jsx). The page header now spells out the four roles explicitly: **core** = a real person's main account; **sub** = a secondary character of the SAME person (alt / sub-account); **substitute** = a backup member (their own person, their own characters); **ignore** = pugs, loot trades, anyone you don't want in analytics. The character-checklist intro got the same treatment. Replaced the visually-hyphenated `<button>sub-stitute</button>` with plain `<button>substitute</button>` (the dash was an unintentional line-wrap artifact).

### Added — sub-account merge in "Who's contributing to wipes"
When a roster member owns multiple character aliases (main + subs), the fault contributors table now collapses them into one row.

- **Backend** ([analysis/fault_attribution.py](analysis/fault_attribution.py)): `fault_aggregate_for_encounter` joins each FFLogs `player_id` to its roster member via static-scoped `character_aliases` (prefer `(name, server)` exact match; fall back to name-only when exactly one member claims the name — same lookup as `resolve_members.py` / `roster_discovery.py`). Per-player rows now carry `member_id`, `member_name`, and `server`.
- **Frontend** ([web/src/Home.jsx](web/src/Home.jsx)): `FaultSection` groupedPlayers regroups by `member_id` when present (`m:${id}` key), falls back to character name (`n:${name}`). Each grouped row tracks its constituent characters in a `characters_list` with per-character `fights` / `score` / `jobs`. The Player cell renders a `+N alt` / `+N alts` accent pill when `character_count > 1` (tooltip lists each character + server + jobs). The row expansion grows a third sub-table — "Characters merged into Alice (3 accounts)" — between the per-job breakdown and the per-mechanic top-killers.

### Tests
- 1 new in [tests/test_fault_attribution.py](tests/test_fault_attribution.py): seed Alice owning P1, Bob owning P2 + P3 (main + sub), compute faults, assert the aggregate response gives all three rows a `member_id`/`member_name` and that Bob's rows share the same member id.
- 1 regression assertion in [tests/test_roster_discovery_v1_15.py](tests/test_roster_discovery_v1_15.py): in `test_discovery_marks_subs_when_owner_has_multiple_aliases`, also assert that "Alice Tankerton" (the primary) stays as `classification: "core"` after Bob is attached.
- 494 tests passing (480 → 494, +14: 13 from v1.15.0 + 1 today).

## [1.15.0] — 2026-05-26 — Roster discovery + classification (core / substitute / sub / ignore)
### Why
The existing Members tab was form-driven: type a name, then type each character + server by hand. For a static with sub-accounts + occasional substitutes + pugs that appear once in a leftover report, this leaves the human doing reconciliation work the database could do — we already KNOW every character that has appeared in any watched report; just show them and ask the user to classify.

### Added — schema (alembic `b5de1c10f4af_roster_kind_ignored_characters`)
- `members.kind` — Text NOT NULL, server-default `'core'`. Existing rows backfill to `'core'`. Allowed values: `'core'` | `'substitute'`. Both kinds count toward analytics identically today; the tag is for the human's roster view. Substitutes show in their own section in the UI to keep the regular roster clean.
- `ignored_characters` — static-scoped table persisting the "this combatant is not part of our static" decision. Unique partial index on `(static_id, character_name, COALESCE(server, ''))` so the same ignore is durable across re-ingests and one (name, server) pair per static yields at most one row. FK cascades on static delete.

### Added — `analysis/roster_discovery.py::discovered_characters_for_static()`
- One server-side aggregation `combatants → fights → watched_reports` filtered on `static_id` yields distinct `(character_name, server)` rows with `fights_seen` (distinct fight count) and `latest_job` (job from the most recent fight). Player-with-NULL-name rows are excluded.
- Each row classified as one of:
  - **`core`** — character is an alias of a `Member` with `kind='core'` and that's the member's only alias.
  - **`substitute`** — same as core but member kind is `substitute`.
  - **`sub`** — character is an alias of a member who owns ≥2 aliases (so it's plausibly an alt). UI hint only; structurally still just an alias.
  - **`ignored`** — listed in `ignored_characters` for this static.
  - **`unclassified`** — no alias or ignore row claims it.
- Alias lookup mirrors `resolve_members.py`: prefer `(name, server)` exact match; fall back to name-only iff exactly one member claims it.
- Sorted by `fights_seen` desc, tiebroken by name. Most-recurrent characters surface first (real members beat pugs).

### Added — `/api/roster/characters` (GET) and `/api/roster/classify` (POST)
- **`GET /api/roster/characters`** — context-scoped, returns `{static_id, characters: [...]}`.
- **`POST /api/roster/classify`** — single-character bulk router. Body `{character_name, server, action, member_id?, member_name?}` where action is one of `core` / `substitute` / `sub` / `ignore` / `clear`:
  - `core` / `substitute`: if `member_id` provided, attach this character as a new alias on that member; else create a new Member (name = `member_name` or `character_name`) with the requested kind + attach the alias.
  - `sub`: requires `member_id`; attaches this character as an alias of that member (used for sub-accounts of an existing core member).
  - `ignore`: adds an `ignored_characters` row (idempotent); removes any existing alias for this (name, server).
  - `clear`: removes any alias and any ignore row → returns the character to `unclassified`.
- Every action wipes prior alias + prior ignore state for the same (name, server) so the transitions are clean (no orphan rows, no ambiguous double-classification).

### Changed — `/api/members`
- `POST /api/members` and `PATCH /api/members/{id}` now accept a `kind` field (validates against `('core', 'substitute')`, returns 422 on invalid). `POST` defaults to `'core'`. `MemberOut` carries `kind`.

### React — `web/src/Members.jsx` rewritten
Three sections:
1. **Core members** — one card per `kind='core'` member with their attached aliases as pills (× to detach), a dropdown that lists the static's `unclassified` characters with their server + latest job + pull count for one-click sub-account attach, and inline `mark substitute` / `promote to core` / `delete` buttons.
2. **Substitutes** — same UI, only rendered when ≥1 substitute member exists.
3. **Characters seen in reports** — filter chips (`all` / `unclassified` / `hide ignored` / `ignored`) + search box + per-row classification table with columns `Character · Server · Latest job · Pulls · Status · Actions`. Action buttons per row: `core`, `substitute`, `sub of <member>…` dropdown + attach, `ignore`, `clear`. Status pills color-coded (accent for core, warning for substitute, danger for ignored, dim for unclassified).
A `+ Add member manually` card sits between sections 2 and 3 for the rare case where you want to create a member before any of their characters appear in logs.

### Tests
- **13 new** in [tests/test_roster_discovery_v1_15.py](tests/test_roster_discovery_v1_15.py): Members.kind round-trip (default core, substitute via POST, PATCH validation, invalid 422 on create); discovery (distinct (name, server) listing, latest_job picks freshest fight, ranks by fights_seen); classification flows (core attaches + classifies, sub when owner has >1 alias, ignore + clear, switching action wipes prior state, sub-without-member-id 422, invalid action 422); cross-static isolation; ignore idempotency.
- **493 tests passing** (480 → 493, +13). Full suite green.

### Live AC against Default Static's DSR data
- `/api/roster/characters` returns 20 distinct characters, ranked by `fights_seen`: real DSR static members at 450+ pulls each, FFLogs pseudo-actor "Multiple Players" at 471 (visible in the list; one `ignore` click hides it permanently).
- Classified "Ayato Polaali" as core via the new API: auto-created Member (id=2101, kind="core") with the character as its sole alias. Next discovery call shows the row as `classification: "core"`, `linked_member_name: "Ayato"`.

### Known minor wart (not blocking)
FFLogs pseudo-actors like "Multiple Players" and "Limit Break" appear in the discovery list since the underlying `combatants` table doesn't filter them. One-click `ignore` makes the decision durable. Server-side filtering can be added later (`resolve_members.py` already has the name list); left out so the UI stays "show every character that appeared" rather than "show every character we think is a player".

## [1.14.6] — 2026-05-26 — repeat-offender amplifier for past-wall root deaths
### Why
After v1.14.5's prog-relevance de-weighting, a player who repeatedly causes wipes in early phases (already-cleared content) was getting a SCORE BREAK they shouldn't get. User asked for an exponential penalty on this pattern, **rate-based** so a 1000-wipe static doesn't get the same curve as a 100-wipe static.

### Added — `repeat_offender_multiplier(past_wall_roots, total_wipes_attended)`
[analysis/fault_attribution.py](analysis/fault_attribution.py). `exp(K × rate)` capped at `REPEAT_PENALTY_CAP = 5.0`. K=4 means:
- 1% rate (1/100) → 1.04×
- 5% rate (5/100 or 50/1000) → 1.22×
- 20% rate → 2.23×
- 40% rate → cap at 5.0×

The rate-based curve self-scales by attendance — 5 offenses in 1000 wipes (0.5%) gives ~1.02× while the same 5 in 100 wipes (5%) gives 1.22×. Different curves for different static sizes, as requested.

### Changed — `fault_aggregate_for_encounter` now uses **attendance**, not just FaultScore rows
Old behavior: `b["fights"] += 1` per FaultScore row meant a player who attended 200 wipes but only had faults in 30 showed `Wipes: 30`. Wrong denominator for per-wipe rates AND for the repeat-offender rate.

New behavior: walks active-player data (`_active_players_by_fight`) per fight to establish ATTENDANCE. Then for each player's attendance-ordered wipe list:
- `total_wipes += 1` every iteration
- If this wipe has a FaultScore row AND is past-wall AND has ≥1 root death → bump `past_wall_roots`, compute `repeat_mult = repeat_offender_multiplier(past_wall_roots, total_wipes)`, multiply into score
- Otherwise multiplier stays 1.0 (no amplification)

Time-ordered so each successive offense's multiplier reflects the **rate at that point in time**. No retroactive amplification of prior offenses — fits the user's "more wipes you cause, more penalty going forward" intent.

### Changed — API response carries new fields
- `past_wall_roots`: cumulative count per player
- `repeat_multiplier_avg`: average multiplier across fault-having wipes (informational)
- `raw_score`: pre-weighted score for transparency
- `fights` now reflects actual attendance, not just fault-having wipes

### UI ([web/src/Home.jsx](web/src/Home.jsx))
- Section copy explains all four multipliers (phase, within-phase, prog relevance, repeat amplifier) with hover tooltips on each
- Score column tooltip shows `raw: X · N past-wall root deaths` so the amplification is auditable per row
- Per-name grouping in the table sums `past_wall_roots` and `raw_score` across the player's multiple FFLogs ids

### Tests
- **10 new** in [tests/test_fault_repeat_offender_v1_14_6.py](tests/test_fault_repeat_offender_v1_14_6.py): rate-based math, cap behavior, 100-wipe vs 1000-wipe comparison, exponential growth verification, serial offender end-to-end, no-past-wall = no-amp baseline.
- **1 fixture updated** in [tests/test_fault_attribution.py](tests/test_fault_attribution.py) to add WatchedReport (the new aggregate scopes attendance via watchlist).
- **1 test reframed** in [tests/test_fault_phase_weighting_v1_14_5.py](tests/test_fault_phase_weighting_v1_14_5.py): the original "de-weighting" test had only 2 wipes which hit the repeat cap; reframed with many no-offense wipes diluting the rate so de-weighting is observable in isolation.
- **480 tests passing** (471 → 480, +9).

## [1.14.5] — 2026-05-26 — phase-weighted fault scoring (prog-aware)
### Why
The score was an absolute sum: a P3 root weighed the same as a P7 root. The user asked for a "mild nonlinear" penalty that weights late-phase wipes heavier, AND a relative weighting that de-emphasizes early-phase wipes once the group has moved past that phase.

### Added — three new multiplier helpers
[analysis/fault_attribution.py](analysis/fault_attribution.py):
- **`_phase_severity(phase)`** — `1 + p(p+1)/14`. Gentle quadratic. P3=1.86, P5=3.14, P7=5.0. P5 is ~1.7× P3.
- **`_within_phase_severity(fight_pct)`** — `1.0` at fp=100% (just entered phase) → `1.5` at fp=0% (boss almost dead). Wipes closer to clearing the phase weigh more.
- **`_prog_relevance(wipe_phase, best_phase_at_time)`** — at the prog wall (delta=0) → 1.0; each phase past the wall drops 15% down to 0.3× floor.
- **`fight_score_multiplier(...)`** composes the three.

### Changed — `fault_aggregate_for_encounter` weights at READ TIME
- Builds a **running-best-phase timeline** by walking ALL watched fights (kills + wipes) in start-time order. `fight_context[fid] = (last_phase, fight_pct, best_phase_at_time)`.
- Per (FaultScore, Fight) row: looks up the context, computes the multiplier, applies it before summing into the per-player score.
- `score` is now the weighted total; `raw_score` is preserved alongside.

### Stored data unchanged
`FaultScore.score` stays raw. Re-running compute-all isn't needed when the formula changes — the weighting is applied at read time only.

### Tests
- **15 new** in [tests/test_fault_phase_weighting_v1_14_5.py](tests/test_fault_phase_weighting_v1_14_5.py).
- 471 total passing (456 → 471, +15).

## [1.14.4] — 2026-05-26 — per-wipe normalization default + Option B planning
### Changed
- Per-wipe normalization toggle in the fault table now **defaults to ON**. Surfaces per-attempt fault density instead of absolute volume, which is the more useful comparison across uneven attendance.

### Planning
- **Option B (per-boss HP tracking)** for the prog curve added to [IDEAS.md](IDEAS.md). Full implementation sketch captured: calibrate max HP from kill pulls, name-match enemy actors across reports, compute per-boss HP at wipe time, surface in tooltip + use aggregate for multi-boss phase Y-axis. Deferred half-day ship.

## [1.14.3] — 2026-05-26 — group fault rows by character name, per-job expansion, polish sweep
### Why
The same person across multiple watched reports got a different FFLogs `player_id` each time (per-report assignment), so the fault aggregate showed them as **multiple rows**. Also: someone who plays multiple jobs deserves a per-job breakdown when you expand their row.

### Added — fault table grouped by name, per-job expansion
[web/src/Home.jsx](web/src/Home.jsx) — `FaultSection`:
- Rows are now grouped by **character name** rather than FFLogs `player_id`. All cross-report ids for the same person collapse into one row.
- New **Job(s) column** shows the single job if one, `Tank, Healer` style list for 2–3, `N jobs` summary for 4+. Hover shows per-job stats.
- Expansion grows a **per-job breakdown table** (when ≥2 jobs played) above the per-mechanic drill-down. Each job gets its own row: Roots / Mit fail / Cascades / Avoidable / DD / Wipes / Score.
- Confidence recomputed from summed kind counts (rather than averaging per-row ratios, which over-weights small-deaths-total rows).

### Added — per-wipe normalization toggle
- New `per wipe attended` checkbox in the fault section header. When ON, every count divides by `fights` (wipes attended). Score thresholds adjust (5/2 → 0.15/0.05). Re-sorts the table by per-wipe score so worst rate-offender bubbles up regardless of attendance.

### Added — "What's killing us" group-by-mechanic toggle + cactbot names
- Cartography output now includes `cactbot_label` (cactbot's human-readable strat name like "Sacred Sever", "Heavensflame").
- Display priority: `cactbot_label (base, "1" suffix stripped)` → `ability_name` → `ability {id}`.
- **Group by mechanic name** toggle merges rows with same base cactbot label so "Sacred Sever 1", "Sacred Sever 2", "Sacred Sever 3" become one row showing total deaths + a `(3 variants)` tag.
- Ability name backfill: pulled FFLogs `masterData.abilities` from 9 DSR reports → 633 ability names added for boss abilities not in XIVAPI's player-tables.

### Added — `topMechanicsForPlayerSet` + `topOffendersForMechanicSet` helpers
Aggregate breakdown rows across multiple FFLogs player_ids (for name-grouped views) and across multiple ability_ids (for cactbot-label-grouped mechanics).

### Fixed — Rules of Hooks bug in `WipeMechanicsSection`
useMemo for the grouped rows was called AFTER an `if (!data) return ...` early return. On first render `data` was null and the hook never ran; once data loaded the hook count changed and React crashed the tree. Moved the useMemo above the early returns with null-safe input.

### Changed — DSR ingest + UI labels
- Encounter id 1076 added to `ENCOUNTER_NAMES` in [Encounters.jsx](web/src/Encounters.jsx) / [FieldStats.jsx](web/src/FieldStats.jsx) / [Home.jsx](web/src/Home.jsx). The legacy DSR encounter id is 1076 (current FFLogs DSR encounter); kept 1065 as "DSR (alt)".

## [1.14.2] — 2026-05-26 — prog curve uses per-encounter trajectory + DSR test data loaded
### Fixed
- **`ProgSection` only showed manual prog points.** It embedded the generic `ProgPoints` component (queries `/api/prog-points` for manual entries) instead of the per-encounter `/api/encounters/{id}/prog-curve` that auto-derives sessions from watched reports. Rewrote `ProgSection` to fetch the per-encounter prog-curve and render a `ComposedChart` with auto sessions + manual points + a sessions table + the manual-entry form. Y axis switched from `% remaining` to a **continuous prog distance** `phase + (1 - fp/100)` because % remaining doesn't reflect phase structure (multi-boss phases collapse weirdly).
- **`prog_trajectory_for_encounter` was returning relative offsets.** `Fight.start_time` is FFLogs convention millisecond OFFSET from `Report.start_time`. The chart was treating these tiny offsets as Unix epoch ms → all sessions plotted in 1970. Fixed by joining `Report` and computing absolute wall-clock ms.
- **Test isolation bug in `test_poll_watched.py`.** The tests assumed `poll_once` returned empty for static_id=1, but the new DSR data load exposed the bug. Tests now filter `summaries` to just the test's seeded `T101_*` codes.

### Added — dev test data: 20 DSR reports under Default Static
- User shared a guild reports URL (FFLogs guild 136735). Pulled via the existing user-OAuth client, added 20 DSR reports as watched under Default Static (skipped 1 actually-FRU report per user instruction).
- Ingested ~510 fights, ~3.1M events, 467 DSR wipes + 5 kills.
- Encounter 1076 (DSR Legacy) had no `fight_model` — cloned the 162 rows from encounter 1065's existing field-backfill model, then ran T-104 consensus (added 1 new ability) + T-203 mechanic classifier (163 labeled: 36 raidwide, 25 aoe_party, 7 tankbuster, 80 cosmetic, 15 unknown).
- Bulk-computed fault scores: 602 root, 1389 cascade, 2125 unknown across the 467 wipes.

### Added — README pointers, no functional change
Encounter labels updated in 3 UI files (Encounters / FieldStats / Home) to include 1076=DSR + 1065=DSR(alt).

## [1.14.1] — 2026-05-26 — dev "view as user" toggle
### Added
- Devs can now flip a `view as user` toggle in the header to render the consumer Home / hide dev-only surfaces (Abilities tab, FieldStats panel, "show all encounters") — without DB poking. Useful for testing the non-dev experience against the dev static's data.
- `MeProvider` ([web/src/me.jsx](web/src/me.jsx)) now exposes `actual_is_developer` (the real status) alongside `me.is_developer` (which gets masked to `false` when the toggle is on). All existing consumers transparently switch to the consumer view; only the toggle button itself reads the real flag.
- State persisted in `localStorage` so the toggle survives reloads. When on, a `viewing as user` accent pill appears in the header brand so you don't forget you're in the masked mode.
- Pure frontend change — no Python touched, no API touched, no tests broken.

## [1.14.0] — 2026-05-26 — body-check fault by assigned role (strat-aware aoe_party)
### Why
aoe_party deaths were always classified as root — but for body-check / soak / spread mechanics, that misattributes fault. A DPS dying to a 4-tank-tower mechanic shouldn't be blamed; the assigned tank's absence (or fail to soak) is the actual issue. v1.14.0 reads strat_config's role_map for the killing mechanic occurrence and reclassifies root → cascade when the dying player's job role isn't in the expected target set.

### Added — `_expected_job_roles_from_role_map()`
[analysis/fault_attribution.py](analysis/fault_attribution.py). Translates strat's `{slot_name: MT|OT|H1|H2|D1..D4}` map into the set of FFXIV job roles ({tank, healer, dps}) expected to be targeted. Handles `"any"` (wildcard → all 3), `null` (skipped), and the empty-map case (returns None → caller falls back to existing classification).

### Added — `_aoe_party_casts()`
Mirrors the raidwide-cast lookup from `mit_audit` but scoped to aoe_party. Returns `(cast_ts, ability_id, occurrence)` per cast, sorted by time — used to match each aoe_party death to its specific occurrence so strat_config (which is per-occurrence) can be consulted.

### Changed — `compute_fault_scores_for_fight` body-check refinement pass
After the first-pass classification, walk death_records:
1. Skip non-root deaths or non-aoe_party kinds.
2. Locate the cast that killed the player (`_aoe_cast_for_death`, same 15s lookback as raidwide).
3. Look up `strat_config.assignments` for `{ability_id}_{occurrence}` mechanic_ref.
4. Derive expected job roles via `_expected_job_roles_from_role_map`.
5. If the dying player's job role isn't in the expected set → flip kind to `cascade`, mark `body_check_reclassified=True` on the death record.

Cleanly opt-in: only fires when the user has actually configured assignments for that mechanic. No strat → existing root behavior preserved. Returns a `body_check_reclassified` counter in the summary so downstream consumers can surface "N body-check faults rebalanced this fight."

### Tests
- **10 new** in [tests/test_body_check_fault_v1_14.py](tests/test_body_check_fault_v1_14.py): pure tests for `_expected_job_roles_from_role_map` (empty / tank-only / mixed / wildcard / null-role-skipped / full party); end-to-end DPS-dies-to-tank-only-aoe_party reclassifies to cascade; tank dying to same mechanic stays root (was assigned); no-strat keeps existing root behavior.
- **456 tests passing** (446 → 456, +10).

### Note on the deeper "missing-slot attribution"
The fuller version of body-check fault attribution — *identify which assigned-role player wasn't in position* — needs damage-target counting against expected slot count. Tractable but not in this ship. v1.14.0 ships the simpler-but-already-valuable version: redistribute blame away from players who shouldn't have been hit. The "find who failed to soak" follow-up can be added later as a strat-aware extension once usage validates the approach.

### No schema change. No new env vars.

## [1.13.0] — 2026-05-26 — fault drill-down: per-mechanic offenders + per-member top failures
### Why
The per-player aggregate ("Alice has 4 roots") and the per-mechanic histogram ("Cyclonic Break killed 6 people") were both top-level signals — but neither answered the actual diagnostic question: *who keeps eating which mechanic*. The user's "I want to be able to see for every member what they most fuck up on" was the explicit driver. Both pivots ship together because they're the same underlying data.

### Added — `fault_breakdown_for_encounter()`
[analysis/fault_breakdown.py](analysis/fault_breakdown.py): walks `fault_scores.reasons.deaths` (populated by T-302 / v1.12.0 classifier) across all the static's watched wipes of an encounter and emits the **joint (player × killing-ability)** table. Each row carries `deaths`, `fights_affected` (distinct wipe ids), and a `by_kind` breakdown (root / cascade / mit_failure / enrage / unknown). Ability names resolved via one JOIN to `abilities`.

### Added — `GET /api/encounters/{id}/fault-breakdown`
Context-scoped, single round trip. Both Home expansions pivot from the same payload client-side.

### Added — expandable rows on Home
Both `WipeMechanicsSection` and `FaultSection` rows now have a `▸/▾` toggle:
- **"What's killing us"**: click a mechanic row → top 5 players who've died to it most, with per-player death count + wipe-count.
- **"Who's contributing to wipes"**: click a player row → top 5 mechanics they die to most, with `Nx root` / `Nx mit-fail` / `Nx cascade` / `Nx unknown` kind breakdown per row.

The breakdown is fetched once in `ConsumerHome` and shared down via props — single API call. Two pure pivot helpers (`topOffendersForMechanic`, `topMechanicsForPlayer`) keep the table-rendering logic clean.

### Tests
- **7 new** in [tests/test_fault_breakdown.py](tests/test_fault_breakdown.py): empty-encounter zero shape; repeat-offender collapses to single row with deaths+fights_affected aggregated; ability name resolution; by_kind counts correctly (cascade detection works); rows sort by deaths desc; wipes_aggregated counts distinct wipes; cross-static isolation.
- **446 tests passing** (439 → 446, +7).

### Note on the per-member view
The user-requested "for every member, what they most fuck up on" view is the per-player expansion on Home. The Members tab currently has only roster CRUD; surfacing this same data there is a natural follow-up but not needed for the current ship — the Home expansion already exposes the information.

### No schema change. No new env vars.

## [1.12.0] — 2026-05-26 — fault classifier overhaul (mit-aware primary, strict causality, continuous decay)
### Why
The death classifier had three structural weaknesses called out in the fault-improvement review: a binary 5s cliff for cascade detection (#9), over-eager cascade attribution that lumped any preceding death together regardless of type (#3), and mit-awareness as a downstream T-304 patch rather than primary classification (#4). v1.12.0 fixes all three at the root in `_death_kind`. The disambiguation pass (T-304) becomes a backward-compat no-op for the normal flow; it stays around for fault_scores rows persisted before this ship.

### Changed — `_death_kind` signature and semantics
Old: `_death_kind(killing_ability_id, ability_label, preceding_death_in_window: bool)`
New: `_death_kind(killing_ability_id, ability_label, cascade_pressure: float, mit_audit_info: dict | None = None)`

Three new behaviors for raidwide deaths:
1. **Mit-aware primary** (#4): if a strat plan exists for the killing raidwide's occurrence and mits were missed → `mit_failure` (full root weight, 1.0). If the plan fully fired → `cascade` (heal/mit overwhelm despite plan, weight 0.1). If no plan exists → fall through to the preceding-death heuristic. Means raidwide deaths get classified by the actually-load-bearing signal (mit state) rather than the proxy (was there a recent death).
2. **Strict causality** (#3): preceding-death pressure only counts deaths whose killing ability is `raidwide` or `aoe_party` (raid-wounding). A tank dying to a single-target tankbuster no longer makes the next raidwide death "cascade" — those are independent faults. Closes the over-cascading hole called out in the review.
3. **Continuous decay** (#9): preceding-death weight decays linearly from 1.0 at t-0 to 0.0 at `PRECEDING_DEATH_WINDOW_MS` (5s). Threshold flip at `CASCADE_PRESSURE_THRESHOLD = 0.5`. Replaces the binary cliff with smooth behavior — but a single death 100ms before still flips cleanly, so the practical "noisy timing" cases are unchanged.

### Added — `_cascade_pressure()` helper
Pure function in [analysis/fault_attribution.py](analysis/fault_attribution.py). Takes `now_ts` and a list of `(ts, label)` preceding deaths, returns the summed decay weight. Reusable + testable in isolation.

### Changed — `compute_fault_scores_for_fight` flow
Loads the T-303 mit audit upfront and builds an `ability_id → sorted casts` index. For each raidwide death, walks the index backwards from `death_ts` within `RAIDWIDE_DEATH_LOOKBACK_MS = 15_000` to find the cast that most likely killed the player, then passes that occurrence's plan info to `_death_kind`. Same death→occurrence matching pattern T-304 used; lifted into the primary classifier.

Per-death `reasons` now carries `cascade_pressure` (rounded) and `mit_audit` (`{no_plan, missed_count}` snapshot) for full transparency on classification choices.

### Changed — T-304 disambiguation is now effectively a no-op
Under the new flow, mit_failure is set upstream — T-304's "walk cascades, upgrade to mit_failure when audit shows misses" has nothing to do in the normal pipeline. The function stays for backward compat: fault_scores rows persisted before v1.12.0 still get correct mit_failure attribution if `disambiguate_for_fight` runs on them. PullDetail's "compute then disambiguate" button now does the work in step 1, with step 2 a confirming pass.

### Changed — score formula carries `mit_failure × 1.0`
Same weight as root, since mit_failure IS the originating fault (the cooldown didn't go out). Constants added: `MIT_FAILURE_SCORE = 1.0`, `RAID_WOUNDING_LABELS = ("raidwide", "aoe_party")`, `RAIDWIDE_DEATH_LOOKBACK_MS = 15_000`.

### Changed — `fault_scores.reasons` JSONB carries `mit_failure` count
Encounter aggregate (`fault_aggregate_for_encounter`) carries `mit_failure` per player. Confidence math counts it as classified (alongside root/cascade/enrage).

### UI ([web/src/Home.jsx](web/src/Home.jsx))
"Who's contributing to wipes" table grew a **Mit fail** column between Roots and Cascades (red when > 0). Updated section description to explain mit_failure semantics.

### Tests
- **9 new** in [tests/test_fault_classifier_v1_12.py](tests/test_fault_classifier_v1_12.py): cascade_pressure decay math, strict-causality filter (tankbuster doesn't contribute), pressure summation, threshold-flip behavior, missed-mit → mit_failure directly via T-302, tankbuster-then-raidwide produces two roots not one+cascade.
- **`_death_kind` pure tests updated** in [tests/test_fault_attribution.py](tests/test_fault_attribution.py) to the new signature: `False/True` boolean preceding-death replaced with `0.0/0.9` cascade pressure; 4 new tests for the mit-aware path (missed/fired/no_plan).
- **Seeded-fixture tests updated**: `test_compute_classifies_root_vs_cascade` now expects 2 roots + 1 cascade (was 1+2) because the leading tankbuster no longer cascades the follow-up raidwides — exactly the strict-causality outcome.
- **T-304 disambiguation tests** rewritten to verify the final state (after full pipeline) rather than asserting T-304 did the upgrade itself. The fixture's missed-mit scenario still produces mit_failure; the path is just upstream now.
- **All 4 fault_disambiguation tests pass** alongside the new T-302 mit-aware tests, proving the backward-compat no-op behavior works.
- **439 tests passing** (426 → 439, +13).

### No schema migration. No new env vars. Old fault_scores rows remain readable; running compute-all on them regenerates with v1.12.0 semantics.

## [1.11.0] — 2026-05-26 — fault signals expanded: avoidable damage, Damage Down, confidence
### Why
T-302 fault attribution was death-only. Per PLAN §3 Invariant 5 the spec was always "**killing-blow ability + avoidable damage-taken** as primary signals; **Damage Down** as a secondary survive-fault flag" — we shipped the killing-blow half and ignored the rest. Result: silent contributors (players who eat 6 avoidables and survive because healers top them, or get Damage Down 4× across a wipe without dying) were invisible. v1.11.0 closes that gap.

### Added — survive-fault signals
- **`_avoidable_damage_by_player()`** ([analysis/fault_attribution.py](analysis/fault_attribution.py)): per-player sum of damage taken from abilities labeled `tankbuster` in fight_model where the target's job isn't a tank. The clear-cut case; aoe_party stays unscored until v1.14.0 strat-aware ship can disambiguate body-check expected-targets. `damage` events only (excludes `calculateddamage` to avoid double-count).
- **`_damage_down_count_by_player()`**: per-player count of `applydebuff` events whose ability has the T-108 `damage_down` label. Skips `refreshdebuff` (re-stack of the same application — only count the moment of the botch). Generic Damage Down + encounter-specific damage-downs both feed in via the label, not a hardcoded ID.
- **New job-role table** at [analysis/_jobs.py](analysis/_jobs.py): `TANK_JOBS`/`HEALER_JOBS`/`MELEE_DPS`/`PHYS_RANGED_DPS`/`CASTER_DPS` frozensets + `role_of(job)` helper. Used here for tankbuster avoidability; reused by v1.14.0 body-check ship.

### Changed — composite score formula
Score is now `roots × 1.0 + cascades × 0.1 + damage_downs × 0.5 + min(avoidable / 100k, 5.0)` where the avoidable component is capped at 5.0 per fight so one extreme hit can't dominate. Constants `DAMAGE_DOWN_SCORE = 0.5`, `AVOIDABLE_DAMAGE_PER_POINT = 100_000`, `AVOIDABLE_DAMAGE_SCORE_CAP = 5.0` — all tuneable, all documented inline.

### Changed — survivors now appear in fault_scores
Before v1.11.0, a player who never died had no `fault_scores` row even if they ate 6 avoidables and got Damage Down twice. Now the per-fight aggregator unions players-with-deaths and players-with-survive-faults, so a healer who botched two body-checks shows up on the Home table.

### Changed — confidence (`classified_fraction`) on per-player rows
Per PLAN §9 M-FAULT "(c) emit `fault_scores` (score + reasons), aggregated weekly. **Never a single-name verdict.**" — v1.11.0 makes the partial-knowledge case visible. Per-player: `(root+cascade+enrage) / total_deaths`. Encounter aggregate carries the cumulative ratio. UI renders a `Conf` column color-graded (green ≥80%, yellow ≥50%, red below) so a player whose 5 deaths are mostly `unknown` shows as low-confidence — pushes the user toward the T-108 review queue rather than blaming on a guess.

### Changed — `fault_scores.reasons` JSONB shape
New keys: `avoidable_damage` (int), `damage_downs` (int), `death_score` / `avoidable_score` / `damage_down_score` (rounded contributions to the composite score), `classified_fraction` (float or null). Existing keys preserved. No migration needed (JSONB).

### UI ([web/src/Home.jsx](web/src/Home.jsx))
"Who's contributing to wipes" table now has 9 columns: Player · Job · Roots · Cascades · **Avoidable** · **DD** · **Conf** · Wipes · Score. Empty values render as `—` so a clean player doesn't look noisy. Avoidable rendered as `500k` style; Conf as color-graded percentage. Updated the section description to explain all three new signals + nudge users at the Abilities review queue when confidence is low.

### Tests
- **7 new** in [tests/test_fault_signals_v1_11.py](tests/test_fault_signals_v1_11.py): tankbuster-to-non-tank surfaces; tankbuster-to-tank doesn't; Damage Down count; survivor-with-survive-fault appears; classified_fraction reflects unknown deaths; composite score blends three signals; encounter aggregate carries new fields.
- All existing 15 fault attribution + 4 fault disambiguation tests pass unchanged — the new signals are additive.
- **426 tests passing** (419 → 426, +7).

### Note on disambiguation interaction
T-304 disambiguation rewrites `score` and `reasons` for fights where a cascade becomes mit_failure. That code path also needs to know about the new keys; verified that the existing disambiguation tests still pass because the keys it doesn't touch flow through unchanged. v1.12.0 (classifier overhaul) will integrate the mit-aware classification more deeply.

## [1.10.0] — 2026-05-26 — job-filterable DPS comparison (your static vs the field)
### Why
Ship 3 of 3 in the consumer-side push. T-204 `dps_check_for_encounter` already aggregates per-phase raid DPS across kills, but it pools everything (ours + field together) and there's no job-level granularity. The consumer goal was: "compare median total DPS of public vs your own static, filter down to your job if you want". v1.10.0 delivers both — static-split distributions and an optional per-job narrowing.

### Added — `dps_comparison_for_encounter()`
- New function in [analysis/dps_check.py](analysis/dps_check.py). Splits per-phase DPS into `ours` (kills from reports in the static's watchlist) vs `field` (everything else), using the same watchlist-scoping rule as v1.8.0 cartography and v1.9.0 mit aggregate.
- **Without `job`**: each kill contributes one value (raid DPS = phase total damage / duration). Top-line "where do we sit vs the field" view.
- **With `job` (e.g. "SAM")**: contributes one value per matching player per kill. Two SAMs in one kill = two data points. Per-player-DPS distribution that answers "where does our SAM sit vs all SAMs that have cleared".
- Returns `jobs_available` (every job seen across both sides) so the Home dropdown doesn't need a second roundtrip.
- Quartile shape reuses the existing `_quartiles` helper (n=1 returns all-equal degenerate; ≥2 uses inclusive quantiles).

### Added — `GET /api/encounters/{id}/dps-comparison?job=<job>`
- `Context`-scoped. Default no-job returns raid-DPS comparison.

### Added — Home section "Your DPS vs the field"
- New `DpsComparisonSection` in [web/src/Home.jsx](web/src/Home.jsx), placed after fault contributors.
- Job dropdown in the section header: `All jobs (raid DPS)` default + every job seen.
- Table per phase: Our median · Field p25 · Field median · Field p75 · **Δ vs median** (percent delta of our median vs field's, color-graded green ≥0 / yellow -10..0 / red <-10).
- Sample-size hint (`n=N`) on our column when ≥2 kills aggregated, since one kill is noisy.
- Two empty states: (a) no kills anywhere in the encounter → "field comparison activates once kill data exists"; (b) job filter produces no rows for one side → friendly note.
- `formatDps()` helper renders 27.3k-style numbers; tabular numerics for clean column alignment.

### Tests
- **6 new** in [tests/test_dps_comparison.py](tests/test_dps_comparison.py): empty-encounter zero shape; two-kill ours+field split (correct raid-DPS for both sides); job filter narrows to per-player DPS; non-matching jobs excluded (ours side empty when no PCT among ours); jobs_available reflects both sides; static with no watched kills sees only field.
- **419 tests passing** (413 → 419, +6).

### Consumer-side push complete
Three ships landed in one session per the plan stated up front: v1.8.0 (Home dashboard), v1.9.0 (mit aggregate), v1.10.0 (job DPS comparison). Non-dev Home now has five vertical sections: encounter picker · Where we are (prog curve) · What's killing us (top wipe mechanics) · How mit usage is going · Who's contributing to wipes · Your DPS vs the field. Together they answer the consumer questions stated this session: "keep inputting reports", "graph progress", "what gimmick are we wiping to", "who's contributing to wipes", "mit being applied or not", "median DPS vs field optionally per job".

### No schema change. No new env vars.

## [1.9.0] — 2026-05-26 — per-encounter mit-audit aggregate ("how mit usage is going")
### Why
Ship 2 of 3 in the consumer-side push. T-303 mit audit already computes per-fight planned-vs-actual; the consumer Home needed an encounter-level rollup so the user can see at a glance: which mits are dropping most often, which raidwide mechanics are taking the most damage unmitigated. Without rollup, the per-fight signal stays hidden inside PullDetail.

### Added — `mit_audit_aggregate_for_encounter()`
- New function in [analysis/mit_audit.py](analysis/mit_audit.py) walks every watched fight for an encounter (same watchlist-scoping pattern as v1.8.0 cartography) and calls `mit_audit_for_fight` on each. Aggregates two perpendicular views:
  - **Per-mit-ability** (`worst_mits`): for each planned mit ability, how many planned occurrences vs how many actually fired. Sorted by miss-rate desc, tiebroken by absolute miss count — so "3/3 missed" surfaces above "5/20 missed", but a totally clean mit doesn't bury one that's missed 1/1.
  - **Per-raidwide-mechanic** (`worst_mechanics`): for each raidwide ability, how many occurrences hit us across watched fights + how many planned mit slots missed on it. Sorted by absolute miss count.
- Also returns top-line totals: `fights_aggregated`, `raidwide_casts`, `planned_slots_total`, `missed_mits_total`, `mit_hit_rate`.
- Ability name lookup folded in via one JOIN to `abilities` so the UI doesn't need a second roundtrip.

### Added — `GET /api/encounters/{id}/mit-audit-aggregate`
- `Context`-scoped; uses current static's watchlist. Returns the aggregate above as JSON.

### Added — Home section "How mit usage is going"
- New `MitAuditSection` in [web/src/Home.jsx](web/src/Home.jsx), placed between "What's killing us" and "Who's contributing to wipes".
- **Empty-strat empty state**: when no plan is configured anywhere for the encounter, the section points the user at the Strat sub-tab instead of showing a blank table.
- **Stat strip**: mit hit rate (color-graded — green ≥90, yellow ≥75, red below), total missed mits, total raidwide casts seen.
- **"Mits dropping the most"** table — top 5 mits by miss rate with planned/missed counts.
- **"Raidwides taking the most damage unmitigated"** table — top 5 raidwides with non-zero misses by absolute miss count. Hidden when nothing's missing.

### Tests
- **5 new** in [tests/test_mit_audit_aggregate.py](tests/test_mit_audit_aggregate.py): empty-watchlist returns zero shape; two-raidwide one-fired-one-missed yields 0.5 hit rate; worst-mit ranks Reprisal correctly; worst-mechanic ranks the raidwide correctly; foreign (unwatched) fights don't contaminate the aggregate.
- **413 tests passing** (408 → 413, +5).

### No schema change. No new env vars.

## [1.8.0] — 2026-05-26 — consumer Home as per-encounter prog dashboard
### Why
Per the v1.7.1 dev/user split, non-dev users land on Home with a generic 5-stat grid (or onboarding for first-timers) — none of the wipe / fault / mit / DPS signal surfaces unless they drill into a specific pull via Reports. The consumer goal stated by the user this session: **keep inputting reports → graph progress → see what gimmick is killing us the most → see who's contributing to wipes**, with manual prog points kept prominent for ACT-down days. v1.8.0 rebuilds the consumer Home around that goal. Ships 1 of 3 in a planned sequence (mit-audit aggregate and job-filterable DPS comparison follow in v1.9.0 and v1.10.0).

### Added — consumer Home, per-encounter
- **New non-dev Home layout** ([web/src/Home.jsx](web/src/Home.jsx)) — for users with at least one watched-and-ingested report, Home is now a per-encounter prog dashboard with three sections:
  - **Encounter picker** in the header: dropdown when multiple encounters watched (auto-defaults to the most recently watched); pill chip when only one. Total pulls / kills / wipes / kill rate strip below.
  - **"Where we are"** — `ProgPoints` panel with a copy hint that calls out manual entry for ACT-down days (post-patch period before the ACT plugin updates).
  - **"What's killing us"** — top-N wipe mechanics, sorted by death count, watchlist-scoped (new endpoint param below). Phase + mechanic type pill (raidwide / tankbuster / aoe_party / enrage). Non-attributable cascade-of-cascade death count surfaced as a footnote so M-FAULT context is clear.
  - **"Who's contributing to wipes"** — per-player root / cascade / wipes / score from `fault_aggregate`. If no fault data yet, shows a single "Analyse all N wipes" button that bulk-computes fault scores for every watched wipe of the active encounter.
- **Onboarding flow unchanged** for users with zero watched reports — same welcome + 2 numbered cards from v1.7.1.
- **Dev Home unchanged** — all-ingested snapshot retained for me (full reports/pulls/kills/wipes/kill-rate grid).

### Added — backend scope filter + new endpoints
- **`cartography_for_encounter(static_id=...)` param** ([analysis/cartography.py](analysis/cartography.py)) — restricts the aggregate to fights from reports in that static's watchlist. Default (unset) keeps the legacy field-wide view for T-208 Compare. The watchlist filter uses a SELECT subquery on `watched_reports.code` so cross-static rows are invisible (same multi-static isolation model as v1.6.0).
- **`GET /api/encounters/{id}/cartography?watched_only=true`** — wires the param to the API. Default `false` (legacy behaviour).
- **`GET /api/me/encounters`** — auto-detects the active encounter for the current static. Returns `{active, encounters: [{encounter_id, pulls, kills, wipes, latest_end_time}]}` sorted by latest_end_time desc, tiebroken by pull count. Drives the Home encounter picker.
- **`POST /api/encounters/{id}/fault-scores/compute-all`** — bulk-computes T-302 fault scores for every watched wipe of an encounter (skips kills; idempotent per the per-fight replace in `compute_fault_scores_for_fight`). Replaces the per-pull "compute" workflow on PullDetail for the consumer use case where you want the whole encounter analysed at once.

### Tests
- **3 new in [tests/test_cartography.py](tests/test_cartography.py)**: scoped vs. unscoped behaviour proves cross-static isolation; empty-watchlist static returns empty results (not all-fights).
- **4 new in [tests/test_my_encounters.py](tests/test_my_encounters.py)**: empty-watchlist returns null active; single-encounter picks itself as active; multi-encounter active = most recently watched (proven by recency beating pull count); foreign-static reports don't leak.
- **408 tests passing** (401 → 408, +7). No regression.

### Operator notes
- No new env vars. No schema migration (uses existing `watched_reports` / `fights` / `fault_scores` tables).
- Server restart picks up the new endpoints; React bundle picks up automatically (StaticFiles reads from disk per request).
- Non-dev users who already had watched data now see the new Home on next refresh.

## [1.7.1] — 2026-05-25 — dev mode vs user mode (two-password split)
### Why
The dashboard is shared with the static; the dev DB has FRU/TOP/DSR/M9–M12 backfill from development that real users shouldn't see. Splitting the experience: developers retain everything (dev data, Abilities review queue, field stats, encounter "show all"); users get a clean static-scoped view + onboarding.

### Added — `DEV_PASSWORD` env + `User.is_developer`
- **Schema**: new `users.is_developer` column (Boolean, NOT NULL, default false). Migration `da8534b86685_user_is_developer`.
- **Config**: optional `DEV_PASSWORD` env var. When set, logging in with this password marks the user as `is_developer=True`. `AUTH_PASSWORD` (existing) gives non-dev access. Either may be unset.
- **Middleware** ([api/main.py](api/main.py)): validates against both passwords; stashes which one matched on `request.state.auth_match`.
- **Auth dependency** ([api/auth.py](api/auth.py)): `Context` now carries `is_developer`. `_resolve_is_developer` priorities: middleware match > username-equals-AUTH_USERNAME fallback (backwards compat for fresh installs with no DEV_PASSWORD configured).
- **`ensure_user_and_membership`**: on first sighting, dev users auto-join Default Static (id=1); **non-dev users get their OWN static auto-created** named "{username}'s raid" (no longer auto-joined to Default). On subsequent logins, `is_developer` is refreshed from the password match — flipping passwords promotes / demotes without DB poking.

### API
- `GET /api/me` now returns `is_developer: bool`.
- `POST /api/statics` always auto-switches the user to the new static (was: only if currently on Default Static — a check that no longer triggered under the per-user-static model).

### UI ([web/src/me.jsx](web/src/me.jsx) — new shared `useMe()` hook)
- New `MeProvider` in `main.jsx` wraps the app, fetches `/api/me` once, exposes `{me, error, refresh}` via React Context. All consumers (StaticSwitcher, App, Home, Reports, Encounters) read from this instead of refetching.
- **Header** ([web/src/App.jsx](web/src/App.jsx)) shows a yellow `dev mode` pill next to the brand when the user is a developer.
- **Tab nav**: the Abilities tab is hidden from non-dev users. If a stale URL hash points there, the app redirects to Home.
- **Home** ([web/src/Home.jsx](web/src/Home.jsx)): non-dev users with no watched reports get a **welcome screen** — "Welcome, {username}" + two numbered onboarding cards ("Add your roster" → Roster, "Watch a report" → Reports) + prog points. Returning users / devs see the stat-grid as before.
- **Reports**: `FieldStats` panel hidden for non-dev users (it's pure backfill telemetry).
- **Encounters**: "show all encounters" toggle hidden for non-dev users (they only ever care about encounters they have data for).
- **StaticSwitcher** now consumes `useMe()` rather than fetching `/api/me` itself — switching statics refreshes the shared context, so the dev pill / hidden tabs / Home onboarding all stay consistent.

### Tests
- 5 new in [tests/test_dev_mode.py](tests/test_dev_mode.py): DEV_PASSWORD promotes to dev; AUTH_PASSWORD keeps non-dev; wrong password 401s; existing user gets promoted on next login with dev_pw (or demoted vice versa); legacy single-AUTH_USERNAME fallback. Plus the existing multi-static test updated for the per-user-static model.
- **401 tests passing** (396 → 401).

### Operator notes
- To roll this out: set `DEV_PASSWORD=<long random>` in `.env.prod`. Tell only yourself. Share `AUTH_PASSWORD` with the static. They log in with their own username + AUTH_PASSWORD → get their own static, no dev clutter.
- The legacy `aoi` user (or whoever matches `AUTH_USERNAME`) keeps dev mode even without `DEV_PASSWORD` set, so existing setups don't break.

## [1.7.0] — 2026-05-25 — UI redesign (dark mode, design system, no new deps)
### Added — design system + dark-mode refresh across the entire React app
- New [web/src/styles.css](web/src/styles.css) — ~500 lines of design-token + reset + base + utility + component-class CSS. CSS custom properties for everything (surfaces, borders, text, accent, role colors, type colors, spacing, type scale, radii, shadows, transitions). Imported once from [web/src/main.jsx](web/src/main.jsx).
- Pure dark mode. No toggle — light mode is out of scope. `<meta name="color-scheme" content="dark">` + `theme-color` + body background painted before React mounts so there's no flash.
- Inter + JetBrains Mono via Google Fonts preconnect (one HTTP request, ~30KB; falls back to system stack).
- **No new dependencies** — Tailwind/Mantine/shadcn-style would have added 100KB+ and config sprawl. Pure CSS variables and small utility classes give the same outcome at 13KB gzipped.

### Component classes (used app-wide)
- `card` / `card-tight` / `card-flush` — surfaces. `card-header` / `card-section`.
- `btn-primary` / `btn-ghost` / `btn-danger` / `btn-sm` / `btn-xs` — button variants. Native `button` styled by default.
- `pill` / `pill-accent` / `pill-success` / `pill-warning` / `pill-danger` — status indicators.
- `chip` — palette chips (used by the strat editor mit palette).
- `table.t` / `table.t-tight` — styled tables; `.num` for tabular-numeric columns.
- `tile` / `tile.is-selected` — picker tiles (reports list, encounters list, pull list, mechanic list).
- `tabs` / `tab.is-active` for the top-level nav; `subtabs` / `subtab` for nested.
- `modal-overlay` / `modal` / `modal-header` / `modal-body` / `modal-footer`.
- `stat` / `stat-grid` — Home dashboard cards.
- `bar-track` / `bar-fill` — inline progress bars in tables.
- `empty` / `loading` / `spinner` — placeholder + loading states with animated dots + spinner keyframe.
- Layout utilities: `row` / `col` / `stack` / `stack-sm` / `stack-lg` / `gap-*` / `grow` / `wrap` / `sidebar` / `split-2` / `split-3` / `app-main` / `app-main` / `row-stack-mobile`.
- Typography utilities: `muted` / `faint` / `mono` / `small` / `text-sm` / `text-strong`.

### Behavior + polish
- Subtle 120-180ms transitions on hover/focus everywhere.
- Cyan accent focus rings (3px glow) on all form controls.
- Custom scrollbar styling for dark mode.
- Recharts surfaces themed via CSS custom-property overrides (axis text, grid lines, tooltips).
- Sticky app header with `backdrop-filter: blur(8px)` and a status-dot connection indicator.
- Mobile breakpoint at 720px: sidebars collapse, grids reflow.
- Brand mark (purple→cyan gradient "V" tile) in the header.

### Touched files (all 15 React components rewrote presentation; no API contract changes)
- [web/index.html](web/index.html) — fonts, color-scheme, theme-color, body bg.
- [web/src/main.jsx](web/src/main.jsx) — imports styles.css.
- [web/src/App.jsx](web/src/App.jsx) — sticky header, brand mark, tab nav using new classes.
- [web/src/Home.jsx](web/src/Home.jsx) — stat-grid + 5 cards (added kill-rate).
- [web/src/Reports.jsx](web/src/Reports.jsx), [WatchedReports.jsx](web/src/WatchedReports.jsx), [FieldStats.jsx](web/src/FieldStats.jsx) — cards, tiles, pill statuses for kill/wipe counts.
- [web/src/ProgPoints.jsx](web/src/ProgPoints.jsx) — table-driven log, themed Recharts curve.
- [web/src/Encounters.jsx](web/src/Encounters.jsx), [FightMap.jsx](web/src/FightMap.jsx), [CompareView.jsx](web/src/CompareView.jsx) — tiles + subtabs; FightMap phase tints reworked for dark; CompareView cards.
- [web/src/Abilities.jsx](web/src/Abilities.jsx) — subtabs, filter card, bulk-apply bar, abilities cards with duration/mit chips.
- [web/src/StratEditor.jsx](web/src/StratEditor.jsx) — tiles for picker, table-driven slot editor, themed SVG window overlay (dark surface, white labels with shadow), chip palette.
- [web/src/ReportDetail.jsx](web/src/ReportDetail.jsx) — full restructure: card-grouped sections, GateVerdictStrip pills, phase strip with role tints, mit audit + fault scores as native tables with pills, timeline diff with semantic drift colors.
- [web/src/Members.jsx](web/src/Members.jsx) — card per member, pill aliases with inline remove ×.
- [web/src/StaticSwitcher.jsx](web/src/StaticSwitcher.jsx) — compact header dropdown + ghost buttons + members modal styled with `.modal`.
- [web/src/FFLogsAuthStatus.jsx](web/src/FFLogsAuthStatus.jsx) — pill states.

### What stayed the same
- All API contracts. All routing. All component props. All business logic.
- All 396 tests pass without changes — UI redesign touched zero Python files.
- Bundle size unchanged (CSS adds 13KB, removed inline-style strings net out).

## [1.6.0] — 2026-05-25 — multi-static (users + statics + N:M memberships)
### MINOR per CLAUDE.md §4 — first real scope expansion past PLAN §1's single-static premise.

User picked **N:M users-to-statics** + **scope-only-user-curated-data** (2026-05-25). The rest of the design is documented inline in [api/auth.py](api/auth.py) + the migration docstring at [alembic/versions/11cd54903d42_multi_static.py](alembic/versions/11cd54903d42_multi_static.py).

### Schema (migration `11cd54903d42_multi_static`)
- **New tables:** `users` (id, username unique, current_static_id, created_at), `statics` (id, name, created_at), `static_memberships` (user_id, static_id PK, joined_at).
- **`static_id` added to** `watched_reports`, `members`, `strat_config`, `prog_points`, `fault_scores`. All existing rows migrated to the seeded **Default Static (id=1)** so a pre-1.6.0 install upgrades without data loss.
- **PK changes:**
  - `watched_reports` (code) → (static_id, code). Two statics can watch the same report; the ingestion ledger short-circuits redundant raw-data work.
  - `strat_config` (encounter_id, mechanic_ref) → (static_id, encounter_id, mechanic_ref).
  - `fault_scores` (fight_id, player_id) → (static_id, fight_id, player_id).
  - `members` unique(name) → unique(static_id, name).
  - `character_aliases`: global unique(character_name, server) **dropped** — scoping is via member → static.

### Auth + identity ([api/auth.py](api/auth.py))
- Existing HTTP Basic middleware loosened: `AUTH_PASSWORD` stays a single shared password, but the USERNAME is now free-form. The middleware no longer enforces `provided_user == AUTH_USERNAME` — anyone with the password can log in as any username, and that becomes their user record on first request.
- New `Context` dataclass + `get_context(request)` FastAPI dependency. The dependency auto-provisions the user record on first sighting and auto-joins them to the Default Static so a fresh deploy with `AUTH_USERNAME=aoi` keeps working out of the box.
- Authorization via `require_static_membership(session, user_id, static_id)`. Cross-static access returns **404 (not 403)** to avoid leaking existence of statics the user can't see.
- Dev/test mode (no `AUTH_*` env): username extracted from the Authorization header if present; otherwise falls back to a `dev` user. Tests use this hook to simulate N users without enabling the password layer.

### API
- **New endpoints**:
  - `GET /api/me` — user + current_static_id + statics list (drives the switcher).
  - `GET /api/statics`, `POST /api/statics` (creator auto-joins + auto-switches if they were on Default Static).
  - `PATCH /api/me/current-static` (404 if not a member).
  - `GET /api/statics/{id}/members`, `POST /api/statics/{id}/members` (404 if username doesn't exist — they must log in once first), `DELETE /api/statics/{id}/members/{user_id}` (409 on last-member removal).
- **Existing endpoints scoped** — all ~21 endpoints that touch `watched_reports` / `members` / `character_aliases` / `strat_config` / `prog_points` / `fault_scores` now take `ctx: Context = Depends(get_context)` and filter by `ctx.current_static_id`. Analytical endpoints (`prog-curve`, `consistency`, `post-clear-targets`, `mit-audit`, `fault-scores/{compute,disambiguate}`, `session-report`, `fault-aggregate`) pass the static_id through to their analysis modules.
- **Shared raw-data endpoints** untouched (reports, fights, events, fight_model, abilities, ability_labels, dps_check, gate-diagnostic, cartography, recovery, wipe-type — all static-agnostic since the underlying tables aren't scoped).

### Analysis modules updated to take `static_id`
- [analysis/strat_config.py](analysis/strat_config.py), [analysis/prog_trajectory.py](analysis/prog_trajectory.py), [analysis/consistency.py](analysis/consistency.py), [analysis/optimization.py](analysis/optimization.py), [analysis/mit_audit.py](analysis/mit_audit.py), [analysis/fault_attribution.py](analysis/fault_attribution.py), [analysis/fault_disambiguation.py](analysis/fault_disambiguation.py), [analysis/session_report.py](analysis/session_report.py).
- `classify_wipe_type` left static-agnostic (reads fight_model + Event only).
- `jobs/poll_watched.py::poll_one_by_code` grew an optional `static_id` kwarg so the API path scopes the lookup; the CLI path (no kwarg) picks the first watch across any static for ad-hoc use.

### React
- New [web/src/StaticSwitcher.jsx](web/src/StaticSwitcher.jsx) in the App header: shows `{username} · <static dropdown>` + a `members` button (opens modal: list + add by username + remove) + a `+ static` button (inline create-and-switch).
- App tabs remount on static switch via a `staticKey` prop so they refetch scoped data without page reload.

### Tests
- **10 new** in [tests/test_multi_static.py](tests/test_multi_static.py): `/api/me` shape, cross-static isolation for **watched_reports / members / strat_config**, 404 on access to other-static rows, 404 on switching to non-member static, add-member then they see the data, unknown-username add returns 404, last-member-removal blocked, member listing. Each test uses unique disposable usernames + tears down its own users/statics.
- **Updated** existing tests (test_consistency, test_fault_attribution, test_fault_disambiguation, test_mit_audit, test_optimization, test_prog_trajectory, test_session_report, test_resolve_members, test_schema, test_roster_api, test_poll_watched, test_auth_middleware) to seed `static_id=1` and pass `static_id` through to updated function signatures. `test_api_route_rejects_wrong_username` retired; replaced with `test_api_route_accepts_any_username_with_right_password` documenting the new multi-user-shared-password model.
- **396 tests passing** (379 → 396, +17).

### Migration ergonomics
- Pre-1.6.0 installs upgrade with no manual steps. Existing single-user `AUTH_USERNAME=aoi` deploys: first request creates user "aoi", joins Default Static (which now holds all migrated data), sets current_static_id = 1. Existing behavior preserved.
- Single shared password is conscious tech debt — adequate for the friend-group case. For a real public deploy this should move to Cloudflare Access (which the named-tunnel runbook in README already covers) and the HTTP Basic middleware unsets.

## [1.5.10] — 2026-05-25 — T-309 complete: drag-to-reorder mit slots
### Added — interactive drag on the strat editor's mit-window overlay
- The v1.5.8 SVG bars in `MitWindowOverlay` ([web/src/StratEditor.jsx](web/src/StratEditor.jsx)) are now draggable. Grab a bar with the mouse, slide it horizontally — `window_offset_ms` updates live and the form row above updates too (they're bound to the same `slots` state).
- **Snap to 500 ms** — matches the existing `<input step={500}>` granularity on the form's window-offset cell, so dragging and typing produce the same set of representable values.
- **Constrained to visible domain** — a bar can't be dragged past the overlay's time bounds. Right-edge clamps at `maxT - duration`; left edge at `minT`.
- **Visual feedback** — dragging bar gets a blue stroke + higher opacity; cursor flips from `grab` to `grabbing`; the bar's text label appends `(t+5.5s)` showing the live offset.
- **Pointer capture** — uses `setPointerCapture` so the drag survives the cursor leaving the bar (no stuck drags if you slide too far).
- **No new tests** — pure UI interaction; covered by manual smoke. The state update (`setSlots[i].window_offset_ms`) is the same path tests already exercise via the form input.

### T-309 status
Both halves of T-309 are now shipped:
- v1.5.8 — click-to-add mit palette + visual window overlay with role colors and overlap-by-construction.
- v1.5.10 — drag-to-reorder. Form row still works for keyboard editing / precise values.

PLAN §11 T-309 is complete. IDEAS.md entry collapsed accordingly.

## [1.5.9] — 2026-05-25 — wiki damage-reduction-% scrape (M-MIT prep)
### Added — `abilities.mit_pct` + extended wiki scraper
- **Schema**: new nullable `abilities.mit_pct` column (Integer, 0..100). Alembic migration `cead7d264bf9_abilities_mit_pct`. 17 tables in dev DB.
- **`ingest/wiki.py`**: extended with `extract_mit_pct(html)` + `fetch_metadata_for_ability(client, name)` + `scrape_metadata_for_abilities(client, batch)`. One HTTP request per ability returns both `duration_ms` and `mit_pct`. Regex: anchored to verb (`reduces?|lowers?|reduced|reducing|lowering`) + ≤120 non-period chars + `(\d{1,2})\s*%`. Takes the FIRST match; sanity-bounds 1..100.
- **`scripts/scrape_ability_durations.py`** updated to populate both fields in one pass. `--force` re-scrapes fully; otherwise rows are scraped if either field is missing. Output table now shows both old/new values per row.
- **API** ([api/main.py](api/main.py)): `AbilityLabelOut.mit_pct` surfaced on `/api/abilities/labels` so the strat editor palette can render it.
- **UI** ([web/src/StratEditor.jsx](web/src/StratEditor.jsx)): `MitPalette` pill now shows the mit % in green (e.g. "Rampart 20s -20%") alongside duration. Tooltip combines both.

### Live AC — 10 mit abilities scraped
- Rampart 20s / **20%**, Reprisal 15s / **10%**, Troubadour 15s / **15%**, Collective Unconscious 18s / **10%**, Intervention 8s / **10%**, Holy Sheltron 8s / **15%**, Oblation 10s / **10%**, Expedient 10s / **10%**, Exaltation 8s / **10%**, **Guardian 15s / 40%** (RPR — high mit value correctly captured). All match in-game values.

### Known limitations (documented in `_MIT_PCT_RE` docstring; M-MIT today doesn't quantify so this is conservative-first)
- Multi-value abilities (Feint 10% physical + 5% magic; Heart of Light 5% physical + 10% magic) get the FIRST listed value, which is the headline for the mit-class but not always what the user wants.
- Patch-note text on the page can shadow the current value (e.g. Mantra "reduced from 20% to 10%" picks up 20).
- Barrier-style mits (Divine Veil) that don't reduce a %-value don't match.
- A wiki cleanup pass + hand-curated overrides could fix all three when M-MIT actually uses this data.

### Tests
- 7 new tests in [tests/test_wiki.py](tests/test_wiki.py): simple reduce, multi-value first-wins, no-verb returns None, sanity bound (0%), combined fetch returns both fields, 404 yields all-None, batch scrape with mixed pages. **386 tests passing** (379 → 386).

## [1.5.8] — 2026-05-25 — T-309 partial: click-to-add mit palette + window overlay
### Added — visual polish on the T-301 strat editor
- **Click-to-add mit palette** ([web/src/StratEditor.jsx](web/src/StratEditor.jsx) → `MitPalette`): collapsible panel below the mit-plan table grouping labeled abilities by `mit_party` / `mit_self` / `mit_boss_debuff`. One click adds a slot pre-populated with that ability; the two-step "+ slot then pick from dropdown" flow is now optional. Each pill shows the wiki-scraped duration when present (e.g. "Reprisal · 15s") so picking is informed.
- **Visual mit-window overlay** (`MitWindowOverlay`): SVG strip rendered below the mit-plan table for the current mechanic. Vertical red dashed line marks the boss cast at t+0. Each configured slot renders as a colored bar from `window_offset_ms` to `window_offset_ms + duration_ms` (wiki-scraped, fallback 15 s). Overlapping mits stack visibly; gaps in coverage are obvious. Bar color tracks `expected_role` (MT/OT/H1/H2/D1..D4/any). Tick marks at +5s/+10s/+15s/etc. for orientation.
- **API**: `AbilityLabelOut` now carries `duration_ms` (the column was added in v1.5.7 but wasn't surfaced). Strat editor reads it to size overlay bars; consumers that don't care can ignore the field.

### Scope (transparent — what's left of T-309)
- **Drag-to-reorder** of mit slots within a phase timeline is NOT in this ship. PLAN §11 flagged T-309 as 5–10× T-301's build effort with "high UX rework risk if requirements shift". The palette + overlay deliver the two highest-value concrete needs called out in IDEAS (faster slot creation; visible overlap/coverage) without committing to a drag-and-drop control scheme that hasn't been validated by usage. The remaining piece is now scheduled separately as "T-309 drag-to-reorder" in IDEAS, awaiting user feedback on the palette/overlay before further investment.

### Tests
- 1 new test in [tests/test_ability_labels_api.py](tests/test_ability_labels_api.py): the labels endpoint surfaces `duration_ms` so the overlay can size its bars.
- **379 tests passing** (378 → 379).

## [1.5.7] — 2026-05-25 — wiki-scraped ability durations feeding M-BURST
### Added — `abilities.duration_ms` + wiki scraper
- **Schema**: new nullable `abilities.duration_ms` column. Alembic migration `fbbd9a93c108_abilities_duration_ms`. 16 tables in dev DB.
- **`ingest/wiki.py`**: small scraper for FFXIV consolegameswiki.com.
  - `extract_duration_ms(html)` — tag-strips the HTML and grabs the first `Duration: <N>s` (handles `Duration:</span>&nbsp;20s` and `&nbsp;` / `&#160;` variants). The first occurrence is the primary buff duration; secondary trait-granted effects (e.g. Divination's "Divining" 30s extension) are correctly ignored.
  - `fetch_duration_for_ability(client, name)` — fetches one wiki page via httpx, returns duration in ms or None (on 404 / network failure / missing field).
  - `scrape_durations_for_abilities(client, abilities, *, pacing_s=0.5)` — batch fetch with polite pacing between requests.
  - No new dependencies — uses existing httpx + stdlib `re` + `urllib`.
- **`scripts/scrape_ability_durations.py`**: one-shot runner. Pulls every ability labeled raid_buff / personal_buff / mit_party / mit_self / mit_boss_debuff, fetches each wiki page, writes `duration_ms` back. Flags: `--force`, `--label X` (repeatable), `--limit N`, `--pacing-s N`. Idempotent (`--force` to refresh).
- **M-BURST integration** ([analysis/burst.py](analysis/burst.py)): new `duration_ms_for_abilities()` helper. The window-construction step now reads each raid-buff cast's `(ts, ability_id)` and sizes the window from `duration_ms` when present, falling back to the 20s default. Per-cast window length, not per-fight — Mage's Ballad's 45s song window and a Divination 20s window can coexist correctly.

### Live AC
- Ran `python -m scripts.scrape_ability_durations --label raid_buff --limit 10`. All 10 extracted real values: **Mage's Ballad 45s** (bard song — the wiki captures this where XIVAPI doesn't), **Divination 20s**, **Radiant Finale 20s**, **Starry Muse 20s** (Pictomancer), **the Balance / the Spear 15s** (AST cards). Status-row equivalents picked up the same values via the existing name-matching pass.

### Tests
- 10 new tests in [tests/test_wiki.py](tests/test_wiki.py) via `httpx.MockTransport`: primary-duration-wins on Divination-style multi-Duration page, span-split parsing on Brotherhood-style page, no-duration returns None, HTML entity handling, end-to-end mock fetch, 404 returns None, network-error swallowed, empty name returns None, URL encoding of spaces, batch scrape with no-pacing.
- 2 new integration tests in [tests/test_burst.py](tests/test_burst.py): per-ability `duration_ms` override produces a 15s window instead of 20s default; mixed durations (one ability with override, one without) produce disjoint windows of correct lengths.
- **378 tests passing** (366 → 378).

## [1.5.6] — 2026-05-25 — abilities review queue: bulk-mark + kind/label filters
### Added — bulk apply + filter controls for the T-108 review queue
- **API**: `PATCH /api/abilities/labels/bulk` — applies the same label to many abilities in one transaction. Body: `{ability_ids: [...], label: "<label>", notes?: "..."}`. Returns `{updated: N, skipped_unknown_ids: [...]}`. Rejects invalid labels with 422. Same locking semantics as the single-PATCH endpoint (`source='user'`, `confidence=1.0`).
- **API**: `GET /api/abilities/review-queue` now accepts `?kind=action|status|unknown` and `?current_label=<label>` filter params. `current_label=` (empty string) restricts to rows that have no label row at all yet.
- **UI** ([web/src/Abilities.jsx](web/src/Abilities.jsx)) — review queue now has:
  - **kind / current label dropdowns** above the list
  - **Per-row checkbox** + "Select all" / "Deselect all"
  - **Bulk apply bar** appears when any row is selected: pick a label from the dropdown, click "Apply to selected", all selected rows land with `source=user`
  - **"Mark all kind=unknown as ignore"** shortcut button (top-right of filter bar) — fetches up to 500 unknown-kind rows and one-shots them to `ignore`. Targets the ~196 boss/environmental rows the v0.12.0 live AC noted.
  - Selected rows render with a blue highlight; bulk-apply result reported inline with refresh.
- 6 new tests in [tests/test_ability_labels_api.py](tests/test_ability_labels_api.py): kind filter restricts results, current_label filter incl. empty-string for no-label rows, bulk writes user-source, bulk skips unknown IDs without failing, bulk rejects invalid label, bulk empty-list is no-op. **366 tests passing** (360 → 366).

## [1.5.5] — 2026-05-25 — Hungarian multi-cast assignment
### Changed — timeline-diff slot-to-cast matching is now globally optimal
- New helper module [analysis/_assignment.py](analysis/_assignment.py): `min_cost_assignment(cost, *, skip_penalty)` — pure-Python O(n³) Jonker-Volgenant solver for rectangular bipartite assignment with forbidden-pair support via a per-row `skip_penalty`. No new dependencies (scipy would be a 50 MB add for one algorithm).
- [analysis/timeline_diff.py](analysis/timeline_diff.py) now builds a `slots × casts` cost matrix per phase (cost = absolute drift in ms, or `SKIP_PENALTY = 10× phase duration` for forbidden ability-set pairings) and calls the new solver. Replaces the v1.4.1 nearest-unused per-slot greedy.
- Greedy was optimal for monotonic same-ability sequences (typical case) but mis-assigned when several slots had overlapping multi-ID candidate pools and a closer-in-time slot ate a cast that a later slot needed. Hungarian finds the globally-min-drift assignment that also maximizes matched slots.
- Live AC on FRU fight 1500: cascade-drift signal preserved exactly vs v1.5.0 baseline (P1 +0.9s / P2 +10.9s / Adds -13.0s / P3 -14.0s / P4 +7.8s / P5 +11.1s). Fired-counts per phase unchanged or slightly higher.
- 10 new tests in [tests/test_assignment.py](tests/test_assignment.py) covering empty matrix, square, rectangular both ways, forbidden-pair, all-forbidden, single-row, and the motivating multi-cast pathological case (slot A accepts {X,Y}, slot B accepts {X} only — greedy strands B, Hungarian swaps). **360 tests passing** (350 → 360).

## [1.5.4] — 2026-05-25 — phase-index alignment between T-103 and cactbot
### Changed — timeline diff aligns phases proportionally instead of by strict equality
- New helper `_align_phases(fight_phase_count, cactbot_phase_count)` in [analysis/timeline_diff.py](analysis/timeline_diff.py) maps each fight phase index to a cactbot phase index by proportional interpolation. 1:1 when counts match; collapse-to-0 when cactbot has one phase; proportional otherwise.
- Used in `timeline_diff_for_fight` to look up cactbot entries per fight phase. Previously the lookup was a strict equality on `phase_index` — when T-103 detected 2 phases on a pull but cactbot only had 1 (M9S-style), fight phase 1 silently received zero cactbot entries.
- After alignment, any cactbot phase that didn't get mapped to (case where fight has fewer phases than cactbot) is folded into the nearest aligned fight phase, so no cactbot entry is dropped from the diff.
- 4 new tests: end-to-end "1 cactbot + 2 fight phases" case showing both fight phases get matches, plus 3 unit tests for `_align_phases` covering identity / collapse-to-1 / proportional. **350 tests passing** (346 → 350).

## [1.5.3] — 2026-05-24 — cactbot variant collapsing
### Changed — timeline diff: mutually-exclusive variants no longer counted as misses
- `timeline_diff_for_fight` now runs a post-match variant-detection pass: any non-firing slot whose expected time is within ±1500 ms of a *firing* slot in the same phase — and which either shares a base label (parenthesized suffix stripped, e.g. "Alley-oop Inferno (fire)" vs "Alley-oop Inferno (lightning)") or shares any ability ID with that firing sibling — is now flagged `alternate_variant: true`. Real examples that triggered cleanly: r10s "Alley-oop Inferno" (B5C0 vs B5C1), DSR "The Bull's Steel" / "Brightblade's Steel" alternates, FRU multi-ID slot collapses.
- Per-phase summary grows a new `entries_alternate` counter. The `entries_missing` count now excludes alternates so it reflects only true unfired mechanics; the cascade-drift median also ignores alternates.
- React `TimelineDiff` (in [web/src/ReportDetail.jsx](web/src/ReportDetail.jsx)) renders alternate rows with grey background + grey "alt variant" tag instead of red "did not fire", and the header shows `N fired, M missing, K alt variant` when any alternates are present.
- 4 new tests in [tests/test_timeline_diff.py](tests/test_timeline_diff.py): shared-label collapsing, shared-ID collapsing, both-miss not collapsed (still counted as misses), different-label same-time not collapsed (sequential mechanics like Spirit Taker+Holy Bladedance). **346 tests passing** (342 → 346).

## [1.5.2] — 2026-05-24 — refresh-cactbot script
### Added — `scripts/refresh_cactbot.ps1`
- One-liner to re-fetch all 7 vendored cactbot timeline files from upstream `raw.githubusercontent.com`. Pinned to verified-as-of-2026-05-24 paths in the OverlayPlugin/cactbot repo (DSR/TOP live under `06-ew/ultimate/`, FRU under `07-dt/ultimate/`, M9–M12 Savage under `07-dt/raid/`).
- Per-file size-diff reporting; `-DryRun` to preview; `-Only <name>` to refresh one file. Failed fetches don't abort the run.
- Final summary suggests re-running `POST /api/encounters/{id}/fight-model/annotate-cactbot` on encounters whose timeline files changed.
- `vendor/cactbot/NOTICE` updated with usage instructions (previously mentioned a placeholder `refresh_cactbot.sh`).

## [1.5.1] — 2026-05-24 — top-level README + named-tunnel operator runbook
### Added — `README.md`
- Top-level project README, ~330 lines, covering the full operator surface so a teammate (or future-self after a long break) can set up + run + operate the project without spelunking through 15 scripts and 9 modules.
- **Sections:** status + state-file pointers (PLAN/PROGRESS/CHANGELOG/IDEAS/CLAUDE), quick-start (one block of commands for the impatient), prerequisites (Python/Node/Postgres/cloudflared/FFLogs API client), step-by-step setup (venv, Postgres `CREATE DATABASE`, `.env` config, `alembic upgrade head`, `npm install`/`build`, smoke test), dev mode (FastAPI `--reload` + `vite` with proxy), prod mode (`scripts/run_prod.ps1` quick-tunnel runner with HTTP Basic auth), first-time data flow (`scripts.bootstrap_abilities` → Roster tab → Watched panel → optional field backfill → optional FFLogs Gold connect → optional scheduled polling), daily operations (during-prog flow, strat editor with the structured-slot + role-assignment shape, session report modal), one-page architecture, scripts tour (all 14 under `scripts/` + the 2 `jobs/`), test invocation, **complete named-tunnel + Cloudflare Access upgrade runbook** (the 6-step ops sequence — `cloudflared login` → `tunnel create` → `tunnel route dns` → `config.yml ingress` → Access app + policy → unset `AUTH_*`), pointer to project state files, cactbot vendor refresh note, license + attribution.
- The deployment-upgrade runbook captures every step needed to swap quick-tunnel → named-tunnel + Cloudflare Access. When the user picks a hostname (is-a.dev PR vs paid domain) the upgrade is one decision plus a runbook follow — no code change required.

### Version
- 1.5.0 → 1.5.1 in [api/__init__.py](api/__init__.py), [pyproject.toml](pyproject.toml), [web/package.json](web/package.json). PATCH bump per CLAUDE.md §4 — README is documentation, not a roadmap feature. 342/342 tests still passing after the bump (smoke test reads `__version__`).

## [1.5.0] — 2026-05-24 — cactbot Stage 2.2 (slot-driven matching: multi-ID variants handled correctly)
### Changed — timeline diff is now slot-driven, not row-driven
- **`timeline_diff_for_fight` refactored.** Previously iterated `fight_model` rows and looked up matching cactbot entries per row. Now iterates **cactbot timeline slots** and looks up matching cast events in the pull. A single cactbot slot can declare multiple ability IDs (`Ability { id: ["9CD3", "9CD5"] }` — random-variant mechanics like Sinsmoke/Sinsmite); the new code finds whichever variant fired in the pull and attributes it to the slot.
- **Effect:** the FRU Sinsmoke +42.8s drift outlier from v1.4.0/v1.4.1 is fixed. In that pull, 9CD5 (Sinsmite) actually fired at 17.5s, but the row for 9CD3 (Sinsmoke) was hunting the late-position cast at 59.4s. Slot-driven correctly sees the 17.5s cast as filling the slot.
- **Type filter dropped.** Cactbot only lists curated mechanics in the timeline body (sub-cast VFX live in comment blocks, which we already skip for the diff). Trusting cactbot's curation means cosmetic-typed slots in our fight_model still appear in the diff. This is necessary because the headline cast (e.g. "Sinbound Blizzard III" 9D42) is what cactbot tracks, even though our auto-classifier marks it cosmetic (the damage comes from a follow-up sub-cast).
- **Result shape unchanged** — same fields per entry (`cactbot_label`, `ability_game_id`, `type_label`, `expected_t_ms`, `actual_t_ms`, `drift_ms`, `fired`). The React `TimelineDiff` component needs no changes.
- **`ability_game_id`** in each entry is now the variant that actually fired (where the slot has multiple IDs), not always the consensus's primary ID. Useful for click-through.
- New API param `version` (existing) still drives which fight_model snapshot supplies type labels.
- Test injection point: `timeline_diff_for_fight(..., _timeline=ParsedTimeline)` — lets tests construct synthetic timelines without depending on vendored cactbot files.

### Live AC — FRU kill fight 1500 (same pull as v1.4.0 baseline)
```
                                        v1.4.1 → v1.5.0
P1   median drift +1.2s   →  +0.9s     (Sinsmoke outlier gone)
P2   median drift +10.9s  → +10.9s     (cascade signal preserved)
Adds NO ENTRIES           →  -13.0s    ← Adds Phase now visible (14/16 fired)
P3   median drift -14.1s  → -14.0s
P4   median drift +7.8s   →  +7.8s
P5   median drift +11.0s  → +11.1s
```
Slot count per phase also up substantially (P1 9→22, P2 16→28, Adds 0→16, P3 14→24, P4 18→34, P5 12→29) — more curated cactbot slots now reflected, since we're not filtering by our own type classifier.

### Tests
- 3 new multi-ID regression tests (`test_multi_id_slot_variant_a_fires`, `test_multi_id_slot_variant_b_fires`, `test_multi_id_slot_two_slots_two_variants`).
- 1 test updated from "cosmetic filtered" to "cosmetic slot still included" to match the new behavior.
- Existing 8 tests rewritten to use synthetic `_timeline=...` injection so they don't need vendored cactbot files for arbitrary encounter IDs.
- **342 tests passing** (340 → 342).

## [1.4.1] — 2026-05-24 — cactbot Stage 2.1 polish (multi-cast assignment + comment-block fallback names)
### Fixed — multi-cast assignment collision
- `timeline_diff_for_fight` previously had each fight_model row pick the closest cast independently — when ability X had N rows and M casts, multiple rows could pick the same cast or distant casts. Now per-ability matching: sort rows by expected time, then for each row pick the nearest *unused* cast and consume it. Optimal for the common case of monotonic timelines; degrades gracefully when there are more rows than casts (extras marked missing).
- 2 new regression tests in `tests/test_timeline_diff.py`: `test_multi_cast_no_collision`, `test_multi_cast_extra_rows_marked_missing`.

### Fixed — adds-phase / sub-cast abilities now get human labels
- Cactbot timelines document many abilities in two places the old parser missed:
  - **Trailing `# <HEX> <Name>` comment block** at the bottom of the file (sub-cast VFX, etc.). Used by FRU + Heavyweight tier.
  - **Commented-out `<time> "<label>" #Ability { id: ... }` lines** scattered through the file body (sub-cast effects with labels). Used heavily by TOP / DSR.
- Parser now harvests both into `ParsedTimeline.fallback_names: dict[ability_id, str]`. The annotator uses fallback names when no timeline-body entry matches. Fallback annotations get a label but no `cactbot_expected_t_ms` (no firm expected timing).
- New result counter: `annotated_fallback`. UI / API consumers see `cactbot_label` populated where it was previously null.
- Loosened the `#Ability` regex to accept `<time> "<label>" duration N #Ability {...}` (TOP-style) by allowing optional intermediate tokens between the closing quote and the `#Ability` marker.
- 4 new tests in `tests/test_cactbot.py`: `test_fallback_names_parsed_from_comment_block`, `test_real_fru_comment_block_picks_up_hiemal_ray`, `test_commented_ability_lines_become_fallback_names`, `test_annotate_uses_fallback_name_when_no_body_entry`.

### Live AC — annotation coverage after the fixes
| Encounter | Before (1.3.0) | After (1.4.1) | Δ |
|---|:-:|:-:|:-:|
| M9S | 38% (16/42) | **93%** (39/42) | +55pp |
| M10S | 52% (32/62) | **94%** (58/62) | +42pp |
| M11S | 63% (52/83) | **94%** (78/83) | +31pp |
| M12S | 50% (28/56) | **84%** (47/56) | +34pp |
| M12S-P2 | 62% (45/72) | **93%** (67/72) | +31pp |
| FRU | 55% (79/144) | **94%** (135/144) | +39pp |
| TOP | 48% (63/132) | **56%** (74/132) | +8pp (cosmetic-heavy remainder) |
| DSR | 46% (75/162) | **72%** (116/162) | +26pp |

**340 tests passing** (334 → 340, +6).

### Known limitation still standing (not in scope of this fix)
- **Multi-ID slot mapping.** When cactbot writes `Ability { id: ["X", "Y"] }` (random-variant mechanics like Sinsmoke/Sinsmite), our fight_model has separate rows per ability_id. A pull where the "Y" variant fired first leaves the "X" row hunting the second-position cast, producing misleading drift like the FRU Sinsmoke +42.8s case. Fixing requires structural changes (slot-level matching where one cactbot slot maps to N possible fight_model rows). Documented for later.

## [1.4.0] — 2026-05-24 — cactbot Stage 2 (per-pull expected-vs-actual timeline diff)
### Added — per-pull timeline diff
- **`analysis/timeline_diff.py::timeline_diff_for_fight(session, fight_id)`** computes per-pull expected-vs-actual drift. For each cactbot-annotated canonical mechanic in this encounter's `fight_model`, finds the closest matching boss-cast event in this pull's events (by `ability_game_id` within the phase window), reports `expected_t_ms`, `actual_t_ms`, `drift_ms`, and a `fired: bool` flag. Cosmetic abilities filtered (not actionable). Per-phase `median_drift_ms` summary surfaces the "everything shifted +Ns after a slow P1" cascade signal in one number.
- **API: `GET /api/fights/{fight_id}/timeline-diff`** — returns the diff payload.
- **React `TimelineDiff` component** added below MitAudit in PullDetail. Collapsible (`▸/▾`). Per-phase header shows `{fired}/{total} fired` + median drift colored by magnitude (green ≤500ms, yellow ≤2s, red >2s or missing). Each row: mechanic name, type, expected time, actual time, drift. Missing mechanics highlighted with a pink row + "did not fire" cell.
- 8 new unit tests (`tests/test_timeline_diff.py`): unknown fight, empty fight, fired-with-drift, missing mechanic, cosmetic filtered, multi-cast closest-match, no fight_model, per-phase median drift. **334 tests passing** (326 → 334).

### Live AC — FRU kill fight 1500
```
P1: median drift +1.2s   (clean, normal cast-fire offset)
P2: median drift +10.9s  ← cascade: P1 ran long, all P2 mechanics shifted +10.9s
P3: median drift -14.0s  (P3 mechanics earlier than expected)
P4: median drift +7.8s
P5: median drift +11.0s
```
The "Phase 1 ran +Ns long → all of P2 shifted late" diagnostic surfaces directly as a single per-phase number. Working as designed.

### Known limitations (Stage 2.1 candidates)
- **Multi-cast assignment.** When an ability fires N times in a pull and has M fight_model rows, each row picks the closest cast independently — competing rows can both pick the same cast or pick distant casts. Visible on FRU P1 "Sinsmoke" showing +42.8s drift (algorithm picked the 2nd cast for what should match the 1st). Hungarian-style optimal assignment would fix it.
- **Adds-phase abilities** in FRU phase 2 don't get cactbot labels — phase index alignment between T-103 and cactbot timelines isn't always perfect for short transition phases. Not blocking the headline cascade-drift signal.

## [1.3.0] — 2026-05-24 — cactbot annotations Stage 1 (human-readable mechanic names + expected timings)
### Added — vendored cactbot timelines + annotation pipeline
- **`vendor/cactbot/`** — 7 timeline files vendored from https://github.com/OverlayPlugin/cactbot (Apache 2.0, NOTICE included): `r9s.txt`, `r10s.txt`, `r11s.txt`, `r12s.txt` (covers M12S + M12S-P2), `futures_rewritten.txt`, `the_omega_protocol.txt`, `dragonsongs_reprise_ultimate.txt`. Refresh by re-curling the upstream raw URLs.
- **`ingest/cactbot.py`** — parser for cactbot's `<time> "<label>" Ability { id: "<HEX>" }` timeline grammar:
  - Extracts `(abs_time_s, label, ability_ids[], phase_index, phase_label)` per entry.
  - Phase detection from `# Phase Two` / `# Phase 5` / `# Adds Phase` / `# -- p2 --` comment markers (handles all formats cactbot uses).
  - Hex ability IDs converted to decimal so they join directly against our `events.ability_game_id` (PLAN Invariant 2).
  - Phase 0 anchors at the pull start (abs_time=0) so phase-relative times line up with our `fight_model.relative_t_ms` (which T-103 measures from fight start in P0).
  - Subsequent phases anchor at the first entry inside them (cactbot doesn't always emit an explicit phase-start abs_time).
- **`annotate_fight_model_for_encounter(session, encounter_id, version=1)`** — for each `fight_model` row, find cactbot entries with matching `ability_game_id`, prefer same-phase candidates, pick the one closest in phase-relative time, persist `cactbot_label` / `cactbot_phase_label` / `cactbot_expected_t_ms` onto the row.
- **Schema migration `8ac127cddffe`** — three nullable columns on `fight_model`: `cactbot_label`, `cactbot_phase_label`, `cactbot_expected_t_ms`.
- **API: `POST /api/encounters/{encounter_id}/fight-model/annotate-cactbot`** — runs the annotator for one encounter, returns `{annotated, missed_no_match, missed_no_timeline}` counts.
- **`read_fight_model()` extended** to include the three new fields in its response payload — so every existing consumer (Fight Map, Compare, fight-model endpoint) sees the annotations.
- **React `FightMap.jsx`**:
  - Phase label column shows `cactbot_phase_label` (e.g. "P2", "Adds") when present, falls back to `P{N}`.
  - Ability dot tooltip prepends `cactbot_label` (e.g. "Killer Voice") above the raw ability name + ID, plus shows expected-time + drift (e.g. "expected 10.4s (drift +0.7s)") for any annotated mechanic.
- 18 new tests in `tests/test_cactbot.py`: hex parsing (3), phase marker detection (7 parametrized), timeline-extraction shape, phase-relative time computation, best-match phase preference, best-match fallback, real vendored file load (M9S + FRU), end-to-end annotate persistence. **326 tests passing** (308 → 326).
- 6-line autouse fixture in `tests/test_fflogs_user_oauth.py` (`_clear_fflogs_user_auth`) to wipe the real connected-user row at test start — those tests started failing once the dev DB had a real connected Gold user from the v1.2.0 live AC. Savepoint rollback restores production data on test exit.

### Live AC — annotation rates across all 8 encounters
| Encounter | Annotated / total | % |
|---|:-:|:-:|
| M9S (101) | 16 / 42 | 38% |
| M10S (102) | 32 / 62 | 52% |
| M11S (103) | 52 / 83 | 63% |
| M12S (104) | 28 / 56 | 50% |
| M12S-P2 (105) | 45 / 72 | 62% |
| FRU (1079) | 79 / 144 | 55% |
| TOP (1068) | 63 / 132 | 48% |
| DSR (1065) | 75 / 162 | 46% |

Missed-match rows are mostly cosmetic / sub-cast abilities cactbot collapses into a single label.

### Sample of what users see now
- M9S P1: "Killer Voice" (drift +0.7s), "Hardcore" (drift +0.7s), "Vamp Stomp" (drift +0.8s), "Sadistic Screech" (drift +1.1s) — real strat-doc names.
- FRU P1: "Cyclonic Break", "Sinsmoke/Sinsmite", "Powder Mark Trail", "Burn Mark", "Burnout".
- TOP P1: "Program Loop", "Storage Violation", "Blaster", "Pantokrator".

### Stage 2 (next, separate ship)
Per-pull expected-vs-actual timeline diff: for each canonical mechanic in a pull, compute how off its actual time was from cactbot's expected phase-relative time. Surfaces "your phase 1 ran 4s long → everything in phase 2 shifted late" diagnostics directly. Deferred until user confirms Stage 1 reads well.

## [1.2.0] — 2026-05-24 — FFLogs user-OAuth (archived + private reports via Gold tier)
### Added — user-OAuth (authorization code) flow
- **`fflogs_user_auth` table** (single-row, `id=1`) stores the connected user's refresh token + cached access token + scope. Alembic migration `267acc60ac83`.
- **`FFLogsClient` user-OAuth methods** in [ingest/fflogs.py](ingest/fflogs.py): `build_authorize_url(redirect_uri, state, scope)`, `exchange_authorization_code(session, code, redirect_uri)`, `refresh_user_token(session)`, `graphql_user(session, query, variables)`, `has_user_auth(session)`, `user_auth_status(session)`. Refresh token rotates per FFLogs spec — we update the row on each refresh.
- **`FFLogsArchivedError`** subclass of `FFLogsAPIError` — `graphql()` raises this specifically when the response is the "report archived, use /user" paywall, so callers can distinguish it.
- **`graphql_with_archive_retry(session, query, variables)`** — convenience: try `/client`, fall back to `/user` only on `FFLogsArchivedError` and only when a user is connected. Used by both `ingest/events.py::ingest_events_for_report` (watched-report path, T-101) and `jobs/backfill_field.py::_ingest_fight_events` (field backfill, T-201). Default path stays cheap; the fallback only fires when needed.
- **API endpoints** in [api/main.py](api/main.py):
  - `GET /auth/fflogs/login` — generates an OAuth state, redirects to FFLogs consent screen.
  - `GET /auth/fflogs/callback` — receives code, exchanges for tokens, persists, redirects to `/#fflogs-connected`.
  - `GET /api/fflogs-auth/status` — returns connection state (drives UI).
  - `DELETE /api/fflogs-auth/connection` — disconnect (deletes the row).
- **React UI** — new [FFLogsAuthStatus.jsx](web/src/FFLogsAuthStatus.jsx) component mounted in the header: shows "FFLogs Gold: Connect" (link to `/auth/fflogs/login`) when disconnected, "FFLogs Gold ✓" with a disconnect option when connected. Auto-clears the `#fflogs-connected` hash after return.
- **New config**: `FFLOGS_REDIRECT_URI` env var (default `http://127.0.0.1:8800/auth/fflogs/callback`). Must match what's registered on the FFLogs OAuth client config page.
- 13 new unit tests in `tests/test_fflogs_user_oauth.py` covering: authorize URL shape, state randomness, code exchange + token persistence, malformed responses, refresh-token usage, refresh on expired token, archived-error subclass detection, archive-retry fallback to /user, propagation when no user auth, has_user_auth and status payloads. Test isolation via `httpx.MockTransport`. **308 tests total** (295 → 308).

### Live AC — re-backfill of TOP + DSR after user connected
- Pre-OAuth (v1.1.1 deep paginated backfill on TOP + DSR): TOP 4 kills_w_events, DSR 1.
- Post-OAuth re-run (same `--encounters 1068 1065 --reports-per-encounter 25 --events-top-n 15`): **TOP 4 → 15, DSR 1 → 15. Zero errors in the run log.** +11 archived reports retrieved for TOP (+199k events), +14 for DSR (+260k events). Total runtime 153s.
- Both ultimates now have enough kill-with-events coverage (≥3) for T-104 cross-pull consensus → `fight_model` for TOP and DSR is buildable.

### Operator setup (one-time, you-side, not code)
1. Open https://www.fflogs.com/api/clients/ → edit existing API client.
2. Add to "Redirect URLs": `http://127.0.0.1:8800/auth/fflogs/callback`. Save.
3. In the dashboard header, click "FFLogs Gold: Connect" → Approve on FFLogs → redirected back, status shows "FFLogs Gold ✓".
4. Run any backfill / Poll-now / watched-report poll as normal; archived/private reports automatically retry via /user.

## [1.1.1] — 2026-05-24 — encounter-name label fix
### Fixed
- Encounter IDs 101–105 are **M9S–M12S** (AAC Heavyweight, 7.3 tier), not M5S–M8S. Past PROGRESS notes had this wrong and the UI label tables in [Encounters.jsx](web/src/Encounters.jsx) and [FieldStats.jsx](web/src/FieldStats.jsx) followed the bad naming. Corrected mapping: 101=M9S (Vamp Fatale), 102=M10S (Red Hot and Deep Blue), 103=M11S (The Tyrant), 104=M12S (Lindwurm), 105=M12S-P2 (Lindwurm II — the "true" phase-2 unlock). Bundle rebuilt; FastAPI serves the new index.html on the next browser refresh.
### Data
- Backfilled M9S–M12S (101–105) + TOP (1068) + DSR (1065). M9–M12: +30 to +50 reports each (was ~30, now 60–84). TOP / DSR: **0 → 27 reports each** — field data exists for the first time. Limitation: only 1 kill-with-events each on TOP/DSR; FFLogs paywalls archived report `events()` behind their paid `/user` endpoint and 2 of the top-3 rankings per ultimate hit that wall. 27 metadata-only reports are enough for prog-vs-field, not enough for `fight_model` (T-104 needs ≥3 kills with events).

## [1.1.0] — 2026-05-24 — single-origin prod + HTTP Basic auth (Cloudflare quick-tunnel ready)
### Added — deployment infrastructure (post-1.0 backlog item 2, partial)
- **Single-origin prod mode.** When `WEB_STATIC_DIR` env var is set, FastAPI serves the React `web/dist/` build at `/` via `StaticFiles(html=True)`. Mounted after every `@app.get` so the API routes stay intact. In dev (`WEB_STATIC_DIR` unset) the Vite dev server keeps doing what it does, no behavior change.
- **HTTP Basic auth middleware** in `api/main.py`, gated on `AUTH_USERNAME` + `AUTH_PASSWORD` env vars being non-empty. Both unset → middleware is a no-op (dev + test behavior preserved). `/healthz` always bypasses auth for monitoring probes. `secrets.compare_digest` for the credential check.
- **`api/config.py`** grew three settings: `web_static_dir`, `auth_username`, `auth_password`. Pydantic-settings handles `.env` and process env identically.
- **`scripts/run_prod.ps1`** — PowerShell runner: confirms `web/dist/` exists (auto-builds if not), sets `WEB_STATIC_DIR`, warns if `AUTH_*` env unset, starts uvicorn on `:8000`, opens a Cloudflare quick tunnel (`cloudflared tunnel --url`) and prints the `*.trycloudflare.com` URL. Ctrl-C cleans up both processes.
- 8 new auth-middleware tests (`tests/test_auth_middleware.py`): /healthz bypass / missing-auth 401 / wrong password / wrong username / correct creds pass / malformed header / invalid base64 / disabled-when-unset. **295 tests passing** (287 → 295).
### Why these choices
- **HTTP Basic over Cloudflare Access** — Access requires a domain on Cloudflare DNS, which we don't have yet. Once a named tunnel + domain land (post-quick-tunnel), unset `AUTH_*` and Access fronts auth instead — no code change.
- **Single-origin** — one tunnel target, no CORS in prod, no separate CDN for the React bundle. The CORS middleware stays in place for the dev-server scenario (Vite on :5173 hitting FastAPI on :8000).
- **PowerShell runner** — dev box is Windows. A `cron` equivalent would be Task Scheduler, but the user chose manual Poll-now over scheduled polling so no cron is wired in.
### Operator steps to actually ship a URL
1. Add to `.env`: `WEB_STATIC_DIR=d:\Misc\Vigil\web\dist`, `AUTH_USERNAME=<pick>`, `AUTH_PASSWORD=<long random>`.
2. `npm run build` in `web/` once (or let the script do it).
3. `.\scripts\run_prod.ps1` from project root. Bookmark the `*.trycloudflare.com` URL printed in the cloudflared output.
4. Stable URL needed → file an [is-a.dev PR](https://github.com/is-a-dev/register) (~24-48h merge) or buy a $10/yr domain; then upgrade to a named tunnel + Cloudflare Access (and unset `AUTH_*`).

## [1.0.1] — 2026-05-24 — T-109 combatant filter
### Added — T-109 tighten T-004 combatant filter for Ultimate reports
- `ingest/events.py::prune_inactive_combatants(session, fight_id)` — deletes combatants whose `player_id` never appears as a `source_id` in this fight's `cast`/`damage`/`calculateddamage` events. Same active-players definition T-203/T-206/T-207 already use downstream. No-op when the fight has no events yet, so the speculative roster seeded by T-004 isn't wiped before T-005 runs.
- Wired into `ingest_events_for_report`: after the event loop flushes, every fight in the report is pruned in one pass. Result dict grows a `combatants_pruned` counter.
- `scripts/backfill_prune_combatants.py [report_code…]` — one-shot to clean up reports that landed before this patch. Per-report before/after counts + grand total.
- 6 new unit tests covering active intersection / idempotency / no-events safety / non-active event-type rejection / null source-id rejection / scoping. **287 tests total** (281 → 287).
- **Live AC on FRU report `4RVNq7drBDLG3JZw`:** fight `#163` (the one kill with events ingested) **dropped from 11,899 → 10 combatants** (99.92% reduction, ≈ the 8 real players + 2 game-actor stragglers). The 207 metadata-only fights stayed at 11,899 each — by design, since they have no events to filter against; the next time their events land, the prune kicks in automatically. Backfill swept 56 reports total, pruned 16,599 rows where events existed.
### Note
- Downstream `_active_players()` workarounds in T-203/T-206/T-207/etc. still function unchanged — they're now redundant for fights whose events have been pulled, but harmless. Leaving them in place keeps a belt-and-suspenders guard.

## [1.0.0] — 2026-05-24 — **PHASE 3 COMPLETE · 1.0.0 SHIPS**
### Added — T-308 post-clear optimization mode
- `analysis/optimization.py::post_clear_targets_for_encounter()` synthesizes the three optimization levers — burst alignment (T-105), GCD drops/min (T-008), raid DPS vs target (T-204) — into a composite `polish_score` (geometric mean of each, 0..1). Per-kill leaderboard sorted worst-first surfaces the biggest gap to fix next.
- Watchlist-scoped — kills not in our watched reports are excluded (don't optimize against random ranking-board pulls).
- Score curves: burst saturates at ≥85% (industry-standard "great" alignment), GCD drops/min from 0 (perfect) → 6 (Pict-tier movement-heavy floor), DPS ratio 0.8 (floor) → 1.0 (matches median).
- API: `GET /api/encounters/{id}/post-clear-targets`.
- 7 new tests (4 pure-function score curves + composite-with-Nones + no-kills-note + live FRU structure). **281 tests total.**

### **1.0.0 ships.** All Phase-3 tasks (T-301..T-308) shipped + stable. Per `CLAUDE.md §4`, the full planned feature set is complete:
- **Phase 0**: foundation + Mode-1 dashboard (T-001..T-011) — ingest, schema, M-WIPE, M-FAULT-Mode-1, M-GCD, dashboard
- **Phase 1**: capture + early inference (T-101 + T-103..T-108) — live polling, phase segmentation, consensus, M-BURST, M-PARSE, member resolution, ability classifier
- **Phase 2**: cross-group field comparison (T-201..T-208) — field backfill, fight_model persistence, mechanic classification, empirical DPS check, prog-vs-field, M-CART, M-GATE, Encounters UI
- **Phase 3**: strat-aware + polish (T-301..T-308) — strat_config editor, M-FAULT Mode-2, M-MIT, fault disambiguation, M-RECOV, M-CONS, M-REPORT, optimization mode
### Phase 1 backlog still open (not gating)
- **T-109** Tighten T-004 combatant filter for Ultimate reports (scheduled; downstream workarounds handle it).
- **T-309** Drag-and-drop visual strat editor (polish on T-301; defer until needed).

## [0.34.0] — 2026-05-24
### Added — T-307 M-REPORT Discord session summary
- `analysis/session_report.py::generate_session_report(session, report_code)` assembles a single Discord-pasteable Markdown summary per session (= per report). Stitches together: encounter header, pulls/kills/wipes/duration, best phase + best fp%, wipe-type breakdown (T-302), top killing abilities (T-206 cartography filtered to this report), mit hit rate (T-303 summary aggregated), per-player fault scores (T-302), worst per-mechanic consistency (T-306). Each section degrades gracefully when its prerequisite isn't ingested.
- API: `GET /api/reports/{code}/session-report`.
- React `SessionReportModal` triggered by a `report` button on each `WatchedReports` row. Shows the markdown in a preformatted block with a copy-to-clipboard button.
- 5 new tests (unknown report, basic render, kill marker, top killers, live M5S AC). **274 tests total.**
- **Live AC on M5S session** (`mVCt9aDdzq2Q8BLJ`, 18 pulls / 2K / 16W / 66 min): renders KILL header, 15-mechanics/2-kill/1-mixed wipe-type breakdown, top 5 killers with names (95× non-attributable, 10× Sustained Damage, 7× ability 45942), and a "no strat plan configured yet" nudge for the 284 raidwide casts.

## [0.33.0] — 2026-05-24
### Added — T-306 M-CONS consistency per mechanic
- `analysis/consistency.py::consistency_for_encounter()` walks every canonical mechanic in `fight_model`, finds each occurrence in our pulls (boss cast event), and checks whether any active-player death lands within `[cast_ts, cast_ts + 5s]`. An occurrence is "clean" iff no deaths in that window.
- Output sorted worst-first (lowest clean_rate) so the prog-worthy mechanics surface to the top — "we cleared P4 Pandora's Knight 8/15 pulls, that's where to drill next."
- Watchlist-scoped (same "ours" semantics as T-205) — public field reports don't pollute.
- API: `GET /api/encounters/{id}/consistency`.
- 5 new tests (per-mechanic clean rate / sort order / no-fight_model edge / no-watchlist edge / our-pulls count). **269 tests total.**

## [0.32.0] — 2026-05-24
### Added — T-305 M-RECOV recovery/resilience
- `analysis/recovery.py::recovery_for_fight()` walks death events, then checks each player's post-death activity (`cast` / `damage` events sourced by them) to detect resurrection. Outputs per-death `time_to_recovery_ms` + `fast` flag (≤5s = likely Swiftcast/Dualcast since base Raise is ~8s).
- Per-fight rollup: `total_deaths`, `recovered_deaths`, `fatal_deaths`, `resilience_pct` (recovered / total × 100), `avg_recovery_ms`, `fast_rez_count`.
- Per-player rollup: deaths, recovered, fatal, avg_recovery_ms, fast_rez_count.
- API: `GET /api/fights/{id}/recovery`.
- 7 new tests (resilience math, fast-rez detection, per-event recovery timing, per-player aggregate, no-deaths edge case). **264 tests total.**

## [0.31.0] — 2026-05-24
### Added — T-304 fault disambiguation via mit audit
- `analysis/fault_disambiguation.py::disambiguate_for_fight()` reads existing `fault_scores` (T-302) + the T-303 mit-audit, walks each cascade death whose killing ability is a raidwide, looks up which occurrence of that raidwide killed them, and checks if its planned mits were missed. If yes: reclassify `cascade` → `mit_failure`. `score` recomputes with mit_failure counted at 1.0 (same as root) instead of 0.1 (cascade).
- API: `POST /api/fights/{id}/fault-scores/disambiguate`. The PullDetail `compute` button auto-runs disambiguation right after compute — single click does both passes.
- React `FaultScores` table grows a `mit fail` column (red when >0).
- 4 new tests covering reclassification, no-upgrade-when-mit-fired, missing-rows edge case, and score recomputation. **257 tests total.**
- Doesn't try to assign mit_failure to a specific player (role→member resolution is ambiguous without strict roster locking). The M-MIT panel below already surfaces which ability was missed — the human reads it and assigns blame. T-309 polish could add explicit member-locked roles later.

## [0.30.0] — 2026-05-24
### Added — T-303 M-MIT mitigation audit
- `analysis/mit_audit.py::mit_audit_for_fight()` — for each raidwide cast in a fight, joins `strat_config.mit_plan` (T-301) and checks whether each planned mit ability fired within `[cast_ts - 15s, cast_ts + 3s]` (cast or applybuff/refreshbuff event suffices). Outputs per-raidwide planned-slot list with `fired` bool + `fired_at_ts` + `fired_source_id`.
- `mit_audit_summary()` rolls up to fight-level stats: `raidwide_count`, `with_plan` / `missing_plan`, `planned_slots_total`, `missed_mits_total`, `mit_hit_rate`.
- API: `GET /api/fights/{id}/mit-audit` (full) + `GET /api/fights/{id}/mit-audit/summary`.
- React `MitAudit` panel in `PullDetail` — per-raidwide row showing planned slots colored green (✓) or red (✗) by fire status, "no plan" greyed out when strat_config doesn't cover that mechanic-occurrence.
- 7 new tests (covers mit-fired-in-window / missed / no-plan / no-raidwides / summary). **253 tests total.**
- Will feed T-304 fault disambiguation: a raidwide death with mits down = mit-failure root; mits up = cascade from earlier death.

## [0.29.0] — 2026-05-24
### Added — T-302 M-FAULT strat-aware fault attribution
- `analysis/fault_attribution.py::compute_fault_scores_for_fight()` walks deaths in one fight and classifies each as **root** / **cascade** / **enrage** / **unknown** based on the killing ability's `type_label` from `fight_model` (T-203):
  - `tankbuster` / `aoe_party` → root (the targeted player failed)
  - `raidwide` → root if it's the *first* death, cascade if a preceding death sits within 5s
  - non-attributable (FFLogs sourceID=-1 / null ability) → cascade
  - `enrage` → enrage (boss DPS check)
- Writes per-(fight, player) rows to `fault_scores` (already in schema from T-003). `score` = roots × 1.0 + cascades × 0.1. `reasons` JSONB carries the per-death breakdown.
- `classify_wipe_type()` categorizes whole fights as `kill` / `enrage_dps` / `body_check` / `mechanics` / `mixed` — headline for T-307 Discord reports.
- `fault_aggregate_for_encounter()` aggregates across all our wipes per encounter, per player — the "weekly summary" view.
- API: `POST /api/fights/{id}/fault-scores/compute`, `GET /api/fights/{id}/fault-scores`, `GET /api/fights/{id}/wipe-type`, `GET /api/encounters/{id}/fault-aggregate`.
- React `FaultScores` panel in `PullDetail` — colored score column (red ≥1.0, orange ≥0.5), per-player root/cascade/unknown counts, compute/re-compute button.
- 15 new tests (7 pure-function `_death_kind` branches + 8 end-to-end including live M5S AC). **246 tests total.**
- **Live AC on M5S wipe #75 (9 deaths):** classified as `mechanics` wipe type, 0 root / 7 cascade / 2 unknown. The high unknown-count is honest: M5S `fight_model` only has 18 canonical abilities (sparse consensus on a short fight), so many killing abilities lack labels. Confirming more labels via the T-108 review queue tightens this directly. T-303 mit audit + T-304 disambiguation will improve cascade classification further.

## [0.28.0] — 2026-05-24 — **Phase 3 begins**
### Added — T-301 strat_config editor
- **`analysis/strat_config.py`** — canonical JSON shapes + validators + CRUD helpers. Decisions captured 2026-05-24:
  - **mit_plan = structured slots**: `{slots: [{ability_id, expected_role, window_offset_ms}, …]}`. `expected_role` is one of the 8 FFXIV roles (MT/OT/H1/H2/D1/D2/D3/D4), `"any"`, or null.
  - **assignments = role-based**: `{role_map: {slot_name: role}}` where role is in the same set.
  - **mechanic_ref = compound** `"{ability_game_id}_{occurrence_idx}"` — explicitly per-occurrence so Akh Morn x4 can carry distinct mit plans per cast (user noted recurring mechanics don't necessarily share mit plans).
- Schema reuses the existing `strat_config` table (T-003 created it). No migration needed.
- API: `GET /api/encounters/{id}/strat-config` (list + role catalog), `GET .../{mechanic_ref}` (one), `PUT .../{mechanic_ref}` (upsert), `DELETE .../{mechanic_ref}`. All validation errors return 422 with detail.
- React `StratEditor.jsx` mounted as a third sub-tab in **Encounters** (alongside Fight map / Compare):
  - Mechanic picker on the left — defaults to raidwide/tankbuster/aoe_party/enrage (the ones likely to need a strat), "show all mechanics" toggle for the full list. Configured mechanics get a ★ marker.
  - Per-mechanic editor on the right — mit-slot table (ability dropdown sourced from T-108's `mit_party`/`mit_self`/`mit_boss_debuff` labels), assignment table for `slot_name → role`. Save / Delete buttons; per-save status pill.
- 18 new tests (9 pure-function: encoding/decoding/validation; 9 API: CRUD + compound-key separation + replacement + 404 + validation errors). **231 tests total.**
### Scheduled
- **T-309** Drag-and-drop visual strat editor — polish on T-301's form-based editor. Captured in IDEAS.md + scheduled in PLAN §11 Phase 3 (not gating 1.0.0). Defer until T-301's form-based editor proves to be the bottleneck.

## [0.27.0] — 2026-05-24 — **PHASE 2 COMPLETE**
### Added
- **T-205** Prog-vs-field curve (PLAN §10 Compare). `analysis/prog_trajectory.py::prog_trajectory_for_encounter()` returns three things for one encounter:
  - **Our sessions** — derived from `Fight` rows whose `report_code` appears in `WatchedReport`. Per session: pulls, kills, best_phase, best_fight_percentage. Folds T-102's "M-PARSE-less prog-point tracker (auto)" AC into this — no separate T-102 ship needed.
  - **Manual prog points** — all `ProgPoint` rows with `source='manual'` (from T-010).
  - **Field distribution** — wipes for the encounter that aren't in our watchlist, binned at 10% `fight_percentage` resolution.
- `GET /api/encounters/{id}/prog-curve`.
- React `ProgCurveView` replaces the T-205 placeholder in `CompareView` — field histogram with inline bars, your-best-bucket highlighted in blue ("← you"), our-session list below.
- 6 new tests covering session aggregation, manual-point surfacing, field/our separation, bucket resolution, chronological ordering, empty encounter. **213 tests total.**
- **Live AC on FRU (202 field wipes ingested):** distribution shape matches the real prog walls — 62 wipes at P0 Fatebreaker (90–100% remaining), 30 wipes at 70–80% (Adds intermission/P3 entry), 25 at 40–50% (Pandora's Knight), 8 at 0–10% (enrage/Fatebound late). Backbone for the "where most groups die" comparison the static needs.
### Phase 2 done
All 8 Phase-2 tasks shipped: T-201 (field backfill), T-202 (consensus → fight_model), T-203 (mechanic classification), T-204 (empirical DPS check), T-205 (prog-vs-field), T-206 (failure cartography), T-207 (M-GATE diagnostic), T-208 (Encounters tab + M-GATE strip).
### Note
- T-102 was listed as a Phase-1 task but never explicitly shipped. Its AC ("M-PARSE-less prog-point tracker (auto + manual): furthest phase/session over time; pulls + hours") is fully covered by T-010 (manual) + T-205 (auto from watched reports). Marking T-102 as **superseded by T-205** rather than re-shipping.

## [0.26.0] — 2026-05-24
### Added
- **T-208** Fight Map + Compare UI — the visualization layer over everything Phase 2 built.
- New top-level **Encounters tab** (`web/src/Encounters.jsx`): left-side encounter picker (filtered to ones with a persisted fight model or ≥3 kills-with-events; "show all" toggle reveals everything ingested), right-side tabs for Fight map / Compare.
- **`FightMap.jsx`** — per-phase rows rendered as horizontal bands, each canonical ability dot-positioned by `relative_t_ms` and color-coded by `type_label` (raidwide=red, tankbuster=orange, aoe_party=yellow, enrage=dark red, cosmetic=grey). Hover tooltip shows ability name + ID + recurrence rate. Pulls ability names from `/api/abilities/labels` for the lookup.
- **`CompareView.jsx`** — three panels: per-phase DPS distribution (p25/median/p75/spread/n from T-204), failure cartography table with inline death bars (T-206), and a stub for the T-205 prog-vs-field curve.
- **`GET /api/encounters`** lists every encounter with at least one ingested fight + counts + fight_model presence flag. Drives the picker.
- **M-GATE verdict strip** added to `PullDetail` — colored pills per phase (`OK / DPS / MECH / BOTH / DEATHS / —`) consuming `/api/fights/{id}/gate-diagnostic`. Hover tooltips show the raid-DPS vs. target and death count.
- **Phase 2 status: 7 of 8 shipped.** Only T-205 (prog-vs-field curve) remains.

## [0.25.0] — 2026-05-24
### Added
- **T-207** M-GATE gated-vs-mechanics diagnostic (PLAN §9, §10 Home headline). `analysis/gate_diagnostic.py::gate_diagnostic_for_fight()` synthesizes T-204 (per-phase DPS verdict) with our own per-phase death counts and emits one of four verdicts per phase: `dps_gated` (DPS<p25, ≤1 death), `mechanics_gated` (DPS OK, ≥2 deaths), `both_gated`, `not_gated`. Falls back to a death-only verdict (`many_deaths` / `clean`) when the encounter has <3 ingested kills and no DPS target distribution exists yet.
- API: `GET /api/fights/{id}/gate-diagnostic`. Output is per-phase: `{phase_index, dps_status, raid_dps, target, deaths, verdict}`.
- 6 new tests covering all four verdict branches + no-target fallback + unknown-fight + live FRU AC. **207 tests total.**
- **Live AC on FRU kill fight #1500:** all 6 phases verdict = `not_gated` (raid DPS slightly above target median + zero/one deaths per phase) — the expected outcome for a kill from a top-ranked group.
- **Phase 2 status: 6 of 8 done.** Remaining: T-205 (prog-vs-field curve), T-208 (Fight map + Compare UI — heavy UI). M-GATE's verdict is now the headline number the PLAN §10 Home dashboard wants — just needs a UI surface (T-208).

## [0.24.0] — 2026-05-24
### Added
- **T-204** Empirical DPS check (PLAN §9 M-INFER #4). `analysis/dps_check.py::dps_check_for_encounter()` aggregates per-phase raid-DPS across every ingested kill of an encounter; returns `{p25, p50, p75, min, max, n}` per phase. The median = the empirical DPS target — if a pull's phase-X raid DPS is below it, that gate is DPS, not mechanics. Powers T-207's gated-vs-mechanics verdict.
- `compare_fight_to_target()` returns a per-phase verdict (`below_p25` / `between_p25_p75` / `above_p75` / `no_target`) for one fight against its encounter's distribution.
- API: `GET /api/encounters/{id}/dps-check` (target distribution) + `GET /api/fights/{id}/dps-check` (per-fight comparison).
- 9 new tests (3 quartile + 5 end-to-end synthetic + 1 live FRU). **201 tests total.**
- **Live AC on FRU (11 kills aggregated):** P0 median 152k raid-DPS, P1 119k (lowest — long phase with downtime), P2 204k (highest — Adds cleave bonus), P3 170k, P4 149k, P5 187k (burst phase). p25–p75 spread is ~3% on every phase — tight because all 11 kills come from top-ranked groups; loose prog data would widen this.
### Note on the "HP at enrage" framing
PLAN §9 M-INFER #4 calls for HP-at-enrage estimation. FFLogs events emit damage deltas, not HP percentages, so direct HP isn't readable. We compute the equivalent gating signal — *raid DPS that historically completes each phase* — which is what M-GATE needs in practice. If a future requirement needs the literal HP%, FFLogs `fightPercentage` on the wipe fight plus boss-max-HP from rankings could reconstruct it.

## [0.23.0] — 2026-05-24
### Added
- **Poll-now button** on each watchlist row — calls `POST /api/watched-reports/{code}/poll` which wraps `ingest_report` + `ingest_events_for_report` with proper commits and per-call error capture, so users never hit the half-committed-session footgun (the bug that broke FRU dev ingest twice during T-103/T-104). Synchronous: returns only after the ingest finishes (or fails). UI shows a per-row status snippet ("ingested · fights+208 · events+29725" / "already complete · no work" / error message).
- `jobs/poll_watched.py` refactored: `_poll_one_row()` extracted as the canonical ingest-one-report path; `poll_once()` and the new `poll_one_by_code()` both go through it. 50-line dedup.
- 2 new API tests for the Poll-now endpoint (404 when not watching; skips-complete behavior). **192 tests total.**
### Changed
- `DEFAULT_ENCOUNTERS` in `jobs/backfill_field.py` expanded to include **TOP (1068)** and **DSR (1065)** alongside FRU (1079) + current Savage tier (101–105). Verified encounter IDs against FFLogs' `worldData.encounter(id).name`. Other ultimates (TEA=1075, UWU=1074, UCoB=1073) documented in the comment for one-line future addition.
- `FieldStats.jsx` encounter-name lookup table updated to render `TOP` / `DSR` instead of raw IDs.
### Scheduled (no code)
- **T-109** — Tighten T-004's combatant filter for Ultimate reports. PLAN §11 Phase 1 table updated. Captured in IDEAS.md: T-004's `type == "Player"` filter on `masterData.actors` lets ~12k NPC actors leak through per Ultimate report (verified on FRU: 208 fights × ~12k = 2.5M combatant rows). Fix: intersect masterData with the per-fight active-source set during ingest, dropping rows that never appear as `source_id` in cast/damage events. Workarounds in T-203/T-206 still function after the fix; they just become redundant.

## [0.22.0] — 2026-05-24
### Added
- **T-206** M-CART failure cartography. `analysis/cartography.py::cartography_for_encounter()` walks all ingested fights for an encounter and aggregates deaths by **killing boss ability**, cross-referencing each ability against the persisted `fight_model` (T-202) for phase + mechanic label (T-203). Uses the stricter "active players" filter from T-203 so non-player target IDs (from masterData leakage on Ultimates) don't pollute counts.
- Bucket fields: `ability_game_id, ability_name, deaths, fights_affected, fight_model_phase, fight_model_label, non_attributable`. Sorted by death count desc.
- `GET /api/encounters/{id}/cartography`.
- 6 new tests (synthetic 5-player wipe + non-attributable bucket + sort + totals + unknown-encounter + live M5S AC). **190 tests total.**
- **Live AC on M5S** (45W/38K, 134 deaths): top "real" killing ability = 45942 (7 deaths in 5 fights), Sustained Damage DoT = 10 deaths in 3 fights. **95 deaths (71%) bucket as non-attributable** — the FFLogs sourceID=-1 / killing_ability_game_id=null cascade-death pattern noted way back in T-007. M-FAULT Mode 2 (T-302) will untangle those.

## [0.21.0] — 2026-05-24
### Added
- **T-203** Mechanic classification. `analysis/mechanic_classifier.py::classify_canonical_abilities()` walks `fight_model` rows for an encounter, scans damage events each canonical ability produced across ingested kills, and labels by **effect signature**:
  - `raidwide` — hits ≥6 players simultaneously
  - `tankbuster` — single target, mean damage ≥50k
  - `aoe_party` — 2–5 simultaneous targets (spread/stack candidate)
  - `enrage` — last canonical ability in phase + ≥5 player deaths within 3s of cast (rare in pure-kill data, will activate on prog wipes)
  - `cosmetic` — no damage events found (visual/transition casts)
  - `unknown` — heuristic couldn't decide
- "Active players" filter intentionally stricter than raw `combatants` (FFLogs masterData leaks NPCs on Ultimates) — counts only IDs that produced ≥1 cast or damage.
- Writes back to `fight_model.type_label`; preserves signature into `meta.signature` for later inspection.
- API: `POST /api/encounters/{id}/fight-model/classify`.
- 8 new tests (5 pure-function label tests + synthetic 3-pull fixture + missing-rows edge case + live FRU AC). **184 tests total.**
- **Live AC on FRU:** 149 abilities labeled — 40 raidwides, 13 tankbusters, 17 aoe_party, 74 cosmetic, 5 unknown. Correctly picked up known mechanics like the P1 tankbuster combo at +11s/+15s (Burnished Glory / Burnt Strike).

## [0.20.0] — 2026-05-24
### Added
- **T-202** Cross-group consensus → `fight_model` persistence. `analysis/consensus.py::write_consensus_to_fight_model(session, encounter_id, *, version=1)` runs T-104, then replaces rows for `(encounter_id, version)` atomically. `seq` is assigned by sorting canonical abilities by `median_relative_t_ms` within each phase. `confidence` = occurrence_rate; `type_label` left null (T-203 will fill: raidwide/tankbuster/tower/spread/stack/tether/enrage). `meta` carries `{sample_count, pulls_reaching}` for downstream filtering.
- `read_fight_model(session, encounter_id)` mirrors the write shape for the API.
- API: `POST /api/encounters/{id}/fight-model/persist` (trigger a write), `GET /api/encounters/{id}/fight-model` (read persisted rows).
- 5 new tests covering write/read/idempotency/seq-ordering/empty cases. **176 tests total.**
- **Live AC:** persisted FRU (encounter 1079) — 6 phases / 149 canonical abilities / 11 pulls aggregated — and M5S (encounter 101) — 2 phases / 18 abilities / 19 pulls.

## [0.19.0] — 2026-05-24
### Added — Phase 2 begins
- **T-201** Field backfill job — `jobs/backfill_field.py::backfill_once()`. For each tracked encounter, queries `worldData.encounter.fightRankings` for the top public reports, then runs T-004 `ingest_report` on each. Ledger short-circuit (PLAN Invariant 1) skips anything `complete`. For the top-N rankings (default 5), also pulls one-fight-scoped events — the slice T-104/T-202/T-204 need without the full 200×-cost of pulling every fight per Ultimate report.
- Default encounter set: FRU (1079) + current Savage tier (101–105). Configurable per-run via `--encounters` / `--reports-per-encounter` / `--events-top-n`.
- `GET /api/field-stats` and `FieldStats.jsx` React panel under Reports → per-encounter `reports_ingested` + `kills_with_events` (green when ≥3, enough for T-104 consensus).
- `python -m jobs.backfill_field [--dry-run]` deployment shape mirrors T-101: one-pass script, externally scheduled.
- 5 new tests (`tests/test_backfill_field.py` — mocked client; covers dry-run, ledger-skip, error-capture, empty rankings, field_stats aggregation). **171 tests total.**
- **Live AC: 103s pass** — discovered 60 rankings across 6 encounters, ingested 43 new report-meta records, pulled events for 9 new kill fights (~98k events). All FRU rankings already in dev DB (ledger-skipped). M5S now has 19 pulls / 18 canonical abilities, M6S 18 pulls / 10 canonical — T-104 consensus produces real timelines for current Savage encounters now, not just FRU.

## [0.18.0] — 2026-05-24
### Added
- **T-104** Cross-pull consensus timeline (boss-side only, PLAN §3 Invariant 3). `analysis/consensus.py::consensus_timeline_for_encounter(session, encounter_id)` reads every ingested kill of the encounter, runs T-103 phase segmentation per pull, and for each phase clusters boss casts by `ability_game_id`. An ability is **canonical** if `occurrence_rate ≥ 0.70` (PLAN-spec default). Median relative-t and a half-IQR variance proxy describe the timing distribution. Player casts are filtered via the `combatants` table.
- `GET /api/encounters/{id}/consensus` exposes the per-encounter canonical timeline.
- React `ConsensusTimeline` component in `PullDetail` (collapsed by default) — per-phase canonical-ability table sorted by median time.
- **Phase 1 of PLAN is now complete.** All 6 Phase-1 tasks (T-101, T-103, T-104, T-105, T-106, T-107, T-108) shipped.
- 8 new tests (6 unit + 2 API smoke). **166 tests total.**
- **Live AC nailed FRU's deterministic structure:** 11 ingested FRU kills produce a 6-phase consensus. The boss is essentially frame-deterministic — most canonical abilities recur at 100% with ±0.1s variance. A few abilities show high variance (±21s on ability 40149 in P0) — likely position-dependent reactions; future M-INFER refinements could disambiguate.
### Data
- Ingested **10 additional public FRU kills** via a one-off `scripts/ingest_fru_kills.py` so the consensus algorithm had ≥3 pulls per encounter to operate on. Dev DB now carries 11 FRU kill fights with events (~100k events of boss casts + damage).

## [0.17.0] — 2026-05-24
### Added
- **T-101** Live polling of the static's open reports — **manual watchlist** strategy (decision recorded 2026-05-24 in IDEAS.md). User pastes a report code or FFLogs URL; the poller fetches new fights+events on its next pass.
- New `watched_reports` table (alembic `51d86f5d4e40`): `code` PK, `label`, `active`, `added_at`, `last_polled_at`, `last_error`. **15 tables in dev DB.**
- `jobs/poll_watched.py::poll_once(session, client)` — iterates active rows, calls existing T-004 `ingest_report` + T-005 `ingest_events_for_report` (with explicit commits per the bug discovered shipping T-103). Skips ledger-`complete` rows so no API calls are wasted (PLAN Invariant 1). Errors are captured per-row in `last_error` rather than crashing the pass. `python -m jobs.poll_watched` runs one pass — user wraps in cron/Task Scheduler for periodic execution.
- CRUD routes: `GET /api/watched-reports`, `POST` (accepts bare code or full `https://www.fflogs.com/reports/<code>` URL — regex-extracts the code), `PATCH /api/watched-reports/{code}` (toggle active, edit label), `DELETE /api/watched-reports/{code}`. 409 on duplicate watch, 404 on unknown, 422 on empty input.
- React `WatchedReports.jsx` panel mounted at the top of the Reports tab — input form, per-row toggle (active/paused) and remove, last-poll timestamp + last-error display, code+label list with monospace code display.
- 13 new tests (9 API + 4 poller with mocked FFLogsClient covering empty-watchlist / complete-skip / inactive-skip / error-capture). **158 tests total passing.**
### Deployment shape
- One-shot script + cron rather than in-process scheduler — keeps the FastAPI server focused on serving requests and lets the user choose cadence externally. For dev: `python -m jobs.poll_watched`. For prod: wrap in cron / Task Scheduler / a while-loop, whatever fits the host.

## [0.16.0] — 2026-05-24
### Added
- **T-106** M-PARSE per-phase damage trajectory. `analysis/parse_trajectory.py::parse_per_phase_for_fight(session, fight_id)` consumes T-103 phase boundaries and aggregates per-player `damage` events within each phase, normalized to phase-duration seconds for DPS. Mode-1 metric (raw DPS) — true aDPS (FFLogs raid-buff-shared credit) is M-PARSE mode-2 work, deferred until the damage-attribution model is needed for M-GATE (T-207).
- `GET /api/fights/{id}/parse` returns `{phases: [{phase_index, start_offset_ms, end_offset_ms, duration_ms, players: [{player_id, name, job, damage_total, dps}]}]}`, players sorted by damage desc within each phase.
- React `ParseTable` component in `PullDetail` — per-player rows × per-phase columns matrix, DPS values formatted as `27.3k`; players ordered by total damage across the fight.
- 6 unit + 2 API tests. **145 tests total.**
- **Live AC on FRU kill #163:** P0 Fatebreaker top SAM 28.6k DPS; P2 Adds intermission spikes to 35.7k (cleave); P5 Fatebound peaks at 33.3k (burst CDs popping). Per-phase DPS curve matches real FRU optimization expectations — adds phases have visible damage spikes, final phase shows the burst climax.

## [0.15.0] — 2026-05-24
### Added
- **T-103** Phase segmentation (boss-side). `analysis/phases.py::detect_phase_boundaries(session, fight_id)` derives per-fight phase intervals from enemy-actor damage-activity windows: each distinct non-player target's `[first_hit, last_hit]` window is a phase, with overlapping windows merging into one phase (handles FRU P4's two concurrent boss models). Default `min_hits=30` filters trivial adds; default `merge_gap_ms=0` (overlap-only) preserves real phase boundaries that have small transition gaps.
- `GET /api/fights/{id}/phases` returns `{phases: [{index, start_ts, end_ts, start_offset_ms, end_offset_ms, boss_target_ids, hit_count}], transitions: [{after_phase, gap_ms}]}`. Offsets are relative to phase-0 start so the UI can render without absolute timestamps.
- React `PhaseStrip` component in `PullDetail` — color-coded horizontal bar with one segment per detected phase (width proportional to phase duration), hover tooltip shows boss-target IDs + phase duration, transition gaps listed beneath.
- 8 new tests (6 unit + 2 API). **137 tests total.**
- **Live AC against FRU kill #163 (1097s):** detected exactly **6 phases** matching the encounter's known structure — P0 Fatebreaker (144s) → P1 Usurper of Frost (185s) → P2 Adds intermission (1393+1395 merged via overlap, 43s) → P3 Oracle of Darkness (161s) → P4 Pandora's Knight + Athena (1421+1424 merged, 165s) → P5 Fatebound (193s). Transitions 4.5s / 22.2s / 27.8s / 5.8s / 77.4s match FFXIV cutscene timings.
### Changed
- Pulled events for one FRU kill fight (report `4RVNq7drBDLG3JZw`, fight DB-id 1500) so we have multi-phase dev data — the older FRU report `YAtzbP6RBrcnTg2j` is paywalled (archived). Dev DB now carries 29.7k events for the FRU kill.
### Known
- `ingest_report` flushes but does not commit; callers must `session.commit()` themselves. Caught this re-ingesting FRU when a TypeError in a subsequent call rolled the session back. Worth a docstring note or a refactor next time the function changes.
- T-004's combatant filter is too permissive on Ultimate reports — pulled 2.5M combatants across 208 FRU fights (~12k per fight) because non-player NPCs are leaking through. Doesn't break analysis (queries always filter by player_id from the table), but bloats storage. Tracked informally for now.

## [0.14.0] — 2026-05-24
### Added
- **T-105** M-BURST 2-minute burst alignment. `analysis/burst.py::burst_alignment_for_report` defines shared **burst windows** from raid-buff cast events (merging overlapping intervals at a fixed 20s default — XIVAPI doesn't expose buff durations cleanly; wiki scrape would refine) and counts per-player how many personal CD activations fired in-window vs. drifted. Action-labeled CDs use cast events; status-labeled CDs use applybuff events targeting the CD owner (split via `partition_by_kind`).
- `GET /api/reports/{code}/burst` returns `{raid_buff_ids, personal_buff_ids, window_ms, fights: [{burst_windows, players: [{in_window, drift, in_window_pct, …}]}]}`.
- React: `PullDetail` now shows a Burst alignment table per pull (color-coded: green ≥80%, grey ≥50%, red <50%).
- 9 unit tests (`tests/test_burst.py`) + 2 API smoke (`tests/test_burst_api.py`). **129 tests total.**
- **Live AC on M5S kill pull #11 (483s):** 9 burst windows detected (one every ~2 min — matches the cycle), Bard Raging Strikes 100% aligned (120s CD), Paladin Fight or Flight 50% (60s CD fires twice per cycle), Dragoon Lance Charge + Power Surge 40% — numbers match game-mechanics expectations.
### Changed
- **T-108 classifier** picked up two quality fixes uncovered while wiring T-105: (1) personal_buff with `self-only hint` bumped from 0.8 to 0.9 so it crosses the auto-high threshold; (2) `relabel_all()` now runs a name-matching post-pass — a *status* whose name matches an *action* labeled `raid_buff`/`mit_party`/`mit_boss_debuff` adopts the same label (Divination status was being mislabeled personal_buff because the status description "Damage dealt is increased." is too terse). Net result: raid_buff coverage 6 → 13, mit_party 9 → 17, personal_buff 17 → 4 (the 13 reassigned were status duplicates of raid buffs that shouldn't have been personal).

## [0.13.0] — 2026-05-23
### Added
- **T-107** Combatant → member resolution. `analysis/resolve_members.py::resolve_combatants_for_report` joins `combatants` to `character_aliases` by `(name, server)`, falls back to name-only when server is NULL — but only if a single alias claims that name (ambiguity leaves it unresolved instead of guessing). `coverage_summary` returns `{total_characters, resolved, unresolved[…]}` and filters FFLogs pseudo-actors (`Multiple Players`, `Limit Break`, empty-name, `Environment`).
- `GET /api/reports/{code}/roster-resolution` returns the per-fight combatant → member mapping + coverage in one payload.
- `ReportDetail.jsx`: roster-coverage banner (green when fully resolved, amber otherwise, listing the unresolved character names), and the pull-detail tables now display member name alongside character name (e.g. "Alice (Alice Tankerton)").
- 9 new tests (7 resolver: known alias / server-null fallback / unknown combatant / per-fight job differs / coverage / unknown-report / ambiguous-name; 2 API smoke). **116 tests total.**

## [0.12.0] — 2026-05-23
### Added
- **T-108** Ability DB + classifier + review queue (Phase 1 — unblocks T-105 M-BURST and T-303 M-MIT).
- Two new tables (alembic `eb34cac3f775`): `abilities` (XIVAPI metadata keyed on `ability_game_id`, with `kind = action|status|unknown`) and `ability_labels` (`label`, `confidence`, `source ∈ {auto, user}`). 14 tables in dev DB.
- `ingest/xivapi.py` — paced HTTP client + bootstrap that routes each ability to the right XIVAPI namespace (Action vs Status) based on dominant event type in the `events` table; handles FFLogs' **`+1,000,000` status-id offset** (fflogs id `1002216` → Status `2216` "The Wanderer's Minuet").
- `analysis/ability_classifier.py` — rule-based labeler emitting `(label, confidence)` across `raid_buff / personal_buff / mit_party / mit_boss_debuff / mit_self / damage_down / ignore / unknown`. Cleans XIVAPI's HTML + `<If(…)>` templates before matching. `relabel_all()` upserts into `ability_labels` and never overwrites `source='user'` rows.
- Three API endpoints: `GET /api/abilities/review-queue` (low-confidence + missing-label rows), `GET /api/abilities/labels` (filterable by label), `PATCH /api/abilities/{id}/label` (user override → `source='user'`, conf=1.0).
- React `Abilities.jsx` (new tab in `App.jsx`) — review-queue list + per-ability label-button row + all-labels filtered view; reads XIVAPI icon URLs directly.
- `scripts/bootstrap_abilities.py` live ingest harness (`--force`, `--limit`, `--skip-fetch` flags + summary output).
- 30 new tests: 13 XIVAPI client (mocked transport, classify_namespace, fetch_one routing, FFLogs status-offset handling), 12 classifier (Rampart / Shake It Off / Reprisal / Feint / Divination / Inner Release / Damage Down / potency-as-ignore), 6 API (review queue, user override, label-vocab validation, 404, filtering, create-on-PATCH). **107 tests total.**
- **Live AC against 608 distinct ability IDs in the dev DB's M5S+FRU events:** 264 resolved as XIVAPI Actions, 174 as Statuses (post offset-fix), 170 stayed unknown (boss-only mechanics / environmental damage — expected). Classifier auto-labeled 204 at ≥0.85 confidence. Correctly picked up real raid mechanics: **Divination / Mage's Ballad / Radiant Finale** (raid_buff), **Collective Unconscious / Intervention / Troubadour** (mit_party), **Reprisal** (mit_boss_debuff), **Rampart / Holy Sheltron / Guardian** (mit_self), **Damage Down** itself.

## [0.11.0] — 2026-05-23
### Added
- **T-009** Mode-1 dashboard — **Phase-0 gate cleared**. React shell with tab nav (Home / Reports / Roster) wired into URL hash; ingested-reports picker, wipe-histogram bar chart, pull-list → pull-detail (deaths with killing ability + ranked damage takers + GCD drop table per player).
- `GET /api/reports` listing ingested reports with fight/kill/wipe counts and most-common encounter id; drives the picker.
- Three new React modules: `Home.jsx` (stats grid + embedded prog-points curve), `Reports.jsx` (picker + ReportDetail wrapper), `ReportDetail.jsx` (wipe histogram + pull list + per-pull detail panel). `App.jsx` rewritten as a tabbed shell.
- 2 API tests for the list endpoint (`tests/test_reports_api.py`, scoped cleanup so it doesn't trample existing ingested reports). **76 tests total passing.**
- Live AC verification against the persisted M5S report `mVCt9aDdzq2Q8BLJ` (34 pulls / 2 kills / 32 wipes) — all four endpoints (`/api/reports`, `/wipes`, `/faults`, `/gcd`) returned the data the React shell expects; wipe histogram top bucket = ability 45917 (3 wipes); pull #1 root death = Kosaki Minami (AST) → ability 45926; worst GCD-drop player = Nailyl Eon (Pictomancer) with 33 drops — all consistent with the per-module verifications shipped in v0.6/0.7/0.8.

## [0.10.0] — 2026-05-23
### Added
- **T-010** Manual prog-point entry. `GET/POST/DELETE /api/prog-points` against the existing `prog_points` table (T-003). Creates default to `source='manual'`; `auto` reserved for live ingestion (T-101+). Validation requires at least one of `phase` or `fight_percentage`.
- React `ProgPoints.jsx` panel: entry form (datetime-local + phase + % remaining + pull count), Recharts line of % remaining over time (Y-axis reversed so further-into-fight reads lower), reverse-chronological list with delete. Wired into `App.jsx`.
- 7 API tests (empty list / create+list / validation 422 / time-sorted / delete / 404 / percentage-only). 74 tests total.
### Fixed
- v0.9.0 folder-name blocker cleared by user-side rename to `d:\Misc\Vigil`. `vite build` produces a clean prod bundle again.

## [0.9.0] — 2026-05-23
### Added
- **T-011** Static roster + character aliases. `Member` and `CharacterAlias` ORM models (`db/models.py`); members are decoupled from job (job derived per fight via CombatantInfo in T-107). `(character_name, server)` is globally unique; alias delete cascades on member delete.
- Alembic migration `8f2f3edb79f7_t_011_members_character_aliases`. 12 tables in dev DB.
- CRUD routes: `GET/POST/PATCH/DELETE /api/members`, `POST /api/members/{id}/aliases`, `DELETE /api/aliases/{id}`. Pydantic schemas + 409s on duplicates.
- React `Members.jsx` editor: list / add / delete members + per-member character aliases. Wired into `App.jsx`.
- 9 API tests (empty list, create-with-aliases, duplicate name 409, duplicate alias 409, patch, cascade delete, add+remove alias, 404s). 67 tests total.
### Known issue
- `vite build` fails on `#` in the project folder name (`Project Alpha (Name #TBD)`). `vite dev` works locally with a warning. Folder rename required before more React lands (T-009).

## [0.8.0] — 2026-05-23
### Added
- **T-008** M-GCD gcd-drop detection (`analysis/gcd.py`). Per-player GCD interval estimated from inter-cast median, spine-based GCD identification (filters oGCD weaves at 0.9 × GCD threshold), drops counted by missed slots in gaps > 1 full GCD. Drop timeline positions returned (capped at 200/fight-player).
- `GET /api/reports/{code}/gcd` route.
- 12 unit tests + live verification on M5S report — drop counts differentiate by role (Pict highest, tanks lowest). 57 tests total.
### Known limitation
- Boss-untargetable / forced-downtime windows currently count as drops. To be subtracted in Mode 2 via M-CONS (T-306) + M-INFER (T-103/202).

## [0.7.0] — 2026-05-23
### Added
- **T-007** Mode-1 fault basics (`analysis/faults.py::mode1_faults_for_report`): per-fight deaths with `killing_ability_game_id` + per-player `damage_taken_total` (non-player → player damage, sorted desc), with name/job from `combatants`.
- `GET /api/reports/{code}/faults` route serving the per-fight rollup.
- 7 unit tests covering death attribution, damage-takers sort/sum, non-player target exclusion, and `calculateddamage` non-double-count. `scripts/verify_faults.py` live AC harness. 45 tests total.

## [0.6.0] — 2026-05-23
### Added
- **T-006** M-WIPE wipe-location histogram (`analysis/wipes.py`). Bucketed by `(last_phase, last boss-cast ability_game_id)` from a configurable lookback window (default 15s) before each wipe's `end_time`. Player casts excluded via the `combatants` table.
- `GET /api/reports/{code}/wipes` route serving the JSON-serializable bucket structure (FastAPI, `api/main.py`).
- `scripts/verify_wipes.py` live AC harness and `scripts/rescan_events.py` for re-ingesting after event-coverage changes. 6 unit tests + 1 API smoke. 38 tests total.
### Changed
- **T-005 follow-up**: `ingest/events.py` now pulls **both friendly and enemy casts**. `DATA_TYPES` is a tuple of `(dataType, hostilityType)` pairs; `EVENTS_QUERY` accepts a `$hostilityType` variable. Required by M-WIPE / M-INFER which key on boss casts. Re-ingested the M5S verification report — +6,801 enemy-cast rows.

## [0.5.0] — 2026-05-23
### Added
- **T-005** Event normalization (`ingest/events.py`): `ingest_events_for_report` walks all 7 PLAN §7 dataTypes (DamageDone, DamageTaken, Casts, Buffs, Debuffs, Deaths, CombatantInfo), paginates on `nextPageTimestamp`, stores each event as a row keyed on `ability_game_id`. Resumable via `ingestion_ledger.last_event_ts`. Death events use `killingAbilityGameID` as the ability-id source when present.
- CombatantInfo events backfill `combatants.stats` JSONB (left null by T-004).
- 6 unit tests covering all-dataTypes, pagination, resume cursor, missing ledger, unknown-fight skipping, cursor-stuck break. 31 tests total.
- `scripts/verify_events.py` live AC harness: 244k events ingested over a 34-fight M5S report, 100% ability_game_id coverage.
### Changed
- `scripts/verify_delta.py` now prefers non-frozen FFLogs zones (archived reports paywall `events()` behind the paid `/user` endpoint).
### Fixed
- Test isolation bug in `tests/test_schema.py::test_report_fight_event_roundtrip` — query for ability id 7535 (Reprisal) wasn't scoped to the test's fight; clashed with real Reprisal events committed by `verify_events`. Now scoped to `fight_id`.

## [0.4.0] — 2026-05-23
### Added
- **T-004** Delta ingestion + ingestion ledger (`ingest/delta.py`): `ingest_report` writes `reports` / `fights` / `combatants` and the ledger row in one transaction. Cache-first per PLAN §3 Invariant 1 — a `complete` ledger short-circuits before any network call; `open` reruns only insert fight IDs not already in `fights_ingested`.
- Status auto-flips to `complete` when `report.endTime` is older than 6h (configurable). `mark_report_complete()` to force the flag.
- Combatants seeded from `masterData.actors` (Player-typed only); per (new fight, player). `stats` deferred to T-005.
- 7 unit tests covering first ingest, complete-rerun no-op (proves zero GraphQL calls), open-rerun delta, auto-complete heuristic, missing report, idempotent rerun. 25 tests total.
- `scripts/verify_delta.py` live AC harness: discovers a public report and exercises first-ingest → rerun → force-complete → rerun on real FFLogs.

## [0.3.0] — 2026-05-23
### Added
- **T-003** Postgres schema + migrations for all PLAN §6 tables (`reports`, `ingestion_ledger`, `fights`, `combatants`, `events`, `fight_model`, `strat_config`, `fault_scores`, `prog_points`, `analysis_cache`). SQLAlchemy 2.0 declarative models in `db/models.py`; Postgres-native `JSONB` and `ARRAY(Integer)` where PLAN requires.
- Initial alembic migration `ab0d3c6000ed_initial_schema_t_003`; applied to local dev DB.
- Indexes: `ix_fights_encounter_id`, `ix_events_fight_ts`, `ix_events_ability_game_id`, `ix_events_fight_type`. Unique constraint `uq_fights_report_fight` on `(report_code, fight_id_in_report)`.
- 7 schema roundtrip tests in `tests/test_schema.py` (all-tables present, BIGSERIAL assignment, JSONB nested data, ARRAY, composite PKs, unique-constraint enforcement). 19 tests total passing.
- `tests/conftest.py` with savepoint-based `db_session` fixture so DB-bound tests roll back cleanly.

## [0.2.0] — 2026-05-23
### Added
- **T-002** FFLogs OAuth client-credentials module (`ingest/fflogs.py`): `FFLogsClient` with in-memory token cache, 60s refresh margin, GraphQL helper, single-retry on 401 with force-refresh. Typed errors (`FFLogsAuthError`, `FFLogsAPIError`).
- 11 unit tests in `tests/test_fflogs_client.py` covering token exchange, caching, refresh, expiry margin, retries, and error paths (mocked via `httpx.MockTransport`).
- `scripts/verify_fflogs.py` — live AC verification: obtain → refresh → rate-limit → discover a public report via `worldData.encounter.fightRankings` → fetch via `reportData.report(code)`.
- `.venv` provisioned (Python 3.12.4) and project installed editable with dev extras (pytest, ruff).
- Local Postgres 18.4 installed with dev DB `fflogs_tracker`; `DATABASE_URL` populated in gitignored `.env`.

## [0.1.0] — 2026-05-23
### Added
- Repo scaffold per PLAN §4: `api/`, `db/`, `ingest/`, `analysis/`, `model/`, `jobs/`, `tests/`, `web/`.
- FastAPI skeleton (`api/main.py`) with `/healthz` route; CORS wired to the Vite dev origin.
- React + Vite skeleton in `web/` with a dev proxy to the API; Recharts + d3 listed as deps for later phases.
- Alembic initialized (`alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/`); migrations land in T-003.
- Environment-driven config via `pydantic-settings` reading `.env` (DATABASE_URL, FFLOGS_CLIENT_ID, FFLOGS_CLIENT_SECRET, API host/port, web origin).
- Project state files: `PROGRESS.md`, `CHANGELOG.md`, `IDEAS.md`.
- `.gitignore` covering `.env`, `CLAUDE.local.md`, Python and Node build artifacts.
- Smoke test (`tests/test_smoke.py`) asserting `/healthz` responds with the current version.
