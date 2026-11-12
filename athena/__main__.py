"""CLI: python -m athena "task" --cwd DIR [--verify "python3 -m unittest"] [--no-delegate] [--json out.json]"""
import argparse
import json
import os
import sys
import time

from .agent import Agent
from .llm import ModelProfile
from .skills import SkillSet

ap = argparse.ArgumentParser(prog="athena", description="Run an Athena agent on a task.")
ap.add_argument("task", help="the task, or @file to read it from a file")
ap.add_argument("--cwd", default=".", help="working directory")
ap.add_argument("--url", default="http://127.0.0.1:8080/v1/chat/completions", help="OpenAI-compatible endpoint")
ap.add_argument("--model", default="ornith")
ap.add_argument("--verify", default=None, help="command that must pass before the task counts as finished")
ap.add_argument("--skills", action="append", default=[], help="skills directory (repeatable)")
ap.add_argument("--no-delegate", action="store_true", help="disable subagents")
ap.add_argument("--max-turns", type=int, default=80)
ap.add_argument("--session", help="session file: resumes the conversation and saves it at the end")
ap.add_argument("--json", dest="out", help="write stats + result (and a transcript) to this file")
ap.add_argument("--quiet", action="store_true")
a = ap.parse_args()
task = open(a.task[1:]).read() if a.task.startswith("@") else a.task
t0 = time.time()
ag = Agent(cwd=a.cwd, profile=ModelProfile(url=a.url, model=a.model), allow_delegate=not a.no_delegate,
           max_turns=a.max_turns, verify=a.verify, skills=SkillSet(*a.skills) if a.skills else None,
           verbose=not a.quiet)
if a.session and os.path.exists(a.session):
    ag.load(a.session)
res = ag.run(task)
if a.session:
    ag.save(a.session)
s = ag.stats
summary = {"secs": round(time.time() - t0, 1), "requests": s.requests, "tool_calls": s.tool_calls,
           "prompt_tokens": s.prompt_tokens, "completion_tokens": s.completion_tokens,
           "secs_model": round(s.secs_model, 1), "result": res}
print(json.dumps({k: v for k, v in summary.items() if k != "result"}, ensure_ascii=False))
if not a.quiet:
    print(res)
if a.out:
    json.dump(summary, open(a.out, "w"), ensure_ascii=False, indent=1)
    json.dump({"messages": ag.messages, "subs": ag.transcripts},
              open(a.out.replace(".json", ".transcript.json"), "w"), ensure_ascii=False, indent=1)
sys.exit(0)
