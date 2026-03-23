from __future__ import annotations

import re


def bump_sequence(artifact_id: str) -> str:
    match = re.match(r"^(.*)-(\d+)$", artifact_id)
    if not match:
        return artifact_id
    prefix, seq = match.groups()
    new_seq = int(seq) + 1
    return f"{prefix}-{new_seq:03d}"


def format_version(prefix: str, major: int, minor: int, patch: int) -> str:
    return f"{prefix}-v{major}.{minor}.{patch}"
