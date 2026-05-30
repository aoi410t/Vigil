"""Tests for ingest/wiki.py — FFXIV wiki HTML scraper for ability durations."""
from __future__ import annotations

import httpx
import pytest

from ingest.wiki import (
    extract_duration_ms,
    extract_mit_pct,
    fetch_duration_for_ability,
    fetch_metadata_for_ability,
    scrape_durations_for_abilities,
    scrape_metadata_for_abilities,
)


# Real shape of the consolegameswiki Divination page snippet (verified live
# against https://ffxiv.consolegameswiki.com/wiki/Divination on 2026-05-25).
DIVINATION_HTML = """
<html><body>
<table class="infobox">
<tr><td><span>Duration:</span>&nbsp;20s</td></tr>
<tr><td>Additional Effect:&nbsp;Grants Divining</td></tr>
<tr><td>Duration:&nbsp;30s</td></tr>
</table>
</body></html>
"""

# Brotherhood snippet — has Duration across a </span> tag split.
BROTHERHOOD_HTML = """
<html><body>
<span class="colorized-description">Duration:</span>&nbsp;20s<br>
<span class="colorized-description">Additional Effect:</span>&nbsp;Grants ...
</body></html>
"""

NO_DURATION_HTML = """
<html><body>
<p>Just some flavor text and Cooldown: 60s.</p>
</body></html>
"""


def test_extract_duration_primary_wins():
    # Divination shows two Duration fields; we want the PRIMARY (first = 20s).
    assert extract_duration_ms(DIVINATION_HTML) == 20_000


def test_extract_duration_across_span_split():
    # Brotherhood's `Duration:</span>&nbsp;20s` is the common pattern; the
    # tag-strip pass removes the `</span>` before the regex hits it.
    assert extract_duration_ms(BROTHERHOOD_HTML) == 20_000


def test_extract_duration_returns_none_when_missing():
    assert extract_duration_ms(NO_DURATION_HTML) is None
    assert extract_duration_ms("") is None
    assert extract_duration_ms("garbage") is None


def test_extract_duration_handles_html_entities():
    html = "Duration:&#160;15s<br>"
    assert extract_duration_ms(html) == 15_000


def test_fetch_duration_via_mock_transport():
    """End-to-end fetch via httpx.MockTransport — no live network call."""
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/wiki/Divination")
        return httpx.Response(200, text=DIVINATION_HTML)
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        assert fetch_duration_for_ability(client, "Divination") == 20_000


def test_fetch_duration_404_returns_none():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not found")
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        assert fetch_duration_for_ability(client, "Made_Up_Ability") is None


def test_fetch_duration_handles_network_error():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure")
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        # Should swallow the error and return None, not raise.
        assert fetch_duration_for_ability(client, "Whatever") is None


def test_fetch_duration_empty_name_returns_none():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=""))
    with httpx.Client(transport=transport) as client:
        assert fetch_duration_for_ability(client, "") is None


def test_fetch_duration_url_encodes_spaces_and_punctuation():
    captured = []
    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(str(req.url))
        return httpx.Response(200, text=BROTHERHOOD_HTML)
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        fetch_duration_for_ability(client, "Searing Light")
    assert captured == ["https://ffxiv.consolegameswiki.com/wiki/Searing_Light"]


def test_scrape_durations_batch_no_pacing():
    pages = {"Divination": DIVINATION_HTML, "Brotherhood": BROTHERHOOD_HTML}
    def handler(req: httpx.Request) -> httpx.Response:
        name = req.url.path.split("/wiki/")[-1]
        html = pages.get(name, "")
        return httpx.Response(200 if html else 404, text=html)
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        out = scrape_durations_for_abilities(
            client,
            [(1, "Divination"), (2, "Brotherhood"), (3, "Made_Up")],
            pacing_s=0.0,
        )
    assert out == {1: 20_000, 2: 20_000, 3: None}


# ---------------------------------------------------------------------------
# mit_pct extraction (v1.5.9)
# ---------------------------------------------------------------------------

# Real shape from https://ffxiv.consolegameswiki.com/wiki/Rampart
RAMPART_HTML = """
<html><body>
<p>Reduces damage taken by 20%. Duration: 20s</p>
</body></html>
"""

# Feint-style "Lowers" verb with multi-value mit
FEINT_HTML = """
<html><body>
<p>Lowers target's physical damage dealt by 10% and magic damage dealt by 5%.</p>
</body></html>
"""


def test_extract_mit_pct_simple_reduce():
    assert extract_mit_pct(RAMPART_HTML) == 20


def test_extract_mit_pct_lowers_first_value_wins():
    # Feint's first listed reduction is the headline (physical 10%); second
    # listed (magic 5%) is ignored. Documented as a known limitation.
    assert extract_mit_pct(FEINT_HTML) == 10


def test_extract_mit_pct_returns_none_when_no_verb():
    assert extract_mit_pct("<p>A spell that does damage.</p>") is None
    assert extract_mit_pct("") is None
    assert extract_mit_pct(None) is None  # type: ignore[arg-type]


def test_extract_mit_pct_sanity_bounds():
    # 200% potency etc. would be a stray match; the regex requires a verb
    # phrase but if it ever picked up a value > 100 we'd reject it.
    bogus = "Reduces nothing by 999% as a joke"
    # Note: the regex captures 2 digits + an optional 3rd, but our re uses
    # {1,2}, so 999% wouldn't match the digits group at all. Use 0% which
    # captures but fails the bound check.
    assert extract_mit_pct("Reduces damage taken by 0% in this fake page") is None


def test_fetch_metadata_returns_both_fields():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=RAMPART_HTML)
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        meta = fetch_metadata_for_ability(client, "Rampart")
    # Rampart HTML above has both Duration: 20s AND Reduces damage taken by 20%.
    assert meta == {"duration_ms": 20_000, "mit_pct": 20}


def test_fetch_metadata_handles_404():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="")
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        meta = fetch_metadata_for_ability(client, "Made_Up")
    assert meta == {"duration_ms": None, "mit_pct": None}


def test_scrape_metadata_batch():
    pages = {"Rampart": RAMPART_HTML, "Feint": FEINT_HTML}
    def handler(req: httpx.Request) -> httpx.Response:
        name = req.url.path.split("/wiki/")[-1]
        html = pages.get(name, "")
        return httpx.Response(200 if html else 404, text=html)
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        out = scrape_metadata_for_abilities(
            client,
            [(1, "Rampart"), (2, "Feint"), (3, "Made_Up")],
            pacing_s=0.0,
        )
    assert out == {
        1: {"duration_ms": 20_000, "mit_pct": 20},
        2: {"duration_ms": None,   "mit_pct": 10},  # Feint HTML has no Duration field
        3: {"duration_ms": None,   "mit_pct": None},
    }
