"""Multi-tier persistent structured memory manager backed by DuckDB.

Supports:
- Five memory tiers: EPISODIC, SEMANTIC, PROCEDURAL, WORKING, AGENT_PERF
- Keyed deduplication & evidence union merging
- Deterministic contradiction detection (prevents silent truth overwrite)
- Evidence-gated promotion with anti-self-promotion guard
- Immutable point-in-time version history & recovery
- Relevance-scored retrieval & TTL/retention archival
"""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from backend.storage.duckdb_store import DuckDBStore, utc_now_iso

MemoryTier = Literal["EPISODIC", "SEMANTIC", "PROCEDURAL", "WORKING", "AGENT_PERF"]
MemoryStatus = Literal[
    "CANDIDATE",
    "VERIFIED_LESSON",
    "CONTRADICTED",
    "REJECTED",
    "ARCHIVED",
    "SUPERSEDED",
]
RetentionPolicy = Literal["PERMANENT", "STANDARD_90D", "WORKING_24H", "ARCHIVAL"]

VALID_TIERS = {"EPISODIC", "SEMANTIC", "PROCEDURAL", "WORKING", "AGENT_PERF"}
VALID_STATUSES = {
    "CANDIDATE",
    "VERIFIED_LESSON",
    "CONTRADICTED",
    "REJECTED",
    "ARCHIVED",
    "SUPERSEDED",
}

_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "is", "it", "of", "on", "or", "that", "the", "to", "was", "with",
}


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9_.-]+", (text or "").lower())
    return {w for w in words if len(w) > 1 and w not in _STOP_WORDS}


def normalize_memory_key(
    *,
    memory_tier: str,
    category: str,
    title: str,
    explicit_key: str | None = None,
) -> str:
    if explicit_key and explicit_key.strip():
        return explicit_key.strip().lower()[:120]
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return f"{memory_tier.lower()}:{category.lower()}:{slug[:72]}"


def normalize_evidence_list(evidence: Any) -> list[dict[str, Any]]:
    if evidence is None:
        return []
    if isinstance(evidence, list):
        return [dict(item) for item in evidence if isinstance(item, dict) and item]
    if isinstance(evidence, dict) and evidence:
        return [dict(evidence)]
    return []


def has_verifiable_evidence(evidence_list: list[dict[str, Any]]) -> bool:
    if not evidence_list:
        return False
    for ev in evidence_list:
        has_source = bool(str(ev.get("source_type") or ev.get("endpoint") or ev.get("formula") or "").strip())
        has_ref = bool(
            str(ev.get("source_ref_id") or ev.get("detail") or ev.get("payload") or ev.get("invariant") or "").strip()
        )
        if has_source and has_ref:
            return True
        if len(ev.keys()) >= 2:
            return True
    return False


def _claims_contradict(
    new_content: dict[str, Any],
    new_summary: str,
    existing_content: dict[str, Any],
    existing_summary: str,
) -> tuple[bool, str]:
    """Detect whether two entries make conflicting assertions on the same subject."""
    c_key_new = str(new_content.get("claim_key") or "").strip().lower()
    c_key_old = str(existing_content.get("claim_key") or "").strip().lower()
    if c_key_new and c_key_old and c_key_new == c_key_old:
        val_new = new_content.get("claim_value")
        val_old = existing_content.get("claim_value")
        if val_new is not None and val_old is not None and str(val_new).strip().lower() != str(val_old).strip().lower():
            return True, f"Conflicting claim_value for '{c_key_new}': '{val_new}' vs '{val_old}'"

    stance_new = str(new_content.get("stance") or "").strip().upper()
    stance_old = str(existing_content.get("stance") or "").strip().upper()
    opposites = {
        ("ALLOW", "BLOCK"),
        ("BLOCK", "ALLOW"),
        ("TRUE", "FALSE"),
        ("FALSE", "TRUE"),
        ("POSITIVE", "NEGATIVE"),
        ("NEGATIVE", "POSITIVE"),
        ("ASC", "DESC"),
        ("DESC", "ASC"),
    }
    if (stance_new, stance_old) in opposites:
        return True, f"Opposing stance '{stance_new}' vs '{stance_old}'"

    # Heuristic text polarity check for explicit negation keywords on identical memory keys
    neg_pairs = [
        ("ascending", "descending"),
        ("enabled", "disabled"),
        ("equivalent", "not equivalent"),
        ("valid", "invalid"),
        ("required", "not required"),
    ]
    low_new = (new_summary or "").lower()
    low_old = (existing_summary or "").lower()
    for a, b in neg_pairs:
        if (a in low_new and b in low_old and a not in low_old) or (
            b in low_new and a in low_old and b not in low_old
        ):
            return True, f"Opposing polarity terms ('{a}' vs '{b}')"

    return False, ""


