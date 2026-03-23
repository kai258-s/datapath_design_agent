from __future__ import annotations

import json
import os
from typing import Any, Dict


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, data: Dict[str, Any]) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=False)


def archive_existing(path: str, archive_root: str, artifact_id: str) -> None:
    if not os.path.exists(path):
        return
    ensure_dir(archive_root)
    basename = os.path.basename(path)
    snapshot = f"{artifact_id}__{basename}"
    snapshot_path = os.path.join(archive_root, snapshot)
    with open(path, "r", encoding="utf-8") as src:
        content = src.read()
    with open(snapshot_path, "w", encoding="utf-8") as dst:
        dst.write(content)


def artifact_path(repo_root: str, relative_path: str) -> str:
    return os.path.join(repo_root, relative_path)
