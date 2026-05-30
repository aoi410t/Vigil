"""FastAPI entrypoint. Run with: `uvicorn api.main:app --reload`."""
from __future__ import annotations

import base64
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from sqlalchemy import func

from analysis._encounter import (
    canonical_encounter_id,
    encounter_id_group,
)
from analysis.ability_classifier import AUTO_HIGH_THRESHOLD, LABELS
from analysis.burst import burst_alignment_for_report
from analysis.consensus import (
    consensus_timeline_for_encounter,
    read_fight_model,
    write_consensus_to_fight_model,
)
from analysis.cartography import cartography_for_encounter
from analysis.consistency import consistency_for_encounter
from analysis.dps_check import (
    compare_fight_to_target as dps_compare_fight,
    dps_check_for_encounter,
    dps_comparison_for_encounter,
)
from analysis.fault_attribution import (
    classify_wipe_type,
    compute_fault_scores_for_fight,
    fault_aggregate_for_encounter,
    fault_scores_for_fight,
)
from analysis.fault_breakdown import fault_breakdown_for_encounter
from analysis.fault_disambiguation import disambiguate_for_fight
from analysis.gate_diagnostic import gate_diagnostic_for_fight
from analysis.mechanic_classifier import classify_canonical_abilities
from analysis.mit_audit import (
    mit_audit_aggregate_for_encounter,
    mit_audit_for_fight,
    mit_audit_summary,
)
from analysis.optimization import post_clear_targets_for_encounter
from analysis.prog_trajectory import prog_trajectory_for_encounter
from analysis.recovery import recovery_for_fight
from analysis.session_report import generate_session_report
from analysis import strat_config as strat_config_mod
from analysis.timeline_diff import timeline_diff_for_fight
from ingest.cactbot import annotate_fight_model_for_encounter
from ingest.fflogs import (
    FFLogsAuthError,
    FFLogsClient,
    FFLogsUserAuthNotConfigured,
)
from jobs.backfill_field import DEFAULT_ENCOUNTERS, field_stats
from jobs.poll_watched import poll_one_by_code
from analysis.faults import mode1_faults_for_report
from analysis.gcd import mode1_gcd_for_report
from analysis.parse_trajectory import parse_per_phase_for_fight
from analysis.phases import detect_phase_boundaries
from analysis.resolve_members import (
    coverage_summary as roster_coverage_summary,
    resolve_combatants_for_report,
)
from analysis.roster_discovery import discovered_characters_for_static
from analysis.wipes import wipe_histogram_for_report
from api import __version__
from api.auth import Context, get_context, require_static_membership
from api.config import settings
from db.models import (
    Ability,
    AbilityLabel,
    CharacterAlias,
    FFLogsUserAuth,
    Fight,
    FightModel,
    IgnoredCharacter,
    Member,
    ProgPoint,
    Report,
    Static,
    StaticMembership,
    User,
    WatchedReport,
)
from db.session import SessionLocal

