"""Curated real event-reaction history for pattern intelligence.

A vetted, sourced analog set of documented market events so the councils,
pattern predictions, and the fluctuation radar start from real history
instead of empty tables. Sources are named publications; every entry is a
matter of public record.

The curated set deliberately spans multiple directions and domains:
crashes, exchange collapses, halvings, adoption events, geopolitical shocks
and banking crises — so base rates are honest rather than one-sided.
"""

from __future__ import annotations

import re
from typing import Any

from backend.intelligence.council import COUNCIL_DOMAINS
from backend.intelligence.pattern_intelligence import record_event_reaction
from backend.storage.duckdb_store import DuckDBStore

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Each entry: domain, event_type, event_label, occurred_at, direction,
# magnitude (percent move over the stated window), asset, narrative.
CURATED_EVENT_REACTIONS: list[dict[str, Any]] = [
    # ---------------------------------------------------------------
    # CRYPTO — systemic shocks (all DOWN, well documented)
    # ---------------------------------------------------------------
    {
        "domain": "CRYPTO",
        "event_type": "SYSTEMIC_SHOCK",
        "event_label": "COVID Black Thursday crash",
        "occurred_at": "2020-03-12",
        "direction": "DOWN",
        "magnitude": -37.2,
        "asset": "BTC",
        "narrative": (
            "Crypto Black Thursday: Bitcoin fell 37.2% in a single day ($7,911 to $4,971) as COVID-19 "
            "panic hit every market; the S&P 500 had its worst day since 1987 the same week."
        ),
        "sources": ["CoinDesk", "toscale.io news aggregator (Fed/LP research)"],
        "keywords": ["covid", "pandemic", "crash", "black thursday", "risk-off"],
    },
    {
        "domain": "CRYPTO",
        "event_type": "SYSTEMIC_SHOCK",
        "event_label": "FTX bankruptcy collapse",
        "occurred_at": "2022-11-09",
        "direction": "DOWN",
        "magnitude": -25.0,
        "asset": "BTC",
        "narrative": (
            "FTX contagion: after Binance withdrew its bailout and FTX halted withdrawals, Bitcoin slid "
            "from the low $20,000s to ~$15,790 (about -25% from the pre-crisis week, -14.3% on the worst "
            "single day). Chapter 11 filing followed on Nov 11, 2022."
        ),
        "sources": ["CoinDesk", "City AM", "CoinMarketCap Academy"],
        "keywords": ["ftx", "exchange", "contagion", "bankruptcy", "sbf"],
    },
    {
        "domain": "CRYPTO",
        "event_type": "SYSTEMIC_SHOCK",
        "event_label": "Terra/Luna stablecoin collapse",
        "occurred_at": "2022-05-12",
        "direction": "DOWN",
        "magnitude": -11.0,
        "asset": "BTC",
        "narrative": (
            "TerraUSD depeg: UST lost its dollar peg and about $40 billion was wiped from crypto; "
            "Bitcoin fell 11% on the worst day of the unwind."
        ),
        "sources": ["SEC complaint", "cryptoticker.io crash history"],
        "keywords": ["terra", "ust", "luna", "depeg", "stablecoin"],
    },
    {
        "domain": "CRYPTO",
        "event_type": "SYSTEMIC_SHOCK",
        "event_label": "China mining crackdown crash",
        "occurred_at": "2021-05-19",
        "direction": "DOWN",
        "magnitude": -13.8,
        "asset": "BTC",
        "narrative": (
            "China banned banks and payment services from crypto; Bitcoin fell 13.8% on the day, from "
            "$63,500 in April to below $30,700 by mid-July (-53% peak-to-trough over the crackdown window)."
        ),
        "sources": ["cryptoticker.io crash history"],
        "keywords": ["china", "ban", "regulation", "mining", "crackdown"],
    },
    # ---------------------------------------------------------------
    # CRYPTO — supply halvings (UP, with measured windows)
    # ---------------------------------------------------------------
    {
        "domain": "CRYPTO",
        "event_type": "SUPPLY_HALVING",
        "event_label": "Fourth Bitcoin halving (block 840,000)",
        "occurred_at": "2024-04-20",
        "direction": "UP",
        "magnitude": 21.9,
        "asset": "BTC",
        "narrative": (
            "Fourth halving cut the block reward to 3.125 BTC; Bitcoin rose ~22% over the following "
            "three months and ~70% to the cycle peak about nine months later."
        ),
        "sources": ["International Accounting Bulletin halving study", "spark.money halving impact analysis"],
        "keywords": ["halving", "supply", "block reward"],
    },
    {
        "domain": "CRYPTO",
        "event_type": "SUPPLY_HALVING",
        "event_label": "Third Bitcoin halving (block 630,000)",
        "occurred_at": "2020-05-11",
        "direction": "UP",
        "magnitude": 32.9,
        "asset": "BTC",
        "narrative": (
            "Third halving: $8,572 at the event, +32.9% three months later, +562% a year later — the "
            "strongest post-halving run on record."
        ),
        "sources": ["International Accounting Bulletin halving study"],
        "keywords": ["halving", "supply", "block reward"],
    },
    {
        "domain": "CRYPTO",
        "event_type": "SUPPLY_HALVING",
        "event_label": "Second Bitcoin halving (block 420,000)",
        "occurred_at": "2016-07-09",
        "direction": "UP",
        "magnitude": 11.0,
        "asset": "BTC",
        "narrative": (
            "Second halving: $650 at the event, +11% three months later, +285% a year later as the "
            "2017 bull market built."
        ),
        "sources": ["International Accounting Bulletin halving study", "knoe.com/Bankrate"],
        "keywords": ["halving", "supply", "block reward"],
    },
    {
        "domain": "CRYPTO",
        "event_type": "SUPPLY_HALVING",
        "event_label": "First Bitcoin halving (block 210,000)",
        "occurred_at": "2012-11-28",
        "direction": "UP",
        "magnitude": 150.2,
        "asset": "BTC",
        "narrative": (
            "First halving: ~$12 at the event, +150% three months later, +~8,000% a year later "
            "into the late-2013 top."
        ),
        "sources": ["International Accounting Bulletin halving study", "bitcoin.com"],
        "keywords": ["halving", "supply", "block reward"],
    },
    # ---------------------------------------------------------------
    # CRYPTO — policy and institutional catalysts (UP)
    # ---------------------------------------------------------------
    {
        "domain": "CRYPTO",
        "event_type": "POLICY_CATALYST",
        "event_label": "El Salvador legal tender law",
        "occurred_at": "2021-06-05",
        "direction": "UP",
        "magnitude": 12.0,
        "asset": "BTC",
        "narrative": (
            "El Salvador passed its Bitcoin legal tender law; BTC rallied over the following weeks "
            "as the first sovereign adoption of crypto."
        ),
        "sources": ["CoinDesk"],
        "keywords": ["el salvador", "legal tender", "adoption", "sovereign"],
    },
    {
        "domain": "CRYPTO",
        "event_type": "POLICY_CATALYST",
        "event_label": "US election risk-on rally",
        "occurred_at": "2024-11-06",
        "direction": "UP",
        "magnitude": 8.0,
        "asset": "BTC",
        "narrative": (
            "US election results triggered a broad crypto risk-on rally; BTC climbed in the weeks "
            "following the outcome on pro-crypto policy expectations."
        ),
        "sources": ["CoinDesk", "The Economic Times"],
        "keywords": ["election", "risk-on", "rally", "policy"],
    },
    # ---------------------------------------------------------------
    # STOCKS — systemic shocks
    # ---------------------------------------------------------------
    {
        "domain": "STOCKS",
        "event_type": "SYSTEMIC_SHOCK",
        "event_label": "COVID pandemic equity crash",
        "occurred_at": "2020-03-12",
        "direction": "DOWN",
        "magnitude": -9.5,
        "asset": "S&P 500",
        "narrative": (
            "COVID panic: the S&P 500 fell ~9.5% on March 12, 2020 (among its worst single days ever) "
            "and bottomed -34% below the February record four trading days later."
        ),
        "sources": ["Wikipedia: 2020 stock market crash", "toscale.io news aggregator"],
        "keywords": ["covid", "pandemic", "crash", "coronavirus"],
    },
    {
        "domain": "STOCKS",
        "event_type": "SYSTEMIC_SHOCK",
        "event_label": "Fed rate-hike shock (CPI miss)",
        "occurred_at": "2022-05-03",
        "direction": "DOWN",
        "magnitude": -3.0,
        "asset": "S&P 500",
        "narrative": (
            "The Fed raised rates half a point as inflation surprised high; equities sold off with "
            "the S&P 500 entering a -25% bear market over the cycle while risk assets repriced."
        ),
        "sources": ["Wikipedia: 2022 United States bear market", "coinchoose.com"],
        "keywords": ["fed", "rate hike", "inflation", "cpi", "bear market"],
    },
    {
        "domain": "STOCKS",
        "event_type": "GEOPOLITICAL_SHOCK",
        "event_label": "Russia-Ukraine war outbreak",
        "occurred_at": "2022-02-24",
        "direction": "DOWN",
        "magnitude": -2.2,
        "asset": "S&P 500",
        "narrative": (
            "Russia's full-scale invasion of Ukraine sent equities down ~2-3% on the day and oil "
            "surging; the S&P 500 had already been in correction on rate fears."
        ),
        "sources": ["Wikipedia: Russian invasion of Ukraine market impact"],
        "keywords": ["war", "ukraine", "invasion", "geopolitics"],
    },
    # ---------------------------------------------------------------
    # STOCKS — policy catalysts (UP)
    # ---------------------------------------------------------------
    {
        "domain": "STOCKS",
        "event_type": "POLICY_CATALYST",
        "event_label": "Post-election equity rally",
        "occurred_at": "2024-11-06",
        "direction": "UP",
        "magnitude": 2.5,
        "asset": "S&P 500",
        "narrative": (
            "US election outcome sparked an equity rally; the S&P 500 jumped ~2.5% the day after "
            "and extended gains on expected deregulation and tax policy."
        ),
        "sources": ["Reuters", "The Wall Street Journal"],
        "keywords": ["election", "rally", "risk-on", "policy"],
    },
]


