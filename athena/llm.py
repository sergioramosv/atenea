"""OpenAI-compatible model client (llama-server, vLLM, Ollama...) with profiles and error recovery."""
from __future__ import annotations

import json
import os
import random
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

    def key(self) -> str | None:
        if self.api_key:
            return self.api_key
        if os.environ.get("ATHENA_API_KEY"):
            return os.environ["ATHENA_API_KEY"]
        p = os.path.expanduser("~/.config/ornith/api_key")
        return open(p).read().strip() if os.path.exists(p) else None


class InvalidToolCall(Exception):
    """The model produced a corrupted tool call (broken JSON, repetition loop...)."""


class LLM:
    def __init__(self, profile: ModelProfile | None = None):
        self.p = profile or ModelProfile()

    def chat(self, messages: list[dict], tools: list[dict] | None = None, temperature: float | None = None) -> dict:
        """Return {"message", "finish", "usage", "secs"}. Raises InvalidToolCall."""
        body = {"model": self.p.model, "messages": messages,
                "temperature": self.p.temperature if temperature is None else temperature,
                "top_p": self.p.top_p, "top_k": self.p.top_k, "max_tokens": self.p.max_tokens, "cache_prompt": True}
        if tools:
            body["tools"] = tools
        hdr = {"Content-Type": "application/json"}
        if self.p.key():
            hdr["Authorization"] = f"Bearer {self.p.key()}"
        t0 = time.time()
        for attempt in range(3):
            body["seed"] = random.randint(0, 2**31 - 1)            # new seed on every attempt
            req = urllib.request.Request(self.p.url, json.dumps(body).encode(), hdr)
            try:
                d = json.load(urllib.request.urlopen(req, timeout=self.p.timeout))
                break
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
        ch = d["choices"][0]
        return {"message": ch["message"], "finish": ch.get("finish_reason"), "usage": d.get("usage", {}),
                "secs": time.time() - t0}
