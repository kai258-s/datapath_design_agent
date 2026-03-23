# datapath_agt (Open Source MVP)

A lightweight **multi-agent** framework for **hardware RTL generation + verification**:

- **Decomposer Agent**: breaks a high-level requirement into a module tree/contracts
- **RTL Coder**: generates Verilog RTL for leaf modules
- **TB Generator**: generates a top-level testbench based on the real DUT ports
- **Auto-Rework Loop**: runs `iverilog` and, on compile failures, invokes a Rework Agent to **overwrite** the RTL bundle and retry (up to 5 attempts)

## Quick Start

### 1) Install

```bash
pip install -r requirements.txt
```

### 2) Toolchain (required for simulation + rework)

Install **Icarus Verilog** so `iverilog` and `vvp` are available in your PATH.

### 3) Run (recommended: mock mode first)

```bash
python -m orchestrator.run_mvp --mock --user-goal-file user_goal.txt --no-progress
```

Outputs are written to `./output/<timestamp>/`:
- `hdl_bundle.v` (generated RTL bundle)
- `tb_top.v` (generated testbench)
- `simulation.log` (build/run output)
- `sim.vvp` (iverilog output)

### 4) Run with a real LLM

Create a local `.env` (do **not** commit it) based on `.env.example`.

Defaults:
- If `OPENAI_API_KEY` is set: uses OpenAI-compatible Chat Completions with `LLM_MODEL=gpt-4o`.
- If `DEEPSEEK_API_KEY` is set (and OpenAI key is empty): uses DeepSeek OpenAI-compatible API with `LLM_MODEL=deepseek-chat`.
- If both are set: prefers OpenAI.

You can override via `LLM_PROVIDER`, `LLM_API_URL`, `LLM_MODEL`, `LLM_TEMPERATURE` (see `orchestrator/config.py`).

## Project Layout

- `orchestrator/` core runtime
- `prompts/` prompt registry (required)
- `user_goal.txt` example goal you can edit
