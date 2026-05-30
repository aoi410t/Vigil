"""ORM models for PLAN.md §6 tables (T-003).

Types and indexes mirror the pseudo-DDL exactly; FKs only where PLAN declares them.
Postgres-specific (`JSONB`, `ARRAY`) — this project is Postgres-only by §4.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.session import Base


class Static(Base):
    """A raid group / static (v1.6.0 multi-tenant).

    Scoped tables (watched_reports, members, strat_config, prog_points,
    fault_scores) carry `static_id` so two statics share raw FFLogs data
    but keep their own watchlists, rosters, strats, prog points, and
    fault scores. The "Default Static" (id=1) holds all data migrated
    from the pre-1.6.0 single-tenant install.
    """

    __tablename__ = "statics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class User(Base):
    """An authenticated user (v1.6.0). HTTP Basic username maps 1:1 to this
    row; the auth middleware auto-creates on first login and creates their
    own static so a fresh install just works.

    `current_static_id` is the user's last-selected static; the static
    switcher in the UI writes it. All scoped API endpoints filter by this.

    `is_developer` (v1.7.1) gates dev-only UI surfaces (Abilities review
    queue, Field data panel, "show all encounters" toggle, etc.). Set on
    login: True when the user logs in with the `DEV_PASSWORD` env var, or
    (backwards compat) when only `AUTH_PASSWORD` is configured and the
    username matches `AUTH_USERNAME`.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    current_static_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("statics.id", ondelete="SET NULL"), nullable=True,
    )
    is_developer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class StaticMembership(Base):
    """User <-> Static N:M relation (v1.6.0)."""

    __tablename__ = "static_memberships"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )
    static_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("statics.id", ondelete="CASCADE"), primary_key=True,
    )
    joined_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Report(Base):
    __tablename__ = "reports"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    owner: Mapped[Optional[str]] = mapped_column(Text)
    region: Mapped[Optional[str]] = mapped_column(Text)
    is_public: Mapped[Optional[bool]] = mapped_column(Boolean)
    start_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class IngestionLedger(Base):
    __tablename__ = "ingestion_ledger"

    report_code: Mapped[str] = mapped_column(
        Text, ForeignKey("reports.code"), primary_key=True
    )
    fights_ingested: Mapped[Optional[list[int]]] = mapped_column(ARRAY(Integer))
    last_event_ts: Mapped[Optional[int]] = mapped_column(BigInteger)
    status: Mapped[Optional[str]] = mapped_column(Text)
    last_polled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Fight(Base):
    __tablename__ = "fights"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    report_code: Mapped[str] = mapped_column(Text, ForeignKey("reports.code"))
    fight_id_in_report: Mapped[int] = mapped_column(Integer)
    encounter_id: Mapped[Optional[int]] = mapped_column(Integer)
    is_kill: Mapped[Optional[bool]] = mapped_column(Boolean)
    fight_percentage: Mapped[Optional[float]] = mapped_column(Numeric)
    last_phase: Mapped[Optional[int]] = mapped_column(Integer)
    start_time: Mapped[Optional[int]] = mapped_column(BigInteger)
    end_time: Mapped[Optional[int]] = mapped_column(BigInteger)
    duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger)

    __table_args__ = (
        Index("ix_fights_encounter_id", "encounter_id"),
        UniqueConstraint(
            "report_code", "fight_id_in_report", name="uq_fights_report_fight"
        ),
    )


class Combatant(Base):
    __tablename__ = "combatants"

    fight_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fights.id"), primary_key=True
    )
    player_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[Optional[str]] = mapped_column(Text)
    server: Mapped[Optional[str]] = mapped_column(Text)
    job: Mapped[Optional[str]] = mapped_column(Text)
    stats: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    fight_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("fights.id"))
    ts: Mapped[Optional[int]] = mapped_column(BigInteger)
    type: Mapped[Optional[str]] = mapped_column(Text)
    source_id: Mapped[Optional[int]] = mapped_column(Integer)
    target_id: Mapped[Optional[int]] = mapped_column(Integer)
    ability_game_id: Mapped[Optional[int]] = mapped_column(Integer)
    amount: Mapped[Optional[int]] = mapped_column(BigInteger)
    raw: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)

    __table_args__ = (
        Index("ix_events_fight_ts", "fight_id", "ts"),
        Index("ix_events_ability_game_id", "ability_game_id"),
        Index("ix_events_fight_type", "fight_id", "type"),
    )


