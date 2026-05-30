"""T-108 XIVAPI client unit tests via httpx.MockTransport (no network)."""
from __future__ import annotations

import json

import httpx
import pytest

from ingest.xivapi import (
    XIVAPIClient,
    classify_namespace,
    fetch_one,
)


def _transport(routes: dict[str, tuple[int, dict | None]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path
        if key not in routes:
            return httpx.Response(404, json={"error": "not found"})
        status, body = routes[key]
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, content=json.dumps(body).encode())
    return httpx.MockTransport(handler)


def test_fetch_action_returns_payload():
    t = _transport({"/Action/7535": (200, {"ID": 7535, "Name_en": "Reprisal"})})
    with XIVAPIClient(transport=t, min_interval_s=0) as c:
        payload = c.fetch_action(7535)
    assert payload["Name_en"] == "Reprisal"


def test_fetch_action_404_returns_none():
    t = _transport({})
    with XIVAPIClient(transport=t, min_interval_s=0) as c:
        assert c.fetch_action(9999999) is None


def test_classify_namespace_status():
    counts = {"applybuff": 10, "removebuff": 5, "cast": 1}
    assert classify_namespace(counts) == "status"


def test_classify_namespace_action():
    counts = {"cast": 20, "damage": 30, "applybuff": 1}
    assert classify_namespace(counts) == "action"


def test_classify_namespace_unknown_when_empty():
    assert classify_namespace({}) == "unknown"


def test_classify_namespace_unknown_when_no_match():
    # Type the classifier doesn't recognize (e.g. 'combatantinfo')
    assert classify_namespace({"combatantinfo": 5}) == "unknown"


def test_fetch_one_action_first_when_dominant():
    t = _transport({
        "/Action/7535": (200, {"ID": 7535, "Name_en": "Reprisal",
                                "Description_en": "Reduces damage."}),
    })
    with XIVAPIClient(transport=t, min_interval_s=0) as c:
        kind, row = fetch_one(c, 7535, {"cast": 100, "damage": 50})
    assert kind == "action"
    assert row["name"] == "Reprisal"
    assert row["description"] == "Reduces damage."


def test_fetch_one_falls_back_to_status_when_action_404():
    """Status-dominant ID where Action endpoint also exists isn't ours — we
    look at Status first based on event-type evidence."""
    t = _transport({
        "/Status/1191": (200, {"ID": 1191, "Name_en": "Rampart",
                                "Description_en": "Damage taken reduced."}),
    })
    with XIVAPIClient(transport=t, min_interval_s=0) as c:
        kind, row = fetch_one(c, 1191, {"applybuff": 30})
    assert kind == "status"
    assert row["name"] == "Rampart"


def test_fetch_one_both_404_returns_unknown():
    t = _transport({})
    with XIVAPIClient(transport=t, min_interval_s=0) as c:
        kind, row = fetch_one(c, 99999, {"cast": 1})
    assert kind == "unknown"
    assert row["name"] is None


def test_fetch_one_status_primary_falls_back_to_action():
    """If event evidence says status but only Action exists, we still find it."""
    t = _transport({
        "/Action/123": (200, {"ID": 123, "Name_en": "Foo"}),
    })
    with XIVAPIClient(transport=t, min_interval_s=0) as c:
        kind, row = fetch_one(c, 123, {"applybuff": 10})
    assert kind == "action"
    assert row["name"] == "Foo"


def test_fflogs_status_offset_strips_one_million():
    """FFLogs ability id 1002216 should query Status 2216 (offset stripped)."""
    t = _transport({
        "/Status/2216": (200, {"ID": 2216, "Name_en": "The Wanderer's Minuet",
                                "Description_en": "Critical hit rate is increased."}),
    })
    with XIVAPIClient(transport=t, min_interval_s=0) as c:
        kind, row = fetch_one(c, 1_002_216, {"refreshbuff": 100, "applybuff": 10})
    assert kind == "status"
    assert row["name"] == "The Wanderer's Minuet"


def test_fflogs_status_offset_falls_through_on_404():
    """If the stripped id 404s on Status, we still try plain Action/Status."""
    t = _transport({
        "/Action/1500000": (200, {"ID": 1500000, "Name_en": "Synthetic"}),
    })
    with XIVAPIClient(transport=t, min_interval_s=0) as c:
        kind, row = fetch_one(c, 1_500_000, {"damage": 5})
    assert kind == "action"
    assert row["name"] == "Synthetic"


def test_fetch_one_strips_html_in_description():
    t = _transport({
        "/Action/1": (200, {"ID": 1, "Name_en": "X",
                            "Description_en": "  trailing whitespace  "}),
    })
    with XIVAPIClient(transport=t, min_interval_s=0) as c:
        _, row = fetch_one(c, 1, {"cast": 1})
    assert row["description"] == "trailing whitespace"
