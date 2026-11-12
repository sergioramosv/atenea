# Atenea

**Atenea** is a personal AI assistant that runs 100% on your own machine, built on **atenea-sdk**, a lightweight agent SDK for **local models**.

| Producto | Qué es | Dónde |
|---|---|---|
| **Atenea SDK** (`atenea-sdk`, `import atenea`) | El motor: bucle del agente, herramientas, modos de permiso, hooks, subagentes, MCP, memoria, tareas programadas, Atenea Equipo | `atenea/` |
| **Atenea** | El asistente: app web/iPhone, Atenea Telegram, Atenea voz, imágenes, documentos, Atenea documentos, Atenea buscador, notificaciones, copias | `atenea/app/` (`pip install "atenea-sdk[app]"`) |
| **Atenea Código** | El agente de programación en la terminal (`atenea`), también desde el Mac | `atenea/codigo/` (`pip install "atenea-sdk[codigo]"`) |

Servicios en ZEUS: `ornith` (modelo), `atenea-web` (panel), `atenea-telegram`, `atenea-voz` (dictado y voz),
`atenea-buscador` (SearXNG privado), `atenea-backup.timer` (copia diaria). Unidades en `servicios/systemd/`.

Atenea runs an agent loop against any OpenAI-compatible endpoint (llama.cpp's `llama-server`, vLLM, Ollama…).
It was designed on a small AMD mini PC (**ZEUS**: Ryzen AI 9 HX 370, Radeon 890M, 64 GB) where every token
counts, so it keeps prompts short and recovers from the quirks of local models.

| Part | What it is |
|---|---|
| **Atenea** | The SDK / agent engine (`atenea/agent.py`) and the governor that orchestrates tools, MCP servers and skills |
| **Atenea Telegram** | Telegram bot to talk to Atenea from your phone (`atenea/telegram.py`) |
| **Web panel** | Chat, settings and status in the browser (`atenea/web.py`) |
| **Hephaestus** | *(planned)* coding pipeline: plan → code → review → test |

## Why not Claude Code / the Claude Agent SDK?

Measured on ZEUS with the same model (Ornith-1.5-35B-A3B, Q4_K_M, llama.cpp Vulkan):

| Task | Atenea | Claude Code |
|---|---|---|
| Inventory project (fix 3 bugs + CSV export, 10 tests) | **10/10 in 2–3 min** | 10/10 in 4–7 min |
| 10 small functions (11 tests) | **11/11 in 2.5 min** | 11/11 in 4–5 min |
| Large project, 5 modules (15 tests) | **15/15 in 19 min** | 15/15 in 36 min |

Claude Code is built for fast cloud models: ~11k tokens of instructions, ~20 tools and a very careful
step-by-step style. On a modest local GPU every token and every step costs seconds; a lean engine wins.

## Features

**Engine (SDK)**
- Agent loop with built-in tools: `bash`, `read_file`, `write_file`, `edit_file`, `list_files`, `grep`, `todo_write`.
- **Streaming** of text and reasoning (`stream=True` → `delta` events).
- **Permission modes** like Claude Code: `ask`, `edits`, `auto`, `plan` (read-only, proposes a plan).
- **Subagents** (`delegate`), in parallel, and **named subagents** from Markdown (`explorador`, `revisor`,
  `probador`, `investigador`, `planificador`, `programador`, plus your own in `~/.config/atenea/agents` or `.atenea/agents`).
- **Hooks**: `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop` shell commands (exit 2 blocks / continues).
- **Custom tools** with `@tool`, **MCP client** (stdio or HTTP), **skills** (`SKILL.md`).
- **Web**: `web_search` (DuckDuckGo, no key) and `web_fetch`. **Background processes**: `bg_start/logs/stop/list`.
- **Memory**: `remember`/`forget` facts about the user, shared by every conversation; `ATENEA.md` /
  `AGENTS.md` / `CLAUDE.md` project instructions.
- **Scheduled tasks** (`schedule_task`): daily, weekdays, weekly, monthly, every N minutes, once.
- **Artifacts** (`create_artifact`): self-contained HTML pages shown in a side panel.
- Verification guard, sessions, and robustness for local models (keeps reasoning, recovers from broken tool
  calls, empty and truncated replies).

**Web panel (Atenea)**
- Many conversations with search, rename, delete, duplicate (fork) and export (Markdown/JSON).
- Live answer, collapsible thinking, task list, steps, approvals with **diff** and **Always allow** rules.
- **Rewind**: restore files and/or conversation to before any message (checkpoints of every edit).
- Floating bar: permission mode (Shift+Tab), context used, tokens/s, model status.
- Message **queue** while Atenea works; tasks survive server restarts and resume automatically.
- `/` commands with autocomplete (`/context /mcp /skills /tools /agents /hooks /doctor /compact /fork
  /export /remember /memory /init /review /schedule /bg /usage /rewind /help`…) and custom commands
  (`~/.config/atenea/commands/*.md`, `$ARGUMENTS`); `@file` mentions attach project files.
- Images, videos and files from ZEUS in the chat; artifacts in a sandboxed side panel.
- **Atenea documentos**: `search_documents` searches your folders, uploads, created files and all past chats (FTS5 + Qwen3-Embedding).
- **Documents**: `create_document` makes PDF, Word and Excel files.
- **Images**: `generate_image` / `edit_image` (FLUX.2 klein 4B on stable-diffusion.cpp Vulkan, ~35 s at 1024 px) and
  `look_at_image` (Qwen3-VL-8B on a llama.cpp server started on demand, stops after 10 idle minutes).
- **Activity tray** above the box: background shells, background subagents (`delegate(background=true)`) and
  **monitors** (`monitor_start`: watch a command, process or log and wake Atenea up in the chat when it fires).
- `ask_user`: Atenea asks you a question with clickable options.
- Settings: model, projects, MCP connections, Telegram, permissions, memory, scheduled tasks, background
  processes, system status. Telegram notifications when long tasks finish or need approval.

## Atenea Código (terminal)

```bash
atenea                       # interactive session in the current folder (like Claude Code)
atenea "fix the failing tests"
atenea -p "summarise this repo"     # print mode for scripts; also: cat log.txt | atenea -p "what failed?"
atenea -c                    # continue the last conversation of this folder · atenea -r to pick one
```

**On another computer (e.g. your Mac, through Tailscale):**

```bash
curl -fsSL https://zeus.tailbf0b1c.ts.net/install.sh | bash     # needs Python 3.10+ (brew install python)
cd ~/my-project && atenea
```

The model and the agent stay on ZEUS; the file and shell tools run on the Mac, in that folder ("runner").
`atenea login` / `atenea logout` / `atenea update`.

Shift+Tab switches the permission mode, Ctrl+C stops the task, `/help` lists commands, `@file` attaches a file.
The conversation lives in the Atenea server, so it also shows up in the web / iPhone app (marked `>_`):
follow it, approve actions or write from the phone and the terminal shows it live. `/remote` prints the link and a QR.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install 'mcp<2' fastapi 'uvicorn[standard]'

# one-off task
.venv/bin/python -m atenea "Fix the failing tests" --cwd ~/projects/my-app --verify "python3 -m unittest"

# web panel (user: sergio, password generated on first run)
.venv/bin/python -m atenea.web

# Telegram bot (set telegram.token and telegram.allowed_users first)
.venv/bin/python -m atenea.telegram
```

```python
from atenea import Agent, ModelProfile, tool

@tool
def weather(city: str) -> str:
    "Return the current weather in a city."
    return "sunny"

agent = Agent(cwd="~/projects/app", profile=ModelProfile(url="http://zeus:8080/v1/chat/completions"),
              tools=[weather], verify="pytest -q")
print(agent.run("Add input validation to the signup form"))
```

Configuration lives in `~/.config/atenea/config.json` (model, Telegram, projects, MCP servers,
tools that need confirmation, instructions) and can be edited from the web panel.

## Security

Atenea reads email, calendars and web pages, so it treats their content as **data, never as instructions**.
Tools with external effects (send, delete, publish…) are marked `dangerous` and require explicit
confirmation (Telegram buttons or the web panel). Only allow-listed Telegram users can talk to Atenea Telegram.

## Tests

```bash
.venv/bin/python -m unittest discover tests     # engine tests against a scripted fake model
```

`bench/` contains the end-to-end benchmarks used to compare with Claude Code.
