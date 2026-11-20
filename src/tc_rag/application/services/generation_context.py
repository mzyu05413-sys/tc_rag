"""记录生成阶段实际使用的证据；不改变模型请求或代替语义评分。"""

from copy import deepcopy
from typing import Any, Literal

from tc_rag.application.ports.contracts import Repository
from tc_rag.domain.identity import fingerprint, new_id
from tc_rag.domain.pipeline import StageSnapshot

ContextState = Literal["prepared", "model_started", "model_completed", "model_error",
                       "cache_hit", "cancelled"]


def evidence_fingerprint(request: dict[str, Any]) -> str:
    return fingerprint([request.get("stage"), request.get("evidence", []),
                        request.get("supplemental_evidence", [])])


class GenerationContextRecorder:
    def __init__(self, repository: Repository):
        self.repository = repository

    def start(
        self, run_id: str, request: dict[str, Any], schema_name: str, model_name: str,
    ) -> str | None:
        if request.get("stage") not in {"plan_points", "generate"}:
            return None
        record_id = f"{run_id}:" + new_id("generation_context")
        source = self.repository.get("stage_snapshots", f"{run_id}:input", StageSnapshot)
        payload = {
            "schema_version": "1.0", "state": "prepared", "provider_attempted": False,
            "stage": request["stage"], "output_schema": schema_name, "model": model_name,
            "request_fingerprint": fingerprint(request),
            "evidence_fingerprint": evidence_fingerprint(request),
            "document_fingerprint": source.payload.get("document_fingerprint") if source else None,
            "core_evidence": deepcopy(request.get("evidence", [])),
            "retrieved_evidence": deepcopy(request.get("supplemental_evidence", [])),
            "rule_ids": [r["rule_id"] for r in request.get("rules", [])],
            "feature_ids": sorted({r["feature_id"] for r in request.get("rules", [])}),
            "point_ids": [p["point_id"] for p in request.get("points", [])],
            "existing_point_ids": [p["point_id"] for p in request.get("existing_points", [])],
        }
        self.repository.save("stage_snapshots", record_id, StageSnapshot(
            snapshot_id=record_id, run_id=run_id, stage="generation_context", payload=payload,
        ))
        key = f"{run_id}:generation_contexts"
        previous = self.repository.get("stage_snapshots", key, StageSnapshot)
        ids = list(previous.payload["record_ids"]) if previous else []
        ids.append(record_id)
        self.repository.save("stage_snapshots", key, StageSnapshot(
            snapshot_id=key, run_id=run_id, stage="generation_contexts",
            payload={"record_ids": ids},
        ))
        return record_id

    def finish(
        self, record_id: str | None, state: ContextState, error_type: str | None = None,
    ) -> None:
        if record_id is None:
            return
        record = self.repository.get("stage_snapshots", record_id, StageSnapshot)
        if record is None:
            raise ValueError("生成上下文记录缺失")
        payload = {**record.payload, "state": state}
        if state in {"model_started", "model_completed", "model_error", "cancelled"}:
            payload["provider_attempted"] = True
        if error_type is not None:
            payload["error_type"] = error_type
        self.repository.save("stage_snapshots", record_id, record.model_copy(update={
            "payload": payload,
        }))
