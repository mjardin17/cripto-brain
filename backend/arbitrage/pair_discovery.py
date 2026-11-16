"""Polymarket <-> Kalshi cross-venue pair discovery and registry.

Discovers CANDIDATE contract pairs across both venues from live, read-only
public market data and registers them in `cross_venue_pairs`.

Fail-closed invariants (consistent with backend/arbitrage/matcher.py):
- Discovery NEVER attests settlement equivalence. Every auto-discovered pair
  is registered with resolution_source_verified / predicate_verified /
  void_rules_compatible / allow_early_close_variance all False, so
  identical_settlement_verified is False and the Risk Gate denies it until a
  human operator re-registers the pair with explicit attestations via
  POST /api/cross-venue/pairs.
- Existing pairs (matched by poly_market_id + kalshi_ticker) are never
  overwritten, so human attestations are never downgraded or upgraded here.
- Candidates are rejected outright (not registered) on hard structural
  mismatches: discriminator conflicts (e.g. VP vs President, Democratic vs
  Republican, cut vs hike), missing numbers/years, cutoff misalignment, or a
  Polymarket negRiskOther catch-all.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any

import httpx

from backend.arbitrage.matcher import assess_contract_equivalence
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso
from backend.venues.kalshi import (
    DEFAULT_HEADERS as KALSHI_HEADERS,
    KALSHI_BASE_URL,
    KalshiBinaryMarket,
    parse_kalshi_market,
)
from backend.venues.polymarket import (
    DEFAULT_HEADERS as POLY_HEADERS,
    GAMMA_BASE_URL,
    PolymarketBinaryMarket,
    parse_gamma_binary_market,
)

DISCOVERY_NOTE_PREFIX = "AUTO_DISCOVERED"

_STOP = {
    "will", "the", "be", "in", "on", "of", "to", "a", "an", "by", "or", "and",
    "for", "before", "after", "at", "is", "are", "yes", "no", "who", "what",
    "which", "next", "win", "wins", "won", "this", "that", "with", "from",
    "than", "its", "their", "his", "her", "any", "end", "market",
}

# Light normalisation so venue phrasing variants land on the same stem.
_STEMS: tuple[tuple[str, str], ...] = (
    ("presidential", "presid"), ("presidency", "presid"), ("president", "presid"),
    ("nomination", "nomin"), ("nominee", "nomin"), ("nominated", "nomin"),
    ("democrats", "democrat"), ("democratic", "democrat"),
    ("republicans", "republican"), ("gop", "republican"),
    ("elections", "elect"), ("election", "elect"), ("elected", "elect"),
    ("decreases", "cut"), ("decrease", "cut"), ("cuts", "cut"),
    ("increases", "hike"), ("increase", "hike"), ("hikes", "hike"), ("raise", "hike"),
    ("championship", "champion"), ("champions", "champion"),
)

# If a group appears on exactly one side, the predicates differ -> reject.
_DISCRIMINATOR_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"vice", "vp"}),
    frozenset({"senate", "senator"}),
    frozenset({"governor", "gubernatorial"}),
    frozenset({"house", "congressional"}),
    frozenset({"mayor", "mayoral"}),
    frozenset({"primary"}),
    frozenset({"nomin"}),
    frozenset({"run", "running", "announce", "announces", "candidacy"}),
    frozenset({"democrat"}),
    frozenset({"republican"}),
    frozenset({"cut"}),
    frozenset({"hike"}),
)

_WORD_RE = re.compile(r"[a-z0-9]+")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _stem(word: str) -> str:
    for src, dst in _STEMS:
        if word == src:
            return dst
    return word


def tokenize(text: str) -> set[str]:
    words = _WORD_RE.findall((text or "").lower().replace("\u2019", "'"))
    return {_stem(w) for w in words if len(w) > 1 and w not in _STOP and not w.isdigit()}


def extract_numbers(text: str) -> set[str]:
    out: set[str] = set()
    for raw in _NUM_RE.findall(text or ""):
        try:
            val = float(raw)
        except ValueError:
            continue
        out.add(f"{val:g}")
    return out


def _parse_iso(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        cleaned = iso_str.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        return datetime.fromisoformat(cleaned)
    except (TypeError, ValueError):
        return None


def kalshi_match_text(market: KalshiBinaryMarket, event_title: str = "") -> str:
    """Kalshi predicate text: market title + YES subtitle + parent event title."""
    parts = [market.title, market.yes_sub_title if market.yes_sub_title not in ("Yes", "") else "", event_title]
    return " ".join(p for p in parts if p)


def discriminator_conflicts(poly_tokens: set[str], kalshi_tokens: set[str]) -> list[str]:
    conflicts = []
    for group in _DISCRIMINATOR_GROUPS:
        if bool(poly_tokens & group) != bool(kalshi_tokens & group):
            conflicts.append("/".join(sorted(group)))
    return conflicts


@dataclass
class PairCandidate:
    poly_market_id: str
    poly_question: str
    poly_end_date: str
    kalshi_ticker: str
    kalshi_title: str
    kalshi_event_title: str
    kalshi_expiration: str
    similarity: float
    shared_tokens: list[str]
    cutoff_delta_hours: float | None
    settlement_status: str = ""
    mismatch_reasons: list[str] = field(default_factory=list)
    registered: bool = False
    pair_id: str | None = None
    skip_reason: str | None = None


def score_candidate(
    poly: PolymarketBinaryMarket,
    kalshi: KalshiBinaryMarket,
    *,
    kalshi_event_title: str = "",
    common_tokens: frozenset[str] = frozenset(),
    max_cutoff_delta_hours: float = 24.0,
) -> PairCandidate | None:
    """Deterministic candidate scoring. Returns None when the pair is not a plausible match."""
    if poly.neg_risk_other or not poly.condition_id:
        return None

    k_text = kalshi_match_text(kalshi, kalshi_event_title)
    p_tok = tokenize(poly.question)
    k_tok = tokenize(k_text)
    if not p_tok or not k_tok:
        return None

    shared = p_tok & k_tok
    # Must share at least one distinctive (non-common) token, e.g. a name, and
    # EVERY distinctive Polymarket token must appear on the Kalshi side
    # (rejects e.g. "Josh Stein" <-> "Josh Shapiro").
    distinctive = p_tok - common_tokens
    if not (shared - common_tokens) or (distinctive - k_tok):
        return None
    if discriminator_conflicts(p_tok, k_tok):
        return None

    # Every number/year in the Polymarket question must appear on the Kalshi side
    # (title, subtitle, event title, or the Kalshi expiration year).
    p_nums = extract_numbers(poly.question)
    k_dt = _parse_iso(kalshi.expiration_time or kalshi.close_time)
    k_nums = extract_numbers(k_text) | extract_numbers(kalshi.ticker.replace("-", " "))
    if k_dt is not None:
        k_nums.add(f"{float(k_dt.year):g}")
    if not p_nums <= k_nums:
        return None

    p_dt = _parse_iso(poly.end_date)
    if p_dt is None or k_dt is None:
        return None
    delta_h = round(abs((p_dt - k_dt).total_seconds()) / 3600.0, 2)
    if delta_h > max_cutoff_delta_hours:
        return None

    similarity = round(len(shared) / len(p_tok | k_tok), 4)
    return PairCandidate(
        poly_market_id=poly.market_id,
        poly_question=poly.question,
        poly_end_date=poly.end_date,
        kalshi_ticker=kalshi.ticker,
        kalshi_title=kalshi.title,
        kalshi_event_title=kalshi_event_title,
        kalshi_expiration=kalshi.expiration_time or kalshi.close_time,
        similarity=similarity,
        shared_tokens=sorted(shared),
        cutoff_delta_hours=delta_h,
    )


def match_markets(
    poly_markets: list[PolymarketBinaryMarket],
    kalshi_markets: list[KalshiBinaryMarket],
    *,
    kalshi_event_titles: dict[str, str] | None = None,
    min_similarity: float = 0.3,
    max_cutoff_delta_hours: float = 24.0,
    common_token_df: int = 200,
) -> list[tuple[PairCandidate, PolymarketBinaryMarket, KalshiBinaryMarket]]:
    """One-to-one greedy matching of Polymarket markets to Kalshi markets."""
    titles = kalshi_event_titles or {}
    k_tokens: dict[str, set[str]] = {}
    index: dict[str, list[int]] = {}
    for i, km in enumerate(kalshi_markets):
        toks = tokenize(kalshi_match_text(km, titles.get(km.ticker, "")))
        k_tokens[km.ticker] = toks
        for t in toks:
            index.setdefault(t, []).append(i)
    common = frozenset(t for t, ids in index.items() if len(ids) > common_token_df)

    scored: list[tuple[PairCandidate, PolymarketBinaryMarket, KalshiBinaryMarket]] = []
    for pm in poly_markets:
        p_tok = tokenize(pm.question)
        rare = [t for t in p_tok if t not in common]
        cand_ids: set[int] = set()
        for t in rare:
            cand_ids.update(index.get(t, ()))
        for i in cand_ids:
            km = kalshi_markets[i]
            cand = score_candidate(
                pm,
                km,
                kalshi_event_title=titles.get(km.ticker, ""),
                common_tokens=common,
                max_cutoff_delta_hours=max_cutoff_delta_hours,
            )
            if cand is not None and cand.similarity >= min_similarity:
                scored.append((cand, pm, km))

    scored.sort(key=lambda x: (-x[0].similarity, x[0].cutoff_delta_hours or 0.0, x[0].kalshi_ticker))
    used_poly: set[str] = set()
    used_kalshi: set[str] = set()
    out = []
    for cand, pm, km in scored:
        if pm.market_id in used_poly or km.ticker in used_kalshi:
            continue
        used_poly.add(pm.market_id)
        used_kalshi.add(km.ticker)
        out.append((cand, pm, km))
    return out


async def _get_with_backoff(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str],
    headers: dict[str, str],
    retries: int = 4,
    base_delay_s: float = 1.0,
) -> httpx.Response:
    """GET with bounded exponential backoff on HTTP 429/5xx (honours Retry-After)."""
    for attempt in range(retries + 1):
        resp = await client.get(url, params=params, headers=headers)
        if resp.status_code != 429 and resp.status_code < 500:
            resp.raise_for_status()
            return resp
        if attempt == retries:
            resp.raise_for_status()
        retry_after = resp.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else base_delay_s * (2**attempt)
        except ValueError:
            delay = base_delay_s * (2**attempt)
        await asyncio.sleep(min(delay, 10.0))
    raise RuntimeError("unreachable")  # pragma: no cover


async def fetch_polymarket_universe(
    client: httpx.AsyncClient, *, pages: int = 5, page_size: int = 100
) -> list[PolymarketBinaryMarket]:
    seen: set[str] = set()
    out: list[PolymarketBinaryMarket] = []
    for page in range(pages):
        resp = await _get_with_backoff(
            client,
            f"{GAMMA_BASE_URL}/markets",
            params={
                "active": "true",
                "closed": "false",
                "enableOrderBook": "true",
                "order": "volume24hr",
                "ascending": "false",
                "limit": str(page_size),
                "offset": str(page * page_size),
            },
            headers=POLY_HEADERS,
        )
        rows = resp.json()
        if not rows:
            break
        for raw in rows:
            parsed = parse_gamma_binary_market(raw)
            if parsed is not None and parsed.market_id not in seen:
                seen.add(parsed.market_id)
                out.append(parsed)
        if len(rows) < page_size:
            break
    return out


async def fetch_kalshi_universe(
    client: httpx.AsyncClient,
    *,
    max_pages: int = 40,
    page_delay_s: float = 0.25,
    warnings: list[str] | None = None,
) -> tuple[list[KalshiBinaryMarket], dict[str, str]]:
    if warnings is None:
        warnings = []
    markets: list[KalshiBinaryMarket] = []
    event_titles: dict[str, str] = {}
    cursor: str | None = None
    for _ in range(max_pages):
        params = {"limit": "200", "status": "open", "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = await _get_with_backoff(
                client, f"{KALSHI_BASE_URL}/events", params=params, headers=KALSHI_HEADERS
            )
        except httpx.HTTPError as exc:
            if not markets:
                raise
            warnings.append(f"Kalshi pagination stopped early after {len(markets)} markets: {exc}")
            break
        payload = resp.json()
        for ev in payload.get("events", []):
            series = str(ev.get("series_ticker") or "")
            ev_title = str(ev.get("title") or "")
            for raw in ev.get("markets", []) or []:
                parsed = parse_kalshi_market(raw, series_ticker=series)
                if parsed is not None:
                    markets.append(parsed)
                    event_titles[parsed.ticker] = ev_title
        cursor = payload.get("cursor") or None
        if not cursor:
            break
        await asyncio.sleep(page_delay_s)  # stay well under public rate limits
    return markets, event_titles


def _structurally_blocked(mismatch_checks: tuple[dict[str, Any], ...]) -> list[str]:
    """Checks that a human attestation cannot fix (EARLY_CLOSE_PARITY can be reconciled)."""
    hard = {"POLY_BINARY_NON_CATCHALL", "OUTCOME_POLARITY_MATCH", "EXPIRATION_CUTOFF_ALIGNMENT"}
    return [c["check_id"] for c in mismatch_checks if c["check_id"] in hard and not c["passed"]]


def register_candidates(
    store: DuckDBStore,
    matches: list[tuple[PairCandidate, PolymarketBinaryMarket, KalshiBinaryMarket]],
    *,
    register: bool,
) -> list[PairCandidate]:
    results: list[PairCandidate] = []
    for orig, pm, km in matches:
        # Work on a fresh copy so callers' candidate objects are never mutated.
        cand = replace(orig, mismatch_reasons=[], registered=False, pair_id=None, skip_reason=None)
        assessment = assess_contract_equivalence(poly_market=pm, kalshi_market=km)
        cand.settlement_status = assessment.status
        cand.mismatch_reasons = list(assessment.mismatch_reasons)
        if assessment.identical_settlement_verified:  # pragma: no cover - impossible without attestations
            raise RuntimeError("Discovery must never produce a verified pair")

        hard = _structurally_blocked(assessment.checks)
        if hard:
            cand.skip_reason = f"Structurally blocked: {', '.join(hard)}"
            results.append(cand)
            continue

        existing = store.find_cross_venue_pair(pm.market_id, km.ticker)
        if existing is not None:
            cand.pair_id = existing["pair_id"]
            cand.skip_reason = "Already registered (left unchanged)"
            results.append(cand)
            continue

        pair_id = f"XPAIR-{km.ticker}-{pm.market_id}".upper()[:64]
        cand.pair_id = pair_id
        if register:
            store.upsert_cross_venue_pair(
                {
                    "pair_id": pair_id,
                    "poly_market_id": pm.market_id,
                    "poly_question": pm.question,
                    "kalshi_ticker": km.ticker,
                    "kalshi_title": km.title,
                    "resolution_source_verified": False,
                    "predicate_verified": False,
                    "polarity_inverted": False,
                    "void_rules_compatible": False,
                    "allow_early_close_variance": False,
                    "identical_settlement_verified": False,
                    "settlement_status": assessment.status,
                    "mismatch_reasons": list(assessment.mismatch_reasons),
                    "cutoff_delta_hours": assessment.cutoff_delta_hours,
                    "notes": (
                        f"{DISCOVERY_NOTE_PREFIX} similarity={cand.similarity} "
                        f"shared={','.join(cand.shared_tokens)} | PENDING HUMAN SETTLEMENT REVIEW"
                    ),
                    "created_at": utc_now_iso(),
                }
            )
            cand.registered = True
        results.append(cand)
    return results


async def discover_pairs(
    store: DuckDBStore,
    *,
    register: bool = False,
    poly_pages: int = 5,
    kalshi_max_pages: int = 40,
    min_similarity: float = 0.3,
    max_cutoff_delta_hours: float = 24.0,
    timeout_s: float = 20.0,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    owns = http_client is None
    client = http_client or httpx.AsyncClient(timeout=timeout_s)
    warnings: list[str] = []
    try:
        poly_markets, (kalshi_markets, event_titles) = await asyncio.gather(
            fetch_polymarket_universe(client, pages=poly_pages),
            fetch_kalshi_universe(client, max_pages=kalshi_max_pages, warnings=warnings),
        )
    finally:
        if owns:
            await client.aclose()

    matches = match_markets(
        poly_markets,
        kalshi_markets,
        kalshi_event_titles=event_titles,
        min_similarity=min_similarity,
        max_cutoff_delta_hours=max_cutoff_delta_hours,
    )
    results = register_candidates(store, matches, register=register)
    return {
        "provenance": "LIVE",
        "mode": "REGISTER" if register else "DRY_RUN",
        "polymarket_markets_scanned": len(poly_markets),
        "kalshi_markets_scanned": len(kalshi_markets),
        "candidates_found": len(results),
        "registered_count": sum(1 for r in results if r.registered),
        "verified_count": 0,
        "warnings": warnings,
        "note": "Auto-discovered pairs are UNVERIFIED and denied by the Risk Gate until a human "
        "attests settlement equivalence via POST /api/cross-venue/pairs.",
        "candidates": [asdict(r) for r in results],
    }


def _main() -> None:  # pragma: no cover - thin CLI wrapper
    ap = argparse.ArgumentParser(description="Discover Polymarket<->Kalshi candidate pairs (read-only).")
    ap.add_argument("--db", default="data/certainty_stack.duckdb")
    ap.add_argument("--register", action="store_true", help="Persist unverified candidates")
    ap.add_argument("--poly-pages", type=int, default=5)
    ap.add_argument("--kalshi-pages", type=int, default=40)
    ap.add_argument("--min-similarity", type=float, default=0.3)
    args = ap.parse_args()
    store = DuckDBStore(db_path=args.db)
    try:
        out = asyncio.run(
            discover_pairs(
                store,
                register=args.register,
                poly_pages=args.poly_pages,
                kalshi_max_pages=args.kalshi_pages,
                min_similarity=args.min_similarity,
            )
        )
    finally:
        store.close()
    print(json.dumps({k: v for k, v in out.items() if k != "candidates"}, indent=2))
    for c in out["candidates"]:
        flag = "REGISTERED" if c["registered"] else (c["skip_reason"] or "dry-run")
        print(f"[{c['similarity']:.2f} | {c['cutoff_delta_hours']}h] {c['poly_question'][:60]!r} <-> {c['kalshi_ticker']} ({flag})")


if __name__ == "__main__":  # pragma: no cover
    _main()
