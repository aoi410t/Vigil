"""FFXIV consolegameswiki scraper for ability metadata not in XIVAPI (T-108
wiki follow-up).

The FFXIV consolegameswiki has trait-aware ability data XIVAPI doesn't expose
cleanly: post-trait Reprisal/Feint/Addle damage reduction, Brotherhood/Searing
Light/Embolden post-trait durations, etc. This module fetches one HTML page
per ability and parses out the `Duration: <N>s` value via tag-stripping + regex.

Used by `scripts/scrape_ability_durations.py` to populate
`abilities.duration_ms` for labelled raid/personal/mit abilities. M-BURST in
`analysis/burst.py` reads that column to size per-raid-buff windows instead of
the fixed 20s default.

Tag-stripping over the raw HTML beats HTML parsing here: the page is short,
the field is reliably formatted (`Duration:`, possibly across a `</span>`
closing tag, then `&nbsp;`, then `<N>s`), and we avoid pulling in
BeautifulSoup or lxml for one regex.
"""
from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote

import httpx

CONSOLEGAMESWIKI_BASE = "https://ffxiv.consolegameswiki.com"
USER_AGENT = "Vigil/1.5 (FFLogs progression tracker; +github)"
# Polite pacing between page fetches — wiki etiquette. The scraper isn't on a
# hot path; this is fine.
DEFAULT_PACING_S = 0.5

_TAG_RE = re.compile(r"<[^>]+>")
_DURATION_RE = re.compile(r"Duration:\s*(\d+)\s*s", re.IGNORECASE)
# Damage-reduction-pct heuristic. Anchored to a verb phrase so we don't grab
# arbitrary percentages on the page (potency tables, recast %, etc.). Takes
# the FIRST match in the body — the in-game description always leads.
# Known limitations on this regex (documented for the user; conservative is OK
# since M-MIT today only checks 'did the mit fire', not its quantified value):
#   - Multi-value abilities (Feint: 10% physical + 5% magic) get the FIRST
#     value, which is the headline for the mit-class but might not be the
#     value the user cares about for their composition.
#   - Patch-note text on the page can shadow the current value (e.g. Mantra
#     "reduced from 20% to 10%" picks up 20).
#   - Barrier-style mits (Divine Veil) that don't reduce a % don't match.
# A wiki cleanup pass + hand-curated overrides could fix all three when M-MIT
# actually grows damage quantification (currently it doesn't).
_MIT_PCT_RE = re.compile(
    r"(?is)(?:reduces?|reducing|reduced|lowers?|lowering)"  # verb
    r"[^.<>]{0,120}?"
    r"(\d{1,2})\s*%"
)


def _ability_name_to_page(name: str) -> str:
    """Convert an XIVAPI ability name to a wiki page slug.

    'Divination' -> 'Divination'. Spaces become underscores per MediaWiki
    convention. URL encoding handles apostrophes / other punctuation.
    """
    return quote(name.strip().replace(" ", "_"), safe="_()")


def _strip_html(html: str) -> str:
    """Lossy: strip tags, replace common HTML entities. Enough for regex."""
    text = _TAG_RE.sub(" ", html)
    text = (
        text
        .replace("&nbsp;", " ")
        .replace("&#160;", " ")
        .replace("&amp;", "&")
        .replace("&apos;", "'")
        .replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return text


def extract_duration_ms(html: str) -> int | None:
    """Parse 'Duration: <N>s' from a stripped-HTML wiki page.

    Returns the duration in milliseconds, or None if no Duration field found.
    The FIRST 'Duration: <N>s' wins — that's the primary buff duration. Pages
    with multiple Duration fields (e.g. Divination has a trait-granted
    secondary status with its own duration) get the primary one, which matches
    what we want for M-BURST raid-buff window sizing.
    """
    if not html:
        return None
    text = _strip_html(html)
    m = _DURATION_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1)) * 1000
    except ValueError:
        return None


def extract_mit_pct(html: str) -> int | None:
    """Parse a damage-reduction percentage from a stripped-HTML wiki page.

    Returns the percentage as an int (0..100), or None if no parseable value.
    Walks the page in order and takes the FIRST verb+percentage pair (see
    _MIT_PCT_RE for the verb list). For multi-value abilities (Feint =
    physical 10% + magic 5%) this picks the first listed value, which is the
    headline-class value (Feint = physical mit primarily).
    """
    if not html:
        return None
    text = _strip_html(html)
    m = _MIT_PCT_RE.search(text)
    if not m:
        return None
    try:
        v = int(m.group(1))
    except ValueError:
        return None
    # Sanity bound — wiki occasionally references damage potencies (e.g.
    # "200% potency") which our regex would otherwise grab; mit % is always
    # in [1, 100].
    if v < 1 or v > 100:
        return None
    return v


def fetch_duration_for_ability(
    client: httpx.Client, ability_name: str,
) -> int | None:
    """Fetch the wiki page for `ability_name` and extract its duration in ms.

    Returns None on 404 or any other fetch failure — caller decides whether to
    retry or move on.
    """
    meta = fetch_metadata_for_ability(client, ability_name)
    return meta.get("duration_ms")


def fetch_metadata_for_ability(
    client: httpx.Client, ability_name: str,
) -> dict[str, int | None]:
    """Fetch the wiki page once and extract every supported metadata field.

    Returns `{duration_ms: int|None, mit_pct: int|None}`. Uses one HTTP
    request per ability — strictly better than calling per-field helpers
    which would each refetch.
    """
    out: dict[str, int | None] = {"duration_ms": None, "mit_pct": None}
    if not ability_name:
        return out
    page = _ability_name_to_page(ability_name)
    url = f"{CONSOLEGAMESWIKI_BASE}/wiki/{page}"
    try:
        resp = client.get(url, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError:
        return out
    if resp.status_code != 200:
        return out
    out["duration_ms"] = extract_duration_ms(resp.text)
    out["mit_pct"] = extract_mit_pct(resp.text)
    return out


def scrape_durations_for_abilities(
    client: httpx.Client,
    abilities: list[tuple[int, str]],
    *,
    pacing_s: float = DEFAULT_PACING_S,
) -> dict[int, int | None]:
    """Fetch durations for a batch of abilities. Returns ability_id -> duration_ms.

    Sleeps `pacing_s` between successive requests (skipped on the first call
    and after a non-network failure). Values may be None when no page was
    found or no Duration field parsed.

    Kept as the original duration-only helper for backwards compatibility
    with the v1.5.7 scrape script and tests. New code should prefer
    `scrape_metadata_for_abilities` which returns both fields in one fetch.
    """
    metadata = scrape_metadata_for_abilities(client, abilities, pacing_s=pacing_s)
    return {aid: m["duration_ms"] for aid, m in metadata.items()}


def scrape_metadata_for_abilities(
    client: httpx.Client,
    abilities: list[tuple[int, str]],
    *,
    pacing_s: float = DEFAULT_PACING_S,
) -> dict[int, dict[str, int | None]]:
    """Fetch full metadata (duration_ms + mit_pct) for a batch of abilities.

    Returns `{ability_id: {duration_ms, mit_pct}}`. One HTTP request per
    ability. Polite pacing between requests.
    """
    out: dict[int, dict[str, int | None]] = {}
    for i, (ability_id, name) in enumerate(abilities):
        if i > 0:
            time.sleep(pacing_s)
        out[ability_id] = fetch_metadata_for_ability(client, name)
    return out
