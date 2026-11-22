"""Read-only, replaceable public data adapters for Solana token discovery & on-chain evidence.

Strict Invariants:
- Zero wallet private keys, zero transaction construction, zero Jupiter swap execution, zero signing.
- Every network call uses bounded timeouts and explicit timestamps.
- Missing, malformed, or unreachable provider fields MUST remain None (never fabricated as verified True).
- Profile feeds are never trusted as liquid proof without a resolved pool pair_address & token mint.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from .models import Candidate, FilterEvidence

DEFAULT_USER_AGENT = "TheCertaintyStack/1.0 (+read-only-solana-research)"
WSOL_MINT = "So11111111111111111111111111111111111111112"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


@dataclass
class DiscoveredPool:
    candidate: Candidate
    price_usd: float | None = None
    liquidity_usd: float | None = None
    volume_1h_usd: float | None = None
    volume_24h_usd: float | None = None
    dex_id: str | None = None
    quote_symbol: str | None = None
    pair_created_at_ms: int | None = None
    provenance: str = "LIVE"
    raw_pair: dict[str, Any] = field(default_factory=dict)


class SolanaDiscoveryProvider(Protocol):
    async def discover_pools(self, *, query: str = "solana", limit: int = 12) -> list[DiscoveredPool]: ...
    async def resolve_mint(self, mint: str) -> DiscoveredPool | None: ...


class DexScreenerSolanaAdapter:
    """Public read-only DexScreener API adapter for Solana DEX pair discovery and volume/liquidity checks."""

    BASE_URL = "https://api.dexscreener.com"

    def __init__(self, timeout_seconds: float = 8.0) -> None:
        self.timeout_seconds = timeout_seconds

    def _parse_pair(self, pair: dict[str, Any], source_label: str) -> DiscoveredPool | None:
        if str(pair.get("chainId") or "").lower() != "solana":
            return None
        base = pair.get("baseToken") or {}
        quote = pair.get("quoteToken") or {}
        mint = str(base.get("address") or "").strip()
        pair_addr = str(pair.get("pairAddress") or "").strip()
        symbol = str(base.get("symbol") or "UNKNOWN").strip()[:32] or "UNKNOWN"
        if len(mint) < 32 or not pair_addr:
            return None

        vol = pair.get("volume") or {}
        liq = pair.get("liquidity") or {}
        vol_1h = _safe_float(vol.get("h1"))
        vol_24h = _safe_float(vol.get("h24"))
        liq_usd = _safe_float(liq.get("usd"))
        price_usd = _safe_float(pair.get("priceUsd"))
        created_ms_raw = pair.get("pairCreatedAt")
        created_ms = int(created_ms_raw) if isinstance(created_ms_raw, (int, float)) else None

        cand = Candidate(
            mint=mint,
            symbol=symbol,
            pair_address=pair_addr,
            source=source_label,
        )
        return DiscoveredPool(
            candidate=cand,
            price_usd=price_usd,
            liquidity_usd=liq_usd,
            volume_1h_usd=vol_1h,
            volume_24h_usd=vol_24h,
            dex_id=str(pair.get("dexId") or "") or None,
            quote_symbol=str(quote.get("symbol") or "") or None,
            pair_created_at_ms=created_ms,
            provenance="LIVE",
            raw_pair={
                "pairAddress": pair_addr,
                "dexId": pair.get("dexId"),
                "url": pair.get("url"),
                "priceUsd": price_usd,
                "liquidityUsd": liq_usd,
                "volume1hUsd": vol_1h,
                "fetchedAt": _utc_now_iso(),
            },
        )

    async def resolve_mint(self, mint: str) -> DiscoveredPool | None:
        clean_mint = mint.strip()
        if len(clean_mint) < 32:
            return None
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, headers={"User-Agent": DEFAULT_USER_AGENT}
        ) as client:
            resp = await client.get(f"{self.BASE_URL}/tokens/v1/solana/{clean_mint}")
            resp.raise_for_status()
            rows = resp.json()
            if not isinstance(rows, list):
                rows = (rows or {}).get("pairs") or []

        best: DiscoveredPool | None = None
        best_liq = -1.0
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            parsed = self._parse_pair(raw, "dexscreener:tokens_v1")
            if parsed is None:
                continue
            liq = parsed.liquidity_usd or 0.0
            if liq > best_liq:
                best = parsed
                best_liq = liq
        return best

    async def discover_pools(self, *, query: str = "", limit: int = 12) -> list[DiscoveredPool]:
        q = query.strip()
        pools_by_mint: dict[str, DiscoveredPool] = {}
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, headers={"User-Agent": DEFAULT_USER_AGENT}
        ) as client:
            if q and q.lower() != "trending":
                resp = await client.get(f"{self.BASE_URL}/latest/dex/search", params={"q": q})
                resp.raise_for_status()
                pairs = (resp.json() or {}).get("pairs") or []
                for p in pairs:
                    parsed = self._parse_pair(p, "dexscreener:search")
                    if parsed and parsed.candidate.mint != WSOL_MINT:
                        prev = pools_by_mint.get(parsed.candidate.mint)
                        if prev is None or (parsed.liquidity_usd or 0.0) > (prev.liquidity_usd or 0.0):
                            pools_by_mint[parsed.candidate.mint] = parsed
            else:
                # Resolve profiles ONLY after validating real liquidity pairs via /tokens/v1/solana/
                resp = await client.get(f"{self.BASE_URL}/token-boosts/latest/v1")
                resp.raise_for_status()
                profiles = resp.json() or []
                mints: list[str] = []
                for item in profiles:
                    if str(item.get("chainId") or "").lower() != "solana":
                        continue
                    m = str(item.get("tokenAddress") or "").strip()
                    if len(m) >= 32 and m != WSOL_MINT and m not in mints:
                        mints.append(m)
                    if len(mints) >= max(limit * 2, 16):
                        break
                if mints:
                    batch_resp = await client.get(
                        f"{self.BASE_URL}/tokens/v1/solana/{','.join(mints[:30])}"
                    )
                    batch_resp.raise_for_status()
                    pairs = batch_resp.json() or []
                    for p in pairs:
                        if not isinstance(p, dict):
                            continue
                        parsed = self._parse_pair(p, "dexscreener:resolved_boost")
                        if parsed and parsed.candidate.mint != WSOL_MINT:
                            prev = pools_by_mint.get(parsed.candidate.mint)
                            if prev is None or (parsed.liquidity_usd or 0.0) > (prev.liquidity_usd or 0.0):
                                pools_by_mint[parsed.candidate.mint] = parsed

        ranked = sorted(
            pools_by_mint.values(),
            key=lambda x: ((x.volume_1h_usd or 0.0), (x.liquidity_usd or 0.0)),
            reverse=True,
        )
        return ranked[:limit]


class GeckoTerminalSolanaAdapter:
    """Secondary read-only public adapter for Solana pool price & 1h volume checks."""

    BASE_URL = "https://api.geckoterminal.com/api/v2"

    def __init__(self, timeout_seconds: float = 8.0) -> None:
        self.timeout_seconds = timeout_seconds

    async def fetch_token_pools(self, mint: str) -> DiscoveredPool | None:
        clean_mint = mint.strip()
        if len(clean_mint) < 32:
            return None
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"},
        ) as client:
            resp = await client.get(
                f"{self.BASE_URL}/networks/solana/tokens/{clean_mint}/pools",
                params={"page": 1},
            )
            resp.raise_for_status()
            data = (resp.json() or {}).get("data") or []
        if not data:
            return None
        top = data[0]
        attrs = top.get("attributes") or {}
        pair_addr = str(attrs.get("address") or "").strip()
        name = str(attrs.get("name") or "UNKNOWN").split("/")[0].strip()[:32] or "UNKNOWN"
        if not pair_addr:
            return None
        vol = attrs.get("volume_usd") or {}
        return DiscoveredPool(
            candidate=Candidate(
                mint=clean_mint,
                symbol=name,
                pair_address=pair_addr,
                source="geckoterminal:token_pools",
            ),
            price_usd=_safe_float(attrs.get("base_token_price_usd")),
            liquidity_usd=_safe_float(attrs.get("reserve_in_usd")),
            volume_1h_usd=_safe_float(vol.get("h1")),
            volume_24h_usd=_safe_float(vol.get("h24")),
            dex_id=str(((top.get("relationships") or {}).get("dex") or {}).get("data", {}).get("id") or "")
            or None,
            provenance="LIVE",
            raw_pair={
                "pairAddress": pair_addr,
                "reserveUsd": attrs.get("reserve_in_usd"),
                "volume1hUsd": vol.get("h1"),
                "fetchedAt": _utc_now_iso(),
            },
        )


class SolanaRpcReadOnlyAdapter:
    """Read-only Solana JSON-RPC client for mint/freeze authority and top-10 holder concentration."""

    def __init__(
        self,
        rpc_url: str = "https://api.mainnet-beta.solana.com",
        timeout_seconds: float = 8.0,
    ) -> None:
        self.rpc_url = rpc_url
        self.timeout_seconds = timeout_seconds

    async def _rpc_call(self, client: httpx.AsyncClient, method: str, params: list[Any]) -> Any:
        resp = await client.post(
            self.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        return payload.get("result")

    async def inspect_mint(self, mint: str) -> dict[str, Any]:
        checked_at = _utc_now_iso()
        out: dict[str, Any] = {
            "source": "solana_rpc_mainnet",
            "checked_at": checked_at,
            "mint": mint,
            "mint_authority_revoked": None,
            "freeze_authority_revoked": None,
            "top10_holders_pct": None,
            "non_transferable": False,
            "transfer_fee_bps": None,
            "slot": None,
            "error": None,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, headers={"User-Agent": DEFAULT_USER_AGENT}
            ) as client:
                acc_res, sup_res, large_res = await asyncio.gather(
                    self._rpc_call(client, "getAccountInfo", [mint, {"encoding": "jsonParsed"}]),
                    self._rpc_call(client, "getTokenSupply", [mint]),
                    self._rpc_call(client, "getTokenLargestAccounts", [mint]),
                )
            val = (acc_res or {}).get("value")
            if isinstance(val, dict):
                out["slot"] = (acc_res or {}).get("context", {}).get("slot")
                parsed = ((val.get("data") or {}).get("parsed") or {})
                info = parsed.get("info") or {}
                if parsed.get("type") == "mint":
                    out["mint_authority_revoked"] = info.get("mintAuthority") is None
                    out["freeze_authority_revoked"] = info.get("freezeAuthority") is None
                    extensions = info.get("extensions") or []
                    for ext in extensions:
                        if not isinstance(ext, dict):
                            continue
                        if ext.get("extension") == "nonTransferable":
                            out["non_transferable"] = True
                        if ext.get("extension") == "transferFeeConfig":
                            newer = (ext.get("state") or {}).get("newerTransferFee") or {}
                            fee_bps = _safe_float(newer.get("transferFeeBasisPoints"))
                            if fee_bps is not None:
                                out["transfer_fee_bps"] = fee_bps

            total_ui = _safe_float(((sup_res or {}).get("value") or {}).get("uiAmount"))
            largest = (large_res or {}).get("value") or []
            if total_ui and total_ui > 0 and isinstance(largest, list) and len(largest) > 0:
                top10_sum = sum(float(_safe_float(a.get("uiAmount")) or 0.0) for a in largest[:10])
                pct = min(100.0, max(0.0, round((top10_sum / total_ui) * 100.0, 2)))
                out["top10_holders_pct"] = pct
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {str(exc)[:140]}"
        return out


class RugCheckReadOnlyAdapter:
    """Optional free public RugCheck summary reader for LP lock % and creator risk checks."""

    BASE_URL = "https://api.rugcheck.xyz/v1"

    def __init__(self, timeout_seconds: float = 8.0) -> None:
        self.timeout_seconds = timeout_seconds

    async def inspect_token(self, mint: str) -> dict[str, Any]:
        checked_at = _utc_now_iso()
        out: dict[str, Any] = {
            "source": "rugcheck_public_v1",
            "checked_at": checked_at,
            "lp_locked_pct": None,
            "dev_wallet_clear": None,
            "rugged": None,
            "risk_names": [],
            "error": None,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, headers={"User-Agent": DEFAULT_USER_AGENT}
            ) as client:
                resp = await client.get(f"{self.BASE_URL}/tokens/{mint}/report/summary")
                resp.raise_for_status()
                data = resp.json() or {}
            lp_pct = _safe_float(data.get("lpLockedPct"))
            if lp_pct is not None and 0.0 <= lp_pct <= 100.0:
                out["lp_locked_pct"] = round(lp_pct, 2)
            rugged = data.get("rugged")
            risks = data.get("risks") or []
            risk_names = [str(r.get("name") or "") for r in risks if isinstance(r, dict)]
            out["risk_names"] = risk_names
            out["rugged"] = bool(rugged) if rugged is not None else None

            creator_flags = [
                n
                for n in risk_names
                if any(
                    k in n.lower()
                    for k in ("creator", "insider", "rug", "freeze", "mint authority", "mutable")
                )
            ]
            if rugged is True or creator_flags:
                out["dev_wallet_clear"] = False
            elif "lpLockedPct" in data:
                # Explicitly clear only when summary report loaded cleanly and flagged zero creator/rug risks
                out["dev_wallet_clear"] = True
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {str(exc)[:140]}"
        return out


class JupiterQuoteReadOnlyAdapter:
    """Read-only sell-route / honeypot quote probe via public Jupiter quote endpoint.

    NEVER builds swap transactions, NEVER accesses private keys, NEVER signs anything.
    """

    QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"

    def __init__(self, timeout_seconds: float = 6.0) -> None:
        self.timeout_seconds = timeout_seconds

    async def probe_sell_route(self, mint: str, *, amount_raw: int = 1_000_000) -> dict[str, Any]:
        checked_at = _utc_now_iso()
        out: dict[str, Any] = {
            "source": "jupiter_readonly_quote",
            "checked_at": checked_at,
            "sell_simulation_passed": None,
            "out_amount": None,
            "price_impact_pct": None,
            "error": None,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, headers={"User-Agent": DEFAULT_USER_AGENT}
            ) as client:
                resp = await client.get(
                    self.QUOTE_URL,
                    params={
                        "inputMint": mint,
                        "outputMint": WSOL_MINT,
                        "amount": str(max(1, amount_raw)),
                        "slippageBps": "300",
                    },
                )
                if resp.status_code == 400:
                    out["sell_simulation_passed"] = False
                    out["error"] = "NO_SELL_ROUTE"
                    return out
                resp.raise_for_status()
                data = resp.json() or {}
            out_amt = _safe_float(data.get("outAmount"))
            impact = _safe_float(data.get("priceImpactPct"))
            out["out_amount"] = out_amt
            out["price_impact_pct"] = impact
            if out_amt is not None and out_amt > 0:
                out["sell_simulation_passed"] = True
            else:
                out["sell_simulation_passed"] = False
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {str(exc)[:140]}"
        return out


class SolanaEvidenceCollector:
    """Aggregates timestamped evidence across replaceable Solana read-only adapters."""

    def __init__(
        self,
        *,
        dex_adapter: DexScreenerSolanaAdapter | None = None,
        gecko_adapter: GeckoTerminalSolanaAdapter | None = None,
        rpc_adapter: SolanaRpcReadOnlyAdapter | None = None,
        rugcheck_adapter: RugCheckReadOnlyAdapter | None = None,
        jupiter_adapter: JupiterQuoteReadOnlyAdapter | None = None,
    ) -> None:
        self.dex = dex_adapter or DexScreenerSolanaAdapter()
        self.gecko = gecko_adapter or GeckoTerminalSolanaAdapter()
        self.rpc = rpc_adapter or SolanaRpcReadOnlyAdapter()
        self.rugcheck = rugcheck_adapter or RugCheckReadOnlyAdapter()
        self.jupiter = jupiter_adapter or JupiterQuoteReadOnlyAdapter()

    async def collect(
        self, mint: str, *, pool_hint: DiscoveredPool | None = None
    ) -> tuple[DiscoveredPool | None, FilterEvidence, dict[str, Any]]:
        clean_mint = mint.strip()
        dex_pool_coro = (
            asyncio.sleep(0, result=pool_hint)
            if pool_hint is not None
            else self.dex.resolve_mint(clean_mint)
        )
        pool, rpc_info, rug_info, jup_info = await asyncio.gather(
            dex_pool_coro,
            self.rpc.inspect_mint(clean_mint),
            self.rugcheck.inspect_token(clean_mint),
            self.jupiter.probe_sell_route(clean_mint),
        )

        # Fallback secondary pool lookup if DexScreener had no pair
        if pool is None:
            try:
                pool = await self.gecko.fetch_token_pools(clean_mint)
            except Exception:
                pool = None

        sources: list[str] = []
        if pool is not None:
            sources.append(f"{pool.candidate.source}@{pool.raw_pair.get('fetchedAt', _utc_now_iso())}")
        if not rpc_info.get("error"):
            sources.append(f"{rpc_info['source']}@{rpc_info['checked_at']}")
        if not rug_info.get("error"):
            sources.append(f"{rug_info['source']}@{rug_info['checked_at']}")
        if not jup_info.get("error"):
            sources.append(f"{jup_info['source']}@{jup_info['checked_at']}")

        sell_ok: bool | None = jup_info.get("sell_simulation_passed")
        if rpc_info.get("non_transferable"):
            sell_ok = False
        transfer_fee = rpc_info.get("transfer_fee_bps")
        if transfer_fee is not None and transfer_fee >= 1000:
            sell_ok = False

        evidence = FilterEvidence(
            mint_authority_revoked=rpc_info.get("mint_authority_revoked"),
            freeze_authority_revoked=rpc_info.get("freeze_authority_revoked"),
            lp_locked_pct=rug_info.get("lp_locked_pct"),
            top10_holders_pct=rpc_info.get("top10_holders_pct"),
            volume_1h_usd=pool.volume_1h_usd if pool is not None else None,
            sell_simulation_passed=sell_ok,
            dev_wallet_clear=rug_info.get("dev_wallet_clear"),
            evidence_sources=sources,
        )

        raw_bundle = {
            "collected_at": _utc_now_iso(),
            "mint": clean_mint,
            "pool": pool.raw_pair if pool is not None else None,
            "rpc": rpc_info,
            "rugcheck": rug_info,
            "jupiter_quote": jup_info,
            "provenance": "LIVE" if sources else "UNAVAILABLE",
        }
        return pool, evidence, raw_bundle
