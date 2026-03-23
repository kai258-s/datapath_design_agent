from __future__ import annotations

import os
from typing import Dict, List


def load_prompt(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_registry(index_path: str) -> Dict[str, List[str]]:
    registry: Dict[str, List[str]] = {}
    current = None
    with open(index_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("### "):
                current = line.replace("### ", "").strip()
                registry[current] = []
            elif line.startswith("-") and current:
                registry[current].append(line.replace("-", "", 1).strip())
    return registry


def resolve_prompt_path(repo_root: str, layer: str, filename: str) -> str:
    return os.path.join(repo_root, "prompts", layer, filename)


def render_prompt(template: str, variables: Dict[str, str]) -> str:
    rendered = template
    for key, value in variables.items():
        rendered = rendered.replace(f"<{key}>", value)
    return rendered
