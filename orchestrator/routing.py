from __future__ import annotations

from typing import Any, Dict


def review_passed(review_artifact: Dict[str, Any]) -> bool:
    content = review_artifact.get("content", {})
    return content.get("review_result") == "pass"


def fault_from_review(review_artifact: Dict[str, Any], fault_id: str) -> Dict[str, Any]:
    return {
        "artifact_id": f"X-FAULT-{fault_id}",
        "artifact_type": "fault",
        "layer": "X",
        "module_scope": review_artifact.get("module_scope", "datapath"),
        "author_agent": "Fault Attribution Agent",
        "author_version": "X-FAULTATTR-v1.0.0",
        "status": "draft",
        "created_at": review_artifact.get("created_at", ""),
        "parent_artifacts": [review_artifact.get("artifact_id", "")],
        "inherited_versions": review_artifact.get("inherited_versions", []),
        "supersedes": [],
        "change_reason": "review failure",
        "content": {
            "fault_id": fault_id,
            "fault_type": "review_failure",
            "severity": "high",
            "evidence": [review_artifact.get("artifact_id", "")],
            "suspected_layer": review_artifact.get("layer", "X"),
            "rollback_target": review_artifact.get("layer", "X"),
            "impacted_artifacts": review_artifact.get("parent_artifacts", []),
            "recommended_action": "rollback_and_regenerate",
            "confidence": 0.6,
        },
    }
