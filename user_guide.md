# User Guide

This project turns a natural-language hardware requirement (`user_goal.txt`) into:
- `hdl_bundle.v` (generated RTL bundle)
- `tb_top.v` (generated testbench)
- `simulation.log` (iverilog/vvp output)

It then runs `iverilog`/`vvp` and, on compile failures, can invoke an LLM ?Rework Agent? that overwrites `hdl_bundle.v` and retries (up to 5 attempts).

## 1) Prerequisites

- Python 3.10+
- Icarus Verilog toolchain in `PATH`:
  - `iverilog`
  - `vvp`

## 2) Install

```bash
pip install -r requirements.txt
```

## 3) Configure API key(s)

Copy `.env.example` to `.env` and fill either key:

```env
OPENAI_API_KEY=...
DEEPSEEK_API_KEY=...
```

Auto-selection rules:
- If only `DEEPSEEK_API_KEY` is set: use DeepSeek.
- If only `OPENAI_API_KEY` is set: use OpenAI.
- If both are set: use OpenAI.

Advanced overrides (optional): `LLM_PROVIDER`, `LLM_API_URL`, `LLM_MODEL`, `LLM_TEMPERATURE`.

## 4) Run

Mock mode (no network; good sanity check):

```bash
python -m orchestrator.run_mvp --mock --user-goal-file user_goal.txt --no-progress
```

Real LLM mode:

```bash
python -m orchestrator.run_mvp --user-goal-file user_goal.txt --no-progress
```

## 5) Outputs

Each run produces `./output/<timestamp>/` containing:
- `user_goal.txt`
- `hdl_bundle.v`
- `tb_top.v`
- `simulation.log`
- `sim.vvp`

`vvp` is executed with `cwd=./output/<timestamp>/` so `$dumpfile` waveforms (VCD) land in the same folder.

## 6) `user_goal.txt` standard template

Use this template to get stable decomposition and clean module interfaces:

```text
## Top module
- Name: <module_name>
- Language: Verilog (SystemVerilog-2012 allowed if needed)
- Clock: clk
- Reset: rst_n (synchronous, active-low)

## Interfaces / ports
Inputs:
- clk, rst_n
- <port>[W-1:0] ...

Outputs:
- <port>[W-1:0] ...

## Microarchitecture constraints
- Synthesizable
- No latches
- No combinational loops
- (Optional) valid/ready conventions if streaming is used

## Verification expectations
- Provide clock/reset stimulus
- Provide basic deterministic vectors
- (Optional) generate a VCD waveform dump
```

## 7) Troubleshooting

- `iverilog not found` / `vvp not found`: install Icarus Verilog and ensure it is on `PATH`.
- Build fails with syntax errors: inspect `simulation.log`; the auto-rework loop will try to patch and retry up to 5 times.