class FightModel(Base):
    """Boss-side ONLY (Invariant 3). Versioned; crowd-mappable."""

    __tablename__ = "fight_model"

    encounter_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    phase: Mapped[int] = mapped_column(Integer, primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    ability_game_id: Mapped[Optional[int]] = mapped_column(Integer)
    relative_t_ms: Mapped[Optional[int]] = mapped_column(BigInteger)
    time_variance_ms: Mapped[Optional[int]] = mapped_column(BigInteger)
    type_label: Mapped[Optional[str]] = mapped_column(Text)
    confidence: Mapped[Optional[float]] = mapped_column(Numeric)
    meta: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    cactbot_label: Mapped[Optional[str]] = mapped_column(Text)
    cactbot_phase_label: Mapped[Optional[str]] = mapped_column(Text)
    cactbot_expected_t_ms: Mapped[Optional[int]] = mapped_column(BigInteger)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class StratConfig(Base):
    """User-authored (Invariant 3). Never inferred from other groups.
    Scoped per static (v1.6.0): two statics may have different strats for
    the same mechanic on the same encounter."""

    __tablename__ = "strat_config"

    static_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("statics.id", ondelete="CASCADE"), primary_key=True,
    )
    encounter_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mechanic_ref: Mapped[str] = mapped_column(Text, primary_key=True)
    assignments: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    mit_plan: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)


class FaultScore(Base):
    """Per-(static, fight, player) fault scores (v1.6.0 scoped). Different
    statics may compute different scores against their own strat plans."""

    __tablename__ = "fault_scores"

    static_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("statics.id", ondelete="CASCADE"), primary_key=True,
    )
    fight_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    player_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    score: Mapped[Optional[float]] = mapped_column(Numeric)
    reasons: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)


class ProgPoint(Base):
    __tablename__ = "prog_points"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    static_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("statics.id", ondelete="CASCADE"), nullable=False,
    )
    ts: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    phase: Mapped[Optional[int]] = mapped_column(Integer)
    fight_percentage: Mapped[Optional[float]] = mapped_column(Numeric)
    pull_count: Mapped[Optional[int]] = mapped_column(Integer)
    source: Mapped[Optional[str]] = mapped_column(Text)  # 'auto' | 'manual'


class AnalysisCache(Base):
    __tablename__ = "analysis_cache"

    fight_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    module: Mapped[str] = mapped_column(Text, primary_key=True)
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    computed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Member(Base):
    """Static roster (T-011). Decoupled from job — job is derived per fight from CombatantInfo
    in T-107. A member can own multiple `CharacterAlias` rows for sub-accounts/alts.

    Scoped per static (v1.6.0): two statics may have a member named "Alice"
    independently; uniqueness is (static_id, name)."""

    __tablename__ = "members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    static_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("statics.id", ondelete="CASCADE"), nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # 'core' (regular static member) | 'substitute' (backup who fills in).
    # Both count toward analytics identically today; the tag is for the human's
    # roster view in v1.15.0. NOT NULL with server-side default 'core' so older
    # rows backfill cleanly.
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="core",
                                      server_default="core")
    role_pref: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("static_id", "name", name="uq_members_static_name"),
    )


class WatchedReport(Base):
    """T-101 manual watchlist — user-pasted report codes the poller will ingest
    on its next pass. `active=False` retires a report without deleting it (so
    the audit trail stays intact). `label` is freeform UI annotation, e.g.
    "FRU prog week 3".

    Scoped per static (v1.6.0): PK is (static_id, code). Two statics may
    watch the same report — the ingestion ledger short-circuits redundant
    raw-data work."""

    __tablename__ = "watched_reports"

    static_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("statics.id", ondelete="CASCADE"), primary_key=True,
    )
    code: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[Optional[str]] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    added_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_polled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)