class StructuredMemoryManager:
    """Manages multi-tier DuckDB memory, deduplication, contradiction checks, and versioning."""

    def __init__(self, store: DuckDBStore) -> None:
        self.store = store

    def _row_to_entry(self, cols: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
        d = dict(zip(cols, row))
        d["content"] = json.loads(d.pop("content_json", "{}"))
        d["evidence"] = json.loads(d.pop("evidence_json", "[]"))
        d["tags"] = json.loads(d.pop("tags_json", "[]"))
        return d

    def _write_version_snapshot(
        self,
        *,
        memory_id: str,
        version: int,
        title: str,
        summary: str,
        content_json: str,
        evidence_json: str,
        confidence: float,
        status: str,
        change_reason: str,
        changed_by: str,
        created_at: str,
    ) -> str:
        version_id = f"MVER-{uuid.uuid4().hex[:10].upper()}"
        self.store._conn.execute(
            """
            INSERT INTO memory_versions VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            [
                version_id,
                memory_id,
                int(version),
                title,
                summary,
                content_json,
                evidence_json,
                float(confidence),
                status,
                change_reason,
                changed_by,
                created_at,
            ],
        )
        return version_id

    def get_entry(self, memory_id: str) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT * FROM memory_entries WHERE memory_id = ?",
                [memory_id],
            ).fetchone()
            if not row:
                return None
            cols = [d[0] for d in self.store._conn.description]
            return self._row_to_entry(cols, row)

    def list_entries(
        self,
        *,
        memory_tier: str | None = None,
        status: str | None = None,
        category: str | None = None,
        include_archived: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if memory_tier:
            clauses.append("memory_tier = ?")
            params.append(memory_tier.upper())
        if status:
            clauses.append("status = ?")
            params.append(status.upper())
        elif not include_archived:
            clauses.append("status NOT IN ('ARCHIVED', 'SUPERSEDED')")
        if category:
            clauses.append("category = ?")
            params.append(category)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM memory_entries {where_sql} ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self.store._lock:
            rows = self.store._conn.execute(sql, params).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            return [self._row_to_entry(cols, r) for r in rows]

    def record_candidate(
        self,
        *,
        memory_tier: MemoryTier,
        category: str,
        title: str,
        summary: str,
        source_agent: str,
        content: dict[str, Any] | None = None,
        evidence: list[dict[str, Any]] | dict[str, Any] | None = None,
        tags: list[str] | None = None,
        confidence: float = 0.65,
        explicit_key: str | None = None,
        retention_policy: RetentionPolicy | None = None,
        ttl_hours: float | None = None,
    ) -> dict[str, Any]:
        """Record a structured memory entry as CANDIDATE (or CONTRADICTED / deduplicated merge).

        Never allows direct autonomous insertion as VERIFIED_LESSON; promotion requires
        explicit verification through verify_and_promote().
        """
        tier_norm = memory_tier.upper()
        if tier_norm not in VALID_TIERS:
            raise ValueError(f"Unsupported memory_tier '{memory_tier}'")

        conf_raw = float(confidence)
        if not math.isfinite(conf_raw):
            raise ValueError("confidence must be a finite float")
        if ttl_hours is not None and not math.isfinite(float(ttl_hours)):
            raise ValueError("ttl_hours must be a finite float")
        conf = round(max(0.0, min(1.0, conf_raw)), 4)
        content_obj = dict(content or {})
        evidence_list = normalize_evidence_list(evidence)
        tags_list = sorted({str(t).strip().lower() for t in (tags or []) if str(t).strip()})
        if category.lower() not in tags_list:
            tags_list.append(category.lower())

        m_key = normalize_memory_key(
            memory_tier=tier_norm,
            category=category,
            title=title,
            explicit_key=explicit_key,
        )
        subject_key = str(content_obj.get("subject_key") or m_key).strip().lower()
        content_obj.setdefault("subject_key", subject_key)

        default_ret: RetentionPolicy = (
            "WORKING_24H"
            if tier_norm == "WORKING"
            else ("PERMANENT" if tier_norm in ("SEMANTIC", "PROCEDURAL") else "STANDARD_90D")
        )
        ret_policy = retention_policy or default_ret
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        expires_at: str | None = None
        if ttl_hours is not None and ttl_hours > 0:
            expires_at = (now_dt + timedelta(hours=ttl_hours)).isoformat()
        elif ret_policy == "WORKING_24H":
            expires_at = (now_dt + timedelta(hours=24)).isoformat()

        with self.store._lock:
            # Look for existing active entries sharing memory_key, subject_key, or claim_key
            rows = self.store._conn.execute(
                """
                SELECT * FROM memory_entries
                WHERE status NOT IN ('ARCHIVED', 'SUPERSEDED', 'REJECTED')
                """
            ).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            candidates = [self._row_to_entry(cols, r) for r in rows]

            new_tokens = _tokenize(title)
            claim_key_new = str(content_obj.get("claim_key") or "").strip().lower()

            matched_same_key: dict[str, Any] | None = None
            contradicted_entry: dict[str, Any] | None = None
            contradiction_reason = ""

            for ex in candidates:
                ex_content = ex["content"]
                ex_subject = str(ex_content.get("subject_key") or ex["memory_key"]).strip().lower()
                ex_claim_key = str(ex_content.get("claim_key") or "").strip().lower()

                same_tier_cat = ex["memory_tier"] == tier_norm and ex["category"] == category
                same_key = ex["memory_key"] == m_key or (same_tier_cat and ex_subject == subject_key)
                same_claim = bool(claim_key_new and ex_claim_key and claim_key_new == ex_claim_key)
                ex_tokens = _tokenize(ex["title"])
                jaccard = (
                    len(new_tokens & ex_tokens) / len(new_tokens | ex_tokens)
                    if (new_tokens and ex_tokens and same_tier_cat)
                    else 0.0
                )

                if same_key or same_claim or jaccard >= 0.85:
                    is_contra, reason = _claims_contradict(
                        content_obj, summary, ex_content, ex["summary"]
                    )
                    if is_contra:
                        contradicted_entry = ex
                        contradiction_reason = reason
                        break
                    if same_key or jaccard >= 0.85:
                        matched_same_key = ex

            # Contradiction detected -> preserve existing truth and persist CONTRADICTED record
            if contradicted_entry is not None:
                mem_id = f"SMEM-{uuid.uuid4().hex[:10].upper()}"
                content_obj["contradiction_reason"] = contradiction_reason
                content_str = json.dumps(content_obj)
                ev_str = json.dumps(evidence_list)
                tags_str = json.dumps(tags_list)
                self.store._conn.execute(
                    """
                    INSERT INTO memory_entries VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        mem_id,
                        m_key,
                        tier_norm,
                        category,
                        title,
                        summary,
                        content_str,
                        ev_str,
                        tags_str,
                        conf,
                        source_agent,
                        "CONTRADICTED",
                        1,
                        1,
                        contradicted_entry["memory_id"],
                        None,
                        ret_policy,
                        expires_at,
                        None,
                        now_iso,
                        now_iso,
                    ],
                )
                self._write_version_snapshot(
                    memory_id=mem_id,
                    version=1,
                    title=title,
                    summary=summary,
                    content_json=content_str,
                    evidence_json=ev_str,
                    confidence=conf,
                    status="CONTRADICTED",
                    change_reason=f"Contradicts {contradicted_entry['memory_id']}: {contradiction_reason}",
                    changed_by=source_agent,
                    created_at=now_iso,
                )
                entry = self.get_entry(mem_id)
                assert entry is not None
                return {
                    "action": "CONTRADICTION_FLAGGED",
                    "deduplicated": False,
                    "contradiction_detected": True,
                    "contradiction_reason": contradiction_reason,
                    "contradicted_memory_id": contradicted_entry["memory_id"],
                    "entry": entry,
                }

            # Deduplicate -> merge into existing entry and increment version & occurrence_count
            if matched_same_key is not None:
                existing = matched_same_key
                merged_evidence = list(existing["evidence"])
                seen_ev = {json.dumps(e, sort_keys=True) for e in merged_evidence}
                for ev in evidence_list:
                    key_str = json.dumps(ev, sort_keys=True)
                    if key_str not in seen_ev:
                        merged_evidence.append(ev)
                        seen_ev.add(key_str)

                merged_tags = sorted(set(existing["tags"]) | set(tags_list))
                merged_content = {**existing["content"], **content_obj}
                new_version = int(existing["version"]) + 1
                new_occ = int(existing["occurrence_count"]) + 1
                new_conf = round(min(0.99, max(float(existing["confidence"]), conf) + 0.03), 4)
                merged_summary = summary if len(summary) > len(existing["summary"]) else existing["summary"]

                c_str = json.dumps(merged_content)
                e_str = json.dumps(merged_evidence)
                t_str = json.dumps(merged_tags)

                self.store._conn.execute(
                    """
                    UPDATE memory_entries SET
                        summary = ?,
                        content_json = ?,
                        evidence_json = ?,
                        tags_json = ?,
                        confidence = ?,
                        version = ?,
                        occurrence_count = ?,
                        updated_at = ?
                    WHERE memory_id = ?
                    """,
                    [
                        merged_summary,
                        c_str,
                        e_str,
                        t_str,
                        new_conf,
                        new_version,
                        new_occ,
                        now_iso,
                        existing["memory_id"],
                    ],
                )
                self._write_version_snapshot(
                    memory_id=existing["memory_id"],
                    version=new_version,
                    title=existing["title"],
                    summary=merged_summary,
                    content_json=c_str,
                    evidence_json=e_str,
                    confidence=new_conf,
                    status=existing["status"],
                    change_reason=f"Deduplicated merge from {source_agent} (occurrence #{new_occ})",
                    changed_by=source_agent,
                    created_at=now_iso,
                )
                updated = self.get_entry(existing["memory_id"])
                assert updated is not None
                return {
                    "action": "DEDUPLICATED_MERGE",
                    "deduplicated": True,
                    "contradiction_detected": False,
                    "entry": updated,
                }

            # Insert brand-new CANDIDATE entry
            mem_id = f"SMEM-{uuid.uuid4().hex[:10].upper()}"
            c_str = json.dumps(content_obj)
            e_str = json.dumps(evidence_list)
            t_str = json.dumps(tags_list)
            self.store._conn.execute(
                """
                INSERT INTO memory_entries VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    mem_id,
                    m_key,
                    tier_norm,
                    category,
                    title,
                    summary,
                    c_str,
                    e_str,
                    t_str,
                    conf,
                    source_agent,
                    "CANDIDATE",
                    1,
                    1,
                    None,
                    None,
                    ret_policy,
                    expires_at,
                    None,
                    now_iso,
                    now_iso,
                ],
            )
            self._write_version_snapshot(
                memory_id=mem_id,
                version=1,
                title=title,
                summary=summary,
                content_json=c_str,
                evidence_json=e_str,
                confidence=conf,
                status="CANDIDATE",
                change_reason=f"Initial candidate recorded by {source_agent}",
                changed_by=source_agent,
                created_at=now_iso,
            )
            created = self.get_entry(mem_id)
            assert created is not None
            return {
                "action": "CREATED_CANDIDATE",
                "deduplicated": False,
                "contradiction_detected": False,
                "entry": created,
            }

    def verify_and_promote(
        self,
        memory_id: str,
        *,
        reviewer_id: str,
        review_reason: str = "Verified against supporting evidence",
        additional_evidence: list[dict[str, Any]] | dict[str, Any] | None = None,
        min_confidence: float = 0.60,
        allow_override_contradiction: bool = False,
    ) -> dict[str, Any]:
        """Promote a memory entry to VERIFIED_LESSON after independent evidence & safety checks."""
        with self.store._lock:
            entry = self.get_entry(memory_id)
            if not entry:
                raise ValueError(f"Unknown memory_id '{memory_id}'")

            if reviewer_id.strip().lower() == entry["source_agent"].strip().lower():
                raise PermissionError(
                    f"Self-promotion denied: agent '{reviewer_id}' cannot verify its own memory '{memory_id}'"
                )

            merged_ev = list(entry["evidence"])
            seen = {json.dumps(e, sort_keys=True) for e in merged_ev}
            for ev in normalize_evidence_list(additional_evidence):
                s = json.dumps(ev, sort_keys=True)
                if s not in seen:
                    merged_ev.append(ev)
                    seen.add(s)

            if not has_verifiable_evidence(merged_ev):
                raise ValueError(
                    f"Promotion denied for '{memory_id}': verifiable supporting evidence is required"
                )

            if entry["status"] == "CONTRADICTED" and not allow_override_contradiction:
                raise ValueError(
                    f"Promotion denied for '{memory_id}': entry is CONTRADICTED with '{entry.get('contradicts_id')}'"
                )

            if float(entry["confidence"]) < min_confidence:
                raise ValueError(
                    f"Promotion denied for '{memory_id}': confidence {entry['confidence']} < {min_confidence}"
                )

            now_iso = utc_now_iso()
            new_ver = int(entry["version"]) + 1
            c_str = json.dumps(entry["content"])
            e_str = json.dumps(merged_ev)

            self.store._conn.execute(
                """
                UPDATE memory_entries SET
                    evidence_json = ?,
                    status = 'VERIFIED_LESSON',
                    version = ?,
                    updated_at = ?
                WHERE memory_id = ?
                """,
                [e_str, new_ver, now_iso, memory_id],
            )
            self._write_version_snapshot(
                memory_id=memory_id,
                version=new_ver,
                title=entry["title"],
                summary=entry["summary"],
                content_json=c_str,
                evidence_json=e_str,
                confidence=entry["confidence"],
                status="VERIFIED_LESSON",
                change_reason=f"Promoted by {reviewer_id}: {review_reason}",
                changed_by=reviewer_id,
                created_at=now_iso,
            )
            updated = self.get_entry(memory_id)
            assert updated is not None
            return updated

    def update_entry(
        self,
        memory_id: str,
        *,
        changed_by: str,
        change_reason: str,
        title: str | None = None,
        summary: str | None = None,
        content: dict[str, Any] | None = None,
        evidence: list[dict[str, Any]] | dict[str, Any] | None = None,
        confidence: float | None = None,
        status: MemoryStatus | None = None,
    ) -> dict[str, Any]:
        """Update an existing memory entry while creating an immutable version snapshot."""
        with self.store._lock:
            entry = self.get_entry(memory_id)
            if not entry:
                raise ValueError(f"Unknown memory_id '{memory_id}'")

            new_title = title if title is not None else entry["title"]
            new_summary = summary if summary is not None else entry["summary"]
            new_content = {**entry["content"], **(content or {})}
            new_ev = (
                normalize_evidence_list(evidence)
                if evidence is not None
                else list(entry["evidence"])
            )
            if confidence is not None and not math.isfinite(float(confidence)):
                raise ValueError("confidence must be a finite float")
            new_conf = (
                round(max(0.0, min(1.0, float(confidence))), 4)
                if confidence is not None
                else float(entry["confidence"])
            )
            new_status = status if status is not None else entry["status"]
            if new_status not in VALID_STATUSES:
                raise ValueError(f"Invalid status '{new_status}'")
            if new_status == "VERIFIED_LESSON":
                if changed_by.strip().lower() == entry["source_agent"].strip().lower():
                    raise PermissionError("Agent cannot self-promote to VERIFIED_LESSON via update_entry")
                if not has_verifiable_evidence(new_ev):
                    raise ValueError("Cannot set VERIFIED_LESSON without verifiable evidence")

            new_ver = int(entry["version"]) + 1
            now_iso = utc_now_iso()
            c_str = json.dumps(new_content)
            e_str = json.dumps(new_ev)

            self.store._conn.execute(
                """
                UPDATE memory_entries SET
                    title = ?,
                    summary = ?,
                    content_json = ?,
                    evidence_json = ?,
                    confidence = ?,
                    status = ?,
                    version = ?,
                    updated_at = ?
                WHERE memory_id = ?
                """,
                [
                    new_title,
                    new_summary,
                    c_str,
                    e_str,
                    new_conf,
                    new_status,
                    new_ver,
                    now_iso,
                    memory_id,
                ],
            )
            self._write_version_snapshot(
                memory_id=memory_id,
                version=new_ver,
                title=new_title,
                summary=new_summary,
                content_json=c_str,
                evidence_json=e_str,
                confidence=new_conf,
                status=new_status,
                change_reason=change_reason,
                changed_by=changed_by,
                created_at=now_iso,
            )
            updated = self.get_entry(memory_id)
            assert updated is not None
            return updated

    def get_versions(self, memory_id: str) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT * FROM memory_versions WHERE memory_id = ? ORDER BY version ASC",
                [memory_id],
            ).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(zip(cols, r))
                d["content"] = json.loads(d.pop("content_json", "{}"))
                d["evidence"] = json.loads(d.pop("evidence_json", "[]"))
                out.append(d)
            return out

    def recover_version(
        self,
        memory_id: str,
        *,
        target_version: int,
        changed_by: str,
        reason: str = "Recover previous verified version",
    ) -> dict[str, Any]:
        """Restore memory fields from a historical version snapshot as a new incremented version."""
        with self.store._lock:
            entry = self.get_entry(memory_id)
            if not entry:
                raise ValueError(f"Unknown memory_id '{memory_id}'")
            row = self.store._conn.execute(
                "SELECT * FROM memory_versions WHERE memory_id = ? AND version = ?",
                [memory_id, int(target_version)],
            ).fetchone()
            if not row:
                raise ValueError(f"Version {target_version} not found for memory_id '{memory_id}'")
            cols = [d[0] for d in self.store._conn.description]
            snap = dict(zip(cols, row))

            new_ver = int(entry["version"]) + 1
            now_iso = utc_now_iso()
            self.store._conn.execute(
                """
                UPDATE memory_entries SET
                    title = ?,
                    summary = ?,
                    content_json = ?,
                    evidence_json = ?,
                    confidence = ?,
                    status = ?,
                    version = ?,
                    updated_at = ?
                WHERE memory_id = ?
                """,
                [
                    snap["title"],
                    snap["summary"],
                    snap["content_json"],
                    snap["evidence_json"],
                    float(snap["confidence"]),
                    snap["status"],
                    new_ver,
                    now_iso,
                    memory_id,
                ],
            )
            self._write_version_snapshot(
                memory_id=memory_id,
                version=new_ver,
                title=snap["title"],
                summary=snap["summary"],
                content_json=snap["content_json"],
                evidence_json=snap["evidence_json"],
                confidence=float(snap["confidence"]),
                status=snap["status"],
                change_reason=f"Recovered v{target_version}: {reason}",
                changed_by=changed_by,
                created_at=now_iso,
            )
            restored = self.get_entry(memory_id)
            assert restored is not None
            return restored

    def search_relevant(
        self,
        query: str = "",
        *,
        memory_tier: MemoryTier | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        statuses: list[MemoryStatus] | None = None,
        include_archived: bool = False,
        now_iso: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Deterministic relevance-scored retrieval across memory tiers."""
        now_ref = now_iso or utc_now_iso()
        candidates = self.list_entries(
            memory_tier=memory_tier,
            category=category,
            include_archived=include_archived,
            limit=500,
        )
        allowed_statuses = {s.upper() for s in statuses} if statuses else None
        filter_tags = {t.strip().lower() for t in (tags or []) if t.strip()}
        q_tokens = _tokenize(query)
        q_lower = (query or "").strip().lower()

        scored: list[tuple[float, dict[str, Any]]] = []
        for entry in candidates:
            if entry.get("expires_at") and str(entry["expires_at"]) <= now_ref:
                continue
            if allowed_statuses and entry["status"] not in allowed_statuses:
                continue

            entry_tags = {str(t).lower() for t in entry["tags"]}
            if filter_tags and not (entry_tags & filter_tags):
                continue

            haystack_tokens = (
                _tokenize(entry["title"])
                | _tokenize(entry["summary"])
                | _tokenize(entry["category"])
                | _tokenize(entry["memory_key"])
                | entry_tags
            )
            if q_tokens:
                overlap = len(q_tokens & haystack_tokens)
                lexical = overlap / max(1, len(q_tokens))
                if q_lower and (q_lower in entry["title"].lower() or q_lower in entry["summary"].lower()):
                    lexical += 0.25
                if lexical == 0.0:
                    continue
            else:
                lexical = 0.2

            status_bonus = {
                "VERIFIED_LESSON": 0.35,
                "CANDIDATE": 0.10,
                "CONTRADICTED": -0.25,
                "REJECTED": -0.50,
                "ARCHIVED": -0.60,
                "SUPERSEDED": -0.60,
            }.get(entry["status"], 0.0)

            conf_bonus = 0.25 * float(entry["confidence"])
            occ_bonus = min(0.15, 0.04 * math.log1p(int(entry["occurrence_count"])))
            tag_bonus = 0.20 if (filter_tags and (entry_tags & filter_tags)) else 0.0
            score = round(lexical + status_bonus + conf_bonus + occ_bonus + tag_bonus, 4)

            scored.append((score, {**entry, "relevance_score": score}))

        scored.sort(key=lambda pair: (pair[0], pair[1]["updated_at"]), reverse=True)
        return [item for _, item in scored[:limit]]

    def run_retention_sweep(
        self,
        *,
        now_iso: str | None = None,
        standard_retention_days: int = 90,
        actor: str = "retention_manager",
    ) -> dict[str, Any]:
        """Archive expired WORKING memories and stale non-permanent memories cleanly."""
        now_dt = (
            datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
            if now_iso
            else datetime.now(timezone.utc)
        )
        now_str = now_dt.isoformat()
        cutoff_str = (now_dt - timedelta(days=standard_retention_days)).isoformat()
        archived_ids: list[str] = []

        with self.store._lock:
            rows = self.store._conn.execute(
                """
                SELECT memory_id, retention_policy, expires_at, updated_at, status,
                       title, summary, content_json, evidence_json, confidence, version
                FROM memory_entries
                WHERE status NOT IN ('ARCHIVED', 'SUPERSEDED')
                """
            ).fetchall()

            for (
                mem_id,
                ret_policy,
                expires_at,
                updated_at,
                status,
                title,
                summary,
                c_str,
                e_str,
                conf,
                ver,
            ) in rows:
                should_archive = False
                reason = ""
                if expires_at and str(expires_at) <= now_str:
                    should_archive = True
                    reason = f"TTL expired at {expires_at}"
                elif ret_policy != "PERMANENT" and status != "VERIFIED_LESSON" and str(updated_at) < cutoff_str:
                    should_archive = True
                    reason = f"Retention policy {ret_policy} exceeded {standard_retention_days}d"

                if should_archive:
                    new_ver = int(ver) + 1
                    self.store._conn.execute(
                        """
                        UPDATE memory_entries SET
                            status = 'ARCHIVED',
                            version = ?,
                            updated_at = ?
                        WHERE memory_id = ?
                        """,
                        [new_ver, now_str, mem_id],
                    )
                    self._write_version_snapshot(
                        memory_id=mem_id,
                        version=new_ver,
                        title=title,
                        summary=summary,
                        content_json=c_str,
                        evidence_json=e_str,
                        confidence=float(conf),
                        status="ARCHIVED",
                        change_reason=reason,
                        changed_by=actor,
                        created_at=now_str,
                    )
                    archived_ids.append(mem_id)

        return {"archived_count": len(archived_ids), "archived_ids": archived_ids, "swept_at": now_str}
