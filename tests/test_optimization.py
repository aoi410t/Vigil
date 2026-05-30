"""T-308 post-clear optimization tests."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from analysis.optimization import (
    _composite,
    _score_burst,
    _score_dps,
    _score_gcd,
    post_clear_targets_for_encounter,
)
from db.models import Fight
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)


# ---- Pure-function score curves ----

def test_score_burst_saturates_at_85_pct():
    assert _score_burst(0.85) == 1.0
    assert _score_burst(1.0) == 1.0
    assert _score_burst(0.0) == 0.0
    assert 0 < _score_burst(0.5) < 1.0


def test_score_gcd_zero_drops_is_perfect():
    assert _score_gcd(0) == 1.0
    assert _score_gcd(3) == 0.5
    assert _score_gcd(6) == 0.0
    assert _score_gcd(10) == 0.0


def test_score_dps_handles_none():
    assert _score_dps(None) is None
    assert _score_dps(1.0) == 1.0
    assert _score_dps(0.9) == pytest.approx(0.5, abs=1e-9)
    assert _score_dps(0.8) == 0.0


def test_composite_geomean_with_nones():
    """A None metric is skipped; geometric mean of the rest."""
    assert _composite(1.0, 1.0, None) == 1.0
    g = _composite(0.5, 0.5, None)
    assert g == pytest.approx(0.5, abs=1e-3)
    assert _composite(None, None, None) is None


# ---- End-to-end ----

def test_no_kills_returns_note():
    with SessionLocal() as s:
        r = post_clear_targets_for_encounter(s, 9_999_998, 1)
    assert r["kills"] == 0
    assert "note" in r


def test_live_fru_optimization_structure_when_no_watchlist():
    """The 11 FRU kills in dev DB aren't in WatchedReport — so output should
    be the empty-watchlist note."""
    with SessionLocal() as s:
        r = post_clear_targets_for_encounter(s, 1079, 1)
    # Either no watchlist kills (note), or it runs (kills > 0). Both are OK.
    if r["kills"] == 0:
        assert "note" in r
    else:
        assert "fights" in r
        for f in r["fights"]:
            assert "players" in f


def test_at_least_one_kill_fight_exists():
    """Sanity check: dev DB has SOME kills somewhere for verification."""
    with SessionLocal() as s:
        any_kill = s.execute(
            select(Fight.id).where(Fight.is_kill.is_(True)).limit(1)
        ).scalar()
    assert any_kill is not None