class Ability(Base):
    """FFXIV action/status metadata from XIVAPI, keyed on `ability_game_id` (T-108).

    `kind` records which XIVAPI namespace the row was sourced from — Action and
    Status overlap on some IDs, so we pick the one matching the dominant event
    type for that ID in our `events` table.
    """

    __tablename__ = "abilities"

    ability_game_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[Optional[str]] = mapped_column(Text)  # 'action' | 'status' | 'unknown'
    name: Mapped[Optional[str]] = mapped_column(Text)
    description: Mapped[Optional[str]] = mapped_column(Text)
    icon: Mapped[Optional[str]] = mapped_column(Text)
    raw: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Optional wiki-scraped buff duration. Populated by
    # scripts/scrape_ability_durations.py. M-BURST in analysis/burst.py uses
    # this when present to size per-raid-buff windows instead of the 20s
    # default. NULL for abilities we haven't scraped or that don't have a
    # parseable duration on the wiki.
    duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # Optional wiki-scraped damage-reduction percentage (0..100). Same scrape
    # source as duration_ms. NULL when no mit value parsed. Strat editor uses
    # it for palette tooltips; reserved for M-MIT damage quantification later.
    mit_pct: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class AbilityLabel(Base):
    """Classifier output + user overrides (T-108).

    `source = 'auto'` rows came from the rule-based classifier; `'user'` rows
    are review-queue confirmations or overrides. M-BURST (T-105) and M-MIT
    (T-303) read `auto` rows only if `confidence >= AUTO_HIGH_THRESHOLD` plus
    all `'user'` rows.
    """

    __tablename__ = "ability_labels"

    ability_game_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("abilities.ability_game_id"), primary_key=True
    )
    label: Mapped[Optional[str]] = mapped_column(Text)
    confidence: Mapped[Optional[float]] = mapped_column(Numeric)
    source: Mapped[Optional[str]] = mapped_column(Text)  # 'auto' | 'user'
    notes: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class CharacterAlias(Base):
    """FFXIV character a member plays under. Scoping is via member -> static
    (v1.6.0); no global (name, server) uniqueness anymore — two statics may
    each have an "Alice (Faerie)" alias."""

    __tablename__ = "character_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    member_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("members.id", ondelete="CASCADE"), nullable=False
    )
    character_name: Mapped[str] = mapped_column(Text, nullable=False)
    server: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class IgnoredCharacter(Base):
    """Per-static "do not show this character anywhere" list (v1.15.0).

    A combatant name that should be hidden from the unclassified-characters
    panel and treated as "not part of our static" by downstream views. Persists
    the user's classification decision so we don't re-surface the same pugs /
    randoms / loot trades every time new fights come in.

    Server may be NULL when the combatant row didn't carry one (older imports);
    we de-dupe on (static_id, character_name, COALESCE(server, '')).
    """

    __tablename__ = "ignored_characters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    static_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("statics.id", ondelete="CASCADE"), nullable=False,
    )
    character_name: Mapped[str] = mapped_column(Text, nullable=False)
    server: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class FFLogsUserAuth(Base):
    """One-row table holding the connected user's FFLogs OAuth tokens.

    Single-user app today; `id=1` is enforced by code. The `refresh_token` is
    long-lived (FFLogs rotates it on every refresh — we update accordingly).
    The `access_token` is short-lived (~1h); we refresh on expiry. Connecting
    a new user overwrites the same row.
    """

    __tablename__ = "fflogs_user_auth"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    access_token: Mapped[Optional[str]] = mapped_column(Text)
    access_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    scope: Mapped[Optional[str]] = mapped_column(Text)
    user_label: Mapped[Optional[str]] = mapped_column(Text)
    connected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
