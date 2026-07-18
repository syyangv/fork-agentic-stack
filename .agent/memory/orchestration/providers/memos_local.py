"""Shadow-mode behavioral provider for the pinned MemOS local bridge."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .._core import canonical_json, contains_sensitive_plaintext, redact
from ..contracts import ContractError, EventEnvelope, ProvenanceRef, RetrievalItem
from ..memos_bridge import (
    MemOSTimeoutError, MemOSTransportError, MemOSUnavailableError,
)
from ..memos_journal import MemosDeliveryJournal


_UPSTREAM_HEAVY_TIMEOUT = 75.0
_UPSTREAM_FINALIZE_TIMEOUT = 15.0
_UPSTREAM_COLD_START_TIMEOUT = 75.0
_ASSIST_RETRIEVAL_TIMEOUT = 0.5
_ASSIST_TOTAL_TIMEOUT = 0.7
_UPSTREAM_HEAVY_METHODS = frozenset({
    "turn.start", "turn.end", "memory.search", "feedback.submit",
})
_UPSTREAM_FINALIZE_METHODS = frozenset({"episode.close", "session.close"})


class _PostDeliveryCorrelationError(RuntimeError):
    ambiguous = True


class MemosLocalProvider:
    """Map normalized events to MemOS without injecting behavioral context yet."""

    def __init__(
        self, *, project_id: str, journal: MemosDeliveryJournal,
        client: Any | None, mode: str = "shadow",
    ) -> None:
        if mode not in {"off", "shadow", "assist"}:
            raise ValueError("MemOS provider mode must be off, shadow, or assist")
        self.project_id = project_id
        self.journal = journal
        self.client = client
        self.mode = mode
        self._validated_health: dict | None = None
        self._assist_deadline: float | None = None
        self._session_lock_error: str | None = None

    def record(self, event: EventEnvelope) -> dict[str, int]:
        if event.project_id != self.project_id:
            raise ValueError("event project does not match MemOS provider")
        if self.mode == "off":
            return {"enqueued": 0, "delivered": 0, "ambiguous": 0, "dead": 0}
        enqueued = 0
        if event.event_type == "tool.completed":
            stored = self.journal.store_tool(
                event.event_id, event.idempotency_key, event.run_id,
                _tool_call(event),
            )
            retrieved = 0
            if (
                self.mode == "assist" and event.payload.get("error_code")
                and not self.journal.has_retrieval_reason(event.run_id, "recovery")
            ):
                items, _health = self.retrieve(
                    event.intent, reason="recovery", run_id=event.run_id,
                )
                retrieved = len(items)
            return {
                "enqueued": int(stored), "delivered": 0,
                "ambiguous": 0, "dead": 0, "retrieved": retrieved,
            }
        if event.event_type == "task.completed":
            if self.mode == "assist":
                self.journal.finalize_retrievals(event.run_id)
            stored = self.journal.defer_completion(
                event.run_id, event.event_id, event.idempotency_key, event.to_dict(),
            )
            lifecycle = self.journal.lifecycle(event.run_id)
            if lifecycle and lifecycle["episode_id"]:
                self._materialize_completion(event.run_id)
            result = self._drain()
            result["enqueued"] = int(stored)
            return result
        assist_retrieved = 0
        if self.mode == "assist" and event.event_type == "retrieval.used":
            retrieval_reason = _retrieval_reason(event.payload.get("reason"))
            raw_ids = event.payload.get("item_ids", [])
            item_ids = [
                str(value)[:512] for value in raw_ids[:100]
                if isinstance(value, str) and value
            ] if isinstance(raw_ids, (list, tuple)) else []
            outcome = str(event.payload.get("outcome", "used"))
            if item_ids and outcome in {"used", "contradicted", "ignored"}:
                self.journal.mark_retrievals(
                    event.run_id, item_ids, outcome, reason=retrieval_reason,
                )
            items, _health = self.retrieve(
                event.intent,
                reason=retrieval_reason,
                run_id=event.run_id,
            )
            assist_retrieved = len(items)
        for method, params, retryable in self._deliveries(event):
            if self.journal.enqueue(
                event.event_id, event.idempotency_key, method, params, retryable
            ):
                enqueued += 1
        result = self._drain()
        result["enqueued"] = enqueued
        if self.mode == "assist" and event.event_type == "task.started":
            items, _health = self.retrieve(
                event.intent, reason="task_start", run_id=event.run_id,
            )
            assist_retrieved = len(items)
        if self.mode == "assist":
            result["retrieved"] = assist_retrieved
        return result

    def _deliveries(self, event: EventEnvelope):
        session_id = _stable_id("session", event.session_id)
        namespace = {
            "agentKind": "hermes",
            "profileId": event.project_id,
            "workspaceId": event.project_id,
            "workspacePath": event.repo_root,
            "sessionKey": session_id,
        }
        common = {"agent": "hermes", "sessionId": session_id, "namespace": namespace}
        timestamp = _epoch_ms(event.timestamp)
        if event.event_type == "task.started":
            self.journal.begin_run(event.run_id, session_id)
            yield "session.open", {
                **common,
                "meta": {
                    "eventId": event.event_id, "runId": event.run_id,
                    "harness": event.harness, "projectId": event.project_id,
                },
            }, True
            yield "turn.start", {
                **common, "userText": event.intent[:2000],
                "contextHints": {
                    "eventId": event.event_id, "harness": event.harness,
                    "revision": event.revision, "runId": event.run_id,
                },
                "ts": timestamp,
            }, False
        elif event.event_type == "feedback.recorded":
            payload = event.payload
            lifecycle = self.journal.lifecycle(event.run_id)
            polarity = str(payload.get("polarity", "neutral")).lower()
            if polarity not in {"positive", "negative", "neutral"}:
                polarity = "neutral"
            magnitude = payload.get("magnitude", 0)
            magnitude = float(magnitude) if isinstance(magnitude, (int, float)) else 0.0
            requested_channel = str(payload.get("channel", "explicit")).lower()
            params = {
                "channel": "implicit" if requested_channel == "implicit" else "explicit",
                "polarity": polarity,
                "magnitude": max(0.0, min(1.0, abs(magnitude))),
                "rationale": str(payload.get("rationale", event.intent))[:1000],
                "ts": timestamp,
            }
            if lifecycle and lifecycle["episode_id"]:
                params["episodeId"] = lifecycle["episode_id"]
            yield "feedback.submit", params, False
        elif event.event_type == "task.completed":
            lifecycle = self.journal.lifecycle(event.run_id)
            if not lifecycle or not lifecycle["episode_id"]:
                raise RuntimeError("cannot materialize completion without an episode")
            episode_id = lifecycle["episode_id"]
            summary = str(event.payload.get("outcome_summary", event.intent))[:2000]
            yield "turn.end", {
                **common, "episodeId": episode_id, "agentText": summary,
                "toolCalls": self.journal.tools_for_run(event.run_id),
                "contextHints": {
                    "eventId": event.event_id, "revision": event.revision,
                    "retrievalOutcomes": self.journal.retrievals_for_run(event.run_id),
                    "verificationEvidence": list(
                        event.payload.get("verification_evidence", ())
                    )[:50] if isinstance(
                        event.payload.get("verification_evidence", ()), (list, tuple)
                    ) else [],
                    "outcomeClass": str(event.payload.get("status", "completed"))[:100],
                },
                "ts": timestamp,
            }, False
            yield "episode.close", {"episodeId": episode_id}, True
            yield "session.close", {"sessionId": session_id}, True
        elif event.event_type == "retrieval.used":
            # Decision/recovery retrieval is exercised in shadow and discarded.
            lifecycle = self.journal.lifecycle(event.run_id)
            requested_top = event.payload.get("top_k", 5)
            requested_top = (
                max(1, min(requested_top, 20))
                if isinstance(requested_top, int) and not isinstance(requested_top, bool)
                else 5
            )
            if self.mode == "shadow":
                yield "memory.search", {
                    **common,
                    "episodeId": lifecycle["episode_id"] if lifecycle else None,
                    "query": event.intent[:2000],
                    "topK": {
                        "tier1": requested_top,
                        "tier2": requested_top,
                        "tier3": requested_top,
                    },
                    "filters": {
                        "reason": str(event.payload.get("reason", "decision_point"))[:100],
                    },
                }, True
            feedback = {
                "channel": "implicit",
                "polarity": "neutral", "magnitude": 0,
                "rationale": event.intent[:1000], "ts": timestamp,
            }
            if lifecycle and lifecycle["episode_id"]:
                feedback["episodeId"] = lifecycle["episode_id"]
            yield "feedback.submit", feedback, False

    def _drain(self, *, limit: int = 100) -> dict[str, int]:
        result = {"delivered": 0, "ambiguous": 0, "dead": 0}
        if self.client is None:
            return result
        with self.journal.delivery_worker():
            try:
                self._ensure_validated()
            except Exception:
                return result
            for _ in range(limit):
                delivery = self.journal.claim_next()
                if delivery is None:
                    break
                try:
                    response = self.client.call(
                        delivery.method, delivery.params,
                        timeout=_method_timeout(delivery.method),
                        retryable=delivery.retryable,
                    )
                    if delivery.method == "turn.start":
                        try:
                            run_id = delivery.params.get("contextHints", {}).get("runId")
                            episode_id = (
                                response.get("query", {}).get("episodeId")
                                if isinstance(response, dict) else None
                            )
                            if not isinstance(run_id, str) or not isinstance(episode_id, str):
                                raise ValueError(
                                    "turn.start result omitted run/episode identity"
                                )
                            self.journal.set_episode(run_id, episode_id)
                        except Exception as exc:
                            raise _PostDeliveryCorrelationError(
                                f"turn.start succeeded but episode mapping failed: {exc}"
                            ) from exc
                except Exception as exc:  # typed transport errors expose `ambiguous`
                    state = self.journal.mark_failed(
                        delivery.delivery_id, f"{type(exc).__name__}: {exc}",
                        ambiguous=bool(getattr(exc, "ambiguous", False)),
                        retryable_failure=isinstance(exc, (
                            MemOSUnavailableError, MemOSTimeoutError, MemOSTransportError,
                        )),
                    )
                    if state in result:
                        result[state] += 1
                    break
                else:
                    self.journal.mark_delivered(delivery.delivery_id)
                    result["delivered"] += 1
                    if delivery.method == "turn.start":
                        self._materialize_completion(run_id)
        return result

    def _materialize_completion(self, run_id: str) -> None:
        raw = self.journal.deferred_completion(run_id)
        if raw is None:
            return
        event = EventEnvelope.from_external(raw)
        deliveries = list(self._deliveries(event))
        self.journal.materialize_completion(
            run_id, event.event_id, event.idempotency_key, deliveries,
        )

    def health(self, *, probe: bool = True) -> dict:
        counts = self.journal.counts()
        warnings = []
        details = None
        if self.client is None:
            warnings.append("behavioral_unavailable")
        elif probe:
            try:
                details = self._ensure_validated()
            except Exception as exc:
                warnings.append(f"behavioral_health_error:{type(exc).__name__}")
        elif self._validated_health is not None:
            details = self._validated_health
        else:
            warnings.append("behavioral_unvalidated")
        if counts["pending"] or counts["inflight"] or counts["deferred"]:
            warnings.append("behavioral_delivery_lag")
        if counts["ambiguous"]:
            warnings.append("behavioral_ambiguous_delivery")
        if counts["dead"]:
            warnings.append("behavioral_dead_delivery")
        return {
            "status": "healthy" if not warnings else "degraded",
            "mode": self.mode,
            "version": details.get("version") if isinstance(details, dict) else None,
            "queue": counts,
            "warnings": warnings,
        }

    def _ensure_validated(self, *, timeout: float = _UPSTREAM_COLD_START_TIMEOUT) -> dict:
        if self.client is None:
            raise RuntimeError("MemOS bridge is unavailable")
        with self.journal.delivery_worker(timeout=timeout):
            if self._validated_health is None:
                self._validated_health = self.client.health(timeout=timeout)
        return self._validated_health

    def retrieve(
        self, intent: str, top_k: int = 5, *,
        reason: str = "task_start", run_id: str | None = None,
    ):
        """Retrieve bounded behavioral items only in explicitly gated assist mode."""
        top_k = max(1, min(top_k, 20))
        if self.mode != "assist":
            health = self.health()
            health["shadow_injection"] = False
            return [], health
        warnings: list[str] = []
        deadline = self._assist_deadline or time.monotonic() + _ASSIST_TOTAL_TIMEOUT
        if self._session_lock_error:
            return [], {
                "status": "degraded", "mode": self.mode, "version": None,
                "queue": {}, "warnings": [self._session_lock_error],
                "assist_candidates": 0,
            }
        if run_id:
            try:
                self.journal.record_retrieval_invocation(
                    run_id, str(reason)[:100], timeout=_remaining(deadline),
                )
            except (OSError, sqlite3.Error):
                warnings.append("behavioral_retrieval_journal_degraded")
        if self.client is None:
            health = self.health()
            health["assist_injection"] = False
            return [], health
        lifecycle = self.journal.lifecycle(run_id) if run_id else None
        namespace = {
            "agentKind": "hermes", "profileId": self.project_id,
            "workspaceId": self.project_id,
        }
        params = {
            "agent": "hermes", "namespace": namespace, "query": str(intent)[:2000],
            "topK": {"tier1": top_k, "tier2": top_k, "tier3": top_k},
            "filters": {"reason": str(reason)[:100]},
        }
        if lifecycle and lifecycle.get("episode_id"):
            params["episodeId"] = lifecycle["episode_id"]
        try:
            with self.journal.delivery_worker(timeout=_remaining(deadline)):
                self._ensure_validated(timeout=_remaining(deadline))
                result = self.client.call(
                    "memory.search", params, timeout=_remaining(deadline),
                    retryable=True,
                )
                items = self._translate_hits(
                    result, warnings, top_k=top_k, namespace=namespace,
                    deadline=deadline,
                )
        except Exception as exc:
            items = []
            warnings.append(f"behavioral_retrieval_error:{type(exc).__name__}")
        base = self.health(probe=False)
        for warning in base.get("warnings", []):
            if warning not in warnings:
                warnings.append(warning)
        return items, {
            **base,
            "status": "healthy" if not warnings else "degraded",
            "warnings": warnings,
            "assist_candidates": len(items),
        }

    def record_injected(
        self, run_id: str, item_ids: list[str], *, reason: str,
    ) -> int:
        """Persist only items selected by fusion, never all raw provider hits."""
        return self.journal.record_retrievals(run_id, item_ids, reason)

    def _translate_hits(
        self, result: Any, warnings: list[str], *, top_k: int,
        namespace: Mapping[str, str], deadline: float,
    ) -> list[RetrievalItem]:
        if not isinstance(result, Mapping):
            warnings.append("behavioral_invalid_result")
            return []
        raw_hits = result.get("hits", [])
        if not isinstance(raw_hits, list):
            warnings.append("behavioral_invalid_hits")
            return []
        translated: list[RetrievalItem] = []
        skill_details: dict[str, Mapping[str, Any]] | None = None
        for raw in raw_hits[:top_k * 3]:
            if not isinstance(raw, Mapping):
                warnings.append("behavioral_invalid_hit")
                continue
            if raw.get("shareScope") == "hub":
                warnings.append("behavioral_shared_hit_rejected")
                continue
            owners = (raw.get("ownerProfileId"), raw.get("ownerWorkspaceId"))
            if any(owner is not None and owner != self.project_id for owner in owners):
                warnings.append("behavioral_cross_project_hit")
                continue
            if contains_sensitive_plaintext(raw):
                warnings.append("behavioral_sensitive_hit")
                continue
            try:
                kind = _hit_kind(raw.get("refKind", ""))
                if kind == "skill" and skill_details is None:
                    response = self.client.call(
                        "skill.list", {"namespace": namespace, "limit": 100},
                        timeout=_remaining(deadline), retryable=True,
                    )
                    rows = response.get("skills", []) if isinstance(response, Mapping) else []
                    skill_details = {
                        str(row.get("id")): row for row in rows
                        if isinstance(row, Mapping) and row.get("id")
                    }
                detail = self._hit_detail(
                    raw, kind, namespace=namespace, deadline=deadline,
                    skill_details=skill_details or {},
                )
                if not _owned_by_project(detail, self.project_id):
                    warnings.append("behavioral_unowned_or_cross_project_hit")
                    continue
                share = detail.get("share")
                if detail.get("shareScope") == "hub" or (
                    isinstance(share, Mapping) and share.get("scope") == "hub"
                ):
                    warnings.append("behavioral_shared_hit_rejected")
                    continue
                if contains_sensitive_plaintext(detail):
                    warnings.append("behavioral_sensitive_hit")
                    continue
                translated.append(self._translate_hit(raw, detail, kind))
            except (ContractError, TypeError, ValueError, RuntimeError):
                warnings.append("behavioral_invalid_hit")
            if len(translated) >= top_k:
                break
        return translated

    def _hit_detail(
        self, raw: Mapping[str, Any], kind: str, *,
        namespace: Mapping[str, str], deadline: float,
        skill_details: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        source_id = str(raw.get("refId") or "")
        if kind == "skill":
            detail = skill_details.get(source_id)
        elif kind == "policy":
            detail = self.client.call(
                "memory.get_policy", {"id": source_id, "namespace": namespace},
                timeout=_remaining(deadline), retryable=True,
            )
        elif kind == "world_model":
            detail = self.client.call(
                "memory.get_world", {"id": source_id, "namespace": namespace},
                timeout=_remaining(deadline), retryable=True,
            )
        elif str(raw.get("refKind")) == "episode":
            response = self.client.call(
                "memory.timeline", {"episodeId": source_id, "namespace": namespace},
                timeout=_remaining(deadline), retryable=True,
            )
            traces = response.get("traces", []) if isinstance(response, Mapping) else []
            detail = next((row for row in traces if isinstance(row, Mapping)), None)
        else:
            detail = self.client.call(
                "memory.get_trace", {"id": source_id, "namespace": namespace},
                timeout=_remaining(deadline), retryable=True,
            )
        if not isinstance(detail, Mapping):
            raise ValueError("behavioral detail is unavailable")
        return detail

    def _translate_hit(
        self, raw: Mapping[str, Any], detail: Mapping[str, Any], kind: str,
    ) -> RetrievalItem:
        source_id = str(raw.get("refId") or "")[:480]
        if not source_id:
            raise ValueError("behavioral hit has no identity")
        summary = str(raw.get("snippet") or "").strip()[:2000]
        if not summary:
            raise ValueError("behavioral hit has no summary")
        status = _hit_status(kind, detail.get("status"))
        score = _bounded_float(raw.get("score", 0.0))
        support = _optional_int(detail.get("support"))
        gain = _optional_signed_float(detail.get("gain"))
        evidence_refs = _detail_evidence(kind, detail)
        observed = _observed_at(detail.get("updatedAt", detail.get("ts")))
        locator = {
            "tier": str(raw.get("tier", ""))[:20],
            "upstream_type": str(raw.get("refKind", kind))[:100],
            "upstream_status": str(detail.get("status", "raw"))[:100],
            "support": support, "gain": gain, "evidence_refs": evidence_refs,
        }
        digest = hashlib.sha256(canonical_json({
            "hit": raw, "status": detail.get("status"),
            "support": support, "gain": gain, "evidence_refs": evidence_refs,
        }).encode("utf-8")).hexdigest()
        provenance = ProvenanceRef(
            kind=kind, provider="memos-local", source_id=source_id,
            project_id=self.project_id, repository_revision=None,
            source_hash="sha256:" + digest, observed_at=observed,
            confidence=score, freshness="fresh", locator=locator,
        )
        return RetrievalItem(
            item_id=f"memos:{source_id}", lane="behavioral", type=kind,
            summary=summary, scope={"project_id": self.project_id, "harness": None},
            status=status, provider_score=score,
            selection_reason=(
                f"MemOS {kind} match; support={_shown(support)}; "
                f"gain={_shown(gain, precision=3)}"
            ),
            provenance=(provenance.to_dict(),),
            token_estimate=min(1200, (len(summary) + 3) // 4), expires_at=None,
        )

    def export_shadow(self, *, limit: int = 20, max_bytes: int = 64 * 1024) -> dict:
        if self.client is None:
            raise RuntimeError("MemOS bridge is unavailable")
        with self.journal.delivery_worker():
            self._ensure_validated()
            limit = max(1, min(limit, 100))
            namespace = {
                "agentKind": "hermes", "profileId": self.project_id,
                "workspaceId": self.project_id,
            }
            trace_items = []
            for episode_id in self.journal.episode_ids(limit=limit):
                timeline = self.client.call(
                    "memory.timeline",
                    {"episodeId": episode_id, "namespace": namespace},
                    retryable=True,
                )
                trace_items.extend(timeline.get("traces", []))
                if len(trace_items) >= limit:
                    break
            skills = self.client.call(
                "skill.list", {"limit": limit, "namespace": namespace}, retryable=True,
            )
            worlds = self.client.call(
                "memory.list_world_models",
                {"limit": limit, "namespace": namespace}, retryable=True,
            )
            result = redact({
                "schema": "agentic.memory.behavioral-shadow.v1",
                "project_id": self.project_id,
                "mode": "shadow",
                "traces": trace_items[:limit],
                "skills": list(skills.get("skills", []))[:limit],
                "world_models": list(worlds.get("worldModels", []))[:limit],
            })
            while len(_json_bytes(result)) > max_bytes:
                candidates = [
                    result["traces"], result["skills"], result["world_models"]
                ]
                largest = max(candidates, key=len)
                if not largest:
                    raise ValueError("max_bytes is too small for export metadata")
                largest.pop()
            return result


def _stable_id(kind: str, value: str) -> str:
    return f"ag_{kind}_{hashlib.sha256(value.encode()).hexdigest()[:24]}"


def _epoch_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _tool_call(event: EventEnvelope) -> dict:
    payload = event.payload
    tool = {
        "name": str(payload.get("tool_name", "tool"))[:200],
        "input": str(payload.get("input_summary", ""))[:1000],
        "output": str(payload.get("output_summary", ""))[:1000],
        "toolCallId": event.event_id,
        "endedAt": _epoch_ms(event.timestamp),
    }
    if payload.get("error_code"):
        tool["errorCode"] = str(payload["error_code"])[:200]
    if isinstance(payload.get("started_at_ms"), int):
        tool["startedAt"] = payload["started_at_ms"]
    if event.code_refs:
        tool["codeRefs"] = [redact(dict(value)) for value in event.code_refs[:50]]
    return redact(tool)


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")


def _hit_kind(value: Any) -> str:
    normalized = str(value).strip().lower().replace("-", "_")
    aliases = {
        "skill": "skill", "policy": "policy", "trace": "trace",
        "episode": "trace", "experience": "policy",
        "repair": "trace", "decision_repair": "trace",
        "world": "world_model", "worldmodel": "world_model",
        "world_model": "world_model",
    }
    if normalized not in aliases:
        raise ValueError("unsupported behavioral hit kind")
    return aliases[normalized]


def _hit_status(kind: str, value: Any) -> str:
    normalized = str(value or "raw").strip().lower()
    if normalized == "candidate":
        return "probationary"
    if normalized in {"active", "probationary", "raw"}:
        return normalized
    if normalized in {"retired", "archived", "stale"}:
        return "stale"
    return "raw" if kind in {"trace", "world_model"} else "probationary"


def _bounded_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    if not math.isfinite(number):
        return 0.0
    return max(0.0, min(1.0, number))


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return max(0, min(1_000_000, int(number)))


def _optional_signed_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return max(-1.0, min(1.0, number))


def _observed_at(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(
                float(value) / 1000, timezone.utc,
            ).isoformat().replace("+00:00", "Z")
        except (OSError, OverflowError, ValueError):
            pass
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("behavioral retrieval deadline exhausted")
    return min(_ASSIST_RETRIEVAL_TIMEOUT, remaining)


def _owned_by_project(detail: Mapping[str, Any], project_id: str) -> bool:
    return (
        detail.get("ownerProfileId") == project_id
        and detail.get("ownerWorkspaceId") == project_id
    )


def _detail_evidence(kind: str, detail: Mapping[str, Any]) -> list[str]:
    fields = {
        "skill": ("evidenceAnchors", "sourcePolicyIds", "sourceWorldModelIds"),
        "policy": ("sourceEpisodeIds", "sourceFeedbackIds", "sourceTraceIds"),
        "world_model": ("policyIds",),
        "trace": ("episodeId",),
    }[kind]
    values: list[str] = []
    for field in fields:
        raw = detail.get(field, [])
        raw_values = raw if isinstance(raw, list) else [raw]
        for value in raw_values:
            if isinstance(value, str) and value and value not in values:
                values.append(value[:128])
            if len(values) >= 50:
                return values
    return values


def _shown(value: int | float | None, *, precision: int | None = None) -> str:
    if value is None:
        return "unavailable"
    if precision is not None:
        return f"{value:.{precision}f}"
    return str(value)


def _retrieval_reason(value: Any) -> str:
    reason = str(value or "decision_point")
    return reason if reason in {
        "task_start", "decision_point", "recovery", "user_feedback", "completion",
    } else "decision_point"


def _method_timeout(method: str) -> float | None:
    if method in _UPSTREAM_HEAVY_METHODS:
        return _UPSTREAM_HEAVY_TIMEOUT
    if method in _UPSTREAM_FINALIZE_METHODS:
        return _UPSTREAM_FINALIZE_TIMEOUT
    return None
