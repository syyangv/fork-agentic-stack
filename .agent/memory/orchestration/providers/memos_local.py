"""Shadow-mode behavioral provider for the pinned MemOS local bridge."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from .._core import redact
from ..contracts import EventEnvelope
from ..memos_bridge import (
    MemOSTimeoutError, MemOSTransportError, MemOSUnavailableError,
)
from ..memos_journal import MemosDeliveryJournal


_UPSTREAM_HEAVY_TIMEOUT = 75.0
_UPSTREAM_FINALIZE_TIMEOUT = 15.0
_UPSTREAM_COLD_START_TIMEOUT = 75.0
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
        if mode not in {"off", "shadow"}:
            raise ValueError("Phase 3 MemOS provider supports only off or shadow mode")
        self.project_id = project_id
        self.journal = journal
        self.client = client
        self.mode = mode
        self._validated_health: dict | None = None

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
            return {"enqueued": int(stored), "delivered": 0, "ambiguous": 0, "dead": 0}
        if event.event_type == "task.completed":
            stored = self.journal.defer_completion(
                event.run_id, event.event_id, event.idempotency_key, event.to_dict(),
            )
            lifecycle = self.journal.lifecycle(event.run_id)
            if lifecycle and lifecycle["episode_id"]:
                self._materialize_completion(event.run_id)
            result = self._drain()
            result["enqueued"] = int(stored)
            return result
        for method, params, retryable in self._deliveries(event):
            if self.journal.enqueue(
                event.event_id, event.idempotency_key, method, params, retryable
            ):
                enqueued += 1
        result = self._drain()
        result["enqueued"] = enqueued
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
                "contextHints": {"eventId": event.event_id, "revision": event.revision},
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

    def health(self) -> dict:
        counts = self.journal.counts()
        warnings = []
        details = None
        if self.client is None:
            warnings.append("behavioral_unavailable")
        else:
            try:
                details = self._ensure_validated()
            except Exception as exc:
                warnings.append(f"behavioral_health_error:{type(exc).__name__}")
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

    def _ensure_validated(self) -> dict:
        if self.client is None:
            raise RuntimeError("MemOS bridge is unavailable")
        with self.journal.delivery_worker():
            if self._validated_health is None:
                self._validated_health = self.client.health(
                    timeout=_UPSTREAM_COLD_START_TIMEOUT,
                )
        return self._validated_health

    def retrieve(self, _intent: str, top_k: int = 5):
        """Phase 3 captures/retrieves for evaluation but injects no items."""
        del top_k
        health = self.health()
        health["shadow_injection"] = False
        return [], health

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
    return redact(tool)


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")


def _method_timeout(method: str) -> float | None:
    if method in _UPSTREAM_HEAVY_METHODS:
        return _UPSTREAM_HEAVY_TIMEOUT
    if method in _UPSTREAM_FINALIZE_METHODS:
        return _UPSTREAM_FINALIZE_TIMEOUT
    return None
