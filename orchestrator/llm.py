from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict, List

from .config import LLMConfig


GLOBAL_HARDWARE_CONSTRAINTS_TEXT = """【全局硬件约束】:
1. 时钟与复位：所有时序逻辑必须使用统一的时钟信号 clk 和同步低有效复位信号 rst_n。
2. 接口协议：模块间数据流传输必须采用 Valid/Ready 握手协议或 AXI4-Stream 标准。
3. 综合纪律：严禁生成任何锁存器 (Latches)；严禁存在组合逻辑环路 (Combinational Loops)；if/case 语句必须完备。

"""


# Read-only global context appended to every LLM request.
# Keep this stable to avoid downstream drift.
GLOBAL_CONTEXT: Dict[str, Any] = {
    "system": "datapath-agent",
    "hardware_constraints": GLOBAL_HARDWARE_CONSTRAINTS_TEXT,
    "clock": {"name": "clk", "reset": {"name": "rst_n", "active_low": True, "sync": True}},
    "interconnect": {"preferred": ["valid_ready", "axi4_stream"]},
    "synthesis_discipline": {
        "no_latches": True,
        "no_combinational_loops": True,
        "if_case_must_be_complete": True,
        "sequential_assignment": "nonblocking_only",
    },
    "performance": {"preference": "ultra_low_latency", "pipeline_depth": "strictly_controlled"},
    "width_policy": {
        "handshake_width": 1,
        "default_payload_width": 32,
        "top_level": {"in_data_width": 64, "out_feature_width": 32},
        "allowed_payload_widths": [32, 64, 128],
        "forbid_unjustified_nonstandard_widths": True
    },
}


def _ensure_json_keyword(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    has_json = any('json' in str(m.get('content', '')).lower() for m in messages)
    if has_json:
        return messages
    out = [dict(m) for m in messages]
    if out and out[0].get('role') == 'system':
        out[0]['content'] = (out[0].get('content') or '') + '\nYou must output in valid JSON format.'
    else:
        out.insert(0, {'role': 'system', 'content': 'You must output in valid JSON format.'})
    return out


class LLMClient:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    def is_mock(self) -> bool:
        return self.config.provider.lower() == "mock" or not self.config.api_url

    def chat(self, messages: List[Dict[str, str]], *, require_json: bool = False) -> str:
        if require_json:
            messages = _ensure_json_keyword(messages)
        if self.is_mock():
            return ""
        return self._http_chat(messages, require_json=require_json)

    def _resolve_url(self) -> str:
        url = self.config.api_url.strip().rstrip("/")
        if url.endswith("/chat/completions"):
            return url
        if url.endswith("/v1"):
            return url + "/chat/completions"
        return url + "/v1/chat/completions"

    def _http_chat(self, messages: List[Dict[str, str]], *, require_json: bool) -> str:
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
        }
        if require_json:
            payload["response_format"] = {"type": "json_object"}

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self._resolve_url(), data=data, method="POST")
        if self.config.api_key:
            req.add_header("Authorization", f"Bearer {self.config.api_key}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
        return _extract_text(raw)


def _extract_text(raw: str) -> str:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if "choices" in obj and obj["choices"]:
        choice = obj["choices"][0]
        if isinstance(choice, dict):
            message = choice.get("message")
            if isinstance(message, dict) and "content" in message:
                return str(message["content"])
            if "text" in choice:
                return str(choice["text"])
    for key in ("output_text", "text", "response"):
        if key in obj and isinstance(obj[key], str):
            return obj[key]
    return raw
