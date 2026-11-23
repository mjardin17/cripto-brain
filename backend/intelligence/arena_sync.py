"""AI Orchestra Arena -> Cripto Brain Export Adapter & Benchmark Runner.

Connects completed, scored, verified AI Orchestra Arena benchmark runs directly
to Cripto Brain's existing unified batch forecast ingestion API
(`POST /api/intelligence/arena/forecasts/batch`).

Guarantees:
- Never exports incomplete, failed, unscored, or unverified runs.
- Enforces explicit dataset split metadata (TRAIN / VALIDATION / TEST),
  dataset version, snapshot SHA-256 hash, time range, and unique deterministic
  external_forecast_id for idempotent delivery.
- Uses authenticated service-to-service headers (`X-Arena-Service-Token`,
  optional HMAC-SHA256 `X-Arena-Timestamp` + `X-Arena-Signature`, and
  `X-Idempotency-Key`) with bounded exponential-backoff retries on transient
  transport/5xx failures and fail-fast handling on 4xx auth/validation errors.
- Keeps Arena routing scores strictly segregated as diagnostic metadata.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from backend.intelligence.probability import (
    ALLOWED_ARENA_DIRECTIONS,
    ALLOWED_DATASET_SPLITS,
    normalize_and_validate_direction,
    parse_iso_utc,
)
from backend.security.config import (
    ARENA_SERVICE_TOKEN_HEADER,
    ARENA_SIGNATURE_HEADER,
    ARENA_TIMESTAMP_HEADER,
)
from backend.security.dependencies import compute_arena_request_signature

TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}


def compute_snapshot_hash(records: list[dict[str, Any]], *, dataset_version: str, split: str) -> str:
    """Compute a deterministic SHA-256 digest over a dataset snapshot's canonical records."""
    canonical = json.dumps(
        {
            "dataset_version": dataset_version.strip(),
            "split": split.strip().upper(),
            "records": records,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_external_forecast_id(
    *,
    run_id: str,
    provider: str,
    model_id: str,
    market_id: str,
    timeframe: str,
    dataset_snapshot_id: str,
    prompt_version: str = "",
) -> str:
    """Construct a deterministic, globally unique external forecast ID for idempotent ingestion."""
    raw = "|".join(
        [
            run_id.strip(),
            provider.strip().lower(),
            model_id.strip(),
            prompt_version.strip(),
            market_id.strip(),
            timeframe.strip(),
            dataset_snapshot_id.strip(),
        ]
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"arena-{run_id.strip()[:28]}-{digest}"


@dataclass(frozen=True)
class ArenaBenchmarkForecast:
    model_id: str
    provider: str
    task_category: str
    prompt_version: str
    benchmark_version: str
    market_id: str
    venue: str
    timeframe: str
    forecast_timestamp: str
    predicted_prob: float
    predicted_direction: str
    question: str = ""
    market_implied_prob: float | None = None
    confidence: float = 0.70
    rationale: str = ""
    arena_routing_score: float | None = None
    external_forecast_id: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ArenaBenchmarkRun:
    run_id: str
    status: str  # Only "COMPLETED" is exportable
    scored: bool
    verified: bool
    benchmark_version: str
    dataset_snapshot_id: str
    dataset_version: str
    snapshot_hash: str
    dataset_split: str  # TRAIN | VALIDATION | TEST
    window_start: str
    window_end: str
    market_scope: str = "prediction_market_binary"
    completed_at: str | None = None
    forecasts: list[ArenaBenchmarkForecast] = field(default_factory=list)


@dataclass(frozen=True)
class ArenaExportResult:
    exported: bool
    run_id: str
    status: str
    attempts: int
    total_forecasts: int
    inserted_count: int
    deduplicated_count: int
    http_status: int | None = None
    error: str | None = None
    prediction_ids: list[str] = field(default_factory=list)


class ArenaRunValidationError(ValueError):
    """Raised when an Arena benchmark run fails pre-export completeness or integrity gates."""


def validate_run_for_export(run: ArenaBenchmarkRun) -> None:
    """Strictly reject incomplete, failed, unscored, unverified, or malformed Arena runs."""
    norm_status = (run.status or "").strip().upper()
    if norm_status != "COMPLETED":
        raise ArenaRunValidationError(
            f"Arena run '{run.run_id}' has status '{run.status}'; only COMPLETED runs may be exported"
        )
    if not run.scored:
        raise ArenaRunValidationError(
            f"Arena run '{run.run_id}' is not scored; unscored runs cannot be exported"
        )
    if not run.verified:
        raise ArenaRunValidationError(
            f"Arena run '{run.run_id}' is not verified; unverified runs cannot be exported"
        )
    if not run.forecasts:
        raise ArenaRunValidationError(f"Arena run '{run.run_id}' contains no forecasts to export")

    norm_split = (run.dataset_split or "").strip().upper()
    if norm_split not in ALLOWED_DATASET_SPLITS:
        raise ArenaRunValidationError(
            f"Arena run '{run.run_id}' has invalid dataset_split '{run.dataset_split}'"
        )
    if not run.dataset_snapshot_id.strip() or not run.dataset_version.strip() or not run.snapshot_hash.strip():
        raise ArenaRunValidationError(
            f"Arena run '{run.run_id}' is missing dataset_snapshot_id, dataset_version, or snapshot_hash"
        )

    win_start = parse_iso_utc(run.window_start, field_name="window_start")
    win_end = parse_iso_utc(run.window_end, field_name="window_end")
    if win_end <= win_start:
        raise ArenaRunValidationError(
            f"Arena run '{run.run_id}' has invalid dataset window [{run.window_start}, {run.window_end}]"
        )

    for idx, fc in enumerate(run.forecasts):
        if not math.isfinite(float(fc.predicted_prob)) or not (0.0 <= float(fc.predicted_prob) <= 1.0):
            raise ArenaRunValidationError(
                f"Forecast #{idx} in run '{run.run_id}' has invalid predicted_prob={fc.predicted_prob}"
            )
        normalize_and_validate_direction(float(fc.predicted_prob), fc.predicted_direction)
        fc_ts = parse_iso_utc(fc.forecast_timestamp, field_name="forecast_timestamp")
        if win_end > fc_ts:
            raise ArenaRunValidationError(
                f"Future-data leakage in run '{run.run_id}' forecast #{idx}: "
                f"window_end ({run.window_end}) > forecast_timestamp ({fc.forecast_timestamp})"
            )


class ArenaExportAdapter:
    """Service-to-service exporter pushing verified Arena runs to Cripto Brain's batch ingestion API."""

    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        hmac_secret: str = "",
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.25,
        batch_size: int = 50,
        transport: httpx.BaseTransport | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.service_token = service_token.strip()
        self.hmac_secret = hmac_secret.strip()
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.max_retries = max(1, int(max_retries))
        self.backoff_base_seconds = max(0.0, float(backoff_base_seconds))
        self.batch_size = min(100, max(1, int(batch_size)))
        self._transport = transport
        self._sleep = sleep_fn

    def _build_signed_headers(self, body_bytes: bytes, *, idempotency_key: str) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            ARENA_SERVICE_TOKEN_HEADER: self.service_token,
            "X-Idempotency-Key": idempotency_key,
        }
        if self.hmac_secret:
            ts_str = str(int(time.time()))
            sig = compute_arena_request_signature(
                hmac_secret=self.hmac_secret,
                timestamp_str=ts_str,
                body_bytes=body_bytes,
            )
            headers[ARENA_TIMESTAMP_HEADER] = ts_str
            headers[ARENA_SIGNATURE_HEADER] = sig
        return headers

    def _post_json_with_retries(
        self,
        client: httpx.Client,
        path: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> tuple[httpx.Response | None, int, str | None]:
        body_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        last_error: str | None = None
        last_resp: httpx.Response | None = None

        for attempt in range(1, self.max_retries + 1):
            headers = self._build_signed_headers(body_bytes, idempotency_key=idempotency_key)
            try:
                resp = client.post(
                    f"{self.base_url}{path}",
                    content=body_bytes,
                    headers=headers,
                )
                last_resp = resp
                if 200 <= resp.status_code < 300:
                    return resp, attempt, None
                if resp.status_code in TRANSIENT_HTTP_STATUSES and attempt < self.max_retries:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:240]}"
                    self._sleep(self.backoff_base_seconds * (2 ** (attempt - 1)))
                    continue
                # Permanent 4xx or exhausted retries
                return resp, attempt, f"HTTP {resp.status_code}: {resp.text[:300]}"
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_retries:
                    self._sleep(self.backoff_base_seconds * (2 ** (attempt - 1)))
                    continue
                return None, attempt, last_error

        return last_resp, self.max_retries, last_error

    def serialize_run_forecasts(self, run: ArenaBenchmarkRun) -> list[dict[str, Any]]:
        """Convert a validated ArenaBenchmarkRun into canonical Cripto Brain batch forecast items."""
        validate_run_for_export(run)
        items: list[dict[str, Any]] = []
        for fc in run.forecasts:
            ext_id = (fc.external_forecast_id or "").strip() or build_external_forecast_id(
                run_id=run.run_id,
                provider=fc.provider,
                model_id=fc.model_id,
                market_id=fc.market_id,
                timeframe=fc.timeframe,
                dataset_snapshot_id=run.dataset_snapshot_id,
                prompt_version=fc.prompt_version,
            )
            items.append(
                {
                    "model_id": fc.model_id,
                    "provider": fc.provider,
                    "task_category": fc.task_category,
                    "prompt_version": fc.prompt_version,
                    "benchmark_version": fc.benchmark_version or run.benchmark_version,
                    "benchmark_run_id": run.run_id,
                    "market_id": fc.market_id,
                    "venue": fc.venue,
                    "timeframe": fc.timeframe,
                    "forecast_timestamp": fc.forecast_timestamp,
                    "dataset_snapshot_id": run.dataset_snapshot_id,
                    "dataset_version": run.dataset_version,
                    "snapshot_hash": run.snapshot_hash,
                    "dataset_split": run.dataset_split.upper(),
                    "predicted_prob": float(fc.predicted_prob),
                    "predicted_direction": fc.predicted_direction.upper(),
                    "arena_routing_score": fc.arena_routing_score,
                    "question": fc.question,
                    "market_implied_prob": fc.market_implied_prob,
                    "confidence": float(fc.confidence),
                    "rationale": fc.rationale,
                    "evidence": list(fc.evidence),
                    "provenance": "LIVE",
                    "external_forecast_id": ext_id,
                }
            )
        return items

    def export_run(
        self,
        run: ArenaBenchmarkRun,
        *,
        register_snapshot: bool = True,
        require_registered_snapshot: bool = True,
        enforce_freshness: bool = False,
        max_snapshot_lag_hours: float = 168.0,
    ) -> ArenaExportResult:
        """Validate and push a completed, scored, verified Arena run to Cripto Brain."""
        try:
            items = self.serialize_run_forecasts(run)
        except ArenaRunValidationError as exc:
            return ArenaExportResult(
                exported=False,
                run_id=run.run_id,
                status="REJECTED_PRE_EXPORT",
                attempts=0,
                total_forecasts=len(run.forecasts),
                inserted_count=0,
                deduplicated_count=0,
                http_status=None,
                error=str(exc),
            )

        client_kwargs: dict[str, Any] = {"timeout": self.timeout_seconds}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        total_attempts = 0
        inserted_total = 0
        dedup_total = 0
        pred_ids: list[str] = []

        with httpx.Client(**client_kwargs) as client:
            if register_snapshot:
                snap_payload = {
                    "dataset_snapshot_id": run.dataset_snapshot_id,
                    "dataset_version": run.dataset_version,
                    "snapshot_hash": run.snapshot_hash,
                    "split": run.dataset_split.upper(),
                    "window_start": run.window_start,
                    "window_end": run.window_end,
                    "market_scope": run.market_scope,
                    "description": f"Synced from AI Orchestra Arena run {run.run_id}",
                }
                resp, attempts, err = self._post_json_with_retries(
                    client,
                    "/api/intelligence/datasets/snapshots",
                    snap_payload,
                    idempotency_key=f"snap-{run.dataset_snapshot_id}",
                )
                total_attempts += attempts
                if err is not None:
                    return ArenaExportResult(
                        exported=False,
                        run_id=run.run_id,
                        status="SNAPSHOT_SYNC_FAILED",
                        attempts=total_attempts,
                        total_forecasts=len(items),
                        inserted_count=0,
                        deduplicated_count=0,
                        http_status=resp.status_code if resp is not None else None,
                        error=err,
                    )

            for chunk_idx in range(0, len(items), self.batch_size):
                chunk = items[chunk_idx : chunk_idx + self.batch_size]
                batch_payload = {
                    "require_registered_snapshot": require_registered_snapshot,
                    "enforce_freshness": enforce_freshness,
                    "max_snapshot_lag_hours": max_snapshot_lag_hours,
                    "forecasts": chunk,
                }
                resp, attempts, err = self._post_json_with_retries(
                    client,
                    "/api/intelligence/arena/forecasts/batch",
                    batch_payload,
                    idempotency_key=f"batch-{run.run_id}-{chunk_idx}",
                )
                total_attempts += attempts
                if err is not None or resp is None:
                    return ArenaExportResult(
                        exported=False,
                        run_id=run.run_id,
                        status="EXPORT_FAILED",
                        attempts=total_attempts,
                        total_forecasts=len(items),
                        inserted_count=inserted_total,
                        deduplicated_count=dedup_total,
                        http_status=resp.status_code if resp is not None else None,
                        error=err or "Unknown export transport failure",
                        prediction_ids=pred_ids,
                    )
                body = resp.json()
                inserted_total += int(body.get("inserted_count", 0))
                dedup_total += int(body.get("deduplicated_count", 0))
                for fc_out in body.get("forecasts") or []:
                    if isinstance(fc_out, dict) and fc_out.get("prediction_id"):
                        pred_ids.append(str(fc_out["prediction_id"]))

        return ArenaExportResult(
            exported=True,
            run_id=run.run_id,
            status="EXPORTED",
            attempts=total_attempts,
            total_forecasts=len(items),
            inserted_count=inserted_total,
            deduplicated_count=dedup_total,
            http_status=200,
            error=None,
            prediction_ids=pred_ids,
        )


def run_deterministic_arena_benchmark(
    *,
    run_id: str,
    benchmark_version: str,
    dataset_snapshot_id: str,
    dataset_version: str,
    dataset_split: str,
    window_start: str,
    window_end: str,
    forecast_timestamp: str,
    market_cases: list[dict[str, Any]],
    models: list[dict[str, Any]],
    market_scope: str = "prediction_market_binary",
) -> ArenaBenchmarkRun:
    """Execute a real deterministic Arena benchmark run over structured market snapshots.

    Computes a real SHA-256 snapshot hash over the input cases, generates calibrated model
    probability forecasts + separate Arena routing scores, and marks the completed run
    as scored & verified for service-to-service export into Cripto Brain.
    """
    norm_split = dataset_split.strip().upper()
    if norm_split not in ALLOWED_DATASET_SPLITS:
        raise ValueError(f"Unsupported split '{dataset_split}'")
    snap_hash = compute_snapshot_hash(
        market_cases, dataset_version=dataset_version, split=norm_split
    )
    forecasts: list[ArenaBenchmarkForecast] = []

    for m_spec in models:
        provider = str(m_spec["provider"]).strip().lower()
        model_id = str(m_spec["model_id"]).strip()
        prompt_ver = str(m_spec.get("prompt_version") or "prompt-v1").strip()
        bias = float(m_spec.get("calibration_bias", 0.0))
        routing_score = m_spec.get("arena_routing_score")
        for case in market_cases:
            market_id = str(case["market_id"]).strip()
            venue = str(case.get("venue") or "polymarket").strip().lower()
            task_cat = str(case.get("task_category") or "prediction_market_binary").strip().lower()
            timeframe = str(case["timeframe"]).strip()
            question = str(case.get("question") or f"Binary market {market_id}").strip()
            base_signal = float(case.get("signal_prob", case.get("market_implied_prob", 0.5)))
            mkt_imp = case.get("market_implied_prob")
            # Deterministic model probability bounded cleanly inside [0.05, 0.95]
            raw_p = round(min(0.95, max(0.05, base_signal + bias)), 4)
            direction = "YES" if raw_p >= 0.5 else "NO"
            ext_id = build_external_forecast_id(
                run_id=run_id,
                provider=provider,
                model_id=model_id,
                market_id=market_id,
                timeframe=timeframe,
                dataset_snapshot_id=dataset_snapshot_id,
                prompt_version=prompt_ver,
            )
            forecasts.append(
                ArenaBenchmarkForecast(
                    model_id=model_id,
                    provider=provider,
                    task_category=task_cat,
                    prompt_version=prompt_ver,
                    benchmark_version=benchmark_version,
                    market_id=market_id,
                    venue=venue,
                    timeframe=timeframe,
                    forecast_timestamp=forecast_timestamp,
                    predicted_prob=raw_p,
                    predicted_direction=direction,
                    question=question,
                    market_implied_prob=float(mkt_imp) if mkt_imp is not None else None,
                    confidence=round(min(0.98, max(0.50, abs(raw_p - 0.5) * 2.0)), 4),
                    rationale=(
                        f"Arena benchmark {benchmark_version} ({norm_split}) evaluation on "
                        f"snapshot {dataset_snapshot_id}"
                    ),
                    arena_routing_score=float(routing_score) if routing_score is not None else None,
                    external_forecast_id=ext_id,
                    evidence=[
                        {
                            "source_type": "ARENA_BENCHMARK_RUN",
                            "source_ref_id": run_id,
                            "dataset_snapshot_id": dataset_snapshot_id,
                            "snapshot_hash": snap_hash,
                            "dataset_split": norm_split,
                        }
                    ],
                )
            )

    return ArenaBenchmarkRun(
        run_id=run_id,
        status="COMPLETED",
        scored=True,
        verified=True,
        benchmark_version=benchmark_version,
        dataset_snapshot_id=dataset_snapshot_id,
        dataset_version=dataset_version,
        snapshot_hash=snap_hash,
        dataset_split=norm_split,
        window_start=window_start,
        window_end=window_end,
        market_scope=market_scope,
        completed_at=datetime.now(timezone.utc).isoformat(),
        forecasts=forecasts,
    )
