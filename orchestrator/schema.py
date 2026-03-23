from __future__ import annotations

from typing import Any, Dict, List

REQUIRED_FIELDS = [
    "artifact_id",
    "artifact_type",
    "layer",
    "module_scope",
    "author_agent",
    "author_version",
    "status",
    "created_at",
    "parent_artifacts",
    "inherited_versions",
    "supersedes",
    "change_reason",
    "content",
]

ALLOWED_ARTIFACT_TYPES = {"spec", "contract", "review", "rtl", "fault", "registry"}
ALLOWED_STATUS = {"draft", "reviewed", "frozen", "rejected", "superseded"}
ALLOWED_LAYERS = {"L1", "L2", "L3", "L4", "X"}


def validate_artifact(artifact: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    for field in REQUIRED_FIELDS:
        if field not in artifact:
            errors.append(f"missing field: {field}")
    if artifact.get("artifact_type") not in ALLOWED_ARTIFACT_TYPES:
        errors.append("invalid artifact_type")
    if artifact.get("status") not in ALLOWED_STATUS:
        errors.append("invalid status")
    if artifact.get("layer") not in ALLOWED_LAYERS:
        errors.append("invalid layer")
    return errors
