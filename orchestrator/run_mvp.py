from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import subprocess
import time
import dataclasses
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional
from collections import deque

from .artifacts import archive_existing, artifact_path, ensure_dir, write_json
from .config import load_config, load_env
from .llm import GLOBAL_CONTEXT, LLMClient
from .schema import validate_artifact


try:
    from tqdm import tqdm  # type: ignore
except Exception:  # pragma: no cover
    tqdm = None


def now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_timestamp(ts: str) -> str:
    return ts.replace(":", "-")


def log(msg: str) -> None:
    # Keep log format stable for grepping.
    print(f"[{now_iso()}] {msg}", flush=True)


def write_artifact(repo_root: str, rel_path: str, artifact: Dict[str, Any]) -> None:
    errors = validate_artifact(artifact)
    if errors:
        raise ValueError(f"artifact validation failed: {errors}")
    path = artifact_path(repo_root, rel_path)
    snapshot_root = artifact_path(repo_root, "artifacts/archive/snapshots")
    archive_existing(path, snapshot_root, artifact["artifact_id"])
    write_json(path, artifact)


def _read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _read_user_goal_from_stdin() -> str:
    """
    If stdin is piped, read all bytes until EOF.
    If running interactively, read multi-line paste until sentinel line <<END>>.
    """
    if not sys.stdin.isatty():
        return (sys.stdin.read() or '').strip()

    # Use unicode escapes to avoid Windows console encoding issues.
    print('\u672a\u63d0\u4f9b --user-goal-file\uff0c\u8bf7\u7c98\u8d34\u9700\u6c42\u6587\u672c\uff0c\u591a\u884c\u53ef\u76f4\u63a5\u7c98\u8d34\u3002')
    print('\u8f93\u5165\u7ed3\u675f\u540e\uff0c\u8bf7\u5355\u72ec\u8f93\u5165\u4e00\u884c\uff1a<<END>>')
    lines: List[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == '<<END>>':
            break
        lines.append(line)
    return '\n'.join(lines).strip()


def _load_prompt(repo_root: str, rel_path: str) -> str:
    path = artifact_path(repo_root, rel_path)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _render(template: str, mapping: Dict[str, str]) -> str:
    out = template
    for k, v in mapping.items():
        out = out.replace(k, v)
    return out


def _strip_markdown_code_fences(text: str) -> str:
    s = text.strip()
    if not s.startswith("```"):
        return s
    lines = s.splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].lstrip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_json_snippet(text: str) -> str:
    s = text.strip()
    lb = s.find("{")
    rb = s.rfind("}")
    if lb != -1 and rb != -1 and rb > lb:
        return s[lb : rb + 1]
    lb = s.find("[")
    rb = s.rfind("]")
    if lb != -1 and rb != -1 and rb > lb:
        return s[lb : rb + 1]
    return s


def parse_llm_json(text: str) -> Optional[Dict[str, Any]]:
    cleaned = _strip_markdown_code_fences(text)
    snippet = _extract_json_snippet(cleaned)
    try:
        obj = json.loads(snippet)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def coerce_bool(value):
    """Best-effort bool parsing for LLM JSON (bool / 'true'/'false' / 0/1)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(int(value))
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "t", "1", "yes", "y"):
            return True
        if s in ("false", "f", "0", "no", "n"):
            return False
    return None


def to_plain(obj):
    """Convert pydantic/dataclass/custom objects into JSON-serializable primitives."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(x) for x in obj]
    # Pydantic v2
    if hasattr(obj, 'model_dump') and callable(getattr(obj, 'model_dump')):
        try:
            return to_plain(obj.model_dump())
        except Exception:
            pass
    # Pydantic v1
    if hasattr(obj, 'dict') and callable(getattr(obj, 'dict')):
        try:
            return to_plain(obj.dict())
        except Exception:
            pass
    # dataclass
    try:
        if dataclasses.is_dataclass(obj):
            return to_plain(dataclasses.asdict(obj))
    except Exception:
        pass
    # generic objects
    try:
        return to_plain(vars(obj))
    except Exception:
        return str(obj)



def _infer_top_module_name(full_rtl_code: str) -> str:
    names = re.findall(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)", full_rtl_code or "", flags=re.M)
    if not names:
        return "top"
    if "top" in names:
        return "top"
    return names[0]


def _module_decl_has_port(full_rtl_code: str, module_name: str, port_name: str) -> bool:
    if not full_rtl_code or not module_name or not port_name:
        return False
    m = re.search(rf"^\s*module\s+{re.escape(module_name)}\b", full_rtl_code, flags=re.M)
    if not m:
        return False
    tail = full_rtl_code[m.start() : m.start() + 4000]
    semi = tail.find(";")
    if semi != -1:
        tail = tail[: semi + 1]
    return re.search(rf"\b{re.escape(port_name)}\b", tail) is not None