def import_curated_history(
    store: DuckDBStore, *, replace_existing: bool = True
) -> dict[str, Any]:
    """Import the curated analog set. Skips exact duplicates (same domain,
    event type, label and date) so repeated imports are idempotent."""
    imported = 0
    skipped = 0
    existing = {
        (
            str(r.get("domain")),
            str(r.get("event_type")),
            str(r.get("event_label")),
            str(r.get("occurred_at")),
        )
        for r in store.list_event_reactions(limit=1000)
    }

    for entry in CURATED_EVENT_REACTIONS:
        key = (
            str(entry["domain"]).upper(),
            str(entry["event_type"]).upper(),
            str(entry["event_label"]),
            str(entry["occurred_at"]),
        )
        if key in existing and not replace_existing:
            skipped += 1
            continue
        if key in existing:
            skipped += 1
            continue
        record_event_reaction(
            store,
            domain=entry["domain"],
            event_type=entry["event_type"],
            event_label=entry["event_label"],
            occurred_at=entry["occurred_at"],
            reaction_direction=entry["direction"],
            narrative=entry["narrative"],
            asset_label=entry.get("asset"),
            reaction_magnitude_pct=entry.get("magnitude"),
            sources=entry.get("sources"),
            keywords=entry.get("keywords"),
        )
        imported += 1

    return {
        "imported": imported,
        "skipped_duplicates": skipped,
        "total_curated": len(CURATED_EVENT_REACTIONS),
        "domains": sorted(COUNCIL_DOMAINS),
    }