app = FastAPI(title="FFLogs Progression Tracker", version=__version__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.web_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# HTTP Basic auth — only active when AUTH_PASSWORD is set (prod-via-quick-
# tunnel mode). Dev + test runs leave it unset and the middleware is a no-op.
# /healthz always passes for monitoring probes.
#
# Multi-user model (v1.6.0+): username is free-form, password validates
# against either AUTH_PASSWORD (user mode) or DEV_PASSWORD (dev mode,
# v1.7.1). Which password matched is stashed on request.state.auth_match so
# api/auth.get_context can set the User.is_developer flag accordingly.
@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    if not (settings.auth_password or settings.dev_password):
        # Dev/test mode: no auth configured. Let everything through.
        return await call_next(request)
    if request.url.path == "/healthz":
        return await call_next(request)
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("basic "):
        return Response(
            content="Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="vigil"'},
        )
    try:
        decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
        provided_user, _, provided_pw = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return Response(content="Unauthorized", status_code=401)
    if not provided_user:
        return Response(content="Unauthorized", status_code=401)
    dev_ok = (settings.dev_password
              and secrets.compare_digest(provided_pw, settings.dev_password))
    user_ok = (settings.auth_password
               and secrets.compare_digest(provided_pw, settings.auth_password))
    if not (dev_ok or user_ok):
        return Response(
            content="Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="vigil"'},
        )
    # Stash which side authenticated so the auth-context dependency can
    # set is_developer accordingly.
    request.state.auth_match = "dev" if dev_ok else "user"
    return await call_next(request)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


# ---------------------------------------------------------------------------
# FFLogs user-OAuth (authorization-code flow). One-time consent unlocks the
# connected user's Gold-tier perks (archived + private reports) via /api/v2/user.
# ---------------------------------------------------------------------------
_oauth_states: dict[str, float] = {}  # state -> expiry epoch
_OAUTH_STATE_TTL_S = 600  # 10 min


def _gc_oauth_states(now: float | None = None) -> None:
    t = now if now is not None else __import__("time").time()
    expired = [k for k, exp in _oauth_states.items() if exp < t]
    for k in expired:
        _oauth_states.pop(k, None)


@app.get("/auth/fflogs/login")
def fflogs_oauth_login() -> RedirectResponse:
    """Step 1: redirect the user to the FFLogs consent screen."""
    import time as _time
    _gc_oauth_states()
    with FFLogsClient() as client:
        state = client.new_state()
        _oauth_states[state] = _time.time() + _OAUTH_STATE_TTL_S
        url = client.build_authorize_url(
            redirect_uri=settings.fflogs_redirect_uri, state=state,
        )
    return RedirectResponse(url, status_code=302)


@app.get("/auth/fflogs/callback")
def fflogs_oauth_callback(code: str | None = None, state: str | None = None,
                          error: str | None = None) -> RedirectResponse:
    """Step 2: receive code from FFLogs, exchange for tokens, persist, redirect home."""
    if error:
        raise HTTPException(status_code=400, detail=f"FFLogs returned error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")
    if state not in _oauth_states:
        raise HTTPException(status_code=400, detail="Invalid or expired state")
    _oauth_states.pop(state, None)
    _require_db()
    with SessionLocal() as session, FFLogsClient() as client:
        try:
            client.exchange_authorization_code(
                session=session, code=code,
                redirect_uri=settings.fflogs_redirect_uri,
            )
        except FFLogsAuthError as e:
            raise HTTPException(status_code=502, detail=f"Token exchange failed: {e}")
        session.commit()
    return RedirectResponse("/#fflogs-connected", status_code=302)


@app.get("/api/fflogs-auth/status")
def fflogs_auth_status() -> dict[str, Any]:
    _require_db()
    with SessionLocal() as session, FFLogsClient() as client:
        return client.user_auth_status(session)


@app.delete("/api/fflogs-auth/connection")
def fflogs_auth_disconnect() -> dict[str, str]:
    _require_db()
    with SessionLocal() as session:
        row = session.get(FFLogsUserAuth, 1)
        if row is not None:
            session.delete(row)
            session.commit()
    return {"status": "disconnected"}


# ---------------------------------------------------------------------------
# v1.6.0 multi-static: user identity + statics management.
# ---------------------------------------------------------------------------


class StaticOut(BaseModel):
    id: int
    name: str


class StaticCreate(BaseModel):
    name: str


class StaticMemberOut(BaseModel):
    user_id: int
    username: str


class MeOut(BaseModel):
    user_id: int
    username: str
    current_static_id: int
    is_developer: bool
    statics: list[StaticOut]


class CurrentStaticPatch(BaseModel):
    static_id: int


class AddMemberByUsernamePayload(BaseModel):
    username: str


@app.get("/api/me", response_model=MeOut)
def whoami(ctx: Context = Depends(get_context)) -> MeOut:
    """Return the current user, their selected static, all statics they
    belong to, and whether they're a developer (drives dev-only UI surfaces)."""
    _require_db()
    with SessionLocal() as session:
        rows = session.execute(
            select(Static)
            .join(StaticMembership, StaticMembership.static_id == Static.id)
            .where(StaticMembership.user_id == ctx.user_id)
            .order_by(Static.name)
        ).scalars().all()
        return MeOut(
            user_id=ctx.user_id,
            username=ctx.username,
            current_static_id=ctx.current_static_id,
            is_developer=ctx.is_developer,
            statics=[StaticOut(id=s.id, name=s.name) for s in rows],
        )


@app.get("/api/me/encounters")
def my_encounters(ctx: Context = Depends(get_context)) -> dict[str, Any]:
    """Encounters the current static has watched-report data for (v1.8.0).

    Returns the auto-detected `active` encounter (most recent watched fight
    wins) and an `encounters` list with per-encounter pull / kill / wipe
    counts. Drives the consumer Home dashboard's encounter picker.
    Empty `encounters` means the user has no watched reports yet — the
    Home falls back to onboarding.

    v1.17.0: cloned encounter IDs (e.g. DSR 1065 + 1076) collapse into
    one row keyed on the canonical ID. Their counts merge.
    """
    _require_db()
    with SessionLocal() as session:
        watched_codes = select(WatchedReport.code).where(
            WatchedReport.static_id == ctx.current_static_id
        )
        rows = session.execute(
            select(
                Fight.encounter_id,
                func.count(Fight.id),
                func.count(Fight.id).filter(Fight.is_kill.is_(True)),
                func.count(Fight.id).filter(Fight.is_kill.is_(False)),
                func.max(Fight.end_time),
            )
            .where(Fight.encounter_id.is_not(None),
                   Fight.report_code.in_(watched_codes))
            .group_by(Fight.encounter_id)
        ).all()

        # v1.17.0: merge cloned encounters into their canonical bucket.
        merged: dict[int, dict[str, Any]] = {}
        for eid, p, k, w, t in rows:
            canonical = canonical_encounter_id(int(eid))
            b = merged.setdefault(canonical, {
                "encounter_id": canonical,
                "pulls": 0, "kills": 0, "wipes": 0,
                "latest_end_time": None,
            })
            b["pulls"] += int(p)
            b["kills"] += int(k)
            b["wipes"] += int(w)
            if t is not None:
                t_int = int(t)
                if b["latest_end_time"] is None or t_int > b["latest_end_time"]:
                    b["latest_end_time"] = t_int
        encounters = list(merged.values())
        encounters.sort(
            key=lambda e: (e["latest_end_time"] or 0, e["pulls"]),
            reverse=True,
        )
        active = encounters[0]["encounter_id"] if encounters else None
        return {"active": active, "encounters": encounters}


@app.patch("/api/me/current-static", response_model=MeOut)
def set_current_static(payload: CurrentStaticPatch,
                       ctx: Context = Depends(get_context)) -> MeOut:
    """Switch the user's current static. 404 if user isn't a member of it."""
    _require_db()
    with SessionLocal() as session:
        require_static_membership(session, ctx.user_id, payload.static_id)
        user = session.get(User, ctx.user_id)
        user.current_static_id = payload.static_id
        session.commit()
    # Return the refreshed /me payload with the new current_static_id
    new_ctx = Context(user_id=ctx.user_id, username=ctx.username,
                      current_static_id=payload.static_id,
                      is_developer=ctx.is_developer)
    return whoami(new_ctx)


@app.get("/api/statics", response_model=list[StaticOut])
def list_my_statics(ctx: Context = Depends(get_context)) -> list[StaticOut]:
    """List statics the current user is a member of."""
    _require_db()
    with SessionLocal() as session:
        rows = session.execute(
            select(Static)
            .join(StaticMembership, StaticMembership.static_id == Static.id)
            .where(StaticMembership.user_id == ctx.user_id)
            .order_by(Static.name)
        ).scalars().all()
        return [StaticOut(id=s.id, name=s.name) for s in rows]


@app.post("/api/statics", response_model=StaticOut, status_code=201)
def create_static(payload: StaticCreate,
                  ctx: Context = Depends(get_context)) -> StaticOut:
    """Create a new static. Creator is auto-joined as a member and (when this
    is their first non-default static) the new static becomes their current."""
    _require_db()
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name required")
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        s = Static(name=name, created_at=now)
        session.add(s)
        session.flush()
        session.add(StaticMembership(user_id=ctx.user_id, static_id=s.id,
                                     joined_at=now))
        # Auto-switch the user to their newly-created static. They explicitly
        # asked to create it; the assumption that they want to use it
        # immediately is reasonable, and the switcher dropdown is right
        # there if they want to flip back.
        user = session.get(User, ctx.user_id)
        user.current_static_id = s.id
        session.commit()
        session.refresh(s)
        return StaticOut(id=s.id, name=s.name)


@app.get("/api/statics/{static_id}/members",
         response_model=list[StaticMemberOut])
def list_static_members(static_id: int,
                        ctx: Context = Depends(get_context)
                       ) -> list[StaticMemberOut]:
    """List users in a static. Caller must be a member."""
    _require_db()
    with SessionLocal() as session:
        require_static_membership(session, ctx.user_id, static_id)
        rows = session.execute(
            select(User)
            .join(StaticMembership, StaticMembership.user_id == User.id)
            .where(StaticMembership.static_id == static_id)
            .order_by(User.username)
        ).scalars().all()
        return [StaticMemberOut(user_id=u.id, username=u.username)
                for u in rows]


@app.post("/api/statics/{static_id}/members",
          response_model=StaticMemberOut, status_code=201)
def add_static_member(static_id: int, payload: AddMemberByUsernamePayload,
                      ctx: Context = Depends(get_context)
                     ) -> StaticMemberOut:
    """Add an existing user (by username) to a static. Caller must be a
    member. The added user must already exist (no auto-create here — that
    happens on first login by that username)."""
    _require_db()
    with SessionLocal() as session:
        require_static_membership(session, ctx.user_id, static_id)
        username = payload.username.strip()
        if not username:
            raise HTTPException(status_code=422, detail="username required")
        target = session.execute(
            select(User).where(User.username == username)
        ).scalar_one_or_none()
        if target is None:
            raise HTTPException(
                status_code=404,
                detail=f"no user '{username}' — they must log in once first",
            )
        existing = session.execute(
            select(StaticMembership)
            .where(StaticMembership.user_id == target.id,
                   StaticMembership.static_id == static_id)
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail="already a member")
        session.add(StaticMembership(user_id=target.id, static_id=static_id,
                                     joined_at=datetime.now(timezone.utc)))
        session.commit()
        return StaticMemberOut(user_id=target.id, username=target.username)


@app.delete("/api/statics/{static_id}/members/{user_id}", status_code=204)
def remove_static_member(static_id: int, user_id: int,
                         ctx: Context = Depends(get_context)) -> Response:
    """Remove a user from a static. Either the user is removing themselves,
    or a fellow member is removing them (no role hierarchy yet). 404 on
    non-member; 409 if removing the last member of a static."""
    _require_db()
    with SessionLocal() as session:
        require_static_membership(session, ctx.user_id, static_id)
        m = session.execute(
            select(StaticMembership)
            .where(StaticMembership.user_id == user_id,
                   StaticMembership.static_id == static_id)
        ).scalar_one_or_none()
        if m is None:
            raise HTTPException(status_code=404,
                                detail="not a member of that static")
        # Last-member guard (would leave the static unreachable + orphan data).
        count = session.execute(
            select(func.count()).select_from(StaticMembership)
            .where(StaticMembership.static_id == static_id)
        ).scalar_one()
        if count <= 1:
            raise HTTPException(
                status_code=409,
                detail="can't remove the last member; delete the static instead",
            )
        session.delete(m)
        # If we just removed the user's current static, switch them to
        # another one they still belong to.
        target = session.get(User, user_id)
        if target is not None and target.current_static_id == static_id:
            other = session.execute(
                select(StaticMembership)
                .where(StaticMembership.user_id == user_id)
                .limit(1)
            ).scalar_one_or_none()
            target.current_static_id = other.static_id if other else None
        session.commit()
    return Response(status_code=204)


@app.get("/api/reports")
def list_reports() -> list[dict[str, Any]]:
    """All ingested reports with quick stats — drives the dashboard picker (T-009)."""
    _require_db()
    with SessionLocal() as session:
        rows = session.execute(
            select(
                Report.code,
                Report.start_time,
                Report.end_time,
                Report.ingested_at,
                func.count(Fight.id),
                func.count(Fight.id).filter(Fight.is_kill.is_(True)),
                func.count(Fight.id).filter(Fight.is_kill.is_(False)),
                func.mode().within_group(Fight.encounter_id),
            )
            .join(Fight, Fight.report_code == Report.code, isouter=True)
            .group_by(Report.code)
            .order_by(Report.start_time.desc().nullslast(), Report.code)
        ).all()
        return [
            {
                "code": code,
                "start_time": start.isoformat() if start else None,
                "end_time": end.isoformat() if end else None,
                "ingested_at": ing.isoformat() if ing else None,
                "fight_count": fc,
                "kill_count": kc,
                "wipe_count": wc,
                "encounter_id": eid,
            }
            for (code, start, end, ing, fc, kc, wc, eid) in rows
        ]


@app.get("/api/reports/{code}/wipes")
def get_wipe_histogram(code: str) -> dict[str, Any]:
    """M-WIPE (T-006): wipes bucketed by (phase, last boss cast ability)."""
    if SessionLocal is None:
        raise HTTPException(status_code=503, detail="database not configured")
    with SessionLocal() as session:
        return wipe_histogram_for_report(session, code)


@app.get("/api/reports/{code}/faults")
def get_mode1_faults(code: str) -> dict[str, Any]:
    """T-007 Mode-1 fault basics: per-pull deaths w/ killing ability + damage takers."""
    if SessionLocal is None:
        raise HTTPException(status_code=503, detail="database not configured")
    with SessionLocal() as session:
        return mode1_faults_for_report(session, code)


@app.get("/api/reports/{code}/gcd")
def get_mode1_gcd(code: str) -> dict[str, Any]:
    """M-GCD (T-008): per-fight, per-player GCD drop count + timeline positions."""
    if SessionLocal is None:
        raise HTTPException(status_code=503, detail="database not configured")
    with SessionLocal() as session:
        return mode1_gcd_for_report(session, code)


@app.get("/api/reports/{code}/roster-resolution")
def get_roster_resolution(code: str) -> dict[str, Any]:
    """T-107: per-fight combatant→member resolution + coverage summary."""
    _require_db()
    with SessionLocal() as session:
        result = resolve_combatants_for_report(session, code)
        result["coverage"] = roster_coverage_summary(session, code)
        return result


@app.get("/api/reports/{code}/burst")
def get_burst_alignment(code: str) -> dict[str, Any]:
    """M-BURST (T-105): per-fight burst windows + per-player alignment ratio."""
    _require_db()
    with SessionLocal() as session:
        return burst_alignment_for_report(session, code)


@app.get("/api/encounters")
def list_encounters() -> list[dict[str, Any]]:
    """T-208: encounters we have data for — drives the Encounters tab picker.

    Returns each encounter with report/fight/kill/wipe counts, kills-with-events,
    and fight_model ability count. Anything with at least one ingested fight
    is listed.
    """
    _require_db()
    from db.models import Event

    with SessionLocal() as s:
        base = s.execute(
            select(
                Fight.encounter_id,
                func.count(func.distinct(Fight.report_code)),
                func.count(Fight.id),
                func.count(Fight.id).filter(Fight.is_kill.is_(True)),
                func.count(Fight.id).filter(Fight.is_kill.is_(False)),
            )
            .where(Fight.encounter_id.is_not(None))
            .group_by(Fight.encounter_id)
        ).all()

        ev_kill_rows = s.execute(
            select(Fight.encounter_id, func.count(func.distinct(Event.fight_id)))
            .join(Event, Event.fight_id == Fight.id)
            .where(Fight.is_kill.is_(True))
            .group_by(Fight.encounter_id)
        ).all()
        ev_lookup = {eid: int(cnt) for eid, cnt in ev_kill_rows}

        fm_rows = s.execute(
            select(FightModel.encounter_id,
                   func.count(FightModel.ability_game_id))
            .group_by(FightModel.encounter_id)
        ).all()
        fm_lookup = {eid: int(cnt) for eid, cnt in fm_rows}

        # v1.17.0: collapse cloned encounter rows into their canonical bucket.
        # Use a set of distinct report_codes per canonical group to avoid
        # double-counting reports that appear under both halves.
        report_codes_by_canonical: dict[int, set[str]] = {}
        if base:
            code_rows = s.execute(
                select(Fight.encounter_id, Fight.report_code)
                .where(Fight.encounter_id.is_not(None))
                .distinct()
            ).all()
            for eid, code in code_rows:
                canonical = canonical_encounter_id(int(eid))
                report_codes_by_canonical.setdefault(canonical, set()).add(code)

        merged: dict[int, dict[str, Any]] = {}
        for eid, rc, fc, kc, wc in base:
            canonical = canonical_encounter_id(int(eid))
            b = merged.setdefault(canonical, {
                "encounter_id": canonical,
                "reports": 0,
                "fights": 0, "kills": 0, "wipes": 0,
                "kills_with_events": 0, "fight_model_abilities": 0,
            })
            b["fights"] += int(fc)
            b["kills"] += int(kc)
            b["wipes"] += int(wc)
            b["kills_with_events"] += ev_lookup.get(eid, 0)
            b["fight_model_abilities"] += fm_lookup.get(eid, 0)
        # Reports use distinct-code counts to avoid double-counting cross-clones.
        for canonical, codes in report_codes_by_canonical.items():
            if canonical in merged:
                merged[canonical]["reports"] = len(codes)
        return sorted(merged.values(), key=lambda r: -r["fights"])


@app.get("/api/field-stats")
def get_field_stats() -> list[dict[str, Any]]:
    """T-201: per-encounter field-data inventory (reports + kills with events)."""
    _require_db()
    with SessionLocal() as session:
        return field_stats(session, DEFAULT_ENCOUNTERS)


@app.get("/api/encounters/{encounter_id}/consensus")
def get_encounter_consensus(encounter_id: int) -> dict[str, Any]:
    """T-104: cross-pull consensus boss-side timeline for one encounter.

    Reads every ingested kill of the encounter, runs phase segmentation,
    surfaces abilities that recur in ≥70% of pulls at low variance.
    """
    _require_db()
    with SessionLocal() as session:
        return consensus_timeline_for_encounter(session, encounter_id)


@app.post("/api/encounters/{encounter_id}/fight-model/persist")
def post_persist_fight_model(encounter_id: int) -> dict[str, Any]:
    """T-202: recompute consensus + persist to `fight_model` (version=1, replace)."""
    _require_db()
    with SessionLocal() as session:
        return write_consensus_to_fight_model(session, encounter_id)


@app.get("/api/encounters/{encounter_id}/fight-model")
def get_fight_model(encounter_id: int) -> dict[str, Any]:
    """T-202: read persisted boss-side timeline rows for one encounter."""
    _require_db()
    with SessionLocal() as session:
        return read_fight_model(session, encounter_id)


@app.post("/api/encounters/{encounter_id}/fight-model/classify")
def post_classify_fight_model(encounter_id: int) -> dict[str, Any]:
    """T-203: run mechanic classifier across fight_model rows for one encounter."""
    _require_db()
    with SessionLocal() as session:
        return classify_canonical_abilities(session, encounter_id)


@app.post("/api/encounters/{encounter_id}/fight-model/annotate-cactbot")
def post_annotate_cactbot(encounter_id: int) -> dict[str, Any]:
    """Annotate fight_model rows with cactbot labels + expected timings (Stage 1)."""
    _require_db()
    with SessionLocal() as session:
        result = annotate_fight_model_for_encounter(session, encounter_id)
        session.commit()
        return result


@app.get("/api/fights/{fight_id}/timeline-diff")
def get_timeline_diff(fight_id: int) -> dict[str, Any]:
    """Cactbot expected-vs-actual diff for one pull (Stage 2)."""
    _require_db()
    with SessionLocal() as session:
        return timeline_diff_for_fight(session, fight_id)


@app.get("/api/encounters/{encounter_id}/post-clear-targets")
def get_encounter_post_clear(encounter_id: int,
                             ctx: Context = Depends(get_context)
                            ) -> dict[str, Any]:
    """T-308: per-kill polish-target leaderboard (burst + GCD + DPS composite).
    Scoped to the current static's watched kills."""
    _require_db()
    with SessionLocal() as session:
        return post_clear_targets_for_encounter(
            session, encounter_id, ctx.current_static_id,
        )


@app.get("/api/encounters/{encounter_id}/consistency")
def get_encounter_consistency(encounter_id: int,
                              ctx: Context = Depends(get_context)
                             ) -> dict[str, Any]:
    """M-CONS (T-306): per-mechanic clean-execution rate across our pulls
    (scoped to the current static's watchlist)."""
    _require_db()
    with SessionLocal() as session:
        return consistency_for_encounter(
            session, encounter_id, ctx.current_static_id,
        )


@app.get("/api/encounters/{encounter_id}/cartography")
def get_encounter_cartography(
    encounter_id: int, watched_only: bool = False,
    ctx: Context = Depends(get_context),
) -> dict[str, Any]:
    """M-CART (T-206): deaths-by-boss-ability map for one encounter.

    With `watched_only=true`, restricts to the current static's watched
    reports — the consumer Home "what are *we* wiping to" view (v1.8.0).
    Default (false) keeps the legacy field-wide aggregate for the Compare
    tab.
    """
    _require_db()
    with SessionLocal() as session:
        return cartography_for_encounter(
            session, encounter_id,
            static_id=ctx.current_static_id if watched_only else None,
        )


@app.get("/api/encounters/{encounter_id}/dps-check")
def get_encounter_dps_check(encounter_id: int) -> dict[str, Any]:
    """T-204: per-phase raid-DPS distribution across all ingested kills.

    Median = empirical DPS target. Powers T-207's gated-vs-mechanics verdict.
    """
    _require_db()
    with SessionLocal() as session:
        return dps_check_for_encounter(session, encounter_id)


@app.get("/api/encounters/{encounter_id}/dps-comparison")
def get_encounter_dps_comparison(
    encounter_id: int, job: str | None = None,
    ctx: Context = Depends(get_context),
) -> dict[str, Any]:
    """v1.10.0: per-phase DPS distribution split into 'ours' (current
    static's kills) vs 'field' (everyone else's). With `?job=SAM`,
    narrows to per-player DPS for that job. Powers the consumer Home
    "Your DPS vs the field" section.
    """
    _require_db()
    with SessionLocal() as session:
        return dps_comparison_for_encounter(
            session, encounter_id, ctx.current_static_id, job=job,
        )


@app.get("/api/fights/{fight_id}/dps-check")
def get_fight_dps_check(fight_id: int) -> dict[str, Any]:
    """T-204: compare one fight's per-phase raid DPS against the empirical
    distribution for its encounter."""
    _require_db()
    with SessionLocal() as session:
        return dps_compare_fight(session, fight_id)


# ----------------------------------------------------------------------------
# T-301: strat_config CRUD (mit plan + role-based assignments per mechanic)
# ----------------------------------------------------------------------------


class StratConfigIn(BaseModel):
    assignments: Optional[dict[str, Any]] = None
    mit_plan: Optional[dict[str, Any]] = None


@app.get("/api/encounters/{encounter_id}/strat-config")
def list_strat_config(encounter_id: int,
                      ctx: Context = Depends(get_context)) -> dict[str, Any]:
    """All strat_config rows for one encounter in the current static."""
    _require_db()
    with SessionLocal() as session:
        return {
            "encounter_id": encounter_id,
            "roles": list(strat_config_mod.ROLES),
            "rows": strat_config_mod.list_for_encounter(
                session, encounter_id, static_id=ctx.current_static_id,
            ),
        }


@app.get("/api/encounters/{encounter_id}/strat-config/{mechanic_ref}")
def get_strat_config(encounter_id: int, mechanic_ref: str,
                     ctx: Context = Depends(get_context)) -> dict[str, Any]:
    _require_db()
    try:
        strat_config_mod.decode_mechanic_ref(mechanic_ref)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    with SessionLocal() as session:
        row = strat_config_mod.get_one(
            session, encounter_id, mechanic_ref,
            static_id=ctx.current_static_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="strat_config not found")
    return row


@app.put("/api/encounters/{encounter_id}/strat-config/{mechanic_ref}")
def upsert_strat_config(encounter_id: int, mechanic_ref: str,
                         payload: StratConfigIn,
                         ctx: Context = Depends(get_context)) -> dict[str, Any]:
    """Create or replace one strat_config row in the caller's current static."""
    _require_db()
    try:
        with SessionLocal() as session:
            return strat_config_mod.upsert(
                session, encounter_id, mechanic_ref,
                assignments=payload.assignments,
                mit_plan=payload.mit_plan,
                static_id=ctx.current_static_id,
            )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.delete("/api/encounters/{encounter_id}/strat-config/{mechanic_ref}",
            status_code=204)
def delete_strat_config(encounter_id: int, mechanic_ref: str,
                        ctx: Context = Depends(get_context)) -> None:
    _require_db()
    try:
        strat_config_mod.decode_mechanic_ref(mechanic_ref)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    with SessionLocal() as session:
        if not strat_config_mod.delete_one(
            session, encounter_id, mechanic_ref,
            static_id=ctx.current_static_id,
        ):
            raise HTTPException(status_code=404, detail="strat_config not found")


@app.get("/api/encounters/{encounter_id}/prog-curve")
def get_encounter_prog_curve(encounter_id: int,
                              ctx: Context = Depends(get_context)
                             ) -> dict[str, Any]:
    """T-205: our prog trajectory (manual + auto-from-watchlist) +
    field fight_percentage distribution (scoped to the current static)."""
    _require_db()
    with SessionLocal() as session:
        return prog_trajectory_for_encounter(
            session, encounter_id, ctx.current_static_id,
        )


@app.get("/api/fights/{fight_id}/gate-diagnostic")
def get_fight_gate_diagnostic(fight_id: int) -> dict[str, Any]:
    """M-GATE (T-207): per-phase dps_gated/mechanics_gated/both_gated/not_gated verdict."""
    _require_db()
    with SessionLocal() as session:
        return gate_diagnostic_for_fight(session, fight_id)


@app.get("/api/reports/{code}/session-report")
def get_session_report(code: str,
                       ctx: Context = Depends(get_context)) -> dict[str, Any]:
    """M-REPORT (T-307): pasteable Discord summary for one report (= session).
    Mit / fault sections scoped to the caller's current static."""
    _require_db()
    with SessionLocal() as session:
        return generate_session_report(session, code, ctx.current_static_id)


@app.get("/api/fights/{fight_id}/recovery")
def get_fight_recovery(fight_id: int) -> dict[str, Any]:
    """M-RECOV (T-305): per-death recovery + per-player resilience rollup."""
    _require_db()
    with SessionLocal() as session:
        return recovery_for_fight(session, fight_id)


@app.get("/api/fights/{fight_id}/mit-audit")
def get_fight_mit_audit(fight_id: int,
                        ctx: Context = Depends(get_context)) -> dict[str, Any]:
    """M-MIT (T-303): per-raidwide missed-mit list joined to current static's strat_config."""
    _require_db()
    with SessionLocal() as session:
        return mit_audit_for_fight(session, fight_id, ctx.current_static_id)


@app.get("/api/fights/{fight_id}/mit-audit/summary")
def get_fight_mit_audit_summary(fight_id: int,
                                ctx: Context = Depends(get_context)
                               ) -> dict[str, Any]:
    """T-303: high-level totals — raidwides with/without plan, mit hit rate."""
    _require_db()
    with SessionLocal() as session:
        return mit_audit_summary(session, fight_id, ctx.current_static_id)


@app.get("/api/encounters/{encounter_id}/mit-audit-aggregate")
def get_encounter_mit_audit_aggregate(
    encounter_id: int, ctx: Context = Depends(get_context),
) -> dict[str, Any]:
    """T-303 aggregate (v1.9.0): per-encounter mit hit rate, worst-offender
    mits + worst-offender raidwide mechanics, across the current static's
    watched fights. Powers the consumer Home "How mit usage is going" section.
    """
    _require_db()
    with SessionLocal() as session:
        return mit_audit_aggregate_for_encounter(
            session, encounter_id, ctx.current_static_id,
        )


@app.post("/api/fights/{fight_id}/fault-scores/compute")
def post_compute_fault_scores(fight_id: int,
                              ctx: Context = Depends(get_context)
                             ) -> dict[str, Any]:
    """M-FAULT (T-302): classify each death as root/cascade/enrage/unknown,
    write per-player aggregates to fault_scores (scoped to caller's static)."""
    _require_db()
    with SessionLocal() as session:
        return compute_fault_scores_for_fight(
            session, fight_id, ctx.current_static_id,
        )


@app.post("/api/fights/{fight_id}/fault-scores/disambiguate")
def post_disambiguate_fault_scores(fight_id: int,
                                   ctx: Context = Depends(get_context)
                                  ) -> dict[str, Any]:
    """T-304: re-classify cascade deaths to `mit_failure` where the killing
    raidwide had missed planned mits (T-303 audit). Run after /compute."""
    _require_db()
    with SessionLocal() as session:
        return disambiguate_for_fight(
            session, fight_id, ctx.current_static_id,
        )


@app.get("/api/fights/{fight_id}/fault-scores")
def get_fight_fault_scores(fight_id: int,
                           ctx: Context = Depends(get_context)) -> dict[str, Any]:
    """T-302: read persisted fault_scores for one fight (current static)."""
    _require_db()
    with SessionLocal() as session:
        return fault_scores_for_fight(
            session, fight_id, ctx.current_static_id,
        )


@app.get("/api/fights/{fight_id}/wipe-type")
def get_fight_wipe_type(fight_id: int) -> dict[str, Any]:
    """T-302: classify whole fight as kill / enrage_dps / body_check / mechanics / mixed.
    Static-independent — reads fight_model + Event only."""
    _require_db()
    with SessionLocal() as session:
        return classify_wipe_type(session, fight_id)


@app.get("/api/encounters/{encounter_id}/fault-aggregate")
def get_encounter_fault_aggregate(encounter_id: int,
                                  ctx: Context = Depends(get_context)
                                 ) -> dict[str, Any]:
    """T-302: cross-fight player aggregate (roots / cascades / score) across
    all our wipes of this encounter — the current static's "weekly summary"."""
    _require_db()
    with SessionLocal() as session:
        return fault_aggregate_for_encounter(
            session, encounter_id, ctx.current_static_id,
        )


@app.get("/api/encounters/{encounter_id}/fault-breakdown")
def get_encounter_fault_breakdown(
    encounter_id: int, ctx: Context = Depends(get_context),
) -> dict[str, Any]:
    """v1.13.0: joint (player × killing-ability) breakdown for this
    encounter's wipes. The Home tables pivot this client-side: row-expand
    on "What's killing us" shows top players per mechanic; row-expand on
    "Who's contributing" shows top mechanics per player.
    """
    _require_db()
    with SessionLocal() as session:
        return fault_breakdown_for_encounter(
            session, encounter_id, ctx.current_static_id,
        )


@app.post("/api/encounters/{encounter_id}/fault-scores/compute-all")
def compute_encounter_fault_scores(
    encounter_id: int, ctx: Context = Depends(get_context),
) -> dict[str, Any]:
    """Compute fault_scores for every watched wipe of this encounter (v1.8.0).

    Lets the consumer Home populate "who's contributing to wipes" with one
    click instead of opening each pull. Skips kills (no faults to attribute)
    and rows already computed (idempotent via the per-fight replace in
    compute_fault_scores_for_fight)."""
    _require_db()
    with SessionLocal() as session:
        watched_codes = select(WatchedReport.code).where(
            WatchedReport.static_id == ctx.current_static_id
        )
        wipe_ids = session.execute(
            select(Fight.id).where(
                Fight.encounter_id == encounter_id,
                Fight.is_kill.is_(False),
                Fight.report_code.in_(watched_codes),
            )
        ).scalars().all()
        for fid in wipe_ids:
            compute_fault_scores_for_fight(session, fid, ctx.current_static_id)
        session.commit()
        return {"computed": len(wipe_ids), "encounter_id": encounter_id}


@app.get("/api/fights/{fight_id}/parse")
def get_fight_parse(fight_id: int) -> dict[str, Any]:
    """M-PARSE (T-106): per-phase per-player damage + DPS for one fight."""
    _require_db()
    with SessionLocal() as session:
        return parse_per_phase_for_fight(session, fight_id)


@app.get("/api/fights/{fight_id}/phases")
def get_fight_phases(fight_id: int) -> dict[str, Any]:
    """T-103: phase boundary detection for one fight."""
    _require_db()
    with SessionLocal() as session:
        result = detect_phase_boundaries(session, fight_id)
        # Convert ts to relative-from-fight-start so the UI doesn't need to
        # know absolute epoch offsets.
        if result["phases"]:
            base = result["phases"][0]["start_ts"]
            for p in result["phases"]:
                p["start_offset_ms"] = p["start_ts"] - base
                p["end_offset_ms"] = p["end_ts"] - base
        return result


# ----------------------------------------------------------------------------
# T-011: static roster + character aliases CRUD
# ----------------------------------------------------------------------------


class AliasIn(BaseModel):
    character_name: str
    server: Optional[str] = None


class AliasOut(BaseModel):
    id: int
    character_name: str
    server: Optional[str] = None


MEMBER_KINDS = ("core", "substitute")


class MemberIn(BaseModel):
    name: str
    kind: str = "core"
    role_pref: Optional[str] = None
    notes: Optional[str] = None
    aliases: list[AliasIn] = []


class MemberPatch(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    role_pref: Optional[str] = None
    notes: Optional[str] = None


class MemberOut(BaseModel):
    id: int
    name: str
    kind: str = "core"
    role_pref: Optional[str] = None
    notes: Optional[str] = None
    aliases: list[AliasOut]


def _member_to_out(session, m: Member) -> MemberOut:
    aliases = (
        session.execute(
            select(CharacterAlias).where(CharacterAlias.member_id == m.id)
            .order_by(CharacterAlias.id)
        )
        .scalars()
        .all()
    )
    return MemberOut(
        id=m.id,
        name=m.name,
        kind=m.kind or "core",
        role_pref=m.role_pref,
        notes=m.notes,
        aliases=[AliasOut(id=a.id, character_name=a.character_name, server=a.server)
                 for a in aliases],
    )


def _require_db():
    if SessionLocal is None:
        raise HTTPException(status_code=503, detail="database not configured")


@app.get("/api/members", response_model=list[MemberOut])
def list_members(ctx: Context = Depends(get_context)) -> list[MemberOut]:
    _require_db()
    with SessionLocal() as session:
        members = session.execute(
            select(Member)
            .where(Member.static_id == ctx.current_static_id)
            .order_by(Member.id)
        ).scalars().all()
        return [_member_to_out(session, m) for m in members]


@app.post("/api/members", response_model=MemberOut, status_code=201)
def create_member(payload: MemberIn,
                  ctx: Context = Depends(get_context)) -> MemberOut:
    _require_db()
    if payload.kind not in MEMBER_KINDS:
        raise HTTPException(status_code=422,
                            detail=f"kind must be one of {MEMBER_KINDS}")
    with SessionLocal() as session:
        member = Member(
            static_id=ctx.current_static_id,
            name=payload.name,
            kind=payload.kind,
            role_pref=payload.role_pref,
            notes=payload.notes,
            created_at=datetime.now(timezone.utc),
        )
        session.add(member)
        try:
            session.flush()
        except IntegrityError as e:
            session.rollback()
            raise HTTPException(status_code=409,
                                detail=f"member name not unique in this static: {payload.name!r}") from e
        for alias in payload.aliases:
            session.add(CharacterAlias(
                member_id=member.id,
                character_name=alias.character_name,
                server=alias.server,
                created_at=datetime.now(timezone.utc),
            ))
        try:
            session.commit()
        except IntegrityError as e:
            session.rollback()
            raise HTTPException(status_code=409,
                                detail="character alias write failed") from e
        session.refresh(member)
        return _member_to_out(session, member)


@app.patch("/api/members/{member_id}", response_model=MemberOut)
def update_member(member_id: int, payload: MemberPatch,
                  ctx: Context = Depends(get_context)) -> MemberOut:
    _require_db()
    with SessionLocal() as session:
        member = session.get(Member, member_id)
        if member is None or member.static_id != ctx.current_static_id:
            raise HTTPException(status_code=404, detail="member not found")
        if payload.name is not None:
            member.name = payload.name
        if payload.kind is not None:
            if payload.kind not in MEMBER_KINDS:
                raise HTTPException(status_code=422,
                                    detail=f"kind must be one of {MEMBER_KINDS}")
            member.kind = payload.kind
        if payload.role_pref is not None:
            member.role_pref = payload.role_pref
        if payload.notes is not None:
            member.notes = payload.notes
        try:
            session.commit()
        except IntegrityError as e:
            session.rollback()
            raise HTTPException(status_code=409,
                                detail="member name not unique in this static") from e
        session.refresh(member)
        return _member_to_out(session, member)


@app.delete("/api/members/{member_id}", status_code=204)
def delete_member(member_id: int,
                  ctx: Context = Depends(get_context)) -> None:
    _require_db()
    with SessionLocal() as session:
        member = session.get(Member, member_id)
        if member is None or member.static_id != ctx.current_static_id:
            raise HTTPException(status_code=404, detail="member not found")
        session.delete(member)
        session.commit()


@app.post("/api/members/{member_id}/aliases",
          response_model=AliasOut, status_code=201)
def add_alias(member_id: int, payload: AliasIn,
              ctx: Context = Depends(get_context)) -> AliasOut:
    _require_db()
    with SessionLocal() as session:
        member = session.get(Member, member_id)
        if member is None or member.static_id != ctx.current_static_id:
            raise HTTPException(status_code=404, detail="member not found")
        alias = CharacterAlias(
            member_id=member_id,
            character_name=payload.character_name,
            server=payload.server,
            created_at=datetime.now(timezone.utc),
        )
        session.add(alias)
        try:
            session.commit()
        except IntegrityError as e:
            session.rollback()
            raise HTTPException(status_code=409,
                                detail="character alias already claimed") from e
        session.refresh(alias)
        return AliasOut(id=alias.id, character_name=alias.character_name, server=alias.server)


@app.delete("/api/aliases/{alias_id}", status_code=204)
def delete_alias(alias_id: int,
                 ctx: Context = Depends(get_context)) -> None:
    _require_db()
    with SessionLocal() as session:
        alias = session.get(CharacterAlias, alias_id)
        if alias is None:
            raise HTTPException(status_code=404, detail="alias not found")
        # Verify the alias's member belongs to the caller's current static
        member = session.get(Member, alias.member_id)
        if member is None or member.static_id != ctx.current_static_id:
            raise HTTPException(status_code=404, detail="alias not found")
        session.delete(alias)
        session.commit()


# ----------------------------------------------------------------------------
# v1.15.0 — Roster discovery + bulk classification
# ----------------------------------------------------------------------------

CLASSIFY_ACTIONS = ("core", "substitute", "sub", "ignore", "clear")


class ClassifyIn(BaseModel):
    """Bulk classification request.

    `action`:
    - "core"       — create or find a Member with this name (kind=core) and
                     attach this character as an alias.
    - "substitute" — same as core but kind=substitute.
    - "sub"        — attach this character as an alias of member_id (required).
    - "ignore"     — add to ignored_characters (removes any existing alias).
    - "clear"      — remove any existing alias and any existing ignore row;
                     character returns to "unclassified".

    For "core" / "substitute" the request may pass `member_id` to attach to
    an EXISTING member instead of creating one (used when promoting a sub
    account to its own core entry, or attaching to an existing same-named
    member). When omitted, a new member is created with `member_name` or
    falling back to `character_name`.
    """

    character_name: str
    server: Optional[str] = None
    action: str
    member_id: Optional[int] = None
    member_name: Optional[str] = None  # used when action creates a member


def _delete_existing_alias(session, static_id: int, name: str,
                           server: Optional[str]) -> None:
    """Delete the matching alias if any. Server NULL match if input is None."""
    q = (
        select(CharacterAlias)
        .join(Member, Member.id == CharacterAlias.member_id)
        .where(
            Member.static_id == static_id,
            CharacterAlias.character_name == name,
        )
    )
    if server is None:
        q = q.where(CharacterAlias.server.is_(None))
    else:
        q = q.where(CharacterAlias.server == server)
    for alias in session.execute(q).scalars().all():
        session.delete(alias)


def _delete_existing_ignore(session, static_id: int, name: str,
                            server: Optional[str]) -> None:
    q = select(IgnoredCharacter).where(
        IgnoredCharacter.static_id == static_id,
        IgnoredCharacter.character_name == name,
    )
    if server is None:
        q = q.where(IgnoredCharacter.server.is_(None))
    else:
        q = q.where(IgnoredCharacter.server == server)
    for row in session.execute(q).scalars().all():
        session.delete(row)


@app.get("/api/roster/characters")
def list_discovered_characters(
    ctx: Context = Depends(get_context),
) -> dict[str, Any]:
    """Every distinct (name, server) seen across this static's watched reports,
    with its current classification."""
    _require_db()
    with SessionLocal() as session:
        rows = discovered_characters_for_static(session, ctx.current_static_id)
        return {"static_id": ctx.current_static_id, "characters": rows}


@app.post("/api/roster/classify")
def classify_character(payload: ClassifyIn,
                       ctx: Context = Depends(get_context)) -> dict[str, Any]:
    _require_db()
    action = payload.action
    if action not in CLASSIFY_ACTIONS:
        raise HTTPException(status_code=422,
                            detail=f"action must be one of {CLASSIFY_ACTIONS}")
    if not payload.character_name or not payload.character_name.strip():
        raise HTTPException(status_code=422, detail="character_name required")
    name = payload.character_name.strip()
    server = payload.server.strip() if payload.server else None
    static_id = ctx.current_static_id

    with SessionLocal() as session:
        if action == "clear":
            _delete_existing_alias(session, static_id, name, server)
            _delete_existing_ignore(session, static_id, name, server)
            session.commit()
            return {"action": action, "ok": True}

        if action == "ignore":
            _delete_existing_alias(session, static_id, name, server)
            # Idempotent — upsert via the unique index.
            existing = session.execute(
                select(IgnoredCharacter).where(
                    IgnoredCharacter.static_id == static_id,
                    IgnoredCharacter.character_name == name,
                    IgnoredCharacter.server.is_(None) if server is None
                    else IgnoredCharacter.server == server,
                )
            ).scalars().first()
            if existing is None:
                session.add(IgnoredCharacter(
                    static_id=static_id,
                    character_name=name,
                    server=server,
                    created_at=datetime.now(timezone.utc),
                ))
            session.commit()
            return {"action": action, "ok": True}

        # Below: core / substitute / sub — all create or attach an alias.
        # Remove any prior ignore + prior alias so the new state is clean.
        _delete_existing_ignore(session, static_id, name, server)
        _delete_existing_alias(session, static_id, name, server)

        if action == "sub":
            if payload.member_id is None:
                raise HTTPException(status_code=422,
                                    detail="member_id required for action=sub")
            member = session.get(Member, payload.member_id)
            if member is None or member.static_id != static_id:
                raise HTTPException(status_code=404, detail="member not found")
        else:
            # core / substitute: get or create a member
            member_kind = action  # 'core' or 'substitute'
            if payload.member_id is not None:
                member = session.get(Member, payload.member_id)
                if member is None or member.static_id != static_id:
                    raise HTTPException(status_code=404,
                                        detail="member not found")
                if payload.member_id is not None and action in ("core", "substitute"):
                    # Keep the existing kind unless the request is changing it
                    # explicitly via the dedicated member endpoint. We don't
                    # silently demote a core member when attaching an alias.
                    pass
            else:
                target_name = (payload.member_name or "").strip() or name
                # Avoid creating a duplicate member if one exists with this name
                member = session.execute(
                    select(Member).where(
                        Member.static_id == static_id,
                        Member.name == target_name,
                    )
                ).scalar_one_or_none()
                if member is None:
                    member = Member(
                        static_id=static_id,
                        name=target_name,
                        kind=member_kind,
                        created_at=datetime.now(timezone.utc),
                    )
                    session.add(member)
                    try:
                        session.flush()
                    except IntegrityError as e:
                        session.rollback()
                        raise HTTPException(
                            status_code=409,
                            detail="member name collision",
                        ) from e

        session.add(CharacterAlias(
            member_id=member.id,
            character_name=name,
            server=server,
            created_at=datetime.now(timezone.utc),
        ))
        try:
            session.commit()
        except IntegrityError as e:
            session.rollback()
            raise HTTPException(status_code=409,
                                detail="alias write conflicted") from e
        return {"action": action, "ok": True, "member_id": member.id}


# ----------------------------------------------------------------------------
# T-010: manual prog-point entry
# ----------------------------------------------------------------------------


class ProgPointIn(BaseModel):
    ts: datetime
    phase: Optional[int] = None
    fight_percentage: Optional[float] = None
    pull_count: Optional[int] = None


class ProgPointOut(BaseModel):
    id: int
    ts: datetime
    phase: Optional[int] = None
    fight_percentage: Optional[float] = None
    pull_count: Optional[int] = None
    source: str


def _prog_to_out(p: ProgPoint) -> ProgPointOut:
    return ProgPointOut(
        id=p.id,
        ts=p.ts,
        phase=p.phase,
        fight_percentage=float(p.fight_percentage) if p.fight_percentage is not None else None,
        pull_count=p.pull_count,
        source=p.source or "manual",
    )


@app.get("/api/prog-points", response_model=list[ProgPointOut])
def list_prog_points(ctx: Context = Depends(get_context)) -> list[ProgPointOut]:
    """All prog points (auto + manual) for the current static, oldest first."""
    _require_db()
    with SessionLocal() as session:
        rows = (
            session.execute(
                select(ProgPoint)
                .where(ProgPoint.static_id == ctx.current_static_id)
                .order_by(ProgPoint.ts, ProgPoint.id)
            )
            .scalars()
            .all()
        )
        return [_prog_to_out(p) for p in rows]


@app.post("/api/prog-points", response_model=ProgPointOut, status_code=201)
def create_prog_point(payload: ProgPointIn,
                      ctx: Context = Depends(get_context)) -> ProgPointOut:
    """Manual entry only — auto points are written by the live-poll job (T-101+)."""
    _require_db()
    if payload.phase is None and payload.fight_percentage is None:
        raise HTTPException(
            status_code=422,
            detail="prog point needs at least one of: phase, fight_percentage",
        )
    with SessionLocal() as session:
        p = ProgPoint(
            static_id=ctx.current_static_id,
            ts=payload.ts,
            phase=payload.phase,
            fight_percentage=payload.fight_percentage,
            pull_count=payload.pull_count,
            source="manual",
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        return _prog_to_out(p)


@app.delete("/api/prog-points/{point_id}", status_code=204)
def delete_prog_point(point_id: int,
                      ctx: Context = Depends(get_context)) -> None:
    _require_db()
    with SessionLocal() as session:
        p = session.get(ProgPoint, point_id)
        if p is None or p.static_id != ctx.current_static_id:
            raise HTTPException(status_code=404, detail="prog point not found")
        session.delete(p)
        session.commit()


# ----------------------------------------------------------------------------
# T-108: ability labels review queue
# ----------------------------------------------------------------------------


class AbilityLabelOut(BaseModel):
    ability_game_id: int
    label: Optional[str] = None
    confidence: Optional[float] = None
    source: Optional[str] = None
    notes: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    kind: Optional[str] = None
    icon: Optional[str] = None
    duration_ms: Optional[int] = None
    mit_pct: Optional[int] = None


class AbilityLabelPatch(BaseModel):
    label: str
    notes: Optional[str] = None


class AbilityLabelBulkPatch(BaseModel):
    ability_ids: list[int]
    label: str
    notes: Optional[str] = None


def _ability_row_to_out(a: Ability, lbl: Optional[AbilityLabel]) -> AbilityLabelOut:
    return AbilityLabelOut(
        ability_game_id=a.ability_game_id,
        label=lbl.label if lbl else None,
        confidence=float(lbl.confidence) if lbl and lbl.confidence is not None else None,
        source=lbl.source if lbl else None,
        notes=lbl.notes if lbl else None,
        name=a.name,
        description=a.description,
        kind=a.kind,
        icon=a.icon,
        duration_ms=a.duration_ms,
        mit_pct=a.mit_pct,
    )


@app.get("/api/abilities/review-queue", response_model=list[AbilityLabelOut])
def review_queue(
    limit: int = 100,
    kind: Optional[str] = None,
    current_label: Optional[str] = None,
) -> list[AbilityLabelOut]:
    """Ability labels needing review: low-confidence auto rows + missing labels.

    Optional filters:
        kind: limit to abilities of this kind (action / status / unknown).
        current_label: limit to abilities with this current auto-label
            (use empty string to filter for rows with NO label).
    """
    _require_db()
    with SessionLocal() as session:
        q = (
            select(Ability, AbilityLabel)
            .join(AbilityLabel,
                  AbilityLabel.ability_game_id == Ability.ability_game_id,
                  isouter=True)
            .where(
                (AbilityLabel.ability_game_id.is_(None))
                | ((AbilityLabel.source == "auto")
                   & (AbilityLabel.confidence < AUTO_HIGH_THRESHOLD))
            )
        )
        if kind:
            q = q.where(Ability.kind == kind)
        if current_label is not None:
            if current_label == "":
                q = q.where(AbilityLabel.label.is_(None))
            else:
                q = q.where(AbilityLabel.label == current_label)
        q = q.order_by(AbilityLabel.confidence.asc().nullsfirst(),
                       Ability.ability_game_id).limit(limit)
        rows = session.execute(q).all()
        return [_ability_row_to_out(a, lbl) for (a, lbl) in rows]


@app.get("/api/abilities/labels", response_model=list[AbilityLabelOut])
def list_labels(label: Optional[str] = None, limit: int = 200) -> list[AbilityLabelOut]:
    """All labelled abilities. Optionally filter by label."""
    _require_db()
    with SessionLocal() as session:
        q = (
            select(Ability, AbilityLabel)
            .join(AbilityLabel,
                  AbilityLabel.ability_game_id == Ability.ability_game_id,
                  isouter=True)
        )
        if label is not None:
            q = q.where(AbilityLabel.label == label)
        q = q.order_by(Ability.ability_game_id).limit(limit)
        return [_ability_row_to_out(a, lbl) for (a, lbl) in session.execute(q).all()]


# ----------------------------------------------------------------------------
# T-101: watched reports CRUD (manual watchlist for the live poller)
# ----------------------------------------------------------------------------

# Accepts either a bare report code or a full FFLogs URL like
# https://www.fflogs.com/reports/<code> with optional #fight=...
_REPORT_CODE_RE = re.compile(r"reports/([A-Za-z0-9]+)")


def _extract_report_code(input_str: str) -> str:
    s = input_str.strip()
    m = _REPORT_CODE_RE.search(s)
    if m:
        return m.group(1)
    return s


class WatchedReportIn(BaseModel):
    code_or_url: str
    label: Optional[str] = None


class WatchedReportPatch(BaseModel):
    active: Optional[bool] = None
    label: Optional[str] = None


class WatchedReportOut(BaseModel):
    code: str
    label: Optional[str] = None
    active: bool
    added_at: Optional[datetime] = None
    last_polled_at: Optional[datetime] = None
    last_error: Optional[str] = None


@app.get("/api/watched-reports", response_model=list[WatchedReportOut])
def list_watched_reports(
    ctx: Context = Depends(get_context),
) -> list[WatchedReportOut]:
    _require_db()
    with SessionLocal() as s:
        rows = s.execute(
            select(WatchedReport)
            .where(WatchedReport.static_id == ctx.current_static_id)
            .order_by(WatchedReport.added_at.desc().nullslast())
        ).scalars().all()
        return [
            WatchedReportOut(
                code=r.code, label=r.label, active=r.active,
                added_at=r.added_at, last_polled_at=r.last_polled_at,
                last_error=r.last_error,
            )
            for r in rows
        ]


@app.post("/api/watched-reports", response_model=WatchedReportOut, status_code=201)
def create_watched_report(payload: WatchedReportIn,
                          ctx: Context = Depends(get_context)
                         ) -> WatchedReportOut:
    _require_db()
    code = _extract_report_code(payload.code_or_url)
    if not code:
        raise HTTPException(status_code=422, detail="empty report code")
    with SessionLocal() as s:
        existing = s.execute(
            select(WatchedReport).where(
                WatchedReport.static_id == ctx.current_static_id,
                WatchedReport.code == code,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"already watching {code}")
        w = WatchedReport(
            static_id=ctx.current_static_id,
            code=code, label=payload.label, active=True,
            added_at=datetime.now(timezone.utc),
        )
        s.add(w)
        s.commit()
        s.refresh(w)
        return WatchedReportOut(
            code=w.code, label=w.label, active=w.active,
            added_at=w.added_at, last_polled_at=w.last_polled_at,
            last_error=w.last_error,
        )


def _find_watched(session, code: str, static_id: int) -> Optional[WatchedReport]:
    return session.execute(
        select(WatchedReport).where(
            WatchedReport.static_id == static_id,
            WatchedReport.code == code,
        )
    ).scalar_one_or_none()


@app.patch("/api/watched-reports/{code}", response_model=WatchedReportOut)
def update_watched_report(code: str, payload: WatchedReportPatch,
                          ctx: Context = Depends(get_context)
                         ) -> WatchedReportOut:
    _require_db()
    with SessionLocal() as s:
        w = _find_watched(s, code, ctx.current_static_id)
        if w is None:
            raise HTTPException(status_code=404, detail="not watching that report")
        if payload.active is not None:
            w.active = payload.active
        if payload.label is not None:
            w.label = payload.label
        s.commit()
        s.refresh(w)
        return WatchedReportOut(
            code=w.code, label=w.label, active=w.active,
            added_at=w.added_at, last_polled_at=w.last_polled_at,
            last_error=w.last_error,
        )


@app.delete("/api/watched-reports/{code}", status_code=204)
def delete_watched_report(code: str,
                          ctx: Context = Depends(get_context)) -> None:
    _require_db()
    with SessionLocal() as s:
        w = _find_watched(s, code, ctx.current_static_id)
        if w is None:
            raise HTTPException(status_code=404, detail="not watching that report")
        s.delete(w)
        s.commit()


@app.post("/api/watched-reports/{code}/poll")
def post_poll_watched_report(code: str,
                             ctx: Context = Depends(get_context)
                            ) -> dict[str, Any]:
    """Trigger an immediate poll for one watched report. Wraps ingest_report +
    ingest_events_for_report with proper commits and per-call error capture so
    users never see the half-committed-session footgun. Synchronous: returns
    only after the ingest completes (or fails)."""
    _require_db()
    # Verify the caller's static actually watches this code before polling.
    with SessionLocal() as s:
        w = _find_watched(s, code, ctx.current_static_id)
    if w is None:
        raise HTTPException(status_code=404, detail="not watching that report")
    with SessionLocal() as s, FFLogsClient() as c:
        result = poll_one_by_code(s, c, code, static_id=ctx.current_static_id)
    if result is None:
        raise HTTPException(status_code=404, detail="not watching that report")
    return result


@app.patch("/api/abilities/{ability_id}/label", response_model=AbilityLabelOut)
def set_label(ability_id: int, payload: AbilityLabelPatch) -> AbilityLabelOut:
    """User confirms or overrides a label. Sets source='user' so reruns of the
    classifier don't wipe the human decision."""
    _require_db()
    if payload.label not in LABELS:
        raise HTTPException(
            status_code=422,
            detail=f"label must be one of: {sorted(LABELS)}",
        )
    with SessionLocal() as session:
        ability = session.get(Ability, ability_id)
        if ability is None:
            raise HTTPException(status_code=404, detail="ability not found")
        lbl = session.get(AbilityLabel, ability_id)
        if lbl is None:
            lbl = AbilityLabel(ability_game_id=ability_id)
            session.add(lbl)
        lbl.label = payload.label
        lbl.confidence = 1.0
        lbl.source = "user"
        lbl.notes = payload.notes
        lbl.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(lbl)
        return _ability_row_to_out(ability, lbl)


@app.patch("/api/abilities/labels/bulk")
def bulk_set_labels(payload: AbilityLabelBulkPatch) -> dict:
    """Apply the same label to many abilities in one round-trip.

    Returns `{updated: N, skipped_unknown_ids: [...]}`. All updates run in a
    single transaction; if the label is invalid the call 422s without writing.
    """
    _require_db()
    if payload.label not in LABELS:
        raise HTTPException(
            status_code=422,
            detail=f"label must be one of: {sorted(LABELS)}",
        )
    if not payload.ability_ids:
        return {"updated": 0, "skipped_unknown_ids": []}
    now = datetime.now(timezone.utc)
    updated = 0
    skipped: list[int] = []
    with SessionLocal() as session:
        for ability_id in payload.ability_ids:
            ability = session.get(Ability, ability_id)
            if ability is None:
                skipped.append(ability_id)
                continue
            lbl = session.get(AbilityLabel, ability_id)
            if lbl is None:
                lbl = AbilityLabel(ability_game_id=ability_id)
                session.add(lbl)
            lbl.label = payload.label
            lbl.confidence = 1.0
            lbl.source = "user"
            lbl.notes = payload.notes
            lbl.updated_at = now
            updated += 1
        session.commit()
    return {"updated": updated, "skipped_unknown_ids": skipped}


# Single-origin prod mode: when WEB_STATIC_DIR is set, mount the React build at
# `/` so the same FastAPI process serves both API + UI behind one tunnel. Must
# be mounted last so it doesn't shadow the /api/* and /healthz routes above.
if settings.web_static_dir:
    static_dir = Path(settings.web_static_dir)
    if not static_dir.is_dir():
        raise RuntimeError(
            f"WEB_STATIC_DIR points to non-existent directory: {static_dir}. "
            "Run `npm run build` in web/ first, or unset WEB_STATIC_DIR for dev."
        )
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="web")
