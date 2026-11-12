"""OpenAI-compatible model client (llama-server, vLLM, Ollama...) with profiles and error recovery."""
from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class ModelProfile:
    """How to talk to a given model."""
    url: str = "http://127.0.0.1:8080/v1/chat/completions"
    model: str = "ornith"
    api_key: str | None = None
    temperature: float = 1.0
    top_p: float = 0.95
    top_k: int = 20
    max_tokens: int = 12000
    timeout: int = 1800
    keep_reasoning: bool = True      # send the model's previous reasoning back (essential with Ornith)
    extra: dict | None = None        # extra request fields, e.g. {"id_slot": 1} to pin a llama.cpp slot

    def key(self) -> str | None:
        if self.api_key:
            return self.api_key
        if os.environ.get("ATHENA_API_KEY"):
            return os.environ["ATHENA_API_KEY"]
        p = os.path.expanduser("~/.config/ornith/api_key")
        return open(p).read().strip() if os.path.exists(p) else None


# At most N generations at once against the model server. On a single (i)GPU, a third request makes every request
# crawl (a long prompt being read starves the others), so extra calls wait their turn. ATHENA_MAX_PARALLEL to change.
GATE = threading.BoundedSemaphore(max(1, int(os.environ.get("ATHENA_MAX_PARALLEL", "2"))))


class InvalidToolCall(Exception):
    """The model produced a corrupted tool call (broken JSON, repetition loop...)."""


class LLM:
    def __init__(self, profile: ModelProfile | None = None):
        self.p = profile or ModelProfile()

    def _req(self, body: dict) -> urllib.request.Request:
        hdr = {"Content-Type": "application/json"}
        if self.p.key():
            hdr["Authorization"] = f"Bearer {self.p.key()}"
        return urllib.request.Request(self.p.url, json.dumps(body).encode(), hdr)

    def chat(self, messages: list[dict], tools: list[dict] | None = None, temperature: float | None = None,
             on_delta=None, on_wait=None) -> dict:
        if not GATE.acquire(blocking=False):
            if on_wait:
                on_wait()
            GATE.acquire()
        try:
            return self._chat(messages, tools, temperature, on_delta)
        finally:
            GATE.release()

    def _chat(self, messages, tools, temperature, on_delta) -> dict:
        """Return {"message", "finish", "usage", "secs"}. Raises InvalidToolCall.
        With on_delta(kind, text) the reply is streamed: kind is "reasoning" or "content"."""
        body = {"model": self.p.model, "messages": messages,
                "temperature": self.p.temperature if temperature is None else temperature,
                "top_p": self.p.top_p, "top_k": self.p.top_k, "max_tokens": self.p.max_tokens, "cache_prompt": True}
        if tools:
            body["tools"] = tools
        if self.p.extra:
            body.update(self.p.extra)
        t0 = time.time()
        for attempt in range(3):
            body["seed"] = random.randint(0, 2**31 - 1)            # new seed on every attempt
            try:
                timings = {}
                if on_delta:
                    msg, finish, usage, timings = self._stream(body, on_delta)
                else:
                    d = json.load(urllib.request.urlopen(self._req(body), timeout=self.p.timeout))
                    ch = d["choices"][0]
                    msg, finish, usage, timings = ch["message"], ch.get("finish_reason"), d.get("usage", {}), d.get("timings") or {}
                return {"message": msg, "finish": finish, "usage": usage, "secs": time.time() - t0, "timings": timings}
            except urllib.error.HTTPError as e:
                txt = e.read().decode(errors="replace")[:400]
                if "parse tool call" in txt or "Failed to parse" in txt:
                    raise InvalidToolCall(txt)
                if attempt == 2:
                    raise RuntimeError(f"HTTP {e.code}: {txt}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt == 2:
                    raise RuntimeError(f"Model unavailable: {e}") from e
            time.sleep(3 * (attempt + 1))
        raise RuntimeError("unreachable")

    def _stream(self, body: dict, on_delta) -> tuple[dict, str | None, dict]:
        """Server-sent events from an OpenAI-compatible server, reassembled into one message."""
        body = dict(body, stream=True, stream_options={"include_usage": True})
        content, reasoning, calls, finish, usage, timings = [], [], {}, None, {}, {}
        with urllib.request.urlopen(self._req(body), timeout=self.p.timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ev = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if "error" in ev:
                    txt = json.dumps(ev["error"])[:400]
                    if "parse tool call" in txt or "Failed to parse" in txt:
                        raise InvalidToolCall(txt)
                    raise RuntimeError(f"Model error: {txt}")
                if ev.get("usage"):
                    usage = ev["usage"]
                if ev.get("timings"):
                    timings = ev["timings"]
                for ch in ev.get("choices") or []:
                    d = ch.get("delta") or {}
                    if d.get("reasoning_content"):
                        reasoning.append(d["reasoning_content"])
                        on_delta("reasoning", d["reasoning_content"])
                    if d.get("content"):
                        content.append(d["content"])
                        on_delta("content", d["content"])
                    for tc in d.get("tool_calls") or []:
                        c = calls.setdefault(tc.get("index", 0), {"id": "", "type": "function",
                                                                  "function": {"name": "", "arguments": ""}})
                        c["id"] = tc.get("id") or c["id"]
                        f = tc.get("function") or {}
                        c["function"]["name"] += f.get("name") or ""
                        c["function"]["arguments"] += f.get("arguments") or ""
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
        msg = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            msg["reasoning_content"] = "".join(reasoning)
        if calls:
            msg["tool_calls"] = [dict(c, id=c["id"] or f"call_{i}") for i, c in sorted(calls.items())]
        return msg, finish, usage, timings