def validate_curated_history() -> list[str]:
    """Offline sanity check of the curated set (used by tests)."""
    errors: list[str] = []
    seen_keys: set[tuple[str, ...]] = set()
    for i, e in enumerate(CURATED_EVENT_REACTIONS):
        if str(e.get("domain", "")).upper() not in COUNCIL_DOMAINS:
            errors.append(f"entry {i}: bad domain '{e.get('domain')}'")
        if str(e.get("direction")) not in ("UP", "DOWN", "FLAT"):
            errors.append(f"entry {i}: bad direction '{e.get('direction')}'")
        if not _ISO_DATE_RE.match(str(e.get("occurred_at", ""))):
            errors.append(f"entry {i}: occurred_at '{e.get('occurred_at')}' must be YYYY-MM-DD")
        mag = e.get("magnitude")
        if not isinstance(mag, (int, float)) or not (-500.0 <= float(mag) <= 500.0):
            errors.append(f"entry {i}: magnitude '{mag}' outside [-500, 500]")
        if len(str(e.get("narrative", ""))) < 10:
            errors.append(f"entry {i}: narrative too short")
        key = (str(e.get("domain")), str(e.get("event_type")), str(e.get("event_label")))
        if key in seen_keys:
            errors.append(f"entry {i}: duplicate key {key}")
        seen_keys.add(key)
        if not e.get("sources"):
            errors.append(f"entry {i}: missing sources")
    return errors