def _placeholder_tb(full_rtl_code: str) -> str:
    top = _infer_top_module_name(full_rtl_code)
    has_clk = _module_decl_has_port(full_rtl_code, top, "clk")
    has_rst_n = _module_decl_has_port(full_rtl_code, top, "rst_n")
    has_rst = _module_decl_has_port(full_rtl_code, top, "rst")

    lines: List[str] = []
    lines.append("`timescale 1ns/1ps")
    lines.append("")
    lines.append("module tb_top;")
    if has_clk:
        lines.append("  reg clk = 1'b0;")
        lines.append("  always #5 clk = ~clk;")
        lines.append("")
    if has_rst_n:
        lines.append("  reg rst_n = 1'b0;")
    elif has_rst:
        lines.append("  reg rst = 1'b1;")
    if has_rst_n or has_rst:
        lines.append("  initial begin")
        if has_rst_n:
            lines.append("    rst_n = 1'b0;")
            lines.append("    repeat (5) @(posedge clk);")
            lines.append("    rst_n = 1'b1;")
        else:
            lines.append("    rst = 1'b1;")
            lines.append("    repeat (5) @(posedge clk);")
            lines.append("    rst = 1'b0;")
        lines.append("  end")
        lines.append("")

    lines.append(f"  {top} dut (")
    conns: List[str] = []
    if has_clk:
        conns.append("    .clk(clk)")
    if has_rst_n:
        conns.append("    .rst_n(rst_n)")
    if has_rst:
        conns.append("    .rst(rst)")
    if not conns:
        conns.append("    /* TODO: connect ports */")
    lines.append(",\n".join(conns))
    lines.append("  );")
    lines.append("")
    lines.append("  initial begin")
    lines.append("    $display(\"tb_top: placeholder testbench (mock/fallback)\");")
    lines.append("    #200;")
    lines.append("    $finish;")
    lines.append("  end")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def _generate_tb_code(
    *,
    repo_root: str,
    client: LLMClient,
    require_json: bool,
    user_goal_text: str,
    full_rtl_code: str,
) -> str:
    sys_prompt = _load_prompt(repo_root, "prompts/cross/tb_verification_agent.md").strip()
    user_prompt = (
        "??????: "
        + (user_goal_text or "").strip()
        + "\n\n"
        + "??????? RTL ???: "
        + "\n"
        + (full_rtl_code or "").strip()
        + "\n\n"
        + "???????????????? Verilog Testbench???????????????????"
        + "????? JSON???? {\\\"tb_code\\\": \\\"...\\\"}?"
    )
    tb_messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        obj = call_llm_json(client, tb_messages, require_json=require_json)
    except Exception as e:
        log(f"TB call failed: {type(e).__name__}: {e}")
        return _placeholder_tb(full_rtl_code)

    if not obj:
        return _placeholder_tb(full_rtl_code)
    tb_code = obj.get("tb_code")
    if isinstance(tb_code, str) and tb_code.strip():
        return tb_code.strip() + "\n"
    return _placeholder_tb(full_rtl_code)

def call_llm_json(client: LLMClient, messages: List[Dict[str, str]], *, require_json: bool) -> Optional[Dict[str, Any]]:
    """Call LLM and parse JSON.

    Important: if require_json=True, we never silently downgrade to require_json=False.
    We retry transient failures while keeping require_json unchanged, then re-raise.
    """
    if client.is_mock():
        return None
    msgs = messages
    if require_json:
        # API guard: ensure the prompt includes the keyword JSON to satisfy some OpenAI-compatible backends.
        has_json = any('json' in str(m.get('content', '')).lower() for m in msgs)
        if not has_json:
            msgs = [dict(m) for m in msgs]
            if msgs and msgs[0].get('role') == 'system':
                msgs[0]['content'] = (msgs[0].get('content') or '') + '\nYou must output in valid JSON format.'
            else:
                msgs.insert(0, {'role': 'system', 'content': 'You must output in valid JSON format.'})

    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            raw = client.chat(msgs, require_json=require_json)
            obj = parse_llm_json(raw)
            if obj is None and require_json:
                # Surface parse failures in strict mode instead of quietly continuing.
                preview = (raw or '')[:400].replace('\r', '').replace('\n', ' ')
                raise ValueError(f'LLM returned non-JSON in require_json mode. preview={preview!r}')
            return obj
        except Exception as e:
            last_exc = e
            if attempt >= 3:
                raise
            # Basic backoff for transient network / rate-limit errors.
            time.sleep(min(2 ** (attempt - 1), 8))

    if last_exc is not None:
        raise last_exc
    return None


@dataclass
class Task:
    task_id: str
    parent_id: str
    local_contract: Dict[str, Any]


class IdGen:
    def __init__(self) -> None:
        self._seq = 0

    def next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:03d}"


def make_base_artifact(
    *,
    artifact_id: str,
    artifact_type: str,
    layer: str,
    module_scope: str,
    author_agent: str,
    author_version: str,
    status: str,
    created_at: str,
    parent_artifacts: List[str],
    inherited_versions: List[str],
    change_reason: str,
    content: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "layer": layer,
        "module_scope": module_scope,
        "author_agent": author_agent,
        "author_version": author_version,
        "status": status,
        "created_at": created_at,
        "parent_artifacts": parent_artifacts,
        "inherited_versions": inherited_versions,
        "supersedes": [],
        "change_reason": change_reason,
        "content": content,
    }


