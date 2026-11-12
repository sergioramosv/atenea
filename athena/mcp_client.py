"""MCP client: connects MCP servers (stdio or HTTP) and exposes their tools as Athena Tools.

    from athena.mcp_client import MCPServer
    gmail = MCPServer("gmail", command="npx", args=["-y", "some-gmail-mcp"], env={...})
    agent = Agent(tools=gmail.tools(dangerous={"send_email"}))

The MCP session lives in its own thread with its own asyncio loop; calls are synchronous for the agent.
"""
from __future__ import annotations

import asyncio
import threading
from contextlib import AsyncExitStack

from .registry import Tool


class MCPServer:
    def __init__(self, name: str, command: str | None = None, args: list[str] | None = None,
                 env: dict | None = None, url: str | None = None, headers: dict | None = None, timeout: int = 120):
        self.name, self.command, self.args, self.env = name, command, args or [], env
        self.url, self.headers, self.timeout = url, headers or {}, timeout
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._session = None
        self._stack = None
        self._tools_meta = []
        self._call(self._connect())

    def _call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(self.timeout)

    async def _connect(self):
        from mcp import ClientSession
        self._stack = AsyncExitStack()
        if self.url:
            from mcp.client.streamable_http import streamablehttp_client
            r, w, _ = await self._stack.enter_async_context(streamablehttp_client(self.url, headers=self.headers))
        else:
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client
            params = StdioServerParameters(command=self.command, args=self.args, env=self.env)
            r, w = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(r, w))
        await self._session.initialize()
        self._tools_meta = (await self._session.list_tools()).tools

    async def _invoke(self, tool: str, args: dict) -> str:
        res = await self._session.call_tool(tool, args)
        out = "\n".join(getattr(c, "text", None) or f"[{getattr(c, 'type', 'content')}]" for c in res.content)
        return ("Tool error: " + out) if getattr(res, "isError", False) else out

    def tools(self, dangerous: set[str] | None = None, prefix: bool = True) -> list[Tool]:
        """Tools exposed by the server. `dangerous`: tool names that require user confirmation."""
        dangerous = dangerous or set()
        out = []
        for t in self._tools_meta:
            name = f"{self.name}__{t.name}" if prefix else t.name
            schema = t.inputSchema or {"type": "object", "properties": {}}
            out.append(Tool(name, (t.description or t.name)[:1000], schema,
                            (lambda tn: (lambda **kw: self._call(self._invoke(tn, kw))))(t.name),
                            dangerous=t.name in dangerous, tags={"mcp", self.name}))
        return out

    def close(self):
        if self._stack:
            try:
                self._call(self._stack.aclose())
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
