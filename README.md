# Athena 🦉

A lightweight agent SDK for **local models** — and the personal assistant built on top of it.

Athena runs an agent loop against any OpenAI-compatible endpoint (llama.cpp's `llama-server`, vLLM, Ollama…).
It was designed on a small AMD mini PC (**ZEUS**: Ryzen AI 9 HX 370, Radeon 890M, 64 GB) where every token
counts, so it keeps prompts short and recovers from the quirks of local models.

| Part | What it is |
|---|---|
| **Athena** | The SDK / agent engine (`athena/agent.py`) and the governor that orchestrates tools, MCP servers and skills |
| **Hermes** | Telegram bot to talk to Athena from your phone (`athena/hermes.py`) |
| **Web panel** | Chat, settings and status in the browser (`athena/web.py`) |
| **Hephaestus** | *(planned)* coding pipeline: plan → code → review → test |

## Why not Claude Code / the Claude Agent SDK?

Measured on ZEUS with the same model (Ornith-1.5-35B-A3B, Q4_K_M, llama.cpp Vulkan):

| Task | Athena | Claude Code |
|---|---|---|
| Inventory project (fix 3 bugs + CSV export, 10 tests) | **10/10 in 2–3 min** | 10/10 in 4–7 min |
| 10 small functions (11 tests) | **11/11 in 2.5 min** | 11/11 in 4–5 min |
| Large project, 5 modules (15 tests) | **15/15 in 19 min** | 15/15 in 36 min |

Claude Code is built for fast cloud models: ~11k tokens of instructions, ~20 tools and a very careful
step-by-step style. On a modest local GPU every token and every step costs seconds; a lean engine wins.

## Features

- **Agent loop** with built-in tools: `bash`, `read_file`, `write_file`, `edit_file`, `list_files`, `grep`.
- **Subagents** (`delegate`), several in parallel.
- **Custom tools** with a decorator (`@tool`), schema inferred from type hints.
- **MCP client**: use any MCP server (stdio or HTTP) as tools.
- **Skills**: `SKILL.md` files loaded on demand (Claude Code-compatible format).
- **Events** (`on_event`) for live progress, **permissions** (`approve`) for dangerous tools.
- **Verification guard** (`verify="python3 -m unittest"`): the agent cannot finish while it fails.
- **Sessions**: save/load conversations.
- Robustness learned the hard way: keeps the model's reasoning in the history, recovers from corrupted
  tool calls, empty and truncated replies.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install 'mcp<2' fastapi 'uvicorn[standard]'

# one-off task
.venv/bin/python -m athena "Fix the failing tests" --cwd ~/projects/my-app --verify "python3 -m unittest"

# web panel (user: sergio, password generated on first run)
.venv/bin/python -m athena.web

# Telegram bot (set telegram.token and telegram.allowed_users first)
.venv/bin/python -m athena.hermes
```

```python
from athena import Agent, ModelProfile, tool

@tool
def weather(city: str) -> str:
    "Return the current weather in a city."
    return "sunny"

agent = Agent(cwd="~/projects/app", profile=ModelProfile(url="http://zeus:8080/v1/chat/completions"),
              tools=[weather], verify="pytest -q")
print(agent.run("Add input validation to the signup form"))
```

Configuration lives in `~/.config/athena/config.json` (model, Telegram, projects, MCP servers,
tools that need confirmation, instructions) and can be edited from the web panel.

## Security

Athena reads email, calendars and web pages, so it treats their content as **data, never as instructions**.
Tools with external effects (send, delete, publish…) are marked `dangerous` and require explicit
confirmation (Telegram buttons or the web panel). Only allow-listed Telegram users can talk to Hermes.

## Tests

```bash
.venv/bin/python -m unittest discover tests     # engine tests against a scripted fake model
```

`bench/` contains the end-to-end benchmarks used to compare with Claude Code.