def _validate_ports(ports: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(ports, list) or not ports:
        return ["ports must be a non-empty list"]
    for idx, p in enumerate(ports):
        if not isinstance(p, dict):
            errors.append(f"ports[{idx}] must be an object")
            continue
        for k in ("name", "direction", "width", "description"):
            if k not in p:
                errors.append(f"ports[{idx}] missing {k}")
        if p.get("direction") not in ("input", "output"):
            errors.append(f"ports[{idx}] invalid direction")
        w = p.get("width")
        if not isinstance(w, int) or w <= 0:
            errors.append(f"ports[{idx}] invalid width")
        name = str(p.get("name", ""))
        if name.endswith("_valid") or name.endswith("_ready"):
            if isinstance(w, int) and w != 1:
                errors.append(f"ports[{idx}] {name} must be width 1")
    return errors


def _validate_width_alignment(sub_modules: Any) -> List[str]:
    # Best-effort width check: same-named signals across sub-modules must share width.
    errors: List[str] = []
    if not isinstance(sub_modules, list):
        return errors
    seen: Dict[str, int] = {}
    for sm in sub_modules:
        if not isinstance(sm, dict):
            continue
        ports = sm.get("ports")
        if not isinstance(ports, list):
            continue
        for p in ports:
            if not isinstance(p, dict):
                continue
            name = p.get("name")
            width = p.get("width")
            if not isinstance(name, str) or not isinstance(width, int):
                continue
            if name in seen and seen[name] != width:
                errors.append(f"width mismatch for signal '{name}': {seen[name]} vs {width}")
            else:
                seen[name] = width
    return errors


def _validate_structural_connections(sub_modules: Any, structural_verilog: Any) -> List[str]:
    """Best-effort structural connectivity check for non-leaf nodes.

    This is intentionally lightweight (regex-based). It catches the most common failure modes:
    - declared sub_module not instantiated
    - declared sub_module port not connected in the instantiation
    """
    errors: List[str] = []
    if not isinstance(sub_modules, list) or not sub_modules:
        return errors
    if not isinstance(structural_verilog, str) or not structural_verilog.strip():
        return ["structural_verilog is required for non-leaf nodes"]

    # Note: use real regex escapes (\s, \() rather than matching literal backslashes.
    inst_re = re.compile(r"(?P<mod>[A-Za-z_][A-Za-z0-9_$]*)\s+(?P<inst>[A-Za-z_][A-Za-z0-9_$]*)\s*\((?P<body>.*?)\)\s*;", re.S)
    conn_re = re.compile(r"\.(?P<port>[A-Za-z_][A-Za-z0-9_$]*)\s*\(\s*(?P<sig>[^)]+?)\s*\)")

    insts = []
    for m in inst_re.finditer(structural_verilog):
        body = m.group("body") or ""
        conns = {c.group("port"): c.group("sig").strip() for c in conn_re.finditer(body)}
        insts.append({"module": m.group("mod"), "connections": conns})



    # DUP_WIRE: reject duplicate internal wire declarations (common glue bug)
    decl_seen = {}
    for ln in structural_verilog.splitlines():
        t = ln.strip()
        if not t.startswith('wire '):
            continue
        t = t[len('wire '):].strip()
        if t.startswith('[') and ']' in t:
            t = t.split(']', 1)[1].strip()
        t = t.rstrip(';')
        for name in [x.strip() for x in t.split(',')]:
            if not name:
                continue
            if name in decl_seen:
                errors.append(f"duplicate wire declaration: {name}")
            else:
                decl_seen[name] = 1

    insts_by_mod = {}
    for inst in insts:
        insts_by_mod.setdefault(inst["module"], []).append(inst)

    for sm in sub_modules:
        if not isinstance(sm, dict):
            continue
        smn = sm.get("module_name")
        if not isinstance(smn, str) or not smn:
            continue
        ports = sm.get("ports")
        if not isinstance(ports, list) or not ports:
            errors.append(f"sub_module {smn}: ports missing")
            continue
        candidates = insts_by_mod.get(smn) or []
        if not candidates:
            errors.append(f"missing instantiation for sub_module {smn}")
            continue
        conns = candidates[0].get("connections") or {}
        for p in ports:
            if not isinstance(p, dict):
                continue
            pn = p.get("name")
            if not isinstance(pn, str) or not pn:
                continue
            if pn not in conns:
                errors.append(f"sub_module {smn}: port {pn} is not connected in structural_verilog")
            elif not str(conns.get(pn, "")).strip():
                errors.append(f"sub_module {smn}: port {pn} connected to empty signal")

    return errors



def _auto_structural_verilog(module_name: str, parent_ports: Any, sub_modules: Any) -> str:
    """Generate a conservative structural Verilog for simple linear pipelines.

    This is used only as a fallback when the Decomposer fails to include required instantiations.
    It connects:
    - clk/rst_n directly
    - linear valid/ready chain using in_valid/in_ready/out_valid/out_ready
    - payload signals by identical port names between adjacent stages

    If the ports do not match the expected pattern, it still instantiates sub-modules and
    connects any ports that match parent port names; remaining ports are wired to internal nets.
    """
    if not isinstance(parent_ports, list):
        parent_ports = []
    if not isinstance(sub_modules, list):
        sub_modules = []

    def vdecl(p):
        name = str(p.get('name'))
        d = str(p.get('direction'))
        w = p.get('width')
        if isinstance(w, int) and w > 1:
            rng = f"[{w-1}:0] "
        else:
            rng = ''
        if d == 'input':
            return f"    input wire {rng}{name}"
        return f"    output wire {rng}{name}"

    # Module header
    hdr_ports = []
    for p in parent_ports:
        if isinstance(p, dict) and p.get('name'):
            hdr_ports.append(vdecl(p))
    header = "module " + module_name + "(\n" + ",\n".join(hdr_ports) + "\n);\n"

    parent_names = {p.get('name') for p in parent_ports if isinstance(p, dict) and isinstance(p.get('name'), str)}
    declared_wires = set()

    # Gather internal nets needed for pipeline chaining.
    internal_lines = []

    # Determine if we have a simple linear handshake chain.
    def has_vr(sm, nm):
        ports = sm.get('ports') if isinstance(sm, dict) else None
        if not isinstance(ports, list):
            return False
        names = {p.get('name') for p in ports if isinstance(p, dict)}
        return {'in_valid','in_ready','out_valid','out_ready'}.issubset(names)

    linear = all(has_vr(sm, i) for i, sm in enumerate(sub_modules)) and len(sub_modules) >= 2

    # Helper: wire decl
    def wire_decl(name, width):
        if name in parent_names:
            return
        if name in declared_wires:
            return
        declared_wires.add(name)
        if not isinstance(width, int) or width <= 0:
            width = 1
        if width == 1:
            internal_lines.append(f"    wire {name};")
        else:
            internal_lines.append(f"    wire [{width-1}:0] {name};")

    # Build wires for stage chaining if linear.
    if linear:
        for i in range(len(sub_modules) - 1):
            wire_decl(f"_s{i}_valid", 1)
            wire_decl(f"_s{i}_ready", 1)

    # Payload wires between adjacent stages (same-name match).
    for i in range(len(sub_modules) - 1):
        a = sub_modules[i]
        b = sub_modules[i+1]
        if not (isinstance(a, dict) and isinstance(b, dict)):
            continue
        ap = a.get('ports')
        bp = b.get('ports')
        if not (isinstance(ap, list) and isinstance(bp, list)):
            continue
        a_out = {p.get('name'): p for p in ap if isinstance(p, dict) and p.get('direction') == 'output'}
        b_in = {p.get('name'): p for p in bp if isinstance(p, dict) and p.get('direction') == 'input'}
        for name, pd in a_out.items():
            if not isinstance(name, str):
                continue
            if name in ('out_valid','in_ready','clk','rst_n'):
                continue
            if name in b_in:
                w = pd.get('width')
                wire_decl(name, w if isinstance(w, int) else 1)

    # Instantiate each submodule.
    inst_lines = []
    for i, sm in enumerate(sub_modules):
        if not isinstance(sm, dict):
            continue
        smn = sm.get('module_name')
        ports = sm.get('ports')
        if not isinstance(smn, str) or not smn or not isinstance(ports, list):
            continue
        inst = f"u_{smn}_{i}"
        conns = []
        port_defs = {p.get('name'): p for p in ports if isinstance(p, dict) and isinstance(p.get('name'), str)}

        def connect(port, sig):
            conns.append(f"        .{port}({sig})")

        # clk/rst_n
        if 'clk' in port_defs:
            connect('clk', 'clk' if 'clk' in parent_names else '1\'b0')
        if 'rst_n' in port_defs:
            connect('rst_n', 'rst_n' if 'rst_n' in parent_names else '1\'b1')

        # Linear handshake chain
        if linear and {'in_valid','in_ready','out_valid','out_ready'}.issubset(port_defs.keys()):
            if i == 0:
                connect('in_valid', 'in_valid' if 'in_valid' in parent_names else "1'b0")
                connect('in_ready', 'in_ready' if 'in_ready' in parent_names else f"_s0_ready")
                connect('out_valid', f"_s0_valid")
                connect('out_ready', f"_s0_ready")
            elif i == len(sub_modules) - 1:
                prev = i - 1
                connect('in_valid', f"_s{prev}_valid")
                connect('in_ready', f"_s{prev}_ready")
                connect('out_valid', 'out_valid' if 'out_valid' in parent_names else f"_s{prev}_valid")
                connect('out_ready', 'out_ready' if 'out_ready' in parent_names else "1'b1")
            else:
                prev = i - 1
                connect('in_valid', f"_s{prev}_valid")
                connect('in_ready', f"_s{prev}_ready")
                connect('out_valid', f"_s{i}_valid")
                connect('out_ready', f"_s{i}_ready")

        # Connect any ports matching parent ports directly.
        for pn, pd in port_defs.items():
            if pn in ('clk','rst_n','in_valid','in_ready','out_valid','out_ready'):
                continue
            if pn in parent_names:
                connect(pn, pn)

        # For remaining ports, connect to internal nets by name.
        for pn, pd in port_defs.items():
            if pn in ('clk','rst_n'):
                continue
            # Already connected?
            if any(f".{pn}(" in c for c in conns):
                continue
            w = pd.get('width')
            wire_decl(pn, w if isinstance(w, int) else 1)
            connect(pn, pn)

        inst_lines.append(f"    {smn} {inst} (\n" + ",\n".join(conns) + "\n    );\n")

    body = "\n".join(internal_lines) + ("\n\n" if internal_lines else "") + "\n".join(inst_lines)
    return header + body + "\nendmodule\n"



def write_output_bundle(repo_root: str, timestamp: str, user_goal_text: str, rtl_files: List[str]) -> str:
    bundle_dir = artifact_path(repo_root, f"output/{safe_timestamp(timestamp)}")
    ensure_dir(bundle_dir)

    with open(os.path.join(bundle_dir, "user_goal.txt"), "w", encoding="utf-8") as f:
        f.write(user_goal_text.strip() + "\n" if user_goal_text.strip() else "")

    hdl_bundle_path = os.path.join(bundle_dir, "hdl_bundle.v")
    with open(hdl_bundle_path, "w", encoding="utf-8") as f:
        for path in rtl_files:
            if not os.path.exists(path):
                continue
            name = os.path.basename(path)
            f.write(f"// ==== {name} ====\n")
            with open(path, "r", encoding="utf-8") as rf:
                f.write(rf.read().rstrip())
            f.write("\n\n")

    return bundle_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Dynamic recursive-tree datapath-agent orchestrator")
    parser.add_argument("--repo-root", default=os.path.dirname(os.path.dirname(__file__)))
    parser.add_argument("--mock", action="store_true", help="force mock generation")
    parser.add_argument("--user-goal-file", default="", help="text file describing the user goal")
    parser.add_argument("--user-goal-text", default="", help="inline user goal text (overrides stdin)")
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--max-nodes", type=int, default=50)
    parser.add_argument("--require-json", action="store_true", help="request JSON mode from backend")
    parser.add_argument("--auto-repair-struct", action="store_true", help="auto-generate structural_verilog when missing instantiations (not pure LLM)")
    parser.add_argument("--no-progress", action="store_true", help="disable progress bar")
    parser.add_argument("--verbose", action="store_true", help="print more logs")
    args = parser.parse_args()

    repo_root = os.path.abspath(args.repo_root)
    log("START run_mvp")
    log(f"repo_root={repo_root}")
    load_env(repo_root)
    config = load_config()
    client = LLMClient(config)
    if args.mock:
        client.config.provider = "mock"
        log("mock_mode=true")

    log(f"llm_provider={client.config.provider} model={client.config.model} api_url={client.config.api_url}")
    if (not client.is_mock()) and (not client.config.api_key):
        print("ERROR: LLM_API_KEY is empty. Fill it in .env (LLM_API_KEY=...) or run with --mock.")
        return

    user_goal_text = ""
    if args.user_goal_file:
        user_goal_text = _read_text_file(args.user_goal_file)
    elif args.user_goal_text.strip():
        user_goal_text = args.user_goal_text
    else:
        user_goal_text = _read_user_goal_from_stdin()
    log(f"user_goal_len={len(user_goal_text)}")

    if not user_goal_text.strip():
        print("ERROR: user_goal is empty. Provide --user-goal-file, --user-goal-text, or paste text then end with <<END>>.")
        return

    decomposer_tpl = _load_prompt(repo_root, "prompts/cross/decomposer_agent.md")
    reviewer_tpl = _load_prompt(repo_root, "prompts/cross/contract_reviewer_agent.md")
    rtl_tpl = _load_prompt(repo_root, "prompts/cross/rtl_leaf_engineer.md")
    log("loaded prompts: decomposer/reviewer/rtl_leaf_engineer")

    ts = now_iso()
    ids = IdGen()

    artifacts_root = artifact_path(repo_root, "artifacts/tree")
    ensure_dir(artifacts_root)
    ensure_dir(os.path.join(artifacts_root, "nodes"))
    ensure_dir(os.path.join(artifacts_root, "reviews"))
    ensure_dir(os.path.join(artifacts_root, "rtl"))
    ensure_dir(os.path.join(artifacts_root, "faults"))

    # Root task local contract: keep minimal and text-driven.
    root_contract = {
        "module_name": "root_requirement",
        "responsibility": "translate top-level user requirement into decomposable datapath tasks",
        "requirement_text": user_goal_text.strip(),
        "constraints": {"global_context_required": True},
    }
    queue: Deque[Task] = deque()
    queue.append(Task(task_id=ids.next("TASK"), parent_id="", local_contract=root_contract))

    written_rtl_paths: List[str] = []

    had_faults = False

    pbar = None
    if (not args.no_progress) and (tqdm is not None):
        pbar = tqdm(total=args.max_nodes, desc="nodes", unit="node")

    nodes_processed = 0
    while queue and nodes_processed < args.max_nodes:
        task = queue.popleft()
        nodes_processed += 1
        if pbar is not None:
            pbar.update(1)
            pbar.set_postfix_str(str(task.local_contract.get("module_name", "unknown")))
        log(f"NODE {nodes_processed}/{args.max_nodes} task_id={task.task_id}")

        module_scope = str(task.local_contract.get("module_name", "unknown"))
        log(f"module_scope={module_scope}")

        # Node-level retry loop.
        last_review: Optional[Dict[str, Any]] = None
        last_output: Optional[Dict[str, Any]] = None

        for attempt in range(1, args.max_retries + 1):
            # Feed previous attempt feedback into the next attempt so retries can converge.
            prev_review_payload: Any = last_review.get("content") if last_review is not None else None
            prev_output_payload: Any = last_output if last_output is not None else None

            prompt = _render(
                decomposer_tpl,
                {
                    "<GLOBAL_CONTEXT_JSON>": json.dumps(GLOBAL_CONTEXT, indent=2, ensure_ascii=False),
                    "<LOCAL_CONTRACT_JSON>": json.dumps(to_plain(task.local_contract), indent=2, ensure_ascii=False),
                    "<PREV_REVIEW_JSON>": json.dumps(prev_review_payload, indent=2, ensure_ascii=False),
                    "<PREV_OUTPUT_JSON>": json.dumps(prev_output_payload, indent=2, ensure_ascii=False),
                },
            )
            messages = [
                {"role": "system", "content": "You are an expert RTL Engineer. You must output in valid JSON format."},
                {"role": "user", "content": prompt},
            ]
            t0 = time.time()
            log(f"DECOMPOSER call attempt={attempt} require_json={args.require_json}")
            out = call_llm_json(client, messages, require_json=args.require_json)
            log(f"DECOMPOSER done dt={time.time()-t0:.2f}s parsed={out is not None}")
            
            parse_failed = False
            if out is None:
                if client.is_mock():
                    out = {
                        "is_leaf": True,
                        "module_name": module_scope,
                        "ports": to_plain(task.local_contract.get("ports", [])),
                        "leaf_rtl_requirements": {
                            "language": "verilog",
                            "module_name": module_scope,
                            "ports": to_plain(task.local_contract.get("ports", [
                                {"name": "clk", "direction": "input", "width": 1, "description": "clock"},
                                {"name": "rst_n", "direction": "input", "width": 1, "description": "sync active-low reset"},
                            ])),
                            "behavior_rules": [],
                            "constraints": ["synthesizable", "no_latches", "no_comb_loops"],
                            "latency_notes": ["mock_mode_placeholder"],
                        },
                    }
                else:
                    # Do not bypass the LLM in normal mode; treat parse failures as retryable errors.
                    parse_failed = True
                    out = {"parse_failed": True, "module_name": module_scope}
            
            local_port_errors: List[str] = []
            if parse_failed:
                local_port_errors.append("llm_output_parse_failed")
                is_leaf = False
            else:
                is_leaf = coerce_bool(out.get("is_leaf"))
            if is_leaf is None:
                local_port_errors.append("is_leaf must be a boolean (true/false)")
                is_leaf = False
            if is_leaf is False:
                subs = out.get("sub_modules")
                if not isinstance(subs, list) or not subs:
                    local_port_errors.append("sub_modules must be a non-empty list")
                else:
                    for idx, sm in enumerate(subs):
                        if not isinstance(sm, dict):
                            local_port_errors.append(f"sub_modules[{idx}] must be an object")
                            continue
                        local_port_errors.extend([f"sub_modules[{idx}]: {e}" for e in _validate_ports(sm.get("ports"))])
                    local_port_errors.extend(_validate_width_alignment(subs))
                    local_port_errors.extend(_validate_structural_connections(subs, out.get("structural_verilog")))
                    if args.auto_repair_struct:
                        # Auto-generate structural glue if Decomposer forgot instantiations (optional).
                        struct_errs = _validate_structural_connections(subs, out.get("structural_verilog"))
                        if any(e.startswith("missing instantiation for sub_module") for e in struct_errs):
                            out["structural_verilog"] = _auto_structural_verilog(module_scope, out.get("ports"), subs)
                            # Replace the earlier structural errors with the post-auto ones.
                            local_port_errors = [e for e in local_port_errors if not e.startswith("missing instantiation for sub_module")]
                            local_port_errors = [e for e in local_port_errors if e != "structural_verilog is required for non-leaf nodes"]
                            local_port_errors.extend(_validate_structural_connections(subs, out.get("structural_verilog")))
                if not isinstance(out.get("structural_verilog"), str) or not str(out.get("structural_verilog")).strip():
                    local_port_errors.append("structural_verilog is required for non-leaf nodes")
            else:
                req = out.get("leaf_rtl_requirements") or {}
                local_port_errors.extend(_validate_ports(req.get("ports")))

            # Persist node output artifact
            node_art_id = ids.next("X-NODE")
            node_art = make_base_artifact(
                artifact_id=node_art_id,
                artifact_type="contract",
                layer="X",
                module_scope=module_scope,
                author_agent="Decomposer Agent",
                author_version="X-DECOMP-v1.0.0",
                status="draft",
                created_at=ts,
                parent_artifacts=[task.parent_id] if task.parent_id else [],
                inherited_versions=[],
                change_reason=f"decompose attempt {attempt}",
                content={
                    "task_id": task.task_id,
                    "local_contract": to_plain(task.local_contract),
                    "decomposer_output": out,
                },
            )
            write_artifact(repo_root, f"artifacts/tree/nodes/{node_art_id}.json", node_art)

            # Review node output
            review_prompt = _render(
                reviewer_tpl,
                {
                    "<GLOBAL_CONTEXT_JSON>": json.dumps(GLOBAL_CONTEXT, indent=2, ensure_ascii=False),
                    "<LOCAL_CONTRACT_JSON>": json.dumps(to_plain(task.local_contract), indent=2, ensure_ascii=False),
                    "<GENERATED_OUTPUT_JSON>": json.dumps(out, indent=2, ensure_ascii=False),
                },
            )
            review_messages = [
                {"role": "system", "content": "Return only JSON."},
                {"role": "user", "content": review_prompt},
            ]
            t1 = time.time()
            log(f"REVIEWER call attempt={attempt} require_json={args.require_json}")
            review_obj = call_llm_json(client, review_messages, require_json=args.require_json)
            log(f"REVIEWER done dt={time.time()-t1:.2f}s parsed={review_obj is not None}")
            if review_obj is None:
                review_obj = {"review_result": "pass", "issues": []}
            if local_port_errors:
                review_obj.setdefault("issues", [])
                for e in local_port_errors:
                    review_obj["issues"].append({"severity": "high", "description": e})
                review_obj["review_result"] = "fail"

            review_art_id = ids.next("X-REVIEW")
            review_art = make_base_artifact(
                artifact_id=review_art_id,
                artifact_type="review",
                layer="X",
                module_scope=module_scope,
                author_agent="Contract Reviewer Agent",
                author_version="X-REVIEW-v1.0.0",
                status="reviewed",
                created_at=ts,
                parent_artifacts=[node_art_id],
                inherited_versions=[],
                change_reason=f"node review attempt {attempt}",
                content={
                    "review_scope": "node_level_contract_review",
                    "review_result": str(review_obj.get("review_result", "fail")),
                    "issues": review_obj.get("issues", []),
                    "attempt": attempt,
                },
            )
            write_artifact(repo_root, f"artifacts/tree/reviews/{review_art_id}.json", review_art)

            last_review = review_art
            last_output = out
            log(f"REVIEW result={str(review_obj.get('review_result'))} issues={len(review_obj.get('issues', []))}")

            if str(review_obj.get("review_result")) == "pass":
                break

        if last_review is None or last_output is None:
            continue

        if last_review["content"].get("review_result") != "pass":
            fault_id = ids.next("X-FAULT")
            fault_art = make_base_artifact(
                artifact_id=fault_id,
                artifact_type="fault",
                layer="X",
                module_scope=module_scope,
                author_agent="Fault Attribution Agent",
                author_version="X-FAULTATTR-v1.0.0",
                status="draft",
                created_at=ts,
                parent_artifacts=[last_review["artifact_id"]],
                inherited_versions=[],
                change_reason="node failed after max retries",
                content={
                    "fault_id": fault_id,
                    "fault_type": "node_review_failure",
                    "severity": "high",
                    "evidence": [last_review["artifact_id"]],
                    "suspected_layer": "X",
                    "rollback_target": "node_local_retry_only",
                    "impacted_artifacts": [module_scope],
                    "recommended_action": "manual_intervention",
                    "confidence": 0.5,
                },
            )
            write_artifact(repo_root, f"artifacts/tree/faults/{fault_id}.json", fault_art)
            had_faults = True
            log(f"FAULT: node_review_failure module_scope={module_scope} after max retries")
            continue

        # Persist non-leaf structural glue RTL if provided
        is_leaf2 = coerce_bool(last_output.get("is_leaf"))
        if is_leaf2 is None:
            is_leaf2 = False
        if is_leaf2 is False:
            sv = last_output.get("structural_verilog")
            if isinstance(sv, str) and sv.strip():
                struct_path = os.path.join(artifacts_root, "rtl", f"{module_scope}_struct.v")
                with open(struct_path, "w", encoding="utf-8") as f:
                    f.write(sv.strip() + "\n")
                written_rtl_paths.append(struct_path)
                log(f"write structural rtl: {struct_path}")

# Route based on decomposer decision.
        if is_leaf2 is False:
            subs = last_output.get("sub_modules") or []
            if isinstance(subs, list):
                for sub in subs:
                    if not isinstance(sub, dict):
                        continue
                    sub_name = str(sub.get("module_name", "sub"))
                    queue.append(Task(task_id=ids.next("TASK"), parent_id=task.task_id, local_contract=sub))
            continue

        # Leaf: RTL generation
        leaf_req = last_output.get("leaf_rtl_requirements") or {}
        rtl_prompt = _render(
            rtl_tpl,
            {
                "<GLOBAL_CONTEXT_JSON>": json.dumps(GLOBAL_CONTEXT, indent=2, ensure_ascii=False),
                "<LEAF_RTL_REQUIREMENTS_JSON>": json.dumps(leaf_req, indent=2, ensure_ascii=False),
            },
        )
        rtl_messages = [
            {"role": "system", "content": "You are an expert RTL Engineer. You must output in valid JSON format."},
            {"role": "user", "content": rtl_prompt},
        ]
        t2 = time.time()
        log("RTL call require_json=True")
        rtl_obj = call_llm_json(client, rtl_messages, require_json=True)
        log(f"RTL done dt={time.time()-t2:.2f}s parsed={rtl_obj is not None}")
        if rtl_obj is None:
            if client.is_mock():
                # Deterministic placeholder RTL only in explicit mock mode.
                mn = str(leaf_req.get("module_name", module_scope))
                rtl_obj = {
                    "module_name": mn,
                    "rtl_file_name": f"{mn}.v",
                    "rtl_code": f"module {mn}();\nendmodule\n",
                }
            else:
                raise ValueError("rtl_output_parse_failed")

        rtl_file_name = str(rtl_obj.get("rtl_file_name", f"{module_scope}.v"))
        rtl_code = str(rtl_obj.get("rtl_code", ""))
        rtl_path = os.path.join(artifacts_root, "rtl", rtl_file_name)
        with open(rtl_path, "w", encoding="utf-8") as f:
            f.write(rtl_code)
        written_rtl_paths.append(rtl_path)
        log(f"write leaf rtl: {rtl_path}")

        rtl_art_id = ids.next("X-RTL")
        rtl_art = make_base_artifact(
            artifact_id=rtl_art_id,
            artifact_type="rtl",
            layer="X",
            module_scope=module_scope,
            author_agent="RTL Leaf Engineer",
            author_version="X-RTL-v1.0.0",
            status="draft",
            created_at=ts,
            parent_artifacts=[task.task_id],
            inherited_versions=[],
            change_reason="leaf rtl generation",
            content={
                "rtl_file_name": rtl_file_name,
                "language": "verilog",
                "module_name": str(rtl_obj.get("module_name", module_scope)),
                "notes": ["generated via rtl_leaf_engineer"],
            },
        )
        write_artifact(repo_root, f"artifacts/tree/rtl/{rtl_art_id}.json", rtl_art)

    bundle_dir = write_output_bundle(repo_root, ts, user_goal_text, written_rtl_paths)
    log(f"output bundle: {bundle_dir}")


    # Verification Agent: generate TB after RTL bundle is complete.
    try:
        hdl_bundle_path = os.path.join(bundle_dir, "hdl_bundle.v")
        full_rtl_code = _read_text_file(hdl_bundle_path) if os.path.exists(hdl_bundle_path) else ""
        goal_out = os.path.join(bundle_dir, "user_goal.txt")
        goal_text = _read_text_file(goal_out) if os.path.exists(goal_out) else user_goal_text

        log("TB call require_json=True")
        tb_code = _generate_tb_code(
            repo_root=repo_root,
            client=client,
            require_json=True,
            user_goal_text=goal_text,
            full_rtl_code=full_rtl_code,
        )

        tb_path = os.path.join(bundle_dir, "tb_top.v")
        with open(tb_path, "w", encoding="utf-8") as f:
            f.write(tb_code.rstrip() + "\n")
        log(f"write tb: {tb_path}")

    except Exception as e:
        log(f"TB generation skipped due to error: {type(e).__name__}: {e}")


    # Auto-simulate with local iverilog toolchain (with file-overwrite auto-rework loop).
    output_dir = bundle_dir
    hdl_bundle_path = os.path.join(output_dir, "hdl_bundle.v")
    tb_path = os.path.join(output_dir, "tb_top.v")
    vvp_path = os.path.join(output_dir, "sim.vvp")
    log_path = os.path.join(output_dir, "simulation.log")

    MAX_REWORK_ATTEMPTS = 5

    if os.path.exists(hdl_bundle_path) and os.path.exists(tb_path):
        for attempt in range(MAX_REWORK_ATTEMPTS):
            try:
                compile_res = subprocess.run(
                    ["iverilog", "-g2012", "-o", vvp_path, hdl_bundle_path, tb_path],
                    capture_output=True,
                    text=True,
                )
            except FileNotFoundError as e:
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write("iverilog not found: " + str(e) + "\n")
                print("\n[Simulation] Build FAILED! Check simulation.log for details.")
                break

            compile_out = (compile_res.stdout or "") + (compile_res.stderr or "")
            if compile_res.returncode == 0:
                try:
                    sim_res = subprocess.run(
                        ["vvp", vvp_path],
                        capture_output=True,
                        text=True,
                        cwd=output_dir,
                    )
                except FileNotFoundError as e:
                    with open(log_path, "w", encoding="utf-8") as f:
                        f.write("vvp not found: " + str(e) + "\n")
                    print("\n[Simulation] Build FAILED! Check simulation.log for details.")
                    break

                with open(log_path, "w", encoding="utf-8") as f:
                    if sim_res.stdout:
                        f.write(sim_res.stdout)
                    if sim_res.stderr:
                        f.write(sim_res.stderr)

                print("\n[Simulation] Build & Run SUCCESS! Log and Waveform saved.")
                break

            # Compile failed -> write log and try to auto-fix.
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(compile_out)

            if attempt == MAX_REWORK_ATTEMPTS - 1:
                print("\n[Simulation] Rework FAILED! Reached MAX_REWORK_ATTEMPTS.")
                raise SystemExit(3)

            try:
                rtl_text = _read_text_file(hdl_bundle_path)
            except Exception:
                rtl_text = ""

            rework_sys = _load_prompt(repo_root, "prompts/cross/rtl_rework_agent.md").strip()
            rework_user = (
                "You are a chip debug expert. Fix syntax so iverilog -g2012 can compile. "
                "Return JSON only: {\"fixed_rtl_code\": \"...\"}.\n\n"
                "[Current RTL Bundle]\n"
                + (rtl_text or "").strip()
                + "\n\n"
                "[iverilog Output]\n"
                + (compile_out or "").strip()
                + "\n"
            )
            rework_messages = [
                {"role": "system", "content": rework_sys},
                {"role": "user", "content": rework_user},
            ]

            fixed_rtl = None
            try:
                obj = call_llm_json(client, rework_messages, require_json=True)
                if isinstance(obj, dict):
                    v = obj.get("fixed_rtl_code")
                    if isinstance(v, str) and v.strip():
                        fixed_rtl = v
            except Exception as e:
                log(f"Rework Agent call failed: {type(e).__name__}: {e}")

            if fixed_rtl is not None:
                with open(hdl_bundle_path, "w", encoding="utf-8") as f:
                    f.write(fixed_rtl.rstrip() + "\n")
                print(f"\n[Simulation] Build failed. Rework Agent applied fix (Attempt {attempt + 1}/{MAX_REWORK_ATTEMPTS}). Retrying...")
            else:
                print(f"\n[Simulation] Build failed. Rework Agent returned no fix (Attempt {attempt + 1}/{MAX_REWORK_ATTEMPTS}). Retrying...")
    if had_faults:
        log("RUN finished with faults. See artifacts/tree/faults and artifacts/tree/reviews.")
        raise SystemExit(2)
    if pbar is not None:
        pbar.close()


if __name__ == "__main__":
    main()
