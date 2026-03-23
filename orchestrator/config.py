from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Optional


def load_env(repo_root: str, filename: str = ".env") -> None:
    """Load environment variables from a local .env file.

    Uses python-dotenv when available; falls back to a tiny parser.
    """
    path = os.path.join(repo_root, filename)
    if not os.path.exists(path):
        return

    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(path, override=False)
        return
    except Exception:
        pass

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            if "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key and key not in os.environ:
                os.environ[key] = value


@dataclass
class LLMConfig:
    provider: str
    api_url: str
    api_key: str
    model: str
    temperature: float


def load_config() -> LLMConfig:
    provider = os.getenv("LLM_PROVIDER", "").strip()
    api_url = os.getenv("LLM_API_URL", "").strip()
    api_key = os.getenv("LLM_API_KEY", "").strip()

    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "").strip()

    # Open-source convenience:
    # - Fill only one of OPENAI_API_KEY / DEEPSEEK_API_KEY
    # - If both are filled, prefer OpenAI
    # - Explicit LLM_* env vars override auto-selection
    if not api_key:
        api_key = openai_key or deepseek_key

    if not provider:
        if openai_key:
            provider = "openai"
        elif deepseek_key:
            provider = "deepseek"
        else:
            provider = "mock"

    if not api_url:
        if provider.lower() == "openai":
            api_url = "https://api.openai.com/v1"
        elif provider.lower() == "deepseek":
            api_url = "https://api.deepseek.com"

    model = os.getenv("LLM_MODEL", "").strip()
    if not model:
        if provider.lower() == "openai":
            model = "gpt-4o"
        elif provider.lower() == "deepseek":
            model = "deepseek-chat"
        else:
            model = "mock-model"

    temperature = float(os.getenv("LLM_TEMPERATURE", "0.2"))
    return LLMConfig(
        provider=provider,
        api_url=api_url,
        api_key=api_key,
        model=model,
        temperature=temperature,
    )
